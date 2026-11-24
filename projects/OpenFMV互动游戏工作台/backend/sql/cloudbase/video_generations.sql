CREATE OR REPLACE FUNCTION sceneturn.video_delivery(p_job sceneturn.generations) RETURNS jsonb
LANGUAGE plpgsql STABLE SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_artifact jsonb;
    v_node text := p_job.input_json #>> '{request,videoArtifactNodeId}';
    v_material boolean;
BEGIN
    v_material := coalesce(v_node <> '', false) OR coalesce(p_job.input_json #>> '{boundaryFrames,runId}' <> '', false) OR EXISTS (
        SELECT 1 FROM sceneturn.agent_runs AS run JOIN sceneturn.agent_steps AS step ON step.run_id = run.id
            CROSS JOIN LATERAL json_array_elements(step.tool_results_json) AS receipt(value)
        WHERE run.owner_id = p_job.owner_id AND run.project_id = p_job.project_id AND run.limits_json ->> 'workspace' = 'materials'
            AND receipt.value ->> 'tool' = 'submit_generation' AND receipt.value #>> '{result,generation,id}' = p_job.id
    );
    IF v_node IS NOT NULL AND p_job.input_json #> '{request,shotSummary}' IS NULL THEN
        SELECT node.value #> '{metadata,creativeArtifact}' INTO v_artifact FROM sceneturn.canvases AS canvas
            CROSS JOIN LATERAL jsonb_array_elements(canvas.data_json::jsonb -> 'nodes') AS node(value)
        WHERE canvas.owner_id = p_job.owner_id AND canvas.project_id = p_job.project_id AND canvas.scene_id = p_job.scene_id
            AND node.value ->> 'id' = v_node AND node.value #>> '{metadata,creativeArtifact,role}' = 'media_producer';
    END IF;
    RETURN jsonb_build_object('job', to_jsonb(p_job), 'material', v_material, 'artifact', v_artifact);
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_video_read(p_owner_id uuid, p_job_id uuid) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_job sceneturn.generations%ROWTYPE;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    SELECT job.* INTO v_job FROM sceneturn.generations AS job
        JOIN sceneturn.projects AS project ON project.id = job.project_id AND project.owner_id = job.owner_id
    WHERE job.id = p_job_id::text AND job.owner_id = p_owner_id::text;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'GENERATION_NOT_FOUND';
    END IF;
    RETURN sceneturn.video_delivery(v_job);
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_video_list(p_owner_id uuid, p_project_id uuid) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM sceneturn.require_gateway_access();
    IF NOT EXISTS (SELECT 1 FROM sceneturn.projects WHERE id = p_project_id::text AND owner_id = p_owner_id::text) THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'PROJECT_NOT_FOUND';
    END IF;
    RETURN coalesce((SELECT jsonb_agg(sceneturn.video_delivery(job) ORDER BY job.created_at DESC, job.id DESC)
        FROM sceneturn.generations AS job WHERE job.project_id = p_project_id::text AND job.owner_id = p_owner_id::text), '[]'::jsonb);
END;
$$;

CREATE OR REPLACE FUNCTION sceneturn.video_quote_digest(p_input jsonb, p_project text, p_scene text, p_source text,
    p_clip text, p_revision integer, p_source_digest text, p_cost integer) RETURNS text
LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $$
    SELECT encode(sha256(convert_to(jsonb_build_object('input', p_input, 'projectId', p_project, 'sceneId', p_scene,
        'sourceNodeId', p_source, 'clipId', p_clip, 'projectRevision', p_revision, 'sourceDigest', p_source_digest,
        'quotedCents', p_cost)::text, 'UTF8')), 'hex');
$$;

CREATE OR REPLACE FUNCTION sceneturn.video_reference_seconds(p_owner text, p_project text, p_input jsonb) RETURNS integer
LANGUAGE plpgsql STABLE SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_item jsonb;
    v_asset sceneturn.assets%ROWTYPE;
    v_video_ms integer := 0;
BEGIN
    IF (p_input -> 'canvasMedia') IS NULL THEN
        RETURN 0;
    END IF;
    IF jsonb_typeof(p_input -> 'canvasMedia') IS DISTINCT FROM 'array' OR jsonb_array_length(p_input -> 'canvasMedia') NOT BETWEEN 1 AND 11 THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'VIDEO_MEDIA_INVALID';
    END IF;
    FOR v_item IN SELECT value FROM jsonb_array_elements(p_input -> 'canvasMedia') AS media(value) LOOP
        IF v_item ->> 'type' IS NULL OR v_item ->> 'type' NOT IN ('first_frame','last_frame','reference_video','reference_image')
            OR coalesce(v_item ->> 'sha256', '') !~ '^[0-9a-f]{64}$' OR length(coalesce(v_item ->> 'nodeId', '')) NOT BETWEEN 1 AND 100
            OR v_item ? 'receiptSha256' THEN
            RAISE SQLSTATE 'PT422' USING MESSAGE = 'VIDEO_MEDIA_INVALID';
        END IF;
        SELECT * INTO v_asset FROM sceneturn.assets WHERE id = v_item ->> 'assetId' AND owner_id = p_owner AND project_id = p_project
            AND sha256 = v_item ->> 'sha256';
        IF NOT FOUND THEN
            RAISE SQLSTATE 'PT409' USING MESSAGE = 'VIDEO_MEDIA_INPUT_CHANGED';
        END IF;
        IF v_item ->> 'type' = 'reference_video' THEN
            IF v_video_ms <> 0 OR v_asset.mime_type <> 'video/mp4' OR v_asset.bytes NOT BETWEEN 1 AND 104857600
                OR coalesce(v_item ->> 'durationMs', '') !~ '^[0-9]{1,5}$' THEN
                RAISE SQLSTATE 'PT422' USING MESSAGE = 'VIDEO_MEDIA_INVALID';
            END IF;
            v_video_ms := (v_item ->> 'durationMs')::integer;
            IF v_video_ms NOT BETWEEN 1000 AND 15000 THEN
                RAISE SQLSTATE 'PT422' USING MESSAGE = 'VIDEO_MEDIA_INVALID';
            END IF;
        ELSIF NOT starts_with(v_asset.mime_type, 'image/') OR v_asset.bytes NOT BETWEEN 1 AND 20971520 THEN
            RAISE SQLSTATE 'PT422' USING MESSAGE = 'VIDEO_MEDIA_INVALID';
        END IF;
    END LOOP;
    RETURN (v_video_ms + 999) / 1000;
END;
$$;

CREATE OR REPLACE FUNCTION sceneturn.video_pricing(p_owner text, p_input jsonb, p_require_key boolean) RETURNS jsonb
LANGUAGE plpgsql STABLE SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_wan sceneturn.provider_settings%ROWTYPE;
    v_ark sceneturn.model_provider_connections%ROWTYPE;
    v_model text := p_input #>> '{modelProfile,profileId}';
    v_price jsonb;
    v_resolution text := p_input ->> 'resolution';
    v_rate integer;
BEGIN
    IF p_input ->> 'region' IS DISTINCT FROM 'cn-beijing' OR v_resolution IS NULL OR v_resolution NOT IN ('480P','720P','1080P') THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'VIDEO_REQUEST_INVALID';
    END IF;
    IF p_input ->> 'provider' = 'aliyun-wan' AND p_input ->> 'model' = 'wan3.0-video' THEN
        SELECT * INTO v_wan FROM sceneturn.provider_settings WHERE owner_id = p_owner;
        IF p_require_key AND (NOT FOUND OR coalesce(v_wan.api_key_encrypted, '') = '') THEN
            RAISE SQLSTATE 'PT503' USING MESSAGE = 'WAN_NOT_CONFIGURED';
        END IF;
        IF FOUND AND (v_wan.workspace_id, v_wan.model, v_wan.region) IS DISTINCT FROM
            (p_input ->> 'workspaceId', p_input ->> 'model', p_input ->> 'region') THEN
            RAISE SQLSTATE 'PT409' USING MESSAGE = 'WAN_CONFIGURATION_CHANGED';
        END IF;
        v_rate := CASE v_resolution WHEN '480P' THEN 30 WHEN '720P' THEN 60 ELSE 120 END;
        v_price := jsonb_build_object('version', 'wan3.0-video-cn-beijing-2026-11-12-list', 'rate', v_rate, 'key', v_wan.api_key_encrypted);
    ELSIF p_input ->> 'provider' = 'volcengine-ark' AND v_model IN ('seedance-2-5','seedance-2-5-flash','seedance-2-0',
        'seedance-2-0-fast','seedance-2-0-mini','seedance-1-0-pro','seedance-1-0-pro-fast') THEN
        SELECT * INTO v_ark FROM sceneturn.model_provider_connections WHERE owner_id = p_owner AND scope_key = p_owner
            AND provider_id = 'volcengine-ark' AND id = p_input #>> '{modelProfile,connectionId}' AND api_key_encrypted <> '';
        IF NOT FOUND THEN
            RAISE SQLSTATE 'PT503' USING MESSAGE = 'MODEL_NOT_CONFIGURED';
        END IF;
        IF v_ark.revision::text IS DISTINCT FROM p_input #>> '{modelProfile,connectionRevision}'
            OR v_ark.config_json ->> 'region' IS DISTINCT FROM p_input ->> 'region'
            OR NOT coalesce((v_ark.config_json -> 'entitledModelIds')::jsonb ? v_model, false) THEN
            RAISE SQLSTATE 'PT409' USING MESSAGE = 'MODEL_CONFIGURATION_CHANGED';
        END IF;
        v_price := (v_ark.config_json -> 'prices' -> v_model)::jsonb;
        IF v_price IS DISTINCT FROM p_input #> '{modelProfile,price}' OR v_price ->> 'verified' IS DISTINCT FROM 'true'
            OR coalesce(v_price -> 'centsPerSecond' ->> v_resolution, '') !~ '^[0-9]{1,9}$'
            OR coalesce(v_price ->> 'outputMicrosPerMillion', '') !~ '^[0-9]{1,12}$' THEN
            RAISE SQLSTATE 'PT409' USING MESSAGE = 'MODEL_PRICE_NOT_CONFIGURED';
        END IF;
        v_rate := (v_price -> 'centsPerSecond' ->> v_resolution)::integer;
        v_price := v_price || jsonb_build_object('rate', v_rate, 'key', v_ark.api_key_encrypted);
    ELSE
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'VIDEO_REQUEST_INVALID';
    END IF;
    IF v_rate < 1 OR p_input ->> 'centsPerSecond' IS DISTINCT FROM v_rate::text OR
        p_input ->> 'priceVersion' IS DISTINCT FROM v_price ->> 'version' THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'MODEL_PRICE_NOT_CONFIGURED';
    END IF;
    RETURN v_price;
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_video_quote(
    p_owner_id uuid, p_project_id uuid, p_job_id uuid, p_scene_id text, p_source_id text, p_clip_id text,
    p_expected_revision integer, p_operation_id text, p_payload_hash text, p_source_digest text,
    p_frozen_input jsonb
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_project sceneturn.projects%ROWTYPE;
    v_operation sceneturn.operations%ROWTYPE;
    v_job sceneturn.generations%ROWTYPE;
    v_canvas jsonb;
    v_price jsonb;
    v_duration integer;
    v_input_seconds integer;
    v_cost bigint;
    v_now double precision;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    IF p_job_id IS NULL OR coalesce(p_payload_hash, '') !~ '^[0-9a-f]{64}$' OR coalesce(p_source_digest, '') !~ '^[0-9a-f]{64}$'
        OR length(coalesce(p_clip_id, '')) NOT BETWEEN 1 AND 100 OR length(coalesce(p_operation_id, '')) NOT BETWEEN 1 AND 100
        OR jsonb_typeof(p_frozen_input) IS DISTINCT FROM 'object' OR jsonb_typeof(p_frozen_input -> 'request') IS DISTINCT FROM 'object'
        OR coalesce(p_frozen_input ->> 'duration', '') !~ '^[0-9]{1,2}$' OR p_frozen_input ->> 'count' IS DISTINCT FROM '1'
        OR p_frozen_input ->> 'inputCompositionVersion' IS DISTINCT FROM '1'
        OR length(coalesce(p_frozen_input ->> 'prompt', '')) NOT BETWEEN 1 AND 20000
        OR p_frozen_input #>> '{request,sceneId}' IS DISTINCT FROM p_scene_id
        OR p_frozen_input #>> '{request,sourceNodeId}' IS DISTINCT FROM p_source_id
        OR p_frozen_input #>> '{request,operationId}' IS DISTINCT FROM p_operation_id
        OR p_frozen_input ?| ARRAY['boundaryFrames','localInputs','apiKey','apiKeyEncrypted','media']
        OR (p_frozen_input -> 'request') ?| ARRAY['retryOf','videoArtifactNodeId','reservedClipId'] THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'VIDEO_REQUEST_INVALID';
    END IF;
    PERFORM pg_advisory_xact_lock(8314610783521762916);
    SELECT * INTO v_project FROM sceneturn.projects WHERE id = p_project_id::text AND owner_id = p_owner_id::text FOR UPDATE;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'PROJECT_NOT_FOUND';
    END IF;
    SELECT * INTO v_operation FROM sceneturn.operations WHERE owner_id = p_owner_id::text AND project_id = p_project_id::text
        AND operation_id = p_operation_id;
    IF FOUND THEN
        IF (v_operation.kind, v_operation.payload_hash) IS DISTINCT FROM ('generation.quote', p_payload_hash) THEN
            RAISE SQLSTATE 'PT409' USING MESSAGE = 'OPERATION_CONFLICT';
        END IF;
        SELECT * INTO v_job FROM sceneturn.generations WHERE id = v_operation.response_json ->> 'generationId'
            AND owner_id = p_owner_id::text AND project_id = p_project_id::text;
        IF NOT FOUND THEN
            RAISE SQLSTATE 'PT404' USING MESSAGE = 'GENERATION_NOT_FOUND';
        END IF;
        RETURN jsonb_build_object('job', sceneturn.video_delivery(v_job), 'replayed', true);
    END IF;
    IF p_expected_revision IS DISTINCT FROM v_project.revision THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'PROJECT_REVISION_CONFLICT';
    END IF;
    SELECT data_json::jsonb INTO v_canvas FROM sceneturn.canvases WHERE owner_id = p_owner_id::text AND project_id = p_project_id::text
        AND scene_id = p_scene_id;
    IF NOT FOUND OR NOT EXISTS (SELECT 1 FROM json_array_elements(v_project.data_json #> '{graphData,nodes}') AS project_node(value)
        WHERE project_node.value ->> 'id' = p_scene_id AND project_node.value ->> 'type' = 'scene') THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'CANVAS_NOT_FOUND';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM jsonb_array_elements(v_canvas -> 'nodes') AS node(value) WHERE node.value ->> 'id' = p_source_id)
        OR (p_frozen_input ->> 'outputNodeId' IS NOT NULL AND (
            p_source_id = p_frozen_input ->> 'outputNodeId' OR NOT EXISTS (
                SELECT 1 FROM jsonb_array_elements(v_canvas -> 'nodes') AS node(value)
                WHERE node.value ->> 'id' = p_frozen_input ->> 'outputNodeId' AND node.value ->> 'type' = 'video')))
    THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'GENERATION_INPUT_CHANGED';
    END IF;
    v_duration := (p_frozen_input ->> 'duration')::integer;
    v_input_seconds := sceneturn.video_reference_seconds(p_owner_id::text, p_project_id::text, p_frozen_input);
    IF v_duration < 2 OR v_duration + v_input_seconds > 30 OR
        coalesce(p_frozen_input ->> 'billableInputSeconds', '0') IS DISTINCT FROM v_input_seconds::text THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'VIDEO_REQUEST_INVALID';
    END IF;
    v_price := sceneturn.video_pricing(p_owner_id::text, p_frozen_input, false);
    v_cost := (v_duration + v_input_seconds) * (v_price ->> 'rate')::bigint;
    IF v_cost NOT BETWEEN 1 AND 2147483647 THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'GENERATION_COST_LIMIT';
    END IF;
    v_now := extract(epoch FROM clock_timestamp());
    INSERT INTO sceneturn.generations(id, owner_id, project_id, scene_id, source_node_id, clip_id, operation_id,
        project_revision, input_json, input_hash, source_digest, quoted_cents, reserved_cents, actual_cents,
        status, usage_json, retry_count, created_at, updated_at)
    VALUES (p_job_id::text, p_owner_id::text, p_project_id::text, p_scene_id, p_source_id, p_clip_id, p_operation_id,
        p_expected_revision, p_frozen_input::json, sceneturn.video_quote_digest(p_frozen_input, p_project_id::text,
            p_scene_id, p_source_id, p_clip_id, p_expected_revision, p_source_digest, v_cost::integer),
        p_source_digest, v_cost, 0, 0, 'awaiting_confirmation', '{}', 0, v_now, v_now) RETURNING * INTO v_job;
    INSERT INTO sceneturn.operations(id, owner_id, project_id, operation_id, kind, payload_hash, response_json, created_at)
    VALUES (gen_random_uuid()::text, p_owner_id::text, p_project_id::text, p_operation_id, 'generation.quote', p_payload_hash,
        json_build_object('generationId', p_job_id::text, 'gatewayQuoteVersion', 1), v_now);
    RETURN jsonb_build_object('job', sceneturn.video_delivery(v_job), 'replayed', false);
END;
$$;

CREATE OR REPLACE FUNCTION sceneturn.video_schedule(p_job_id uuid, p_revision uuid, p_due_ms bigint) RETURNS jsonb
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_previous jsonb;
    v_now_ms bigint := floor(extract(epoch FROM clock_timestamp()) * 1000);
BEGIN
    IF (p_revision IS NULL) <> (p_due_ms IS NULL) OR (p_due_ms IS NOT NULL AND p_due_ms NOT BETWEEN v_now_ms + 1 AND v_now_ms + 360000) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'VIDEO_DISPATCH_REQUIRED';
    END IF;
    SELECT jsonb_build_object('jobId', job_id, 'revision', revision, 'dueMs', due_ms) INTO v_previous
        FROM sceneturn.deployment_dispatch WHERE kind = 'video' AND job_id = p_job_id::text AND due_ms IS NOT NULL;
    IF p_revision IS NULL THEN
        UPDATE sceneturn.deployment_dispatch SET due_ms = NULL WHERE kind = 'video' AND job_id = p_job_id::text;
    ELSE
        INSERT INTO sceneturn.deployment_dispatch(kind, job_id, revision, due_ms)
        VALUES ('video', p_job_id::text, p_revision::text, p_due_ms)
        ON CONFLICT (kind, job_id) DO UPDATE SET revision = EXCLUDED.revision, due_ms = EXCLUDED.due_ms;
    END IF;
    RETURN v_previous;
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_video_confirm(
    p_owner_id uuid, p_job_id uuid, p_hash text, p_expected_revision integer, p_source_digest text,
    p_budget_limit integer, p_request_limit integer, p_dispatch_revision uuid, p_due_ms bigint
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_job sceneturn.generations%ROWTYPE;
    v_revision integer;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    PERFORM pg_advisory_xact_lock(8314610783521762916);
    SELECT * INTO v_job FROM sceneturn.generations WHERE id = p_job_id::text AND owner_id = p_owner_id::text FOR UPDATE;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'GENERATION_NOT_FOUND';
    END IF;
    IF p_hash IS DISTINCT FROM v_job.input_hash THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'CONFIRMATION_MISMATCH';
    END IF;
    IF v_job.status <> 'awaiting_confirmation' THEN
        RETURN jsonb_build_object('job', sceneturn.video_delivery(v_job), 'replayed', true);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM sceneturn.operations WHERE owner_id = p_owner_id::text AND project_id = v_job.project_id
        AND operation_id = v_job.operation_id AND kind = 'generation.quote' AND response_json ->> 'generationId' = v_job.id
        AND response_json ->> 'gatewayQuoteVersion' = '1') OR v_job.input_hash IS DISTINCT FROM sceneturn.video_quote_digest(
            v_job.input_json::jsonb, v_job.project_id, v_job.scene_id, v_job.source_node_id, v_job.clip_id, v_job.project_revision,
            v_job.source_digest, v_job.quoted_cents) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'CONFIRMATION_INPUT_INVALID';
    END IF;
    SELECT revision INTO v_revision FROM sceneturn.projects WHERE id = v_job.project_id AND owner_id = p_owner_id::text FOR UPDATE;
    IF p_expected_revision IS DISTINCT FROM v_revision OR p_expected_revision IS DISTINCT FROM v_job.project_revision THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'PROJECT_REVISION_CONFLICT';
    END IF;
    IF p_source_digest IS DISTINCT FROM v_job.source_digest THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'GENERATION_INPUT_CHANGED';
    END IF;
    IF p_request_limit IS NULL OR p_request_limit NOT BETWEEN 1 AND 1000 OR v_job.quoted_cents NOT BETWEEN 1 AND p_request_limit
        OR p_budget_limit IS NULL OR p_budget_limit NOT BETWEEN 1 AND 30000 THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'GENERATION_COST_LIMIT';
    END IF;
    IF p_dispatch_revision IS NULL OR p_due_ms IS NULL THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'VIDEO_DISPATCH_REQUIRED';
    END IF;
    PERFORM sceneturn.video_pricing(v_job.owner_id, v_job.input_json::jsonb, true);
    PERFORM sceneturn.video_reference_seconds(v_job.owner_id, v_job.project_id, v_job.input_json::jsonb);
    INSERT INTO sceneturn.budgets(id, limit_cents, spent_cents, reserved_cents, text_spent_micros, text_reserved_micros)
        VALUES ('global', p_budget_limit, 0, 0, 0, 0) ON CONFLICT (id) DO NOTHING;
    UPDATE sceneturn.budgets SET reserved_cents = reserved_cents + v_job.quoted_cents WHERE id = 'global'
        AND ((spent_cents::bigint + reserved_cents + v_job.quoted_cents) * 10000 + text_spent_micros + text_reserved_micros)
            <= least(limit_cents, p_budget_limit)::bigint * 10000;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT402' USING MESSAGE = 'BUDGET_EXCEEDED';
    END IF;
    PERFORM sceneturn.video_schedule(p_job_id, p_dispatch_revision, p_due_ms);
    UPDATE sceneturn.generations SET status = 'queued', reserved_cents = quoted_cents, updated_at = extract(epoch FROM clock_timestamp())
        WHERE id = p_job_id::text RETURNING * INTO v_job;
    RETURN jsonb_build_object('job', sceneturn.video_delivery(v_job), 'replayed', false);
END;
$$;

CREATE OR REPLACE FUNCTION sceneturn.video_release(p_job sceneturn.generations, p_actual integer) RETURNS void
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF p_actual IS NULL OR p_actual NOT BETWEEN 0 AND p_job.reserved_cents OR p_job.usage_json ->> 'settled' = 'true' THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'GENERATION_COST_OUT_OF_BOUNDS';
    END IF;
    UPDATE sceneturn.budgets SET reserved_cents = reserved_cents - p_job.reserved_cents, spent_cents = spent_cents + p_actual
        WHERE id = 'global' AND reserved_cents >= p_job.reserved_cents;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'BUDGET_STATE_INVALID';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_video_claim(
    p_job_id uuid, p_worker uuid, p_revision uuid, p_due_ms bigint, p_immediate boolean,
    p_recovery_revision uuid, p_recovery_due_ms bigint, p_lease_seconds integer
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_job sceneturn.generations%ROWTYPE;
    v_schedule sceneturn.deployment_dispatch%ROWTYPE;
    v_obsolete jsonb;
    v_pricing jsonb;
    v_now double precision;
    v_phase text;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    PERFORM pg_advisory_xact_lock(8314610783521762916);
    v_now := extract(epoch FROM clock_timestamp());
    SELECT * INTO v_schedule FROM sceneturn.deployment_dispatch WHERE kind = 'video' AND job_id = p_job_id::text;
    IF v_schedule.due_ms IS NULL OR (v_schedule.revision, v_schedule.due_ms) IS DISTINCT FROM (p_revision::text, p_due_ms) THEN
        RETURN jsonb_build_object('state', 'stale');
    END IF;
    IF p_immediate IS NOT TRUE AND p_due_ms > v_now * 1000 THEN
        RETURN jsonb_build_object('state', 'early');
    END IF;
    SELECT * INTO v_job FROM sceneturn.generations WHERE id = p_job_id::text FOR UPDATE;
    IF NOT FOUND OR v_job.status NOT IN ('queued','submitting','processing','downloading') THEN
        v_obsolete := sceneturn.video_schedule(p_job_id, NULL, NULL);
        RETURN jsonb_build_object('state', 'stale', 'obsolete', v_obsolete);
    END IF;
    IF v_job.lease_expires_at > v_now THEN
        RETURN jsonb_build_object('state', 'leased');
    END IF;
    IF v_job.status = 'submitting' THEN
        UPDATE sceneturn.generations SET status = 'submission_unknown', lease_owner = NULL, lease_expires_at = NULL,
            error_code = 'VIDEO_SUBMISSION_UNKNOWN', error_message = '原视频提交待核实，费用预留继续保留', updated_at = v_now
            WHERE id = p_job_id::text;
        v_obsolete := sceneturn.video_schedule(p_job_id, NULL, NULL);
        RETURN jsonb_build_object('state', 'submission_unknown', 'obsolete', v_obsolete);
    END IF;
    IF p_worker IS NULL OR p_recovery_revision IS NULL OR p_recovery_revision = p_revision OR p_lease_seconds IS NULL
        OR p_lease_seconds NOT BETWEEN 30 AND 240 OR p_recovery_due_ms IS NULL OR p_recovery_due_ms < (v_now + p_lease_seconds) * 1000 THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'VIDEO_DISPATCH_REQUIRED';
    END IF;
    IF v_job.status = 'queued' THEN
        IF NOT EXISTS (SELECT 1 FROM sceneturn.projects WHERE id = v_job.project_id AND owner_id = v_job.owner_id
            AND revision = v_job.project_revision) THEN
            PERFORM sceneturn.video_release(v_job, 0);
            UPDATE sceneturn.generations SET status = 'failed', reserved_cents = 0, usage_json = '{"settled":true}',
                error_code = 'GENERATION_INPUT_CHANGED', error_message = '提交前项目已有新编辑，请重新核对报价', updated_at = v_now,
                finished_at = v_now WHERE id = p_job_id::text;
            v_obsolete := sceneturn.video_schedule(p_job_id, NULL, NULL);
            RETURN jsonb_build_object('state', 'invalidated', 'obsolete', v_obsolete);
        END IF;
        PERFORM sceneturn.video_reference_seconds(v_job.owner_id, v_job.project_id, v_job.input_json::jsonb);
        v_phase := 'submit';
    ELSE
        IF v_job.remote_task_id IS NULL OR v_job.next_poll_at > v_now THEN
            RETURN jsonb_build_object('state', 'early');
        END IF;
        v_phase := CASE v_job.status WHEN 'processing' THEN 'query' ELSE 'download' END;
    END IF;
    IF v_phase <> 'download' THEN
        v_pricing := sceneturn.video_pricing(v_job.owner_id, v_job.input_json::jsonb, true);
    END IF;
    v_obsolete := sceneturn.video_schedule(p_job_id, p_recovery_revision, p_recovery_due_ms);
    UPDATE sceneturn.generations SET status = CASE WHEN v_phase = 'submit' THEN 'submitting' ELSE status END, lease_owner = p_worker::text,
        lease_expires_at = v_now + p_lease_seconds, updated_at = v_now,
        started_at = CASE WHEN v_phase = 'submit' THEN v_now ELSE started_at END
        WHERE id = p_job_id::text RETURNING * INTO v_job;
    RETURN jsonb_build_object('state', 'claimed', 'phase', v_phase, 'job', to_jsonb(v_job),
        'apiKeyEncrypted', v_pricing ->> 'key', 'obsolete', v_obsolete);
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_video_submission(
    p_job_id uuid, p_worker uuid, p_outcome text, p_remote_task text, p_remote_request text,
    p_error_code text, p_error_message text, p_revision uuid, p_due_ms bigint
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_job sceneturn.generations%ROWTYPE;
    v_obsolete jsonb;
    v_now double precision;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    PERFORM pg_advisory_xact_lock(8314610783521762916);
    v_now := extract(epoch FROM clock_timestamp());
    SELECT * INTO v_job FROM sceneturn.generations WHERE id = p_job_id::text AND status = 'submitting'
        AND lease_owner = p_worker::text AND lease_expires_at > v_now FOR UPDATE;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('state', 'lease_lost');
    END IF;
    IF p_outcome = 'submitted' THEN
        IF coalesce(p_remote_task, '') !~ '^[A-Za-z0-9_-]{1,200}$' OR length(coalesce(p_remote_request, '')) > 200
            OR p_revision IS NULL OR p_due_ms IS NULL OR p_error_code IS NOT NULL OR p_error_message IS NOT NULL THEN
            RAISE SQLSTATE 'PT422' USING MESSAGE = 'VIDEO_RECEIPT_INVALID';
        END IF;
        v_obsolete := sceneturn.video_schedule(p_job_id, p_revision, p_due_ms);
        UPDATE sceneturn.generations SET status = 'processing', remote_task_id = p_remote_task, remote_request_id = p_remote_request,
            next_poll_at = p_due_ms / 1000.0, lease_owner = NULL, lease_expires_at = NULL, updated_at = v_now,
            error_code = NULL, error_message = NULL WHERE id = p_job_id::text RETURNING * INTO v_job;
    ELSE
        IF p_outcome IS NULL OR p_outcome NOT IN ('failed','unknown') OR p_remote_task IS NOT NULL OR p_remote_request IS NOT NULL
            OR p_revision IS NOT NULL OR p_due_ms IS NOT NULL OR coalesce(p_error_code, '') !~ '^[A-Z][A-Z0-9_]{0,99}$'
            OR length(coalesce(p_error_message, '')) NOT BETWEEN 1 AND 800 THEN
            RAISE SQLSTATE 'PT422' USING MESSAGE = 'VIDEO_RECEIPT_INVALID';
        END IF;
        IF p_outcome = 'failed' THEN
            PERFORM sceneturn.video_release(v_job, 0);
        END IF;
        v_obsolete := sceneturn.video_schedule(p_job_id, NULL, NULL);
        UPDATE sceneturn.generations SET status = CASE WHEN p_outcome = 'unknown' THEN 'submission_unknown' ELSE 'failed' END,
            reserved_cents = CASE WHEN p_outcome = 'unknown' THEN reserved_cents ELSE 0 END,
            usage_json = CASE WHEN p_outcome = 'unknown' THEN usage_json ELSE (usage_json::jsonb || '{"settled":true}'::jsonb)::json END,
            finished_at = CASE WHEN p_outcome = 'failed' THEN v_now END, lease_owner = NULL, lease_expires_at = NULL,
            error_code = p_error_code, error_message = p_error_message, updated_at = v_now WHERE id = p_job_id::text RETURNING * INTO v_job;
    END IF;
    RETURN jsonb_build_object('state', 'recorded', 'job', sceneturn.video_delivery(v_job), 'obsolete', v_obsolete);
END;
$$;

REVOKE ALL ON FUNCTION sceneturn.video_delivery(sceneturn.generations),
    sceneturn.video_quote_digest(jsonb, text, text, text, text, integer, text, integer),
    sceneturn.video_reference_seconds(text, text, jsonb), sceneturn.video_pricing(text, jsonb, boolean),
    sceneturn.video_schedule(uuid, uuid, bigint), sceneturn.video_release(sceneturn.generations, integer)
    FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON FUNCTION public.sceneturn_video_read(uuid, uuid), public.sceneturn_video_list(uuid, uuid),
    public.sceneturn_video_quote(uuid, uuid, uuid, text, text, text, integer, text, text, text, jsonb),
    public.sceneturn_video_confirm(uuid, uuid, text, integer, text, integer, integer, uuid, bigint),
    public.sceneturn_video_claim(uuid, uuid, uuid, bigint, boolean, uuid, bigint, integer),
    public.sceneturn_video_submission(uuid, uuid, text, text, text, text, text, uuid, bigint)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.sceneturn_video_read(uuid, uuid), public.sceneturn_video_list(uuid, uuid),
    public.sceneturn_video_quote(uuid, uuid, uuid, text, text, text, integer, text, text, text, jsonb),
    public.sceneturn_video_confirm(uuid, uuid, text, integer, text, integer, integer, uuid, bigint),
    public.sceneturn_video_claim(uuid, uuid, uuid, bigint, boolean, uuid, bigint, integer),
    public.sceneturn_video_submission(uuid, uuid, text, text, text, text, text, uuid, bigint)
    TO service_role;
