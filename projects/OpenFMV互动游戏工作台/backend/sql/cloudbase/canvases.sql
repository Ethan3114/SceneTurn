CREATE OR REPLACE FUNCTION sceneturn.canvas_require_scene(p_project jsonb, p_scene_id text) RETURNS void
LANGUAGE plpgsql STABLE SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM jsonb_array_elements(p_project #> '{graphData,nodes}') AS node(value)
        WHERE node.value ->> 'id' = p_scene_id AND node.value ->> 'type' = 'scene'
    ) THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'SCENE_NOT_FOUND';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION sceneturn.canvas_material_node_ids(p_owner_id text, p_project_id text, p_scene_id text)
RETURNS jsonb
LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $$
    SELECT COALESCE(sceneturn.project_material_node_ids(p_owner_id, p_project_id) -> p_scene_id, '[]'::jsonb);
$$;

CREATE OR REPLACE FUNCTION sceneturn.canvas_state_token(p_canvas jsonb, p_material_node_ids jsonb) RETURNS text
LANGUAGE sql IMMUTABLE STRICT SET search_path = pg_catalog, pg_temp AS $$
    SELECT encode(sha256(convert_to(jsonb_build_array(p_canvas, p_material_node_ids)::text, 'UTF8')), 'hex');
$$;

CREATE OR REPLACE FUNCTION sceneturn.canvas_replay(
    p_owner_id text, p_project_id text, p_operation_id text, p_payload_hash text
) RETURNS jsonb
LANGUAGE plpgsql STABLE SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_operation sceneturn.operations%ROWTYPE;
BEGIN
    SELECT * INTO v_operation FROM sceneturn.operations
    WHERE owner_id = p_owner_id AND project_id = p_project_id AND operation_id = p_operation_id;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    IF (v_operation.kind, v_operation.payload_hash::text) IS DISTINCT FROM ('canvas.save', p_payload_hash) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'OPERATION_CONFLICT';
    END IF;
    RETURN v_operation.response_json::jsonb;
END;
$$;

CREATE OR REPLACE FUNCTION sceneturn.canvas_read_snapshot(p_owner_id text, p_project_id text, p_scene_id text)
RETURNS jsonb
LANGUAGE plpgsql STABLE SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_canvas jsonb;
BEGIN
    SELECT data_json::jsonb INTO v_canvas FROM sceneturn.canvases
    WHERE owner_id = p_owner_id AND project_id = p_project_id AND scene_id = p_scene_id;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'CANVAS_NOT_FOUND';
    END IF;
    RETURN v_canvas;
END;
$$;

CREATE OR REPLACE FUNCTION sceneturn.canvas_validate_graph(p_canvas jsonb, p_scene_id text) RETURNS void
LANGUAGE plpgsql IMMUTABLE SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF p_canvas ->> 'sceneId' IS DISTINCT FROM p_scene_id
        OR jsonb_typeof(p_canvas -> 'nodes') IS DISTINCT FROM 'array'
        OR jsonb_typeof(p_canvas -> 'connections') IS DISTINCT FROM 'array'
        OR jsonb_typeof(p_canvas -> 'viewport') IS DISTINCT FROM 'object' THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'CANVAS_REQUEST_INVALID';
    END IF;
    IF jsonb_array_length(p_canvas -> 'nodes') > 10000 OR jsonb_array_length(p_canvas -> 'connections') > 50000
        OR EXISTS (
            WITH node_ids AS (
                SELECT value ->> 'id' AS id FROM jsonb_array_elements(p_canvas -> 'nodes')
            ), connections AS (
                SELECT value ->> 'id' AS id, value ->> 'fromNodeId' AS from_id, value ->> 'toNodeId' AS to_id
                FROM jsonb_array_elements(p_canvas -> 'connections')
            )
            SELECT 1 FROM node_ids GROUP BY id HAVING id IS NULL OR length(id) NOT BETWEEN 1 AND 100 OR count(*) > 1
            UNION ALL
            SELECT 1 FROM connections GROUP BY id HAVING id IS NULL OR length(id) NOT BETWEEN 1 AND 100 OR count(*) > 1
            UNION ALL
            SELECT 1 FROM connections AS edge
            LEFT JOIN node_ids AS source ON source.id = edge.from_id
            LEFT JOIN node_ids AS destination ON destination.id = edge.to_id
            WHERE source.id IS NULL OR destination.id IS NULL
        ) THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'CANVAS_REQUEST_INVALID';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_canvas_read(p_owner_id text, p_project_id text, p_scene_id text)
RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_project sceneturn.projects%ROWTYPE;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    SELECT * INTO v_project FROM sceneturn.projects WHERE id = p_project_id AND owner_id = p_owner_id;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'PROJECT_NOT_FOUND';
    END IF;
    PERFORM sceneturn.canvas_require_scene(v_project.data_json::jsonb, p_scene_id);
    RETURN jsonb_build_object('canvas', sceneturn.canvas_read_snapshot(p_owner_id, p_project_id, p_scene_id),
        'revision', v_project.revision, 'materialNodeIds', sceneturn.canvas_material_node_ids(p_owner_id, p_project_id, p_scene_id));
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_canvas_prepare(
    p_owner_id text, p_project_id text, p_scene_id text, p_expected_revision integer, p_operation_id text, p_payload_hash text
) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_project sceneturn.projects%ROWTYPE;
    v_replay jsonb;
    v_canvas jsonb;
    v_material_node_ids jsonb;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    SELECT * INTO v_project FROM sceneturn.projects WHERE id = p_project_id AND owner_id = p_owner_id;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'PROJECT_NOT_FOUND';
    END IF;
    PERFORM sceneturn.canvas_require_scene(v_project.data_json::jsonb, p_scene_id);
    v_material_node_ids := sceneturn.canvas_material_node_ids(p_owner_id, p_project_id, p_scene_id);
    v_replay := sceneturn.canvas_replay(p_owner_id, p_project_id, p_operation_id, p_payload_hash);
    IF v_replay IS NOT NULL THEN
        RETURN v_replay || jsonb_build_object('baseToken', NULL, 'materialNodeIds', v_material_node_ids);
    END IF;
    IF v_project.revision IS DISTINCT FROM p_expected_revision THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'REVISION_CONFLICT';
    END IF;
    v_canvas := sceneturn.canvas_read_snapshot(p_owner_id, p_project_id, p_scene_id);
    RETURN jsonb_build_object('canvas', v_canvas, 'revision', v_project.revision, 'materialNodeIds', v_material_node_ids,
        'baseToken', sceneturn.canvas_state_token(v_canvas, v_material_node_ids));
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_canvas_save(
    p_owner_id text, p_project_id text, p_scene_id text, p_expected_revision integer, p_operation_id text, p_payload_hash text,
    p_base_token text, p_canvas jsonb
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_project sceneturn.projects%ROWTYPE;
    v_replay jsonb;
    v_current jsonb;
    v_material_node_ids jsonb;
    v_now double precision;
    v_result jsonb;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    PERFORM pg_advisory_xact_lock(8314610783521762916);
    SELECT * INTO v_project FROM sceneturn.projects WHERE id = p_project_id AND owner_id = p_owner_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'PROJECT_NOT_FOUND';
    END IF;
    PERFORM sceneturn.canvas_require_scene(v_project.data_json::jsonb, p_scene_id);
    v_material_node_ids := sceneturn.canvas_material_node_ids(p_owner_id, p_project_id, p_scene_id);
    v_replay := sceneturn.canvas_replay(p_owner_id, p_project_id, p_operation_id, p_payload_hash);
    IF v_replay IS NOT NULL THEN
        RETURN v_replay || jsonb_build_object('materialNodeIds', v_material_node_ids);
    END IF;
    IF v_project.revision IS DISTINCT FROM p_expected_revision THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'REVISION_CONFLICT';
    END IF;
    SELECT data_json::jsonb INTO v_current FROM sceneturn.canvases
    WHERE owner_id = p_owner_id AND project_id = p_project_id AND scene_id = p_scene_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'CANVAS_NOT_FOUND';
    END IF;
    IF p_base_token IS DISTINCT FROM sceneturn.canvas_state_token(v_current, v_material_node_ids) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'REVISION_CONFLICT';
    END IF;
    IF p_operation_id IS NULL OR length(p_operation_id) NOT BETWEEN 1 AND 100
        OR p_payload_hash IS NULL OR p_payload_hash !~ '^[0-9a-f]{64}$' THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'CANVAS_REQUEST_INVALID';
    END IF;
    PERFORM sceneturn.canvas_validate_graph(p_canvas, p_scene_id);

    v_now := extract(epoch FROM clock_timestamp());
    UPDATE sceneturn.canvases SET data_json = p_canvas::json, updated_at = v_now
    WHERE owner_id = p_owner_id AND project_id = p_project_id AND scene_id = p_scene_id;
    UPDATE sceneturn.projects SET revision = revision + 1, updated_at = v_now
    WHERE id = p_project_id AND owner_id = p_owner_id;
    PERFORM sceneturn.record_project_envelope(p_owner_id, p_project_id,
        sceneturn.project_snapshot_envelope(p_owner_id, p_project_id, v_project.data_json::jsonb, v_project.revision + 1), v_now);
    v_result := jsonb_build_object('canvas', p_canvas, 'revision', v_project.revision + 1);
    INSERT INTO sceneturn.operations(id, owner_id, project_id, operation_id, kind, payload_hash, response_json, created_at)
    VALUES (gen_random_uuid()::text, p_owner_id, p_project_id, p_operation_id, 'canvas.save', p_payload_hash, v_result::json, v_now);
    RETURN v_result || jsonb_build_object('materialNodeIds', v_material_node_ids);
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_canvas_versions(
    p_owner_id text, p_project_id text, p_scene_id text, p_node_id text, p_before integer
) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_project sceneturn.projects%ROWTYPE;
    v_versions jsonb;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    SELECT * INTO v_project FROM sceneturn.projects WHERE id = p_project_id AND owner_id = p_owner_id;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'PROJECT_NOT_FOUND';
    END IF;
    PERFORM sceneturn.canvas_require_scene(v_project.data_json::jsonb, p_scene_id);
    SELECT COALESCE(jsonb_agg(jsonb_build_object('revision', record.revision, 'node', record.node) ORDER BY record.revision DESC), '[]'::jsonb)
    INTO v_versions
    FROM (
        SELECT version.revision, item.value AS node FROM sceneturn.project_versions AS version
        CROSS JOIN LATERAL json_array_elements(version.canvases_json) AS scene(value)
        CROSS JOIN LATERAL json_array_elements(scene.value -> 'nodes') AS item(value)
        WHERE version.project_id = p_project_id AND version.owner_id = p_owner_id
            AND scene.value ->> 'sceneId' = p_scene_id AND item.value ->> 'id' = p_node_id
            AND (p_before IS NULL OR version.revision < p_before)
        ORDER BY version.revision DESC LIMIT 13
    ) AS record;
    RETURN jsonb_build_object('versions', v_versions, 'materialNodeIds',
        sceneturn.canvas_material_node_ids(p_owner_id, p_project_id, p_scene_id));
END;
$$;

REVOKE EXECUTE ON FUNCTION
    sceneturn.canvas_require_scene(jsonb, text),
    sceneturn.canvas_material_node_ids(text, text, text),
    sceneturn.canvas_state_token(jsonb, jsonb),
    sceneturn.canvas_replay(text, text, text, text),
    sceneturn.canvas_read_snapshot(text, text, text),
    sceneturn.canvas_validate_graph(jsonb, text)
FROM PUBLIC, anon, authenticated, service_role;

REVOKE EXECUTE ON FUNCTION
    public.sceneturn_canvas_read(text, text, text),
    public.sceneturn_canvas_prepare(text, text, text, integer, text, text),
    public.sceneturn_canvas_save(text, text, text, integer, text, text, text, jsonb),
    public.sceneturn_canvas_versions(text, text, text, text, integer)
FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION
    public.sceneturn_canvas_read(text, text, text),
    public.sceneturn_canvas_prepare(text, text, text, integer, text, text),
    public.sceneturn_canvas_save(text, text, text, integer, text, text, text, jsonb),
    public.sceneturn_canvas_versions(text, text, text, text, integer)
TO service_role;
