CREATE OR REPLACE FUNCTION sceneturn.text_price(p_band text) RETURNS jsonb
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp AS $$
    SELECT jsonb_build_object(
        'version', 'deepseek-flash-2026-11-13', 'band', p_band, 'currency', 'CNY', 'costUnit', 'microYuan',
        'cacheHitMicrosPerMillion', CASE WHEN p_band = 'offPeak' THEN 20000 ELSE 40000 END,
        'cacheMissMicrosPerMillion', CASE WHEN p_band = 'offPeak' THEN 1000000 ELSE 2000000 END,
        'outputMicrosPerMillion', CASE WHEN p_band = 'offPeak' THEN 4000000 ELSE 8000000 END
    );
$$;

CREATE OR REPLACE FUNCTION sceneturn.text_release_budget(p_reservation integer, p_actual integer) RETURNS void
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF p_reservation < 0 OR p_actual NOT BETWEEN 0 AND p_reservation THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'TEXT_COST_OUT_OF_BOUNDS';
    END IF;
    UPDATE sceneturn.budgets
        SET text_reserved_micros = text_reserved_micros - p_reservation,
            text_spent_micros = text_spent_micros + p_actual
        WHERE id = 'global' AND text_reserved_micros >= p_reservation;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'TEXT_BUDGET_INCONSISTENT';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_text_settings(
    p_actor_id uuid, p_admin_email text, p_encrypted_key text
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_email text;
    v_provider sceneturn.text_provider_settings%ROWTYPE;
    v_can_manage boolean;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    SELECT email INTO v_email FROM sceneturn.users WHERE id = p_actor_id::text;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'MODEL_OWNER_NOT_FOUND';
    END IF;
    v_can_manage := coalesce(length(trim(p_admin_email)) > 0 AND lower(v_email) = lower(trim(p_admin_email)), false);
    IF p_encrypted_key IS NOT NULL THEN
        IF NOT v_can_manage THEN
            RAISE SQLSTATE 'PT403' USING MESSAGE = 'TEXT_SETTINGS_FORBIDDEN';
        END IF;
        IF length(p_encrypted_key) NOT BETWEEN 100 AND 4096 OR p_encrypted_key !~ '^gAAAAA[A-Za-z0-9_-]+={0,2}$' THEN
            RAISE SQLSTATE 'PT422' USING MESSAGE = 'TEXT_KEY_INVALID';
        END IF;
        PERFORM pg_advisory_xact_lock(8314610783521762916);
        INSERT INTO sceneturn.text_provider_settings(id, admin_owner_id, api_key_encrypted, provider, model, updated_at)
        VALUES ('platform', p_actor_id::text, p_encrypted_key, 'deepseek', 'deepseek-flash', extract(epoch FROM clock_timestamp()))
        ON CONFLICT (id) DO UPDATE SET admin_owner_id = EXCLUDED.admin_owner_id, api_key_encrypted = EXCLUDED.api_key_encrypted,
            provider = EXCLUDED.provider, model = EXCLUDED.model, updated_at = EXCLUDED.updated_at;
    END IF;
    SELECT * INTO v_provider FROM sceneturn.text_provider_settings WHERE id = 'platform';
    RETURN jsonb_build_object('configured', coalesce(v_provider.api_key_encrypted <> '' AND v_provider.model = 'deepseek-flash', false),
        'canManage', v_can_manage, 'revision', CASE WHEN v_can_manage AND v_provider.admin_owner_id = p_actor_id::text THEN v_provider.updated_at END);
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_text_prepare(
    p_actor_id uuid, p_project_id uuid, p_scene_id text
) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_project sceneturn.projects%ROWTYPE;
    v_provider sceneturn.text_provider_settings%ROWTYPE;
    v_canvas jsonb;
    v_video_resources jsonb;
    v_result_sources jsonb;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    SELECT * INTO v_project FROM sceneturn.projects WHERE id = p_project_id::text AND owner_id = p_actor_id::text;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'PROJECT_NOT_FOUND';
    END IF;
    SELECT * INTO v_provider FROM sceneturn.text_provider_settings WHERE id = 'platform' AND provider = 'deepseek'
        AND model = 'deepseek-flash' AND api_key_encrypted <> '';
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT503' USING MESSAGE = 'TEXT_NOT_CONFIGURED';
    END IF;
    IF v_provider.admin_owner_id <> p_actor_id::text THEN
        RAISE SQLSTATE 'PT402' USING MESSAGE = 'TEXT_BILLING_NOT_CONFIGURED';
    END IF;
    SELECT data_json::jsonb INTO v_canvas FROM sceneturn.canvases WHERE owner_id = p_actor_id::text
        AND project_id = p_project_id::text AND scene_id = p_scene_id;
    IF NOT FOUND OR NOT EXISTS (
        SELECT 1 FROM json_array_elements(v_project.data_json #> '{graphData,nodes}') AS node(value)
        WHERE node.value ->> 'id' = p_scene_id AND node.value ->> 'type' = 'scene'
    ) THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'CANVAS_NOT_FOUND';
    END IF;
    WITH latest AS (
        SELECT DISTINCT ON (coalesce(task.input_json ->> 'outputNodeId', task.source_node_id))
            task.id, task.asset_id, task.source_node_id, task.status, coalesce(task.input_json ->> 'outputNodeId', task.source_node_id) AS node_id
        FROM sceneturn.generations AS task WHERE task.owner_id = p_actor_id::text
            AND task.project_id = p_project_id::text AND task.scene_id = p_scene_id
        ORDER BY coalesce(task.input_json ->> 'outputNodeId', task.source_node_id), task.created_at DESC, task.id DESC
    )
    SELECT coalesce(jsonb_object_agg(task.node_id, jsonb_build_object(
        'assetId', asset.id, 'generationId', task.id, 'sourceNodeId', task.source_node_id
    )), '{}'::jsonb) INTO v_video_resources
        FROM latest AS task JOIN sceneturn.assets AS asset ON asset.id = task.asset_id AND asset.generation_id = task.id
        WHERE task.status = 'ready' AND asset.owner_id = p_actor_id::text AND asset.project_id = p_project_id::text
            AND starts_with(asset.mime_type, 'video/');
    WITH node_metadata AS (
        SELECT node.value -> 'metadata' AS metadata FROM jsonb_array_elements(v_canvas -> 'nodes') AS node(value)
    ), source_links AS (
        SELECT job.output_node_id AS output_id, job.source_node_id AS source_id FROM sceneturn.text_generations AS job
        WHERE job.id IN (SELECT metadata ->> 'textGenerationId' FROM node_metadata)
            AND job.owner_id = p_actor_id::text AND job.project_id = p_project_id::text AND job.scene_id = p_scene_id AND job.status = 'ready'
        UNION
        SELECT job.request_json #>> '{canvas,outputNodeId}', job.node_id FROM sceneturn.image_jobs AS job
        WHERE job.id IN (SELECT metadata ->> 'imageGenerationId' FROM node_metadata)
            AND job.owner_id = p_actor_id::text AND job.project_id = p_project_id::text AND job.scene_id = p_scene_id
            AND job.run_id IS NULL AND job.status = 'ready'
    )
    SELECT coalesce(jsonb_object_agg(output_id, source_id), '{}'::jsonb) INTO v_result_sources FROM source_links WHERE output_id IS NOT NULL;
    RETURN jsonb_build_object('revision', v_project.revision, 'providerRevision', v_provider.updated_at, 'canvas', v_canvas,
        'modelDefault', v_project.data_json #>> '{metadata,modelDefaults,text}',
        'videoResources', v_video_resources, 'historicalSources', v_result_sources);
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_text_replay(
    p_owner_id uuid, p_project_id uuid, p_operation_id text, p_payload_hash text
) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_operation sceneturn.operations%ROWTYPE;
    v_job jsonb;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    PERFORM 1 FROM sceneturn.projects WHERE id = p_project_id::text AND owner_id = p_owner_id::text;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'PROJECT_NOT_FOUND';
    END IF;
    SELECT * INTO v_operation FROM sceneturn.operations WHERE owner_id = p_owner_id::text AND project_id = p_project_id::text
        AND operation_id = p_operation_id;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    IF (v_operation.kind, v_operation.payload_hash) IS DISTINCT FROM ('text.generate', p_payload_hash) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'OPERATION_CONFLICT';
    END IF;
    SELECT to_jsonb(job) INTO v_job FROM sceneturn.text_generations AS job
        WHERE job.id = v_operation.response_json ->> 'textGenerationId' AND job.owner_id = p_owner_id::text;
    IF v_job IS NULL THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'TEXT_GENERATION_NOT_FOUND';
    END IF;
    RETURN v_job;
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_text_accept(
    p_actor_id uuid, p_project_id uuid, p_job_id uuid, p_scene_id text, p_source_id text, p_output_id text,
    p_operation_id text, p_task text, p_expected_revision integer, p_payload_hash text, p_source_digest text,
    p_provider_revision double precision, p_frozen_input jsonb, p_budget_limit_cents integer, p_request_limit_cents integer,
    p_dispatch_revision uuid, p_due_ms bigint
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_project sceneturn.projects%ROWTYPE;
    v_job sceneturn.text_generations%ROWTYPE;
    v_operation sceneturn.operations%ROWTYPE;
    v_provider sceneturn.text_provider_settings%ROWTYPE;
    v_canvas jsonb;
    v_input_upper integer;
    v_max_tokens integer;
    v_reserved integer;
    v_now double precision;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    IF p_task IS NULL OR p_task NOT IN ('text','translate','summary','character','prop','scene','plot','shot')
        OR p_job_id IS NULL OR p_operation_id IS NULL OR length(p_operation_id) NOT BETWEEN 1 AND 100
        OR coalesce(p_payload_hash, '') !~ '^[0-9a-f]{64}$' OR coalesce(p_source_digest, '') !~ '^[0-9a-f]{64}$'
        OR p_expected_revision IS NULL OR p_expected_revision < 1 OR p_source_id IS NOT DISTINCT FROM p_output_id
        OR p_budget_limit_cents IS NULL OR p_budget_limit_cents NOT BETWEEN 1 AND 30000
        OR p_request_limit_cents IS NULL OR p_request_limit_cents NOT BETWEEN 1 AND 1000
        OR jsonb_typeof(p_frozen_input) IS DISTINCT FROM 'object'
        OR jsonb_typeof(p_frozen_input -> 'messages') IS DISTINCT FROM 'array'
        OR coalesce(p_frozen_input ->> 'maxTokens', '') !~ '^[0-9]{1,5}$'
        OR (p_frozen_input - ARRAY['inputCompositionVersion','sourcePrompt','prompt','inputs','outputNodeId',
            'request','messages','maxTokens','maxAttempts','promptVersion','inputTokenUpperBound','providerRevision','price']) <> '{}'::jsonb THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'TEXT_REQUEST_INVALID';
    END IF;
    IF jsonb_array_length(p_frozen_input -> 'messages') <> 2 OR
        (p_frozen_input #>> '{messages,0,role}') IS DISTINCT FROM 'system' OR
        (p_frozen_input #>> '{messages,1,role}') IS DISTINCT FROM 'user' OR
        jsonb_typeof(p_frozen_input #> '{messages,0,content}') IS DISTINCT FROM 'string' OR
        jsonb_typeof(p_frozen_input #> '{messages,1,content}') IS DISTINCT FROM 'string' THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'TEXT_REQUEST_INVALID';
    END IF;
    v_max_tokens := (p_frozen_input ->> 'maxTokens')::integer;
    v_input_upper := octet_length(p_frozen_input #>> '{messages,0,content}') + octet_length(p_frozen_input #>> '{messages,1,content}') + 1024;
    IF v_max_tokens NOT BETWEEN 64 AND 32768 OR v_input_upper > 250000 THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'TEXT_OUTPUT_LIMIT';
    END IF;
    v_reserved := 2 * v_input_upper + 8 * v_max_tokens;
    PERFORM pg_advisory_xact_lock(8314610783521762916);
    v_now := extract(epoch FROM clock_timestamp());
    SELECT * INTO v_project FROM sceneturn.projects WHERE id = p_project_id::text AND owner_id = p_actor_id::text FOR UPDATE;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'PROJECT_NOT_FOUND';
    END IF;
    SELECT * INTO v_operation FROM sceneturn.operations
        WHERE project_id = p_project_id::text AND owner_id = p_actor_id::text AND operation_id = p_operation_id;
    IF FOUND THEN
        IF (v_operation.kind, v_operation.payload_hash) IS DISTINCT FROM ('text.generate', p_payload_hash) THEN
            RAISE SQLSTATE 'PT409' USING MESSAGE = 'OPERATION_CONFLICT';
        END IF;
        SELECT * INTO v_job FROM sceneturn.text_generations
            WHERE id = v_operation.response_json ->> 'textGenerationId' AND owner_id = p_actor_id::text;
        RETURN jsonb_build_object('job', to_jsonb(v_job), 'replayed', true);
    END IF;
    IF v_project.revision <> p_expected_revision THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'PROJECT_REVISION_CONFLICT';
    END IF;
    SELECT * INTO v_provider FROM sceneturn.text_provider_settings WHERE id = 'platform'
        AND provider = 'deepseek' AND model = 'deepseek-flash' AND api_key_encrypted <> '';
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT503' USING MESSAGE = 'TEXT_NOT_CONFIGURED';
    END IF;
    IF v_provider.admin_owner_id <> p_actor_id::text THEN
        RAISE SQLSTATE 'PT402' USING MESSAGE = 'TEXT_BILLING_NOT_CONFIGURED';
    END IF;
    IF v_provider.updated_at IS DISTINCT FROM p_provider_revision THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'TEXT_INPUT_CHANGED';
    END IF;
    SELECT data_json::jsonb INTO v_canvas FROM sceneturn.canvases WHERE owner_id = p_actor_id::text
        AND project_id = p_project_id::text AND scene_id = p_scene_id;
    IF NOT FOUND OR NOT EXISTS (
        SELECT 1 FROM jsonb_array_elements(v_canvas -> 'nodes') AS node(value) WHERE node.value ->> 'id' = p_source_id
    ) OR NOT EXISTS (
        SELECT 1 FROM jsonb_array_elements(v_canvas -> 'nodes') AS node(value)
        WHERE node.value ->> 'id' = p_output_id AND node.value ->> 'type' = 'text'
    ) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'TEXT_INPUT_CHANGED';
    END IF;
    IF p_dispatch_revision IS NULL OR p_due_ms IS NULL OR p_due_ms <= v_now * 1000 OR p_due_ms > (v_now + 120) * 1000 THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'TEXT_DISPATCH_REQUIRED';
    END IF;
    IF v_reserved > p_request_limit_cents * 10000 THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'TEXT_COST_LIMIT';
    END IF;
    INSERT INTO sceneturn.budgets(id, limit_cents, spent_cents, reserved_cents, text_spent_micros, text_reserved_micros)
        VALUES ('global', p_budget_limit_cents, 0, 0, 0, 0) ON CONFLICT (id) DO NOTHING;
    UPDATE sceneturn.budgets SET text_reserved_micros = text_reserved_micros + v_reserved
        WHERE id = 'global' AND ((spent_cents::bigint + reserved_cents) * 10000 + text_spent_micros + text_reserved_micros + v_reserved)
            <= least(limit_cents, p_budget_limit_cents) * 10000;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT402' USING MESSAGE = 'BUDGET_EXCEEDED';
    END IF;
    INSERT INTO sceneturn.text_generations (
        id, owner_id, project_id, scene_id, source_node_id, output_node_id, operation_id, task, project_revision, source_digest,
        input_json, partial_text, status, provider, model, input_tokens, output_tokens, total_tokens, reserved_cost_micros, cost_micros,
        usage_json, events_json, stop_requested, created_at, updated_at
    ) VALUES (
        p_job_id::text, p_actor_id::text, p_project_id::text, p_scene_id, p_source_id, p_output_id, p_operation_id, p_task,
        p_expected_revision, p_source_digest, (p_frozen_input || jsonb_build_object('providerRevision', v_provider.updated_at,
            'price', sceneturn.text_price('peak'), 'maxAttempts', 1, 'inputTokenUpperBound', v_input_upper))::json,
        '', 'queued', 'deepseek', 'deepseek-flash', 0, 0, 0, v_reserved, 0, '{}', '[]', 0, v_now, v_now
    ) RETURNING * INTO v_job;
    INSERT INTO sceneturn.operations(id, owner_id, project_id, operation_id, kind, payload_hash, response_json, created_at)
        VALUES (gen_random_uuid()::text, p_actor_id::text, p_project_id::text, p_operation_id, 'text.generate',
            p_payload_hash, json_build_object('textGenerationId', p_job_id::text), v_now);
    INSERT INTO sceneturn.deployment_dispatch(kind, job_id, revision, due_ms)
        VALUES ('text', p_job_id::text, p_dispatch_revision::text, p_due_ms);
    RETURN jsonb_build_object('job', to_jsonb(v_job), 'replayed', false);
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_text_read(p_owner_id uuid, p_job_id uuid) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_job jsonb;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    SELECT to_jsonb(job) INTO v_job FROM sceneturn.text_generations AS job
        JOIN sceneturn.projects AS project ON project.id = job.project_id AND project.owner_id = job.owner_id
        WHERE job.id = p_job_id::text AND job.owner_id = p_owner_id::text;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'TEXT_GENERATION_NOT_FOUND';
    END IF;
    RETURN v_job;
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_text_list(p_owner_id uuid, p_project_id uuid) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM sceneturn.require_gateway_access();
    IF NOT EXISTS (SELECT 1 FROM sceneturn.projects WHERE id = p_project_id::text AND owner_id = p_owner_id::text) THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'PROJECT_NOT_FOUND';
    END IF;
    RETURN coalesce((SELECT jsonb_agg(to_jsonb(job) ORDER BY job.created_at DESC, job.id)
        FROM sceneturn.text_generations AS job WHERE job.project_id = p_project_id::text AND job.owner_id = p_owner_id::text), '[]'::jsonb);
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_text_usage(p_owner_id uuid, p_project_id uuid) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM sceneturn.require_gateway_access();
    IF NOT EXISTS (SELECT 1 FROM sceneturn.projects WHERE id = p_project_id::text AND owner_id = p_owner_id::text) THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'PROJECT_NOT_FOUND';
    END IF;
    RETURN (SELECT jsonb_build_object('billingPolicyConfigured', false, 'currency', 'CNY', 'costUnit', 'microYuan',
        'inputTokens', coalesce(sum(input_tokens), 0), 'outputTokens', coalesce(sum(output_tokens), 0),
        'totalTokens', coalesce(sum(total_tokens), 0), 'costMicros', coalesce(sum(cost_micros), 0),
        'entries', coalesce(jsonb_agg(jsonb_build_object(
            'generationId', generation_id, 'remoteRequestId', remote_request_id, 'inputTokens', input_tokens,
            'outputTokens', output_tokens, 'totalTokens', total_tokens, 'cacheHitTokens', cache_hit_tokens,
            'cacheMissTokens', cache_miss_tokens, 'costMicros', cost_micros, 'price', price_json
        ) ORDER BY created_at, id), '[]'::jsonb)
    ) FROM sceneturn.text_usage_ledger WHERE project_id = p_project_id::text AND owner_id = p_owner_id::text);
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_text_claim(
    p_job_id uuid, p_worker_id uuid, p_marker_revision uuid, p_marker_due_ms bigint, p_immediate boolean,
    p_recovery_revision uuid, p_recovery_due_ms bigint, p_lease_seconds integer, p_price_band text, p_admin_email text
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_job sceneturn.text_generations%ROWTYPE;
    v_dispatch sceneturn.deployment_dispatch%ROWTYPE;
    v_provider sceneturn.text_provider_settings%ROWTYPE;
    v_now double precision;
    v_project_revision integer;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    PERFORM pg_advisory_xact_lock(8314610783521762916);
    v_now := extract(epoch FROM clock_timestamp());
    SELECT * INTO v_dispatch FROM sceneturn.deployment_dispatch WHERE kind = 'text' AND job_id = p_job_id::text;
    IF v_dispatch.due_ms IS NULL OR (v_dispatch.revision, v_dispatch.due_ms) IS DISTINCT FROM
        (p_marker_revision::text, p_marker_due_ms) THEN
        RETURN jsonb_build_object('state', 'stale');
    END IF;
    IF p_immediate IS NOT TRUE AND p_marker_due_ms > v_now * 1000 THEN
        RETURN jsonb_build_object('state', 'early');
    END IF;
    SELECT * INTO v_job FROM sceneturn.text_generations WHERE id = p_job_id::text FOR UPDATE;
    IF NOT FOUND OR v_job.status NOT IN ('queued','submitting','running') THEN
        UPDATE sceneturn.deployment_dispatch SET due_ms = NULL WHERE kind = 'text' AND job_id = p_job_id::text;
        RETURN jsonb_build_object('state', 'stale');
    END IF;
    IF v_job.status IN ('submitting','running') THEN
        IF v_job.lease_expires_at > v_now THEN
            RETURN jsonb_build_object('state', 'leased');
        END IF;
        UPDATE sceneturn.text_generations SET status = 'submission_unknown', lease_owner = NULL, lease_expires_at = NULL,
            error_code = 'TEXT_SUBMISSION_UNKNOWN', error_message = '原文本请求结果待核实，费用预留继续保留',
            finished_at = v_now, updated_at = v_now WHERE id = p_job_id::text;
        UPDATE sceneturn.deployment_dispatch SET due_ms = NULL WHERE kind = 'text' AND job_id = p_job_id::text;
        RETURN jsonb_build_object('state', 'submission_unknown');
    END IF;
    SELECT revision INTO v_project_revision FROM sceneturn.projects WHERE id = v_job.project_id AND owner_id = v_job.owner_id FOR UPDATE;
    SELECT * INTO v_provider FROM sceneturn.text_provider_settings WHERE id = 'platform'
        AND model = 'deepseek-flash' AND provider = 'deepseek' AND api_key_encrypted <> '';
    IF v_provider.admin_owner_id IS DISTINCT FROM v_job.owner_id OR v_job.project_revision IS DISTINCT FROM v_project_revision
        OR (to_jsonb(v_provider.updated_at)) IS DISTINCT FROM (v_job.input_json -> 'providerRevision')::jsonb
        OR NOT EXISTS (SELECT 1 FROM sceneturn.users WHERE id = v_job.owner_id AND lower(email) = lower(btrim(p_admin_email)))
        OR v_job.model <> 'deepseek-flash' OR v_job.provider <> 'deepseek' THEN
        PERFORM sceneturn.text_release_budget(v_job.reserved_cost_micros, 0);
        UPDATE sceneturn.text_generations SET status = 'interrupted', reserved_cost_micros = 0, usage_json = '{"settled":true}',
            error_code = 'TEXT_INPUT_CHANGED', error_message = '提交前项目或平台模型配置已变化，请重新提交',
            finished_at = v_now, updated_at = v_now WHERE id = p_job_id::text;
        UPDATE sceneturn.deployment_dispatch SET due_ms = NULL WHERE kind = 'text' AND job_id = p_job_id::text;
        RETURN jsonb_build_object('state', 'interrupted');
    END IF;
    IF EXISTS (SELECT 1 FROM sceneturn.text_generations WHERE status IN ('submitting','running') AND lease_expires_at > v_now) THEN
        RETURN jsonb_build_object('state', 'busy');
    END IF;
    IF p_worker_id IS NULL OR p_recovery_revision IS NULL OR p_recovery_revision = p_marker_revision
        OR p_price_band IS NULL OR p_price_band NOT IN ('peak','offPeak') OR p_lease_seconds IS NULL OR p_lease_seconds NOT BETWEEN 65 AND 660
        OR p_recovery_due_ms IS NULL OR p_recovery_due_ms < (v_now + p_lease_seconds) * 1000
        OR p_recovery_due_ms > (v_now + p_lease_seconds + 120) * 1000 THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'TEXT_DISPATCH_REQUIRED';
    END IF;
    UPDATE sceneturn.text_generations SET status = 'submitting', lease_owner = p_worker_id::text, lease_expires_at = v_now + p_lease_seconds,
        input_json = (input_json::jsonb || jsonb_build_object('price', sceneturn.text_price(p_price_band)))::json,
        started_at = v_now, updated_at = v_now WHERE id = p_job_id::text RETURNING * INTO v_job;
    UPDATE sceneturn.deployment_dispatch SET revision = p_recovery_revision::text, due_ms = p_recovery_due_ms
        WHERE kind = 'text' AND job_id = p_job_id::text;
    RETURN jsonb_build_object('state', 'claimed', 'job', to_jsonb(v_job), 'apiKeyEncrypted', v_provider.api_key_encrypted);
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_text_stop(p_owner_id uuid, p_job_id uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_job sceneturn.text_generations%ROWTYPE;
    v_previous jsonb;
    v_now double precision;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    PERFORM pg_advisory_xact_lock(8314610783521762916);
    v_now := extract(epoch FROM clock_timestamp());
    SELECT * INTO v_job FROM sceneturn.text_generations WHERE id = p_job_id::text AND owner_id = p_owner_id::text FOR UPDATE;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'TEXT_GENERATION_NOT_FOUND';
    END IF;
    IF v_job.status = 'queued' THEN
        SELECT jsonb_build_object('jobId', job_id, 'revision', revision, 'dueMs', due_ms)
            INTO v_previous FROM sceneturn.deployment_dispatch WHERE kind = 'text' AND job_id = p_job_id::text AND due_ms IS NOT NULL;
        PERFORM sceneturn.text_release_budget(v_job.reserved_cost_micros, 0);
        UPDATE sceneturn.deployment_dispatch SET due_ms = NULL WHERE kind = 'text' AND job_id = p_job_id::text;
        UPDATE sceneturn.text_generations SET reserved_cost_micros = 0, status = 'interrupted', usage_json = '{"settled":true}',
            stop_requested = 1, finished_at = v_now, updated_at = v_now WHERE id = p_job_id::text RETURNING * INTO v_job;
    ELSIF v_job.status IN ('submitting','running','interrupted') THEN
        UPDATE sceneturn.text_generations SET stop_requested = 1, updated_at = v_now
            WHERE id = p_job_id::text RETURNING * INTO v_job;
    END IF;
    RETURN jsonb_build_object('job', to_jsonb(v_job), 'obsolete', v_previous);
END;
$$;

CREATE OR REPLACE FUNCTION sceneturn.text_apply_result(
    p_job sceneturn.text_generations, p_project sceneturn.projects, p_result jsonb, p_output text, p_now double precision
) RETURNS integer
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_canvas sceneturn.canvases%ROWTYPE;
    v_nodes jsonb;
    v_target integer;
    v_source integer;
    v_metadata jsonb;
    v_revision integer := p_project.revision + 1;
BEGIN
    SELECT * INTO v_canvas FROM sceneturn.canvases WHERE project_id = p_job.project_id
        AND owner_id = p_job.owner_id AND scene_id = p_job.scene_id FOR UPDATE;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    v_nodes := v_canvas.data_json -> 'nodes';
    SELECT (position - 1)::integer INTO v_target FROM jsonb_array_elements(v_nodes) WITH ORDINALITY AS node(value, position)
        WHERE node.value ->> 'id' = p_job.output_node_id AND node.value ->> 'type' = 'text';
    IF NOT FOUND OR p_job.output_node_id = p_job.source_node_id THEN
        RETURN NULL;
    END IF;
    v_metadata := coalesce(v_nodes -> v_target -> 'metadata', '{}'::jsonb) || jsonb_build_object(
        'content', p_output, 'status', 'success', 'pendingResult', false, 'textGenerationId', p_job.id, 'result', p_result
    );
    IF p_job.task = 'plot' THEN
        v_metadata := v_metadata || jsonb_build_object('scriptVersion', 'current', 'scriptRows', (
            SELECT jsonb_agg(jsonb_build_object('id', gen_random_uuid()::text, 'duration', NULL, 'title', '',
                'visual', shot.value ->> 'text', 'action', '', 'dialogue', '') ORDER BY shot.sequence)
            FROM jsonb_array_elements(p_result -> 'shots') WITH ORDINALITY AS shot(value, sequence)
        ));
    END IF;
    v_nodes := jsonb_set(v_nodes, ARRAY[v_target::text, 'metadata'], v_metadata);
    IF p_job.task = 'translate' THEN
        SELECT (position - 1)::integer INTO v_source FROM jsonb_array_elements(v_nodes) WITH ORDINALITY AS node(value, position)
            WHERE node.value ->> 'id' = p_job.source_node_id
            AND node.value #>> '{metadata,composerContent}' = p_job.input_json ->> 'sourcePrompt';
        IF FOUND THEN
            v_nodes := jsonb_set(v_nodes, ARRAY[v_source::text, 'metadata', 'composerContent'], to_jsonb(p_output));
        END IF;
    END IF;
    UPDATE sceneturn.canvases SET data_json = jsonb_set(data_json::jsonb, '{nodes}', v_nodes)::json, updated_at = p_now
        WHERE id = v_canvas.id;
    UPDATE sceneturn.projects SET revision = v_revision, updated_at = p_now WHERE id = p_job.project_id;
    PERFORM sceneturn.record_project_envelope(p_job.owner_id, p_job.project_id,
        sceneturn.project_snapshot_envelope(p_job.owner_id, p_job.project_id, p_project.data_json::jsonb, v_revision), p_now);
    RETURN v_revision;
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_text_finish(
    p_job_id uuid, p_worker_id uuid, p_outcome text, p_remote_request_id text, p_remote_model text, p_usage jsonb,
    p_partial_text text, p_result jsonb, p_output_text text, p_error_code text, p_error_message text
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_job sceneturn.text_generations%ROWTYPE;
    v_project sceneturn.projects%ROWTYPE;
    v_previous jsonb;
    v_now double precision;
    v_hit integer;
    v_miss integer;
    v_input integer;
    v_output integer;
    v_actual bigint;
    v_price jsonb;
    v_status text;
    v_error text := p_error_code;
    v_message text := p_error_message;
    v_usage jsonb := '{}'::jsonb;
    v_keep_reservation boolean := false;
    v_applied_revision integer;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    PERFORM pg_advisory_xact_lock(8314610783521762916);
    v_now := extract(epoch FROM clock_timestamp());
    SELECT * INTO v_job FROM sceneturn.text_generations
        WHERE id = p_job_id::text AND lease_owner = p_worker_id::text AND status = 'submitting'
        AND lease_expires_at > v_now FOR UPDATE;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('state', 'lease_lost');
    END IF;
    IF p_outcome IS NULL OR p_outcome NOT IN ('received','unknown','failed')
        OR (p_error_code IS NOT NULL AND (p_error_code !~ '^[A-Z][A-Z0-9_]{0,99}$' OR length(coalesce(p_error_message, '')) NOT BETWEEN 1 AND 800))
        OR length(coalesce(p_partial_text, '')) > 200000 THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'TEXT_RESULT_INVALID';
    END IF;
    v_status := CASE WHEN p_outcome = 'unknown' THEN 'submission_unknown' ELSE 'failed' END;
    v_actual := 0;
    IF p_outcome = 'received' THEN
        IF length(coalesce(p_remote_request_id, '')) NOT BETWEEN 1 AND 200 OR p_remote_model IS NULL
            OR p_remote_model NOT IN ('deepseek-flash','DeepSeek-V4.1-Flash-1017') OR jsonb_typeof(p_usage) IS DISTINCT FROM 'object'
            OR EXISTS (SELECT 1 FROM unnest(ARRAY['prompt_tokens','completion_tokens','total_tokens',
                'prompt_cache_hit_tokens','prompt_cache_miss_tokens']) AS item(key)
                WHERE coalesce(p_usage ->> item.key, '') !~ '^[0-9]{1,8}$') OR
            (p_result IS NULL) <> (p_output_text IS NULL) OR
            (p_result IS NULL AND p_error_code IS NULL) OR (p_result IS NOT NULL AND (p_error_code IS NOT NULL
                OR jsonb_typeof(p_result) IS DISTINCT FROM 'object' OR length(p_output_text) NOT BETWEEN 1 AND 200000
                OR (v_job.task = 'plot' AND jsonb_typeof(p_result -> 'shots') IS DISTINCT FROM 'array'))) THEN
            RAISE SQLSTATE 'PT422' USING MESSAGE = 'TEXT_RESULT_INVALID';
        END IF;
        v_hit := (p_usage ->> 'prompt_cache_hit_tokens')::integer;
        v_miss := (p_usage ->> 'prompt_cache_miss_tokens')::integer;
        v_input := (p_usage ->> 'prompt_tokens')::integer;
        v_output := (p_usage ->> 'completion_tokens')::integer;
        IF v_input < 1 OR v_input <> v_hit + v_miss OR (p_usage ->> 'total_tokens')::integer <> v_input + v_output THEN
            RAISE SQLSTATE 'PT422' USING MESSAGE = 'TEXT_USAGE_INVALID';
        END IF;
        v_price := v_job.input_json -> 'price';
        v_actual := (
            v_hit::bigint * (v_price ->> 'cacheHitMicrosPerMillion')::integer +
            v_miss::bigint * (v_price ->> 'cacheMissMicrosPerMillion')::integer +
            v_output::bigint * (v_price ->> 'outputMicrosPerMillion')::integer + 999999
        ) / 1000000;
        v_keep_reservation := v_input > (v_job.input_json ->> 'inputTokenUpperBound')::integer
            OR v_output > (v_job.input_json ->> 'maxTokens')::integer OR v_actual > v_job.reserved_cost_micros;
        v_usage := p_usage || jsonb_build_object('price', v_price, 'settled', NOT v_keep_reservation, 'costEstimate', true);
        IF v_keep_reservation THEN
            v_status := 'submission_unknown';
            v_error := 'TEXT_USAGE_OUT_OF_BOUNDS';
            v_message := '供应商上报用量超出预留上界，真实回执已保存';
            v_usage := v_usage || jsonb_build_object('reportedCostMicros', v_actual);
        ELSE
            INSERT INTO sceneturn.text_usage_ledger(id, generation_id, owner_id, project_id, remote_request_id, provider, model,
                input_tokens, output_tokens, total_tokens, cache_hit_tokens, cache_miss_tokens, cost_micros, price_json, created_at)
            VALUES (gen_random_uuid()::text, v_job.id, v_job.owner_id, v_job.project_id, p_remote_request_id, 'deepseek', p_remote_model,
                v_input, v_output, v_input + v_output, v_hit, v_miss, v_actual, v_price::json, v_now);
            IF p_result IS NOT NULL THEN
                SELECT * INTO v_project FROM sceneturn.projects WHERE id = v_job.project_id AND owner_id = v_job.owner_id FOR UPDATE;
                IF v_job.stop_requested <> 0 THEN
                    v_status := 'interrupted';
                    v_error := 'TEXT_STOPPED';
                ELSIF v_project.revision IS DISTINCT FROM v_job.project_revision THEN
                    v_status := 'conflict';
                    v_error := 'PROJECT_REVISION_CONFLICT';
                ELSE
                    v_applied_revision := sceneturn.text_apply_result(v_job, v_project, p_result, p_output_text, v_now);
                    v_status := CASE WHEN v_applied_revision IS NULL THEN 'conflict' ELSE 'ready' END;
                    v_error := CASE WHEN v_applied_revision IS NULL THEN 'TEXT_OUTPUT_MISSING' ELSE NULL END;
                END IF;
                v_message := CASE WHEN v_error IS NULL THEN NULL ELSE '文本结果已保存，等待采用' END;
            END IF;
        END IF;
    ELSE
        IF p_error_code IS NULL OR p_result IS NOT NULL OR p_output_text IS NOT NULL OR p_usage IS NOT NULL THEN
            RAISE SQLSTATE 'PT422' USING MESSAGE = 'TEXT_RESULT_INVALID';
        END IF;
        v_keep_reservation := p_outcome = 'unknown';
        v_usage := jsonb_build_object('settled', NOT v_keep_reservation);
    END IF;
    IF NOT v_keep_reservation THEN
        PERFORM sceneturn.text_release_budget(v_job.reserved_cost_micros, v_actual::integer);
    END IF;
    SELECT jsonb_build_object('jobId', job_id, 'revision', revision, 'dueMs', due_ms) INTO v_previous
        FROM sceneturn.deployment_dispatch WHERE kind = 'text' AND job_id = p_job_id::text AND due_ms IS NOT NULL;
    UPDATE sceneturn.deployment_dispatch SET due_ms = NULL WHERE kind = 'text' AND job_id = p_job_id::text;
    UPDATE sceneturn.text_generations SET status = v_status, error_code = v_error, error_message = v_message,
        remote_request_id = p_remote_request_id, input_tokens = coalesce(v_input, 0), output_tokens = coalesce(v_output, 0),
        total_tokens = coalesce(v_input + v_output, 0), partial_text = coalesce(p_partial_text, ''),
        result_json = CASE WHEN v_keep_reservation THEN NULL ELSE p_result::json END,
        output_text = CASE WHEN v_keep_reservation THEN NULL ELSE p_output_text END,
        applied_revision = v_applied_revision,
        reserved_cost_micros = CASE WHEN v_keep_reservation THEN reserved_cost_micros ELSE 0 END,
        cost_micros = CASE WHEN v_keep_reservation THEN cost_micros ELSE v_actual END,
        usage_json = v_usage::json, lease_owner = NULL, lease_expires_at = NULL, finished_at = v_now, updated_at = v_now
        WHERE id = p_job_id::text RETURNING * INTO v_job;
    RETURN jsonb_build_object('state', 'settled', 'job', to_jsonb(v_job), 'obsolete', v_previous);
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_text_apply(
    p_owner_id uuid, p_job_id uuid, p_expected_revision integer, p_operation_id text, p_payload_hash text
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_job sceneturn.text_generations%ROWTYPE;
    v_project sceneturn.projects%ROWTYPE;
    v_operation sceneturn.operations%ROWTYPE;
    v_revision integer;
    v_now double precision;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    PERFORM pg_advisory_xact_lock(8314610783521762916);
    v_now := extract(epoch FROM clock_timestamp());
    SELECT * INTO v_job FROM sceneturn.text_generations WHERE id = p_job_id::text AND owner_id = p_owner_id::text FOR UPDATE;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'TEXT_GENERATION_NOT_FOUND';
    END IF;
    SELECT * INTO v_operation FROM sceneturn.operations
        WHERE project_id = v_job.project_id AND owner_id = v_job.owner_id AND operation_id = p_operation_id;
    IF FOUND THEN
        IF (v_operation.kind, v_operation.payload_hash) IS DISTINCT FROM ('text.apply', p_payload_hash) THEN
            RAISE SQLSTATE 'PT409' USING MESSAGE = 'OPERATION_CONFLICT';
        END IF;
        RETURN to_jsonb(v_job);
    END IF;
    IF v_job.result_json IS NULL OR v_job.output_text IS NULL THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'TEXT_NOT_READY';
    END IF;
    IF v_job.applied_revision IS NULL THEN
        SELECT * INTO v_project FROM sceneturn.projects WHERE id = v_job.project_id AND owner_id = v_job.owner_id FOR UPDATE;
        IF v_project.revision IS DISTINCT FROM p_expected_revision THEN
            RAISE SQLSTATE 'PT409' USING MESSAGE = 'PROJECT_REVISION_CONFLICT';
        END IF;
        v_revision := sceneturn.text_apply_result(v_job, v_project, v_job.result_json::jsonb, v_job.output_text, v_now);
        IF v_revision IS NULL THEN
            RAISE SQLSTATE 'PT409' USING MESSAGE = 'TEXT_OUTPUT_MISSING';
        END IF;
        UPDATE sceneturn.text_generations SET status = 'ready', applied_revision = v_revision, error_code = NULL, error_message = NULL
            WHERE id = p_job_id::text RETURNING * INTO v_job;
    END IF;
    INSERT INTO sceneturn.operations(id, owner_id, project_id, operation_id, kind, payload_hash, response_json, created_at)
        VALUES (gen_random_uuid()::text, v_job.owner_id, v_job.project_id, p_operation_id, 'text.apply',
            p_payload_hash, json_build_object('textGenerationId', p_job_id::text), v_now);
    RETURN to_jsonb(v_job);
END;
$$;

REVOKE EXECUTE ON FUNCTION sceneturn.text_price(text), sceneturn.text_release_budget(integer, integer)
FROM PUBLIC, anon, authenticated, service_role;
REVOKE EXECUTE ON FUNCTION sceneturn.text_apply_result(sceneturn.text_generations, sceneturn.projects, jsonb, text, double precision)
FROM PUBLIC, anon, authenticated, service_role;
REVOKE EXECUTE ON FUNCTION
    public.sceneturn_text_settings(uuid, text, text), public.sceneturn_text_prepare(uuid, uuid, text), public.sceneturn_text_replay(uuid, uuid, text, text),
    public.sceneturn_text_accept(uuid, uuid, uuid, text, text, text, text, text, integer, text, text, double precision, jsonb, integer, integer, uuid, bigint),
    public.sceneturn_text_read(uuid, uuid), public.sceneturn_text_list(uuid, uuid), public.sceneturn_text_usage(uuid, uuid),
    public.sceneturn_text_claim(uuid, uuid, uuid, bigint, boolean, uuid, bigint, integer, text, text),
    public.sceneturn_text_stop(uuid, uuid), public.sceneturn_text_finish(uuid, uuid, text, text, text, jsonb, text, jsonb, text, text, text),
    public.sceneturn_text_apply(uuid, uuid, integer, text, text)
FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION
    public.sceneturn_text_settings(uuid, text, text), public.sceneturn_text_prepare(uuid, uuid, text), public.sceneturn_text_replay(uuid, uuid, text, text),
    public.sceneturn_text_accept(uuid, uuid, uuid, text, text, text, text, text, integer, text, text, double precision, jsonb, integer, integer, uuid, bigint),
    public.sceneturn_text_read(uuid, uuid), public.sceneturn_text_list(uuid, uuid), public.sceneturn_text_usage(uuid, uuid),
    public.sceneturn_text_claim(uuid, uuid, uuid, bigint, boolean, uuid, bigint, integer, text, text),
    public.sceneturn_text_stop(uuid, uuid), public.sceneturn_text_finish(uuid, uuid, text, text, text, jsonb, text, jsonb, text, text, text),
    public.sceneturn_text_apply(uuid, uuid, integer, text, text)
TO service_role;
