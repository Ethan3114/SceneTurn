CREATE OR REPLACE FUNCTION sceneturn.matching_import(p_owner_id text, p_operation_id text, p_identity text)
RETURNS sceneturn.transfer_jobs LANGUAGE plpgsql STABLE SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_job sceneturn.transfer_jobs%ROWTYPE;
BEGIN
    SELECT * INTO v_job FROM sceneturn.transfer_jobs WHERE owner_id = p_owner_id AND operation_id = p_operation_id;
    IF FOUND THEN
        IF (v_job.kind::text, v_job.identity_sha256::text) IS DISTINCT FROM ('import', p_identity) THEN
            RAISE SQLSTATE 'PT409' USING MESSAGE = 'OPERATION_CONFLICT';
        END IF;
        RETURN v_job;
    END IF;
    SELECT * INTO v_job FROM sceneturn.transfer_jobs WHERE owner_id = p_owner_id AND kind = 'import' AND identity_sha256 = p_identity;
    RETURN v_job;
END;
$$;

CREATE OR REPLACE FUNCTION sceneturn.scoped_import_upload(p_owner_id text, p_job_id text, p_session_hash text)
RETURNS sceneturn.transfer_jobs LANGUAGE plpgsql STABLE SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_job sceneturn.transfer_jobs%ROWTYPE;
BEGIN
    SELECT * INTO v_job FROM sceneturn.transfer_jobs WHERE id = p_job_id AND owner_id = p_owner_id AND kind = 'import';
    IF NOT FOUND OR p_session_hash IS NULL OR v_job.request_json #>> '{directUpload,sessionHash}' IS DISTINCT FROM p_session_hash THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'UPLOAD_NOT_FOUND';
    END IF;
    IF v_job.status = 'awaiting_upload' AND (v_job.request_json #>> '{directUpload,expiresAt}')::double precision
        <= extract(epoch FROM clock_timestamp()) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'UPLOAD_EXPIRED';
    END IF;
    RETURN v_job;
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_import_reserve(
    p_owner_id text, p_job_id uuid, p_operation_id text, p_identity text, p_session_hash text,
    p_filename text, p_content_type text, p_bytes integer, p_sha256 text
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_job sceneturn.transfer_jobs%ROWTYPE;
    v_prior jsonb;
    v_now double precision;
    v_reused boolean;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    IF p_job_id IS NULL OR coalesce(length(p_operation_id), 0) NOT BETWEEN 1 AND 100 OR
        coalesce(p_identity, '') !~ '^[a-f0-9]{64}$' OR coalesce(p_sha256, '') !~ '^[a-f0-9]{64}$' OR
        coalesce(p_session_hash, '') !~ '^[a-f0-9]{64}$' OR coalesce(length(p_filename), 0) NOT BETWEEN 1 AND 200 OR
        right(lower(p_filename), 4) <> '.zip' OR strpos(p_filename, '/') > 0 OR strpos(p_filename, chr(92)) > 0 OR
        strpos(p_filename, ':') > 0 OR coalesce(length(p_content_type), 0) NOT BETWEEN 1 AND 200 OR
        p_content_type ~ E'[\r\n]' OR p_bytes IS NULL OR p_bytes <= 0 THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'TRANSFER_REQUEST_INVALID';
    END IF;
    PERFORM pg_advisory_xact_lock(8314610783521762916);
    v_job := sceneturn.matching_import(p_owner_id, p_operation_id, p_identity);
    v_reused := v_job.id IS NOT NULL;
    v_now := extract(epoch FROM clock_timestamp());
    IF v_reused THEN
        IF v_job.status <> 'awaiting_upload' THEN
            RETURN jsonb_build_object('job', to_jsonb(v_job), 'reused', true);
        END IF;
        v_prior := v_job.request_json::jsonb -> 'directUpload';
        IF (v_prior ->> 'sha256', (v_prior ->> 'bytes')::integer) IS DISTINCT FROM (p_sha256, p_bytes) THEN
            RAISE SQLSTATE 'PT409' USING MESSAGE = 'OPERATION_CONFLICT';
        END IF;
        IF (v_prior ->> 'expiresAt')::double precision > v_now THEN
            IF v_prior ->> 'sessionHash' IS DISTINCT FROM p_session_hash THEN
                RAISE SQLSTATE 'PT409' USING MESSAGE = 'UPLOAD_SESSION_CHANGED';
            END IF;
            RETURN jsonb_build_object('job', to_jsonb(v_job), 'reused', true);
        END IF;
    END IF;
    IF (SELECT count(*) FROM sceneturn.transfer_jobs WHERE owner_id = p_owner_id AND status = 'awaiting_upload'
        AND (request_json #>> '{directUpload,expiresAt}')::double precision > v_now) >= 4 THEN
        RAISE SQLSTATE 'PT429' USING MESSAGE = 'TOO_MANY_PENDING_UPLOADS';
    END IF;
    v_prior := jsonb_build_object('phase', 'preview', 'archiveSha256', p_sha256, 'directUpload', jsonb_build_object(
        'key', 'transfers/' || coalesce(v_job.id, p_job_id::text) || '/source.zip', 'filename', p_filename,
        'contentType', p_content_type, 'bytes', p_bytes, 'sha256', p_sha256, 'expiresAt', v_now + 3600, 'sessionHash', p_session_hash));
    IF v_reused THEN
        UPDATE sceneturn.transfer_jobs SET request_json = v_prior::json, updated_at = v_now WHERE id = v_job.id RETURNING * INTO v_job;
    ELSE
        INSERT INTO sceneturn.transfer_jobs(id, owner_id, kind, operation_id, identity_sha256, status, request_json,
            private_json, result_json, created_at, updated_at)
        VALUES (p_job_id::text, p_owner_id, 'import', p_operation_id, p_identity, 'awaiting_upload', v_prior::json,
            '{}'::json, '{}'::json, v_now, v_now) RETURNING * INTO v_job;
    END IF;
    RETURN jsonb_build_object('job', to_jsonb(v_job), 'reused', v_reused);
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_import_read_upload(p_owner_id text, p_job_id text, p_session_hash text)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM sceneturn.require_gateway_access();
    RETURN to_jsonb(sceneturn.scoped_import_upload(p_owner_id, p_job_id, p_session_hash));
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_import_enqueue(
    p_owner_id text, p_job_id uuid, p_session_hash text, p_operation_id text, p_identity text, p_archive_sha256 text,
    p_dispatch_revision uuid, p_due_ms bigint
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_job sceneturn.transfer_jobs%ROWTYPE;
    v_now double precision;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    PERFORM pg_advisory_xact_lock(8314610783521762916);
    IF p_session_hash IS NOT NULL THEN
        IF p_operation_id IS NOT NULL OR p_identity IS NOT NULL OR p_archive_sha256 IS NOT NULL THEN
            RAISE SQLSTATE 'PT422' USING MESSAGE = 'TRANSFER_REQUEST_INVALID';
        END IF;
        v_job := sceneturn.scoped_import_upload(p_owner_id, p_job_id::text, p_session_hash);
        IF v_job.status <> 'awaiting_upload' THEN
            RETURN jsonb_build_object('job', to_jsonb(v_job), 'queued', false);
        END IF;
    ELSE
        IF p_job_id IS NULL OR coalesce(length(p_operation_id), 0) NOT BETWEEN 1 AND 100 OR
            coalesce(p_identity, '') !~ '^[a-f0-9]{64}$' OR coalesce(p_archive_sha256, '') !~ '^[a-f0-9]{64}$' THEN
            RAISE SQLSTATE 'PT422' USING MESSAGE = 'TRANSFER_REQUEST_INVALID';
        END IF;
        v_job := sceneturn.matching_import(p_owner_id, p_operation_id, p_identity);
        IF v_job.id IS NOT NULL THEN
            IF v_job.status = 'awaiting_upload' THEN
                RAISE SQLSTATE 'PT409' USING MESSAGE = 'IMPORT_UPLOAD_IN_PROGRESS';
            END IF;
            RETURN jsonb_build_object('job', to_jsonb(v_job), 'queued', false);
        END IF;
    END IF;
    IF p_dispatch_revision IS NULL OR p_due_ms IS NULL THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'TRANSFER_DISPATCH_REQUIRED';
    END IF;
    PERFORM sceneturn.transfer_schedule(p_job_id, p_dispatch_revision, p_due_ms);
    v_now := extract(epoch FROM clock_timestamp());
    IF p_session_hash IS NOT NULL THEN
        UPDATE sceneturn.transfer_jobs SET status = 'queued', updated_at = v_now WHERE id = p_job_id::text RETURNING * INTO v_job;
    ELSE
        INSERT INTO sceneturn.transfer_jobs(id, owner_id, kind, operation_id, identity_sha256, status, request_json,
            private_json, result_json, created_at, updated_at)
        VALUES (p_job_id::text, p_owner_id, 'import', p_operation_id, p_identity, 'queued',
            json_build_object('phase', 'preview', 'archiveSha256', p_archive_sha256), '{}'::json, '{}'::json, v_now, v_now)
        RETURNING * INTO v_job;
    END IF;
    RETURN jsonb_build_object('job', to_jsonb(v_job), 'queued', true);
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_import_resume(
    p_owner_id text, p_job_id uuid, p_confirmation_hash text, p_dispatch_revision uuid, p_due_ms bigint
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_job sceneturn.transfer_jobs%ROWTYPE;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    PERFORM pg_advisory_xact_lock(8314610783521762916);
    SELECT * INTO v_job FROM sceneturn.transfer_jobs WHERE id = p_job_id::text AND owner_id = p_owner_id AND kind = 'import' FOR UPDATE;
    IF NOT FOUND OR v_job.status = 'awaiting_upload' THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'TRANSFER_NOT_FOUND';
    END IF;
    IF p_confirmation_hash IS NOT NULL AND v_job.confirmation_hash IS DISTINCT FROM p_confirmation_hash THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'TRANSFER_CONFIRMATION_CHANGED';
    END IF;
    IF v_job.status IN ('queued', 'processing') OR (v_job.status = 'ready' AND p_confirmation_hash IS NOT NULL) THEN
        RETURN jsonb_build_object('job', to_jsonb(v_job), 'queued', false);
    END IF;
    IF p_confirmation_hash IS NOT NULL THEN
        IF v_job.status <> 'awaiting_confirmation' OR json_array_length(v_job.result_json -> 'issues') <> 0 THEN
            RAISE SQLSTATE 'PT409' USING MESSAGE = 'TRANSFER_NOT_CONFIRMABLE';
        END IF;
    ELSIF v_job.status <> 'failed' OR v_job.request_json ->> 'phase' <> 'apply' THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'TRANSFER_RETRY_NOT_ALLOWED';
    END IF;
    IF p_dispatch_revision IS NULL OR p_due_ms IS NULL THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'TRANSFER_DISPATCH_REQUIRED';
    END IF;
    PERFORM sceneturn.transfer_schedule(p_job_id, p_dispatch_revision, p_due_ms);
    UPDATE sceneturn.transfer_jobs SET status = 'queued', error_code = NULL, error_message = NULL,
        request_json = (request_json::jsonb || '{"phase":"apply"}'::jsonb)::json,
        updated_at = extract(epoch FROM clock_timestamp()) WHERE id = v_job.id RETURNING * INTO v_job;
    RETURN jsonb_build_object('job', to_jsonb(v_job), 'queued', true);
END;
$$;

CREATE OR REPLACE FUNCTION sceneturn.apply_verified_import(p_job sceneturn.transfer_jobs, p_written_at double precision)
RETURNS text LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_first_project text;
    v_item jsonb;
    v_project text;
    v_snapshot jsonb;
    v_envelope jsonb;
BEGIN
    FOR v_item IN SELECT value FROM jsonb_array_elements(p_job.private_json::jsonb -> 'projects') LOOP
        v_snapshot := v_item -> 'project';
        v_project := v_snapshot ->> 'id';
        IF v_snapshot #>> '{metadata,importId}' IS DISTINCT FROM p_job.id THEN
            RAISE SQLSTATE 'PT422' USING MESSAGE = 'TRANSFER_REQUEST_INVALID';
        END IF;
        INSERT INTO sceneturn.projects(id, owner_id, title, revision, data_json, created_at, updated_at)
        VALUES (v_project, p_job.owner_id, v_snapshot ->> 'title', 1, v_snapshot::json, p_written_at, p_written_at);
        INSERT INTO sceneturn.assets(id, owner_id, project_id, name, relpath, poster_relpath, mime_type, bytes, sha256,
            duration_ms, width, height, metadata_json, created_at)
        SELECT file.id, p_job.owner_id, v_project, file.name, file.relpath, file.poster_relpath, file.mime_type,
            file.bytes, file.sha256, file.duration_ms, file.width, file.height,
            (file.metadata_json || jsonb_build_object('importId', p_job.id, 'sourceId', file."sourceId"))::json, p_written_at
        FROM jsonb_to_recordset(v_item -> 'files') AS file(id text, "sourceId" text, name text, relpath text, poster_relpath text,
            mime_type text, bytes integer, sha256 text, duration_ms integer, width integer, height integer, metadata_json jsonb);
        INSERT INTO sceneturn.canvases(id, owner_id, project_id, scene_id, data_json, updated_at)
        SELECT gen_random_uuid()::text, p_job.owner_id, v_project, canvas.value ->> 'sceneId', canvas.value::json, p_written_at
        FROM jsonb_array_elements(v_item -> 'canvases') AS canvas(value);
        v_envelope := sceneturn.project_snapshot_envelope(p_job.owner_id, v_project, v_snapshot, 1);
        PERFORM sceneturn.record_project_envelope(p_job.owner_id, v_project, v_envelope, p_written_at);
        v_first_project := coalesce(v_first_project, v_project);
    END LOOP;
    IF v_first_project IS NULL THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'TRANSFER_REQUEST_INVALID';
    END IF;
    RETURN v_first_project;
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_import_settle(
    p_job_id uuid, p_worker_id uuid, p_preview jsonb, p_confirmation_hash text,
    p_verified_source_digest text, p_error_code text, p_error_message text
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_job sceneturn.transfer_jobs%ROWTYPE;
    v_existing sceneturn.transfer_jobs%ROWTYPE;
    v_previous jsonb;
    v_status text;
    v_now double precision;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    PERFORM pg_advisory_xact_lock(8314610783521762916);
    v_now := extract(epoch FROM clock_timestamp());
    SELECT * INTO v_job FROM sceneturn.transfer_jobs WHERE id = p_job_id::text AND kind = 'import'
        AND status = 'processing' AND lease_owner = p_worker_id::text AND lease_expires_at > v_now FOR UPDATE;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('state', 'lease_lost');
    END IF;
    IF p_error_code IS NOT NULL THEN
        IF p_preview IS NOT NULL OR p_confirmation_hash IS NOT NULL OR p_verified_source_digest IS NOT NULL OR
            p_error_code !~ '^[A-Z][A-Z0-9_]{0,99}$' OR coalesce(length(p_error_message), 0) NOT BETWEEN 1 AND 800 THEN
            RAISE SQLSTATE 'PT422' USING MESSAGE = 'TRANSFER_FAILURE_INVALID';
        END IF;
        v_status := 'failed';
    ELSIF v_job.request_json ->> 'phase' = 'preview' THEN
        IF jsonb_typeof(p_preview) IS DISTINCT FROM 'object' OR coalesce(p_preview ->> 'sourceDigest', '') !~ '^[a-f0-9]{64}$' OR
            jsonb_typeof(p_preview #> '{view,issues}') IS DISTINCT FROM 'array' OR jsonb_typeof(p_preview -> 'projects') IS DISTINCT FROM 'array'
            OR coalesce(p_confirmation_hash, '') !~ '^[a-f0-9]{64}$' OR p_verified_source_digest IS NOT NULL OR p_error_message IS NOT NULL THEN
            RAISE SQLSTATE 'PT422' USING MESSAGE = 'TRANSFER_REQUEST_INVALID';
        END IF;
        v_job.private_json := p_preview::json;
        v_job.result_json := (p_preview -> 'view')::json;
        v_job.source_digest := p_preview ->> 'sourceDigest';
        v_job.confirmation_hash := p_confirmation_hash;
        v_status := CASE WHEN jsonb_array_length(p_preview #> '{view,issues}') > 0 THEN 'blocked' ELSE 'awaiting_confirmation' END;
    ELSIF v_job.request_json ->> 'phase' = 'apply' THEN
        IF p_preview IS NOT NULL OR p_error_message IS NOT NULL OR p_confirmation_hash IS NOT NULL
            OR p_verified_source_digest IS NULL OR p_verified_source_digest IS DISTINCT FROM v_job.source_digest THEN
            RAISE SQLSTATE 'PT422' USING MESSAGE = 'TRANSFER_REQUEST_INVALID';
        END IF;
        SELECT * INTO v_existing FROM sceneturn.transfer_jobs WHERE owner_id = v_job.owner_id AND kind = 'import'
            AND source_digest = v_job.source_digest AND status = 'ready' AND id <> v_job.id ORDER BY created_at LIMIT 1;
        IF FOUND THEN
            v_job.project_id := v_existing.project_id;
            v_job.result_json := v_existing.result_json;
        ELSE
            v_job.project_id := sceneturn.apply_verified_import(v_job, v_now);
        END IF;
        v_status := 'ready';
    ELSE
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'TRANSFER_REQUEST_INVALID';
    END IF;
    v_previous := sceneturn.transfer_schedule(p_job_id, NULL, NULL);
    UPDATE sceneturn.transfer_jobs SET project_id = v_job.project_id, status = v_status, result_json = v_job.result_json,
        private_json = v_job.private_json, source_digest = v_job.source_digest, confirmation_hash = v_job.confirmation_hash,
        error_code = p_error_code, error_message = p_error_message, lease_owner = NULL, lease_expires_at = NULL, updated_at = v_now
    WHERE id = v_job.id;
    RETURN jsonb_build_object('state', 'settled', 'obsolete', v_previous);
END;
$$;

CREATE OR REPLACE FUNCTION sceneturn.transfer_summary(p_job sceneturn.transfer_jobs) RETURNS jsonb
LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $$
    SELECT jsonb_build_object('id', p_job.id, 'kind', p_job.kind, 'project_id', p_job.project_id,
        'status', p_job.status, 'source_revision', p_job.source_revision, 'result_json', p_job.result_json,
        'confirmation_hash', p_job.confirmation_hash, 'error_code', p_job.error_code, 'error_message', p_job.error_message,
        'created_at', p_job.created_at, 'updated_at', p_job.updated_at);
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_transfer_summary(p_owner_id text, p_job_id text) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_summary jsonb;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    SELECT sceneturn.transfer_summary(job) INTO v_summary FROM sceneturn.transfer_jobs AS job
    WHERE job.owner_id = p_owner_id AND job.id = p_job_id AND job.kind IN ('import', 'backup', 'game')
        AND job.status <> 'awaiting_upload';
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'TRANSFER_NOT_FOUND';
    END IF;
    RETURN v_summary;
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_transfer_list(p_owner_id text, p_project_id text) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM sceneturn.require_gateway_access();
    IF p_project_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM sceneturn.projects WHERE id = p_project_id AND owner_id = p_owner_id) THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'PROJECT_NOT_FOUND';
    END IF;
    RETURN coalesce((SELECT jsonb_agg(sceneturn.transfer_summary(job) ORDER BY job.created_at DESC, job.id)
        FROM sceneturn.transfer_jobs AS job WHERE job.owner_id = p_owner_id AND job.kind IN ('import', 'backup', 'game')
        AND job.status <> 'awaiting_upload' AND (p_project_id IS NULL OR job.project_id = p_project_id)), '[]'::jsonb);
END;
$$;

REVOKE EXECUTE ON FUNCTION sceneturn.matching_import(text, text, text), sceneturn.scoped_import_upload(text, text, text),
    sceneturn.apply_verified_import(sceneturn.transfer_jobs, double precision), sceneturn.transfer_summary(sceneturn.transfer_jobs)
FROM PUBLIC, anon, authenticated;

REVOKE EXECUTE ON FUNCTION public.sceneturn_import_reserve(text, uuid, text, text, text, text, text, integer, text),
    public.sceneturn_import_read_upload(text, text, text), public.sceneturn_import_enqueue(text, uuid, text, text, text, text, uuid, bigint),
    public.sceneturn_import_resume(text, uuid, text, uuid, bigint), public.sceneturn_import_settle(uuid, uuid, jsonb, text, text, text, text),
    public.sceneturn_transfer_list(text, text), public.sceneturn_transfer_summary(text, text)
FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION public.sceneturn_import_reserve(text, uuid, text, text, text, text, text, integer, text),
    public.sceneturn_import_read_upload(text, text, text), public.sceneturn_import_enqueue(text, uuid, text, text, text, text, uuid, bigint),
    public.sceneturn_import_resume(text, uuid, text, uuid, bigint), public.sceneturn_import_settle(uuid, uuid, jsonb, text, text, text, text),
    public.sceneturn_transfer_list(text, text), public.sceneturn_transfer_summary(text, text)
TO service_role;
