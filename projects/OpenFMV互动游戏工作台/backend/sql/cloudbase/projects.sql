CREATE OR REPLACE FUNCTION sceneturn.project_timestamp(p_epoch double precision) RETURNS text
LANGUAGE sql STABLE STRICT SET search_path = pg_catalog, pg_temp AS $$
    SELECT to_char(to_timestamp(p_epoch) AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US') || '+00:00';
$$;

CREATE OR REPLACE FUNCTION sceneturn.project_snapshot_envelope(
    p_owner_id text, p_project_id text, p_snapshot jsonb, p_revision integer
) RETURNS jsonb
LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $$
    SELECT jsonb_build_object(
        'project', jsonb_set(p_snapshot, '{metadata,serverRevision}', to_jsonb(p_revision)),
        'revision', p_revision,
        'canvases', COALESCE((
            SELECT jsonb_agg(canvas.data_json ORDER BY canvas.scene_id COLLATE "C")
            FROM sceneturn.canvases AS canvas
            JOIN jsonb_array_elements(p_snapshot #> '{graphData,nodes}') AS node(value)
              ON node.value ->> 'id' = canvas.scene_id AND node.value ->> 'type' = 'scene'
            WHERE canvas.project_id = p_project_id AND canvas.owner_id = p_owner_id
        ), '[]'::jsonb)
    );
$$;

CREATE OR REPLACE FUNCTION sceneturn.project_material_node_ids(
    p_owner_id text, p_project_id text
) RETURNS jsonb
LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $$
    WITH material_runs AS (
        SELECT id, scene_id, created_ids_json
        FROM sceneturn.agent_runs
        WHERE owner_id = p_owner_id AND project_id = p_project_id
          AND (limits_json ->> 'workspace' = 'materials' OR limits_json ->> 'materialConversationId' IS NOT NULL)
    ), identified_nodes AS (
        SELECT run.scene_id, created.node_id
        FROM material_runs AS run
        CROSS JOIN LATERAL json_array_elements_text(run.created_ids_json) AS created(node_id)
        UNION
        SELECT run.scene_id, receipt.value #>> '{result,sourceNodeId}'
        FROM material_runs AS run
        JOIN sceneturn.agent_steps AS step ON step.run_id = run.id
        CROSS JOIN LATERAL json_array_elements(step.tool_results_json) AS receipt(value)
        WHERE receipt.value ->> 'tool' IN ('write_artifact', 'patch_artifact')
    ), per_scene AS (
        SELECT scene_id, jsonb_agg(node_id ORDER BY node_id) AS node_ids
        FROM identified_nodes WHERE node_id IS NOT NULL
        GROUP BY scene_id
    )
    SELECT COALESCE(jsonb_object_agg(scene_id, node_ids), '{}'::jsonb) FROM per_scene;
$$;

CREATE OR REPLACE FUNCTION sceneturn.record_project_envelope(
    p_owner_id text, p_project_id text, p_envelope jsonb, p_written_at double precision
) RETURNS void
LANGUAGE sql SET search_path = pg_catalog, pg_temp AS $$
    INSERT INTO sceneturn.project_versions(id, project_id, owner_id, revision, data_json, canvases_json, created_at)
    VALUES (gen_random_uuid()::text, p_project_id, p_owner_id, (p_envelope ->> 'revision')::integer,
        (p_envelope -> 'project')::json, (p_envelope -> 'canvases')::json, p_written_at);
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_project_list(p_owner_id text) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM sceneturn.require_gateway_access();
    RETURN jsonb_build_object('projects', COALESCE((
        SELECT jsonb_agg(jsonb_build_object(
            'id', id, 'title', title, 'revision', revision,
            'createdAt', sceneturn.project_timestamp(created_at),
            'updatedAt', sceneturn.project_timestamp(updated_at),
            'archivedAt', CASE WHEN json_typeof(data_json #> '{metadata,archivedAt}') = 'string'
                THEN data_json #>> '{metadata,archivedAt}' END
        ) ORDER BY updated_at DESC, id)
        FROM sceneturn.projects WHERE owner_id = p_owner_id
    ), '[]'::jsonb));
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_project_get(p_owner_id text, p_project_id text) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_project sceneturn.projects%ROWTYPE;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    SELECT * INTO v_project FROM sceneturn.projects WHERE id = p_project_id AND owner_id = p_owner_id;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'PROJECT_NOT_FOUND';
    END IF;
    RETURN jsonb_build_object(
        'envelope', sceneturn.project_snapshot_envelope(p_owner_id, p_project_id, v_project.data_json::jsonb, v_project.revision),
        'materialNodeIds', sceneturn.project_material_node_ids(p_owner_id, p_project_id)
    );
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_project_create(
    p_owner_id text, p_project_id text, p_snapshot jsonb, p_canvas jsonb
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_scene_id text := p_canvas ->> 'sceneId';
    v_now double precision;
    v_envelope jsonb;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    IF p_snapshot ->> 'id' IS DISTINCT FROM p_project_id THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'PROJECT_ID_MISMATCH';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM sceneturn.users WHERE id = p_owner_id) THEN
        RAISE SQLSTATE 'PT401' USING MESSAGE = 'AUTH_REQUIRED';
    END IF;
    IF p_project_id IS NULL OR jsonb_typeof(p_snapshot -> 'metadata') IS DISTINCT FROM 'object'
        OR jsonb_typeof(p_snapshot #> '{graphData,nodes}') IS DISTINCT FROM 'array'
        OR jsonb_typeof(p_canvas -> 'nodes') IS DISTINCT FROM 'array'
        OR jsonb_typeof(p_canvas -> 'connections') IS DISTINCT FROM 'array' THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'PROJECT_REQUEST_INVALID';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM jsonb_array_elements(p_snapshot #> '{graphData,nodes}') AS node(value)
        WHERE node.value ->> 'id' = v_scene_id AND node.value ->> 'type' = 'scene'
    ) THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'PROJECT_REQUEST_INVALID';
    END IF;
    v_now := extract(epoch FROM clock_timestamp());
    p_snapshot := p_snapshot || jsonb_build_object(
        'createdAt', sceneturn.project_timestamp(v_now), 'updatedAt', sceneturn.project_timestamp(v_now)
    );
    INSERT INTO sceneturn.projects(id, owner_id, title, revision, data_json, created_at, updated_at)
    VALUES (p_project_id, p_owner_id, p_snapshot ->> 'title', 1, p_snapshot::json, v_now, v_now);
    INSERT INTO sceneturn.canvases(id, owner_id, project_id, scene_id, data_json, updated_at)
    VALUES (gen_random_uuid()::text, p_owner_id, p_project_id, v_scene_id, p_canvas::json, v_now);
    v_envelope := sceneturn.project_snapshot_envelope(p_owner_id, p_project_id, p_snapshot, 1);
    PERFORM sceneturn.record_project_envelope(p_owner_id, p_project_id, v_envelope, v_now);
    RETURN jsonb_build_object('envelope', v_envelope, 'materialNodeIds', '{}'::jsonb);
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_project_save(
    p_owner_id text, p_project_id text, p_expected_revision integer, p_operation_id text,
    p_payload_hash text, p_snapshot jsonb, p_canvas_template jsonb
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_project sceneturn.projects%ROWTYPE;
    v_operation sceneturn.operations%ROWTYPE;
    v_now double precision;
    v_envelope jsonb;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    PERFORM pg_advisory_xact_lock(8314610783521762916);
    SELECT * INTO v_project FROM sceneturn.projects WHERE id = p_project_id AND owner_id = p_owner_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'PROJECT_NOT_FOUND';
    END IF;
    SELECT * INTO v_operation FROM sceneturn.operations
    WHERE owner_id = p_owner_id AND project_id = p_project_id AND operation_id = p_operation_id;
    IF FOUND THEN
        IF (v_operation.kind, v_operation.payload_hash::text) IS DISTINCT FROM ('project.save', p_payload_hash) THEN
            RAISE SQLSTATE 'PT409' USING MESSAGE = 'OPERATION_CONFLICT';
        END IF;
        RETURN jsonb_build_object('envelope', v_operation.response_json,
            'materialNodeIds', sceneturn.project_material_node_ids(p_owner_id, p_project_id));
    END IF;
    IF v_project.revision IS DISTINCT FROM p_expected_revision THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'REVISION_CONFLICT';
    END IF;
    IF p_snapshot ->> 'id' IS DISTINCT FROM p_project_id THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'PROJECT_ID_MISMATCH';
    END IF;
    IF p_operation_id IS NULL OR length(p_operation_id) NOT BETWEEN 1 AND 100 OR
        p_payload_hash IS NULL OR p_payload_hash !~ '^[0-9a-f]{64}$' OR
        jsonb_typeof(p_snapshot #> '{graphData,nodes}') IS DISTINCT FROM 'array' OR
        jsonb_typeof(p_snapshot -> 'assets') IS DISTINCT FROM 'array' OR
        jsonb_typeof(p_snapshot -> 'metadata') IS DISTINCT FROM 'object' OR
        jsonb_typeof(p_canvas_template) IS DISTINCT FROM 'object' THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'PROJECT_REQUEST_INVALID';
    END IF;
    IF EXISTS (
        WITH requested_ids AS (
            SELECT asset.value ->> 'id' AS asset_id FROM jsonb_array_elements(p_snapshot -> 'assets') AS asset(value)
            UNION
            SELECT clip.value ->> 'assetId'
            FROM jsonb_array_elements(p_snapshot #> '{graphData,nodes}') AS node(value)
            CROSS JOIN LATERAL jsonb_array_elements(node.value #> '{data,timeline,tracks}') AS track(value)
            CROSS JOIN LATERAL jsonb_array_elements(track.value -> 'clips') AS clip(value)
            WHERE NULLIF(clip.value ->> 'assetId', '') IS NOT NULL
        )
        SELECT 1 FROM requested_ids AS requested
        WHERE NOT EXISTS (
            SELECT 1 FROM sceneturn.assets AS asset
            WHERE asset.project_id = p_project_id AND asset.owner_id = p_owner_id AND asset.id = requested.asset_id
        )
    ) THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'ASSET_NOT_REGISTERED';
    END IF;
    v_now := extract(epoch FROM clock_timestamp());
    p_snapshot := p_snapshot || jsonb_build_object(
        'metadata', (p_snapshot -> 'metadata') - 'serverRevision',
        'createdAt', v_project.data_json -> 'createdAt',
        'updatedAt', sceneturn.project_timestamp(v_now)
    );
    UPDATE sceneturn.projects SET title = p_snapshot ->> 'title', data_json = p_snapshot::json,
        updated_at = v_now, revision = revision + 1
    WHERE id = p_project_id AND owner_id = p_owner_id;
    INSERT INTO sceneturn.canvases(id, owner_id, project_id, scene_id, data_json, updated_at)
    SELECT gen_random_uuid()::text, p_owner_id, p_project_id, node.value ->> 'id',
        (p_canvas_template || jsonb_build_object('sceneId', node.value ->> 'id'))::json, v_now
    FROM jsonb_array_elements(p_snapshot #> '{graphData,nodes}') AS node(value)
    WHERE node.value ->> 'type' = 'scene'
    ON CONFLICT (project_id, scene_id) DO NOTHING;
    v_envelope := sceneturn.project_snapshot_envelope(p_owner_id, p_project_id, p_snapshot, v_project.revision + 1);
    PERFORM sceneturn.record_project_envelope(p_owner_id, p_project_id, v_envelope, v_now);
    INSERT INTO sceneturn.operations(id, owner_id, project_id, operation_id, kind, payload_hash, response_json, created_at)
    VALUES (gen_random_uuid()::text, p_owner_id, p_project_id, p_operation_id, 'project.save', p_payload_hash, v_envelope::json, v_now);
    RETURN jsonb_build_object('envelope', v_envelope,
        'materialNodeIds', sceneturn.project_material_node_ids(p_owner_id, p_project_id));
END;
$$;

REVOKE EXECUTE ON FUNCTION
    sceneturn.project_timestamp(double precision),
    sceneturn.project_snapshot_envelope(text, text, jsonb, integer),
    sceneturn.project_material_node_ids(text, text),
    sceneturn.record_project_envelope(text, text, jsonb, double precision),
    public.sceneturn_project_list(text),
    public.sceneturn_project_get(text, text),
    public.sceneturn_project_create(text, text, jsonb, jsonb),
    public.sceneturn_project_save(text, text, integer, text, text, jsonb, jsonb)
FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION
    public.sceneturn_project_list(text),
    public.sceneturn_project_get(text, text),
    public.sceneturn_project_create(text, text, jsonb, jsonb),
    public.sceneturn_project_save(text, text, integer, text, text, jsonb, jsonb)
TO service_role;
