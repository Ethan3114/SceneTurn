CREATE OR REPLACE FUNCTION public.sceneturn_scene_create(
    p_owner_id text, p_project_id text, p_expected_revision integer,
    p_operation_id text, p_payload_hash text, p_scene jsonb, p_canvas jsonb
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_project sceneturn.projects%ROWTYPE;
    v_operation sceneturn.operations%ROWTYPE;
    v_scene_id text := p_scene ->> 'id';
    v_snapshot jsonb;
    v_envelope jsonb;
    v_now double precision;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    PERFORM pg_advisory_xact_lock(8314610783521762916);
    SELECT * INTO v_project FROM sceneturn.projects
    WHERE id = p_project_id AND owner_id = p_owner_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'PROJECT_NOT_FOUND';
    END IF;
    SELECT * INTO v_operation FROM sceneturn.operations
    WHERE owner_id = p_owner_id AND project_id = p_project_id AND operation_id = p_operation_id;
    IF FOUND THEN
        IF (v_operation.kind, v_operation.payload_hash::text) IS DISTINCT FROM ('scene.create', p_payload_hash) THEN
            RAISE SQLSTATE 'PT409' USING MESSAGE = 'OPERATION_CONFLICT';
        END IF;
        RETURN jsonb_build_object('envelope', v_operation.response_json, 'replayed', true,
            'materialNodeIds', sceneturn.project_material_node_ids(p_owner_id, p_project_id));
    END IF;
    IF v_project.revision IS DISTINCT FROM p_expected_revision THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'REVISION_CONFLICT';
    END IF;
    IF p_operation_id IS NULL OR length(p_operation_id) NOT BETWEEN 1 AND 100
       OR p_payload_hash IS NULL OR p_payload_hash !~ '^[0-9a-f]{64}$'
       OR v_scene_id IS NULL OR v_scene_id !~ '^[0-9a-f-]{36}$'
       OR (p_scene ->> 'type') IS DISTINCT FROM 'scene'
       OR (p_scene #>> '{data,type}') IS DISTINCT FROM 'scene'
       OR (p_scene #>> '{data,timeline,version}') IS DISTINCT FROM '2'
       OR (p_canvas ->> 'sceneId') IS DISTINCT FROM v_scene_id THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'PROJECT_REQUEST_INVALID';
    END IF;
    v_now := extract(epoch FROM clock_timestamp());
    v_snapshot := v_project.data_json::jsonb;
    p_scene := jsonb_set(p_scene, '{position,x}',
        to_jsonb(320 + jsonb_array_length(v_snapshot #> '{graphData,nodes}') * 280));
    v_snapshot := jsonb_set(v_snapshot, '{graphData,nodes}',
        (v_snapshot #> '{graphData,nodes}') || jsonb_build_array(p_scene));
    v_snapshot := v_snapshot || jsonb_build_object(
        'metadata', (v_snapshot -> 'metadata') - 'serverRevision',
        'updatedAt', sceneturn.project_timestamp(v_now)
    );
    UPDATE sceneturn.projects SET data_json = v_snapshot::json, revision = revision + 1, updated_at = v_now
    WHERE id = p_project_id AND owner_id = p_owner_id;
    INSERT INTO sceneturn.canvases(id, owner_id, project_id, scene_id, data_json, updated_at)
    VALUES (gen_random_uuid()::text, p_owner_id, p_project_id, v_scene_id, p_canvas::json, v_now);
    v_envelope := sceneturn.project_snapshot_envelope(p_owner_id, p_project_id, v_snapshot, v_project.revision + 1);
    PERFORM sceneturn.record_project_envelope(p_owner_id, p_project_id, v_envelope, v_now);
    INSERT INTO sceneturn.operations(id, owner_id, project_id, operation_id, kind, payload_hash, response_json, created_at)
    VALUES (gen_random_uuid()::text, p_owner_id, p_project_id, p_operation_id, 'scene.create', p_payload_hash, v_envelope::json, v_now);
    RETURN jsonb_build_object('envelope', v_envelope, 'replayed', false,
        'materialNodeIds', sceneturn.project_material_node_ids(p_owner_id, p_project_id));
END;
$$;
REVOKE EXECUTE ON FUNCTION public.sceneturn_scene_create(text, text, integer, text, text, jsonb, jsonb)
FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.sceneturn_scene_create(text, text, integer, text, text, jsonb, jsonb)
TO service_role;
