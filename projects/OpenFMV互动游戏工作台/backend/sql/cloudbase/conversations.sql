CREATE OR REPLACE FUNCTION sceneturn.conversation_project(p_owner_id text, p_project_id text)
RETURNS sceneturn.projects
LANGUAGE plpgsql STABLE SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_project sceneturn.projects%ROWTYPE;
BEGIN
    SELECT * INTO v_project FROM sceneturn.projects WHERE id = p_project_id AND owner_id = p_owner_id;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'PROJECT_NOT_FOUND';
    END IF;
    RETURN v_project;
END;
$$;

CREATE OR REPLACE FUNCTION sceneturn.conversation_lookup(p_owner_id text, p_project_id text, p_conversation_id text)
RETURNS sceneturn.project_conversations
LANGUAGE plpgsql STABLE SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_conversation sceneturn.project_conversations%ROWTYPE;
BEGIN
    SELECT * INTO v_conversation FROM sceneturn.project_conversations
    WHERE id = p_conversation_id AND owner_id = p_owner_id AND project_id = p_project_id;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'CONVERSATION_NOT_FOUND';
    END IF;
    RETURN v_conversation;
END;
$$;

CREATE OR REPLACE FUNCTION sceneturn.conversation_summary(p_row sceneturn.project_conversations) RETURNS jsonb
LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $$
    SELECT jsonb_build_object('id', p_row.id, 'projectId', p_row.project_id, 'title', p_row.title,
        'createdAt', sceneturn.project_timestamp(p_row.created_at), 'updatedAt', sceneturn.project_timestamp(p_row.updated_at));
$$;

CREATE OR REPLACE FUNCTION sceneturn.conversation_replay(
    p_owner_id text, p_project_id text, p_operation_id text, p_payload_hash text, p_kind text
) RETURNS jsonb
LANGUAGE plpgsql STABLE SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_operation sceneturn.operations%ROWTYPE;
BEGIN
    IF p_operation_id IS NULL OR length(p_operation_id) NOT BETWEEN 1 AND 100
        OR p_payload_hash IS NULL OR p_payload_hash !~ '^[0-9a-f]{64}$' THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'CHAT_CONTEXT_INVALID';
    END IF;
    SELECT * INTO v_operation FROM sceneturn.operations
    WHERE owner_id = p_owner_id AND project_id = p_project_id AND operation_id = p_operation_id;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    IF (v_operation.kind, v_operation.payload_hash::text) IS DISTINCT FROM (p_kind, p_payload_hash) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'OPERATION_CONFLICT';
    END IF;
    RETURN v_operation.response_json::jsonb;
END;
$$;

CREATE OR REPLACE FUNCTION sceneturn.conversation_delivery(p_row sceneturn.project_conversations, p_revision integer)
RETURNS jsonb
LANGUAGE plpgsql STABLE SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_run sceneturn.agent_runs%ROWTYPE;
    v_messages jsonb;
    v_steps jsonb := '[]'::jsonb;
    v_calls jsonb := '[]'::jsonb;
    v_reservations jsonb := '[]'::jsonb;
BEGIN
    SELECT COALESCE(jsonb_agg(jsonb_build_object('id', id, 'role', role, 'content', content, 'context', context_json,
        'runId', run_id, 'createdAt', sceneturn.project_timestamp(created_at)) ORDER BY sequence), '[]'::jsonb)
    INTO v_messages FROM sceneturn.conversation_messages
    WHERE conversation_id = p_row.id AND owner_id = p_row.owner_id AND project_id = p_row.project_id;

    SELECT * INTO v_run FROM sceneturn.agent_runs
    WHERE id = p_row.active_run_id AND owner_id = p_row.owner_id AND project_id = p_row.project_id
        AND executor = 'codex' AND role = 'director' AND limits_json ->> 'conversationId' = p_row.id;
    IF FOUND THEN
        SELECT COALESCE(jsonb_agg(to_jsonb(step) - ARRAY['run_id', 'created_at', 'updated_at'] ORDER BY step.index), '[]'::jsonb)
        INTO v_steps FROM sceneturn.agent_steps AS step WHERE step.run_id = v_run.id;
        SELECT COALESCE(jsonb_agg(to_jsonb(call) - ARRAY['run_id', 'input_json', 'raw_output'] ORDER BY call.created_at), '[]'::jsonb)
        INTO v_calls FROM sceneturn.agent_calls AS call WHERE call.run_id = v_run.id;
        SELECT COALESCE(jsonb_agg(jsonb_build_object('reserved_micros', reservation.reserved_micros)), '[]'::jsonb)
        INTO v_reservations FROM sceneturn.budget_reservations AS reservation
        JOIN sceneturn.agent_calls AS call ON call.id = reservation.id
        WHERE call.run_id = v_run.id;
    END IF;

    RETURN jsonb_build_object('summary', sceneturn.conversation_summary(p_row), 'messages', v_messages, 'revision', p_revision,
        'run', CASE WHEN v_run.id IS NOT NULL THEN to_jsonb(v_run)
            - ARRAY['owner_id', 'operation_id', 'lease_owner', 'lease_expires_at'] END,
        'steps', v_steps, 'calls', v_calls, 'reservations', v_reservations);
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_conversation_require_project(p_owner_id text, p_project_id text)
RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM sceneturn.require_gateway_access();
    PERFORM sceneturn.conversation_project(p_owner_id, p_project_id);
    RETURN true;
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_conversation_list(p_owner_id text, p_project_id text) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM sceneturn.require_gateway_access();
    PERFORM sceneturn.conversation_project(p_owner_id, p_project_id);
    RETURN jsonb_build_object('conversations', COALESCE((
        SELECT jsonb_agg(sceneturn.conversation_summary(c) ORDER BY c.updated_at DESC, c.id)
        FROM sceneturn.project_conversations AS c WHERE c.owner_id = p_owner_id AND c.project_id = p_project_id
    ), '[]'::jsonb));
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_conversation_read(
    p_owner_id text, p_project_id text, p_conversation_id text
) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_project sceneturn.projects%ROWTYPE;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    v_project := sceneturn.conversation_project(p_owner_id, p_project_id);
    RETURN sceneturn.conversation_delivery(sceneturn.conversation_lookup(p_owner_id, p_project_id, p_conversation_id), v_project.revision);
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_conversation_create(
    p_owner_id text, p_project_id text, p_conversation_id text, p_operation_id text, p_payload_hash text, p_title text
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_project sceneturn.projects%ROWTYPE;
    v_conversation sceneturn.project_conversations%ROWTYPE;
    v_replay jsonb;
    v_now double precision;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    PERFORM pg_advisory_xact_lock(8314610783521762916);
    v_project := sceneturn.conversation_project(p_owner_id, p_project_id);
    v_replay := sceneturn.conversation_replay(p_owner_id, p_project_id, p_operation_id, p_payload_hash, 'conversation.create');
    IF v_replay IS NOT NULL THEN
        RETURN sceneturn.conversation_delivery(sceneturn.conversation_lookup(p_owner_id, p_project_id, v_replay ->> 'conversationId'), v_project.revision);
    END IF;
    IF p_conversation_id IS NULL OR length(p_conversation_id) NOT BETWEEN 1 AND 36
        OR p_title IS NULL OR length(p_title) NOT BETWEEN 1 AND 200 THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'CHAT_CONTEXT_INVALID';
    END IF;
    v_now := extract(epoch FROM clock_timestamp());
    INSERT INTO sceneturn.project_conversations (id, owner_id, project_id, title, created_at, updated_at)
    VALUES (p_conversation_id, p_owner_id, p_project_id, p_title, v_now, v_now) RETURNING * INTO v_conversation;
    INSERT INTO sceneturn.operations (id, owner_id, project_id, operation_id, kind, payload_hash, response_json, created_at)
    VALUES (gen_random_uuid()::text, p_owner_id, p_project_id, p_operation_id, 'conversation.create',
        p_payload_hash, json_build_object('conversationId', p_conversation_id), v_now);
    RETURN sceneturn.conversation_delivery(v_conversation, v_project.revision);
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_conversation_send(
    p_owner_id text, p_project_id text, p_conversation_id text, p_operation_id text, p_payload_hash text,
    p_expected_revision integer, p_content text, p_limits jsonb
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_project sceneturn.projects%ROWTYPE;
    v_conversation sceneturn.project_conversations%ROWTYPE;
    v_active sceneturn.agent_runs%ROWTYPE;
    v_context jsonb := p_limits -> 'editingContext';
    v_nodes jsonb;
    v_scene_ids jsonb;
    v_scene_id text;
    v_thread_id text;
    v_limits jsonb;
    v_run_id text;
    v_sequence integer;
    v_now double precision;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    PERFORM pg_advisory_xact_lock(8314610783521762916);
    SELECT * INTO v_project FROM sceneturn.projects WHERE id = p_project_id AND owner_id = p_owner_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'PROJECT_NOT_FOUND';
    END IF;
    v_conversation := sceneturn.conversation_lookup(p_owner_id, p_project_id, p_conversation_id);
    IF sceneturn.conversation_replay(p_owner_id, p_project_id, p_operation_id, p_payload_hash, 'conversation.send') IS NOT NULL THEN
        RETURN sceneturn.conversation_delivery(v_conversation, v_project.revision);
    END IF;
    SELECT * INTO v_active FROM sceneturn.agent_runs
    WHERE id = v_conversation.active_run_id AND owner_id = p_owner_id AND project_id = p_project_id
        AND executor = 'codex' AND limits_json ->> 'conversationId' = p_conversation_id;
    IF v_active.status IN ('queued', 'running', 'stopping', 'waiting_connection', 'waiting_confirmation', 'conflict', 'submission_unknown') THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'CONVERSATION_BUSY';
    END IF;
    IF v_project.revision IS DISTINCT FROM p_expected_revision THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'REVISION_CONFLICT';
    END IF;
    IF p_content IS NULL OR length(p_content) > 12000 OR btrim(p_content) = ''
        OR p_limits ->> 'workspace' IS DISTINCT FROM 'project'
        OR p_limits ->> 'conversationId' IS DISTINCT FROM p_conversation_id
        OR jsonb_typeof(v_context -> 'selectedNodeIds') IS DISTINCT FROM 'array'
        OR jsonb_typeof(v_context -> 'selectedClipIds') IS DISTINCT FROM 'array' THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'CHAT_CONTEXT_INVALID';
    END IF;
    v_nodes := v_project.data_json::jsonb #> '{graphData,nodes}';
    SELECT jsonb_agg(node ->> 'id' ORDER BY position) INTO v_scene_ids
    FROM jsonb_array_elements(v_nodes) WITH ORDINALITY AS scene(node, position) WHERE node ->> 'type' = 'scene';
    IF v_scene_ids IS NULL THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'SCENE_REQUIRED';
    END IF;
    v_scene_id := COALESCE(NULLIF(v_context ->> 'sceneId', ''), v_scene_ids ->> 0);
    IF NOT v_scene_ids ? v_scene_id THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'SCENE_NOT_FOUND';
    END IF;
    IF EXISTS (
        SELECT 1 FROM jsonb_array_elements_text(v_context -> 'selectedNodeIds') AS selected(id)
        WHERE NOT EXISTS (SELECT 1 FROM jsonb_array_elements(v_nodes) AS g(node) WHERE node ->> 'id' = selected.id)
    ) OR EXISTS (
        SELECT 1 FROM jsonb_array_elements_text(v_context -> 'selectedClipIds') AS selected(id)
        WHERE NOT EXISTS (
            SELECT 1 FROM jsonb_array_elements(v_nodes) AS g(node)
            CROSS JOIN LATERAL jsonb_array_elements(
                CASE WHEN node ->> 'type' = 'scene' THEN node #> '{data,timeline,tracks}' END
            ) AS t(track)
            CROSS JOIN LATERAL jsonb_array_elements(track -> 'clips') AS c(clip) WHERE clip ->> 'id' = selected.id
        )
    ) THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'CHAT_CONTEXT_INVALID';
    END IF;
    IF EXISTS (SELECT 1 FROM sceneturn.agent_runs WHERE project_id = p_project_id AND operation_id = p_operation_id) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'OPERATION_CONFLICT';
    END IF;

    v_now := extract(epoch FROM clock_timestamp());
    v_run_id := gen_random_uuid()::text;
    v_thread_id := COALESCE(NULLIF(v_conversation.thread_id, ''), v_active.thread_id);
    v_limits := p_limits;
    IF v_active.id IS NOT NULL AND NULLIF(v_thread_id, '') IS NOT NULL THEN
        v_limits := v_limits || jsonb_build_object('codexThreadUsageBaseline', v_active.result_json #> '{usage,threadTotals}');
    END IF;
    INSERT INTO sceneturn.agent_runs (
        id, owner_id, project_id, scene_id, operation_id, role, executor, goal, target_ids_json,
        created_ids_json, limits_json, initial_revision, project_revision, plan_json, status,
        stop_requested, thread_id, created_at, updated_at
    ) VALUES (
        v_run_id, p_owner_id, p_project_id, v_scene_id, p_operation_id, 'director', 'codex', p_content, v_scene_ids::json,
        '[]'::json, v_limits::json, v_project.revision, v_project.revision, '[]'::json, 'waiting_connection',
        0, v_thread_id, v_now, v_now
    );
    SELECT COALESCE(max(sequence), 0) + 2 INTO v_sequence FROM sceneturn.conversation_messages
    WHERE conversation_id = p_conversation_id AND owner_id = p_owner_id AND project_id = p_project_id;
    INSERT INTO sceneturn.conversation_messages (
        id, owner_id, project_id, conversation_id, operation_id, sequence, role, content, context_json, run_id, created_at
    ) VALUES (gen_random_uuid()::text, p_owner_id, p_project_id, p_conversation_id, p_operation_id,
        v_sequence, 'user', p_content, v_context::json, v_run_id, v_now);
    UPDATE sceneturn.project_conversations
    SET active_run_id = v_run_id, updated_at = v_now, title = CASE WHEN v_sequence = 2 THEN left(p_content, 60) ELSE title END
    WHERE id = p_conversation_id AND owner_id = p_owner_id AND project_id = p_project_id RETURNING * INTO v_conversation;
    INSERT INTO sceneturn.operations (id, owner_id, project_id, operation_id, kind, payload_hash, response_json, created_at)
    VALUES (gen_random_uuid()::text, p_owner_id, p_project_id, p_operation_id, 'conversation.send',
        p_payload_hash, json_build_object('runId', v_run_id), v_now);
    RETURN sceneturn.conversation_delivery(v_conversation, v_project.revision);
END;
$$;

REVOKE EXECUTE ON FUNCTION sceneturn.conversation_project(text, text), sceneturn.conversation_lookup(text, text, text),
    sceneturn.conversation_summary(sceneturn.project_conversations),
    sceneturn.conversation_replay(text, text, text, text, text),
    sceneturn.conversation_delivery(sceneturn.project_conversations, integer)
FROM PUBLIC, anon, authenticated, service_role;

REVOKE EXECUTE ON FUNCTION public.sceneturn_conversation_require_project(text, text), public.sceneturn_conversation_list(text, text),
    public.sceneturn_conversation_read(text, text, text), public.sceneturn_conversation_create(text, text, text, text, text, text),
    public.sceneturn_conversation_send(text, text, text, text, text, integer, text, jsonb)
FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION public.sceneturn_conversation_require_project(text, text), public.sceneturn_conversation_list(text, text),
    public.sceneturn_conversation_read(text, text, text), public.sceneturn_conversation_create(text, text, text, text, text, text),
    public.sceneturn_conversation_send(text, text, text, text, text, integer, text, jsonb)
TO service_role;
