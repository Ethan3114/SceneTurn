CREATE OR REPLACE FUNCTION sceneturn.asset_upload_descriptor(p_job sceneturn.transfer_jobs) RETURNS jsonb
LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $$
    SELECT jsonb_build_object(
        'id', p_job.id, 'status', p_job.status,
        'directUpload', (p_job.request_json::jsonb -> 'directUpload') - 'sessionHash',
        'errorCode', p_job.error_code, 'errorMessage', p_job.error_message,
        'assetId', p_job.result_json ->> 'assetId'
    );
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_asset_upload_reserve(
    p_owner_id text, p_project_id text, p_job_id uuid, p_operation_id text, p_identity_sha256 text,
    p_session_hash text, p_filename text, p_content_type text, p_bytes integer, p_sha256 text
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_job sceneturn.transfer_jobs%ROWTYPE;
    v_slot jsonb;
    v_now double precision;
    v_reused boolean;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    IF p_job_id IS NULL OR coalesce(length(p_operation_id), 0) NOT BETWEEN 1 AND 100
        OR coalesce(length(p_filename), 0) NOT BETWEEN 1 AND 200 OR strpos(p_filename, '/') > 0 OR strpos(p_filename, chr(92)) > 0
        OR coalesce(length(p_content_type), 0) NOT BETWEEN 1 AND 200 OR p_content_type ~ E'[\r\n]'
        OR p_bytes IS NULL OR p_bytes <= 0
        OR coalesce(p_sha256, '') !~ '^[0-9a-f]{64}$' OR coalesce(p_identity_sha256, '') !~ '^[0-9a-f]{64}$'
        OR coalesce(p_session_hash, '') !~ '^[0-9a-f]{64}$' THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'UPLOAD_REQUEST_INVALID';
    END IF;
    PERFORM pg_advisory_xact_lock(8314610783521762916);
    IF NOT EXISTS (SELECT 1 FROM sceneturn.projects WHERE id = p_project_id AND owner_id = p_owner_id) THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'PROJECT_NOT_FOUND';
    END IF;
    SELECT * INTO v_job FROM sceneturn.transfer_jobs WHERE owner_id = p_owner_id AND operation_id = p_operation_id FOR UPDATE;
    IF FOUND THEN
        IF (v_job.kind::text, v_job.project_id::text, v_job.identity_sha256::text) IS DISTINCT FROM
            ('asset_upload', p_project_id, p_identity_sha256) THEN
            RAISE SQLSTATE 'PT409' USING MESSAGE = 'OPERATION_CONFLICT';
        END IF;
    ELSE
        SELECT * INTO v_job FROM sceneturn.transfer_jobs WHERE owner_id = p_owner_id AND project_id = p_project_id
            AND kind = 'asset_upload' AND identity_sha256 = p_identity_sha256 FOR UPDATE;
    END IF;
    v_reused := FOUND;
    v_now := extract(epoch FROM clock_timestamp());
    IF v_reused THEN
        IF v_job.status <> 'awaiting_upload' THEN
            RETURN jsonb_build_object('job', sceneturn.asset_upload_descriptor(v_job), 'reused', true);
        END IF;
        v_slot := v_job.request_json::jsonb -> 'directUpload';
        IF (v_slot ->> 'sha256', (v_slot ->> 'bytes')::integer) IS DISTINCT FROM (p_sha256, p_bytes) THEN
            RAISE SQLSTATE 'PT409' USING MESSAGE = 'OPERATION_CONFLICT';
        END IF;
        IF (v_slot ->> 'expiresAt')::double precision > v_now THEN
            IF v_slot ->> 'sessionHash' IS DISTINCT FROM p_session_hash THEN
                RAISE SQLSTATE 'PT409' USING MESSAGE = 'UPLOAD_SESSION_CHANGED';
            END IF;
            RETURN jsonb_build_object('job', sceneturn.asset_upload_descriptor(v_job), 'reused', true);
        END IF;
    END IF;
    IF (
        SELECT count(*) FROM sceneturn.transfer_jobs WHERE owner_id = p_owner_id AND status = 'awaiting_upload'
          AND (request_json #>> '{directUpload,expiresAt}')::double precision > v_now
    ) >= 4 THEN
        RAISE SQLSTATE 'PT429' USING MESSAGE = 'TOO_MANY_PENDING_UPLOADS';
    END IF;
    v_slot := jsonb_build_object('phase', 'ingest', 'directUpload', jsonb_build_object(
        'key', 'incoming/' || coalesce(v_job.id, p_job_id::text) || '/source', 'filename', p_filename,
        'contentType', p_content_type, 'bytes', p_bytes, 'sha256', p_sha256,
        'expiresAt', v_now + 3600, 'sessionHash', p_session_hash
    ));
    IF v_reused THEN
        UPDATE sceneturn.transfer_jobs SET request_json = v_slot::json, updated_at = v_now
        WHERE id = v_job.id RETURNING * INTO v_job;
    ELSE
        INSERT INTO sceneturn.transfer_jobs(id, owner_id, project_id, kind, operation_id, identity_sha256,
            status, request_json, private_json, result_json, created_at, updated_at)
        VALUES (p_job_id::text, p_owner_id, p_project_id, 'asset_upload', p_operation_id, p_identity_sha256,
            'awaiting_upload', v_slot::json, '{}'::json, '{}'::json, v_now, v_now)
        RETURNING * INTO v_job;
    END IF;
    RETURN jsonb_build_object('job', sceneturn.asset_upload_descriptor(v_job), 'reused', v_reused);
END;
$$;

CREATE OR REPLACE FUNCTION sceneturn.asset_upload_scoped_job(
    p_owner_id text, p_project_id text, p_job_id text, p_session_hash text
) RETURNS sceneturn.transfer_jobs
LANGUAGE plpgsql STABLE SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_job sceneturn.transfer_jobs%ROWTYPE;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM sceneturn.projects WHERE id = p_project_id AND owner_id = p_owner_id) THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'PROJECT_NOT_FOUND';
    END IF;
    SELECT * INTO v_job FROM sceneturn.transfer_jobs WHERE owner_id = p_owner_id AND project_id = p_project_id
      AND id = p_job_id AND kind = 'asset_upload' AND request_json -> 'directUpload' IS NOT NULL;
    IF NOT FOUND OR (p_session_hash IS NOT NULL AND v_job.request_json #>> '{directUpload,sessionHash}' IS DISTINCT FROM p_session_hash) THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'UPLOAD_NOT_FOUND';
    END IF;
    IF p_session_hash IS NOT NULL AND v_job.status = 'awaiting_upload' AND
        (v_job.request_json #>> '{directUpload,expiresAt}')::double precision <= extract(epoch FROM clock_timestamp()) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'UPLOAD_EXPIRED';
    END IF;
    RETURN v_job;
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_asset_upload_get(
    p_owner_id text, p_project_id text, p_job_id text, p_session_hash text
) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM sceneturn.require_gateway_access();
    RETURN sceneturn.asset_upload_descriptor(sceneturn.asset_upload_scoped_job(p_owner_id, p_project_id, p_job_id, p_session_hash));
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_asset_upload_complete(
    p_owner_id text, p_project_id text, p_job_id text, p_session_hash text, p_dispatch_revision uuid, p_due_ms bigint
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_job sceneturn.transfer_jobs%ROWTYPE;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    IF p_session_hash IS NULL THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'UPLOAD_NOT_FOUND';
    END IF;
    PERFORM pg_advisory_xact_lock(8314610783521762916);
    v_job := sceneturn.asset_upload_scoped_job(p_owner_id, p_project_id, p_job_id, p_session_hash);
    IF v_job.status <> 'awaiting_upload' THEN
        RETURN jsonb_build_object('job', sceneturn.asset_upload_descriptor(v_job), 'queued', false);
    END IF;
    IF p_dispatch_revision IS NULL OR p_due_ms IS NULL THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'TRANSFER_DISPATCH_REQUIRED';
    END IF;
    PERFORM sceneturn.transfer_schedule(v_job.id::uuid, p_dispatch_revision, p_due_ms);
    UPDATE sceneturn.transfer_jobs SET status = 'queued', updated_at = extract(epoch FROM clock_timestamp())
    WHERE id = v_job.id RETURNING * INTO v_job;
    RETURN jsonb_build_object('job', sceneturn.asset_upload_descriptor(v_job), 'queued', true);
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_asset_upload_settle(
    p_job_id uuid, p_worker_id uuid, p_media jsonb, p_error_code text, p_error_message text
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_job sceneturn.transfer_jobs%ROWTYPE;
    v_slot jsonb;
    v_previous jsonb;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    PERFORM pg_advisory_xact_lock(8314610783521762916);
    SELECT * INTO v_job FROM sceneturn.transfer_jobs WHERE id = p_job_id::text AND kind = 'asset_upload'
        AND status = 'processing' AND lease_owner = p_worker_id::text
        AND lease_expires_at > extract(epoch FROM clock_timestamp()) FOR UPDATE;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('state', 'lease_lost');
    END IF;
    IF p_error_code IS NULL THEN
        v_slot := v_job.request_json::jsonb -> 'directUpload';
        IF jsonb_typeof(p_media) IS DISTINCT FROM 'object' OR p_error_message IS NOT NULL OR
            (v_slot ->> 'sha256', v_slot ->> 'bytes') IS DISTINCT FROM (p_media ->> 'sha256', p_media ->> 'bytes') THEN
            RAISE SQLSTATE 'PT422' USING MESSAGE = 'UPLOAD_REQUEST_INVALID';
        END IF;
        PERFORM public.sceneturn_asset_register(v_job.owner_id, v_job.project_id, p_job_id, p_media ->> 'name',
            p_media ->> 'relpath', p_media ->> 'poster_relpath', p_media ->> 'mime_type', (p_media ->> 'bytes')::integer,
            p_media ->> 'sha256', (p_media ->> 'duration_ms')::integer, (p_media ->> 'width')::integer,
            (p_media ->> 'height')::integer, p_media -> 'metadata_json');
    ELSIF p_media IS NOT NULL OR p_error_code !~ '^[A-Z][A-Z0-9_]{0,99}$' OR coalesce(length(p_error_message), 0) NOT BETWEEN 1 AND 800 THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'TRANSFER_FAILURE_INVALID';
    END IF;
    v_previous := sceneturn.transfer_schedule(p_job_id, NULL, NULL);
    UPDATE sceneturn.transfer_jobs SET status = CASE WHEN p_error_code IS NULL THEN 'ready' ELSE 'failed' END,
        result_json = CASE WHEN p_error_code IS NULL THEN json_build_object('assetId', p_job_id) ELSE '{}'::json END,
        error_code = p_error_code, error_message = p_error_message, lease_owner = NULL, lease_expires_at = NULL,
        updated_at = extract(epoch FROM clock_timestamp()) WHERE id = v_job.id;
    RETURN jsonb_build_object('state', 'settled', 'obsolete', v_previous);
END;
$$;

REVOKE EXECUTE ON FUNCTION
    sceneturn.asset_upload_descriptor(sceneturn.transfer_jobs),
    sceneturn.asset_upload_scoped_job(text, text, text, text),
    public.sceneturn_asset_upload_reserve(text, text, uuid, text, text, text, text, text, integer, text),
    public.sceneturn_asset_upload_get(text, text, text, text),
    public.sceneturn_asset_upload_complete(text, text, text, text, uuid, bigint),
    public.sceneturn_asset_upload_settle(uuid, uuid, jsonb, text, text)
FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION
    public.sceneturn_asset_upload_reserve(text, text, uuid, text, text, text, text, text, integer, text),
    public.sceneturn_asset_upload_get(text, text, text, text),
    public.sceneturn_asset_upload_complete(text, text, text, text, uuid, bigint),
    public.sceneturn_asset_upload_settle(uuid, uuid, jsonb, text, text)
TO service_role;
