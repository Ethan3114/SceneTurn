CREATE OR REPLACE FUNCTION sceneturn.transfer_schedule(
    p_job_id uuid, p_dispatch_revision uuid, p_due_ms bigint
) RETURNS jsonb
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_previous jsonb;
    v_now_ms bigint := floor(extract(epoch FROM clock_timestamp()) * 1000);
BEGIN
    IF (p_dispatch_revision IS NULL) <> (p_due_ms IS NULL) OR
        (p_due_ms IS NOT NULL AND p_due_ms NOT BETWEEN v_now_ms + 1 AND v_now_ms + 120000) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'TRANSFER_DISPATCH_EXPIRED';
    END IF;
    SELECT jsonb_build_object('jobId', job_id, 'revision', revision, 'dueMs', due_ms)
        INTO v_previous FROM sceneturn.deployment_dispatch
        WHERE kind = 'transfer' AND job_id = p_job_id::text AND due_ms IS NOT NULL;
    IF p_dispatch_revision IS NULL THEN
        UPDATE sceneturn.deployment_dispatch SET due_ms = NULL
            WHERE kind = 'transfer' AND job_id = p_job_id::text;
    ELSE
        INSERT INTO sceneturn.deployment_dispatch(kind, job_id, revision, due_ms)
        VALUES ('transfer', p_job_id::text, p_dispatch_revision::text, p_due_ms)
        ON CONFLICT (kind, job_id) DO UPDATE SET revision = EXCLUDED.revision, due_ms = EXCLUDED.due_ms;
    END IF;
    IF v_previous ->> 'revision' = p_dispatch_revision::text THEN
        RETURN NULL;
    END IF;
    RETURN v_previous;
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_transfer_read(p_owner_id uuid, p_job_id uuid) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_job sceneturn.transfer_jobs%ROWTYPE;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    SELECT * INTO v_job FROM sceneturn.transfer_jobs
        WHERE id = p_job_id::text AND owner_id = p_owner_id::text;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'TRANSFER_NOT_FOUND';
    END IF;
    RETURN to_jsonb(v_job);
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_transfer_artifact(p_owner_id uuid, p_job_id uuid) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_job sceneturn.transfer_jobs%ROWTYPE;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    SELECT * INTO v_job FROM sceneturn.transfer_jobs
        WHERE id = p_job_id::text AND owner_id = p_owner_id::text AND kind IN ('import', 'backup', 'game') AND status <> 'awaiting_upload';
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'TRANSFER_NOT_FOUND';
    END IF;
    RETURN to_jsonb(v_job) - 'request_json' - 'private_json' || jsonb_build_object('private_json',
        CASE WHEN v_job.kind IN ('backup', 'game') AND v_job.status = 'ready' THEN jsonb_build_object(
            'archive', v_job.private_json -> 'archive', 'archiveBytes', v_job.private_json -> 'archiveBytes',
            'archiveSha256', v_job.private_json -> 'archiveSha256', 'directory', v_job.private_json -> 'directory',
            'files', CASE WHEN v_job.kind = 'game' THEN v_job.private_json -> 'files' ELSE '[]'::json END,
            'fileManifest', v_job.private_json -> 'fileManifest') ELSE '{}'::jsonb END);
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_transfer_prepare_export(
    p_owner_id uuid, p_project_id uuid, p_operation_id text, p_kind text, p_expected_revision integer, p_identity_sha256 text
) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_owner text := p_owner_id::text;
    v_project_id text := p_project_id::text;
    v_project sceneturn.projects%ROWTYPE;
    v_job sceneturn.transfer_jobs%ROWTYPE;
    v_project_data jsonb;
    v_assets jsonb;
    v_canvases jsonb;
    v_history jsonb := '{}'::jsonb;
    v_conflicts jsonb := '[]'::jsonb;
    v_source_id text;
    v_source_history jsonb;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    IF p_kind IS NULL OR p_kind NOT IN ('backup', 'game') OR p_operation_id IS NULL OR length(p_operation_id) NOT BETWEEN 1 AND 100
        OR p_expected_revision IS NULL OR p_expected_revision < 1 OR coalesce(p_identity_sha256, '') !~ '^[0-9a-f]{64}$' THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'TRANSFER_REQUEST_INVALID';
    END IF;
    SELECT * INTO v_project FROM sceneturn.projects WHERE id = v_project_id AND owner_id = v_owner;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'PROJECT_NOT_FOUND';
    END IF;
    SELECT * INTO v_job FROM sceneturn.transfer_jobs WHERE owner_id = v_owner AND operation_id = p_operation_id;
    IF FOUND THEN
        IF (v_job.project_id, v_job.kind, v_job.identity_sha256) IS DISTINCT FROM (v_project_id, p_kind, p_identity_sha256) THEN
            RAISE SQLSTATE 'PT409' USING MESSAGE = 'OPERATION_CONFLICT';
        END IF;
        RETURN jsonb_build_object('existing', to_jsonb(v_job));
    END IF;
    SELECT * INTO v_job FROM sceneturn.transfer_jobs WHERE owner_id = v_owner AND project_id = v_project_id
        AND kind = p_kind AND identity_sha256 = p_identity_sha256;
    IF FOUND THEN
        RETURN jsonb_build_object('existing', to_jsonb(v_job));
    END IF;
    IF v_project.revision <> p_expected_revision THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'PROJECT_REVISION_CONFLICT';
    END IF;
    v_project_data := jsonb_set(v_project.data_json::jsonb, '{metadata,serverRevision}', to_jsonb(v_project.revision));
    SELECT coalesce(jsonb_agg(to_jsonb(a) - 'owner_id' - 'metadata_json' ORDER BY a.id), '[]'::jsonb)
        INTO v_assets FROM sceneturn.assets AS a WHERE a.owner_id = v_owner AND a.project_id = v_project_id;
    SELECT coalesce(jsonb_agg(c.data_json ORDER BY c.scene_id COLLATE "C"), '[]'::jsonb)
        INTO v_canvases FROM sceneturn.canvases AS c
        JOIN jsonb_array_elements(v_project_data #> '{graphData,nodes}') AS n(value)
            ON n.value ->> 'type' = 'scene' AND n.value ->> 'id' = c.scene_id
        WHERE c.owner_id = v_owner AND c.project_id = v_project_id;

    IF p_kind = 'game' THEN
        SELECT coalesce(jsonb_agg(jsonb_build_object('code', CASE WHEN a.status = 'duration_conflict' THEN 'MEDIA_DURATION_CONFLICT'
                ELSE a.proposal_json ->> 'fileError' END, 'clipId', a.clip_id) ORDER BY a.id), '[]'::jsonb)
            INTO v_conflicts FROM sceneturn.media_adoptions AS a WHERE a.owner_id = v_owner AND a.project_id = v_project_id
            AND a.status IN ('duration_conflict', 'blocked');
    ELSE
        v_history := jsonb_build_object(
            'project_versions', (SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY r.revision), '[]'::jsonb) FROM sceneturn.project_versions AS r WHERE r.owner_id = v_owner AND r.project_id = v_project_id),
            'project_memories', (SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY r.id), '[]'::jsonb) FROM sceneturn.project_memories AS r WHERE r.owner_id = v_owner AND r.project_id = v_project_id),
            'agent_runs', (SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY r.id), '[]'::jsonb) FROM sceneturn.agent_runs AS r WHERE r.owner_id = v_owner AND r.project_id = v_project_id),
            'agent_steps', (SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY r.id), '[]'::jsonb) FROM sceneturn.agent_steps AS r JOIN sceneturn.agent_runs AS a ON a.id = r.run_id WHERE a.owner_id = v_owner AND a.project_id = v_project_id),
            'agent_calls', (SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY r.id), '[]'::jsonb) FROM sceneturn.agent_calls AS r JOIN sceneturn.agent_runs AS a ON a.id = r.run_id WHERE a.owner_id = v_owner AND a.project_id = v_project_id),
            'project_conversations', (SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY r.id), '[]'::jsonb) FROM sceneturn.project_conversations AS r WHERE r.owner_id = v_owner AND r.project_id = v_project_id),
            'conversation_messages', (SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY r.id), '[]'::jsonb) FROM sceneturn.conversation_messages AS r WHERE r.owner_id = v_owner AND r.project_id = v_project_id),
            'material_conversations', (SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY r.id), '[]'::jsonb) FROM sceneturn.material_conversations AS r WHERE r.owner_id = v_owner AND r.project_id = v_project_id),
            'material_messages', (SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY r.id), '[]'::jsonb) FROM sceneturn.material_messages AS r JOIN sceneturn.material_conversations AS c ON c.id = r.conversation_id WHERE r.owner_id = v_owner AND c.owner_id = v_owner AND c.project_id = v_project_id),
            'generations', (SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY r.id), '[]'::jsonb) FROM sceneturn.generations AS r WHERE r.owner_id = v_owner AND r.project_id = v_project_id),
            'text_generations', (SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY r.id), '[]'::jsonb) FROM sceneturn.text_generations AS r WHERE r.owner_id = v_owner AND r.project_id = v_project_id),
            'text_usage_ledger', (SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY r.id), '[]'::jsonb) FROM sceneturn.text_usage_ledger AS r WHERE r.owner_id = v_owner AND r.project_id = v_project_id),
            'image_jobs', (SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY r.id), '[]'::jsonb) FROM sceneturn.image_jobs AS r WHERE r.owner_id = v_owner AND r.project_id = v_project_id),
            'media_sequences', (SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY r.id), '[]'::jsonb) FROM sceneturn.media_sequences AS r WHERE r.owner_id = v_owner AND r.project_id = v_project_id),
            'media_adoptions', (SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY r.id), '[]'::jsonb) FROM sceneturn.media_adoptions AS r WHERE r.owner_id = v_owner AND r.project_id = v_project_id),
            'operations', (SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY r.id), '[]'::jsonb) FROM sceneturn.operations AS r WHERE r.owner_id = v_owner AND r.project_id = v_project_id)
        );
        v_source_id := v_project_data #>> '{metadata,importId}';
        IF coalesce(v_source_id, '') <> '' THEN
            SELECT coalesce(private_json::jsonb -> 'history', '{}'::jsonb) INTO v_source_history FROM sceneturn.transfer_jobs
                WHERE id = v_source_id AND owner_id = v_owner AND kind = 'import' AND status <> 'awaiting_upload';
            IF NOT FOUND THEN
                RAISE SQLSTATE 'PT404' USING MESSAGE = 'TRANSFER_NOT_FOUND';
            END IF;
            v_history := v_history || jsonb_build_object('sourceArchive', v_source_history);
        END IF;
    END IF;
    RETURN jsonb_build_object('project', v_project_data, 'canvases', v_canvases, 'assets', v_assets, 'history', v_history, 'conflicts', v_conflicts);
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_transfer_accept_export(
    p_owner_id uuid, p_project_id uuid, p_job_id uuid, p_operation_id text, p_kind text,
    p_expected_revision integer, p_identity_sha256 text, p_confirmation_hash text,
    p_private_snapshot jsonb, p_preview jsonb, p_dispatch_revision uuid, p_due_ms bigint
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_job sceneturn.transfer_jobs%ROWTYPE;
    v_project_revision integer;
    v_status text;
    v_now double precision;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    IF p_kind IS NULL OR p_kind NOT IN ('backup', 'game') OR p_operation_id IS NULL OR length(p_operation_id) NOT BETWEEN 1 AND 100
        OR p_expected_revision IS NULL OR p_expected_revision < 1
        OR p_identity_sha256 IS NULL OR p_identity_sha256 !~ '^[0-9a-f]{64}$'
        OR p_confirmation_hash IS NULL OR p_confirmation_hash !~ '^[0-9a-f]{64}$'
        OR jsonb_typeof(p_private_snapshot) IS DISTINCT FROM 'object' OR jsonb_typeof(p_preview) IS DISTINCT FROM 'object'
        OR jsonb_typeof(p_preview -> 'issues') IS DISTINCT FROM 'array' THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'TRANSFER_REQUEST_INVALID';
    END IF;
    PERFORM pg_advisory_xact_lock(8314610783521762916);
    SELECT revision INTO v_project_revision FROM sceneturn.projects
        WHERE id = p_project_id::text AND owner_id = p_owner_id::text FOR UPDATE;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'PROJECT_NOT_FOUND';
    END IF;
    SELECT * INTO v_job FROM sceneturn.transfer_jobs
        WHERE owner_id = p_owner_id::text AND operation_id = p_operation_id;
    IF FOUND THEN
        IF (v_job.project_id, v_job.kind, v_job.identity_sha256) IS DISTINCT FROM
            (p_project_id::text, p_kind, p_identity_sha256) THEN
            RAISE SQLSTATE 'PT409' USING MESSAGE = 'OPERATION_CONFLICT';
        END IF;
        RETURN jsonb_build_object('job', to_jsonb(v_job), 'replayed', true);
    END IF;
    SELECT * INTO v_job FROM sceneturn.transfer_jobs
        WHERE owner_id = p_owner_id::text AND project_id = p_project_id::text AND kind = p_kind AND identity_sha256 = p_identity_sha256;
    IF FOUND THEN
        RETURN jsonb_build_object('job', to_jsonb(v_job), 'replayed', true);
    END IF;
    IF v_project_revision <> p_expected_revision THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'PROJECT_REVISION_CONFLICT';
    END IF;
    v_status := CASE WHEN jsonb_array_length(p_preview -> 'issues') > 0 THEN 'blocked'
        WHEN p_kind = 'game' THEN 'awaiting_confirmation' ELSE 'queued' END;
    IF v_status = 'queued' AND (p_dispatch_revision IS NULL OR p_due_ms IS NULL) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'TRANSFER_DISPATCH_REQUIRED';
    END IF;
    v_now := extract(epoch FROM clock_timestamp());
    INSERT INTO sceneturn.transfer_jobs (
        id, owner_id, project_id, kind, operation_id, identity_sha256, source_revision, status,
        request_json, private_json, result_json, confirmation_hash, created_at, updated_at
    ) VALUES (
        p_job_id::text, p_owner_id::text, p_project_id::text, p_kind, p_operation_id,
        p_identity_sha256, p_expected_revision, v_status, '{"phase":"build"}',
        p_private_snapshot, p_preview, p_confirmation_hash, v_now, v_now
    ) RETURNING * INTO v_job;
    IF v_status = 'queued' THEN
        PERFORM sceneturn.transfer_schedule(p_job_id, p_dispatch_revision, p_due_ms);
    END IF;
    RETURN jsonb_build_object('job', to_jsonb(v_job), 'replayed', false);
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_transfer_resume_export(
    p_owner_id uuid, p_job_id uuid, p_confirmation_hash text, p_missing_archive_sha256 text,
    p_dispatch_revision uuid, p_due_ms bigint
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_job sceneturn.transfer_jobs%ROWTYPE;
    v_previous jsonb;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    PERFORM pg_advisory_xact_lock(8314610783521762916);
    SELECT * INTO v_job FROM sceneturn.transfer_jobs WHERE id = p_job_id::text AND owner_id = p_owner_id::text
        AND kind IN ('backup', 'game') FOR UPDATE;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'TRANSFER_NOT_FOUND';
    END IF;
    IF p_confirmation_hash IS NOT NULL AND v_job.confirmation_hash IS DISTINCT FROM p_confirmation_hash THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'TRANSFER_CONFIRMATION_CHANGED';
    END IF;
    IF v_job.status IN ('queued', 'processing') OR (v_job.status = 'ready' AND p_confirmation_hash IS NOT NULL) THEN
        RETURN jsonb_build_object('job', to_jsonb(v_job), 'replayed', true);
    END IF;
    IF p_confirmation_hash IS NOT NULL THEN
        IF v_job.status <> 'awaiting_confirmation' OR NOT EXISTS (
            SELECT 1 FROM sceneturn.projects WHERE id = v_job.project_id AND owner_id = p_owner_id::text
                AND revision = v_job.source_revision
        ) THEN
            RAISE SQLSTATE 'PT409' USING MESSAGE = 'TRANSFER_NOT_CONFIRMABLE';
        END IF;
    ELSIF v_job.status <> 'failed' AND (
        v_job.status <> 'ready' OR p_missing_archive_sha256 IS NULL
        OR (v_job.private_json ->> 'archiveSha256') IS DISTINCT FROM p_missing_archive_sha256
    ) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'TRANSFER_RETRY_NOT_ALLOWED';
    END IF;
    IF p_dispatch_revision IS NULL OR p_due_ms IS NULL THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'TRANSFER_DISPATCH_REQUIRED';
    END IF;
    v_previous := sceneturn.transfer_schedule(p_job_id, p_dispatch_revision, p_due_ms);
    UPDATE sceneturn.transfer_jobs SET status = 'queued', error_code = NULL, error_message = NULL,
        updated_at = extract(epoch FROM clock_timestamp()) WHERE id = p_job_id::text RETURNING * INTO v_job;
    RETURN jsonb_build_object('job', to_jsonb(v_job), 'replayed', false, 'obsolete', v_previous);
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_transfer_claim(
    p_job_id uuid, p_worker_id uuid, p_marker_revision uuid, p_marker_due_ms bigint,
    p_immediate boolean, p_recovery_revision uuid, p_recovery_due_ms bigint
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_job sceneturn.transfer_jobs%ROWTYPE;
    v_dispatch sceneturn.deployment_dispatch%ROWTYPE;
    v_previous jsonb;
    v_now double precision;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    PERFORM pg_advisory_xact_lock(8314610783521762916);
    v_now := extract(epoch FROM clock_timestamp());
    SELECT * INTO v_dispatch FROM sceneturn.deployment_dispatch
        WHERE kind = 'transfer' AND job_id = p_job_id::text;
    IF v_dispatch.due_ms IS NULL OR (v_dispatch.revision, v_dispatch.due_ms) IS DISTINCT FROM
        (p_marker_revision::text, p_marker_due_ms) THEN
        RETURN jsonb_build_object('state', 'stale');
    END IF;
    IF p_immediate IS NOT TRUE AND p_marker_due_ms > v_now * 1000 THEN
        RETURN jsonb_build_object('state', 'early');
    END IF;
    SELECT * INTO v_job FROM sceneturn.transfer_jobs WHERE id = p_job_id::text FOR UPDATE;
    IF NOT FOUND OR v_job.status NOT IN ('queued', 'processing') THEN
        PERFORM sceneturn.transfer_schedule(p_job_id, NULL, NULL);
        RETURN jsonb_build_object('state', 'stale');
    END IF;
    IF v_job.status = 'processing' AND v_job.lease_expires_at > v_now THEN
        RETURN jsonb_build_object('state', 'leased');
    END IF;
    IF v_job.kind NOT IN ('backup', 'game', 'asset_upload', 'import') THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'TRANSFER_WORKER_UNAVAILABLE';
    END IF;
    IF p_worker_id IS NULL OR p_recovery_revision IS NULL OR p_recovery_revision = p_marker_revision OR
        p_recovery_due_ms IS NULL OR p_recovery_due_ms < (v_now + 30) * 1000 THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'TRANSFER_DISPATCH_REQUIRED';
    END IF;
    v_previous := sceneturn.transfer_schedule(p_job_id, p_recovery_revision, p_recovery_due_ms);
    UPDATE sceneturn.transfer_jobs SET status = 'processing', lease_owner = p_worker_id::text,
        lease_expires_at = v_now + 30, updated_at = v_now WHERE id = p_job_id::text RETURNING * INTO v_job;
    RETURN jsonb_build_object('state', 'claimed', 'job', to_jsonb(v_job), 'obsolete', v_previous);
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_transfer_renew(p_job_id uuid, p_worker_id uuid) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_now double precision := extract(epoch FROM clock_timestamp());
BEGIN
    PERFORM sceneturn.require_gateway_access();
    UPDATE sceneturn.transfer_jobs SET lease_expires_at = v_now + 30
        WHERE id = p_job_id::text AND lease_owner = p_worker_id::text AND status = 'processing' AND lease_expires_at > v_now;
    RETURN FOUND;
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_transfer_settle_export(
    p_job_id uuid, p_worker_id uuid, p_archive jsonb, p_error_code text, p_error_message text
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_job sceneturn.transfer_jobs%ROWTYPE;
    v_previous jsonb;
    v_now double precision;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    PERFORM pg_advisory_xact_lock(8314610783521762916);
    v_now := extract(epoch FROM clock_timestamp());
    SELECT * INTO v_job FROM sceneturn.transfer_jobs WHERE id = p_job_id::text AND kind IN ('backup', 'game')
        AND status = 'processing' AND lease_owner = p_worker_id::text AND lease_expires_at > v_now FOR UPDATE;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('state', 'lease_lost');
    END IF;
    IF p_error_code IS NULL THEN
        IF jsonb_typeof(p_archive) IS DISTINCT FROM 'object' OR
            coalesce(p_archive ->> 'archive', '') !~ ('^transfers/' || p_job_id::text || '/' || p_worker_id::text || '\.zip$')
            OR (p_archive ->> 'directory') IS DISTINCT FROM ('transfers/' || p_job_id::text || '/' || p_worker_id::text)
            OR coalesce(p_archive ->> 'archiveSha256', '') !~ '^[0-9a-f]{64}$'
            OR jsonb_typeof(p_archive -> 'archiveBytes') IS DISTINCT FROM 'number'
            OR (p_archive ->> 'archiveBytes')::numeric <= 0
            OR jsonb_typeof(p_archive -> 'fileManifest') IS DISTINCT FROM 'array'
            OR p_error_message IS NOT NULL THEN
            RAISE SQLSTATE 'PT422' USING MESSAGE = 'TRANSFER_ARTIFACT_INVALID';
        END IF;
    ELSIF p_archive IS NOT NULL OR p_error_code !~ '^[A-Z][A-Z0-9_]{0,99}$' OR
        p_error_message IS NULL OR length(p_error_message) NOT BETWEEN 1 AND 800 THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'TRANSFER_FAILURE_INVALID';
    END IF;
    v_previous := sceneturn.transfer_schedule(p_job_id, NULL, NULL);
    UPDATE sceneturn.transfer_jobs SET status = CASE WHEN p_error_code IS NULL THEN 'ready' ELSE 'failed' END,
        private_json = CASE WHEN p_archive IS NULL THEN private_json ELSE (private_json::jsonb || jsonb_build_object(
            'archive', p_archive -> 'archive', 'directory', p_archive -> 'directory',
            'archiveSha256', p_archive -> 'archiveSha256', 'archiveBytes', p_archive -> 'archiveBytes',
            'fileManifest', p_archive -> 'fileManifest'
        ))::json END,
        error_code = p_error_code, error_message = p_error_message, lease_owner = NULL, lease_expires_at = NULL,
        updated_at = v_now WHERE id = p_job_id::text RETURNING * INTO v_job;
    RETURN jsonb_build_object('state', 'settled', 'job', to_jsonb(v_job), 'obsolete', v_previous);
END;
$$;

REVOKE ALL ON FUNCTION sceneturn.transfer_schedule(uuid, uuid, bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION
    public.sceneturn_transfer_read(uuid, uuid),
    public.sceneturn_transfer_artifact(uuid, uuid),
    public.sceneturn_transfer_prepare_export(uuid, uuid, text, text, integer, text),
    public.sceneturn_transfer_accept_export(uuid, uuid, uuid, text, text, integer, text, text, jsonb, jsonb, uuid, bigint),
    public.sceneturn_transfer_resume_export(uuid, uuid, text, text, uuid, bigint),
    public.sceneturn_transfer_claim(uuid, uuid, uuid, bigint, boolean, uuid, bigint),
    public.sceneturn_transfer_renew(uuid, uuid),
    public.sceneturn_transfer_settle_export(uuid, uuid, jsonb, text, text)
FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION
    public.sceneturn_transfer_read(uuid, uuid),
    public.sceneturn_transfer_artifact(uuid, uuid),
    public.sceneturn_transfer_prepare_export(uuid, uuid, text, text, integer, text),
    public.sceneturn_transfer_accept_export(uuid, uuid, uuid, text, text, integer, text, text, jsonb, jsonb, uuid, bigint),
    public.sceneturn_transfer_resume_export(uuid, uuid, text, text, uuid, bigint),
    public.sceneturn_transfer_claim(uuid, uuid, uuid, bigint, boolean, uuid, bigint),
    public.sceneturn_transfer_renew(uuid, uuid),
    public.sceneturn_transfer_settle_export(uuid, uuid, jsonb, text, text)
TO service_role;
