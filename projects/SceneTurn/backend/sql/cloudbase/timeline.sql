CREATE OR REPLACE FUNCTION sceneturn.timeline_input(
    p_owner_id text, p_project_id text, p_expected_revision integer, p_operation_id text,
    p_payload_hash text, p_scene_id text, p_asset_id text
) RETURNS jsonb
LANGUAGE plpgsql STABLE SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_project sceneturn.projects%ROWTYPE;
    v_operation sceneturn.operations%ROWTYPE;
    v_asset sceneturn.assets%ROWTYPE;
    v_scene_index integer;
BEGIN
    SELECT * INTO v_project FROM sceneturn.projects WHERE id = p_project_id AND owner_id = p_owner_id;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'PROJECT_NOT_FOUND';
    END IF;
    SELECT * INTO v_operation FROM sceneturn.operations
    WHERE owner_id = p_owner_id AND project_id = p_project_id AND operation_id = p_operation_id;
    IF FOUND THEN
        IF (v_operation.kind, v_operation.payload_hash::text) IS DISTINCT FROM ('timeline.asset', p_payload_hash) THEN
            RAISE SQLSTATE 'PT409' USING MESSAGE = 'OPERATION_CONFLICT';
        END IF;
        RETURN jsonb_build_object('replay', jsonb_build_object('envelope', v_operation.response_json,
            'materialNodeIds', sceneturn.project_material_node_ids(p_owner_id, p_project_id)));
    END IF;
    IF v_project.revision IS DISTINCT FROM p_expected_revision THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'REVISION_CONFLICT';
    END IF;
    IF p_operation_id IS NULL OR length(p_operation_id) NOT BETWEEN 1 AND 100
       OR p_payload_hash IS NULL OR p_payload_hash !~ '^[0-9a-f]{64}$' THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'PROJECT_REQUEST_INVALID';
    END IF;
    SELECT n.ordinality::integer - 1 INTO v_scene_index
    FROM json_array_elements(v_project.data_json #> '{graphData,nodes}') WITH ORDINALITY AS n(value, ordinality)
    WHERE n.value ->> 'id' = p_scene_id AND n.value ->> 'type' = 'scene';
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'SCENE_NOT_FOUND';
    END IF;
    SELECT * INTO v_asset FROM sceneturn.assets
    WHERE id = p_asset_id AND owner_id = p_owner_id AND project_id = p_project_id;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'ASSET_NOT_FOUND';
    END IF;
    RETURN jsonb_build_object('snapshot', v_project.data_json, 'sceneIndex', v_scene_index,
        'asset', sceneturn.private_asset_descriptor(v_asset));
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_timeline_prepare(
    p_owner_id text, p_project_id text, p_expected_revision integer, p_operation_id text,
    p_payload_hash text, p_scene_id text, p_asset_id text
) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM sceneturn.require_gateway_access();
    RETURN sceneturn.timeline_input(p_owner_id, p_project_id, p_expected_revision, p_operation_id,
        p_payload_hash, p_scene_id, p_asset_id);
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_timeline_save(
    p_owner_id text, p_project_id text, p_expected_revision integer, p_operation_id text,
    p_payload_hash text, p_scene_id text, p_asset_id text, p_timeline jsonb, p_assets jsonb
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_input jsonb;
    v_snapshot jsonb;
    v_envelope jsonb;
    v_now double precision;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    PERFORM pg_advisory_xact_lock(8314610783521762916);
    v_input := sceneturn.timeline_input(p_owner_id, p_project_id, p_expected_revision, p_operation_id,
        p_payload_hash, p_scene_id, p_asset_id);
    IF v_input ? 'replay' THEN
        RETURN v_input -> 'replay';
    END IF;
    IF (p_timeline ->> 'version') IS DISTINCT FROM '2' OR
        jsonb_typeof(p_timeline -> 'tracks') IS DISTINCT FROM 'array' OR
        jsonb_typeof(p_assets) IS DISTINCT FROM 'array' THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'PROJECT_REQUEST_INVALID';
    END IF;
    IF EXISTS (
        SELECT 1 FROM jsonb_array_elements(p_assets) AS submitted(asset)
        WHERE NOT EXISTS (
            SELECT 1 FROM sceneturn.assets AS a
            WHERE a.id = submitted.asset ->> 'id' AND a.owner_id = p_owner_id AND a.project_id = p_project_id
        )
    ) THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'ASSET_NOT_REGISTERED';
    END IF;
    v_now := extract(epoch FROM clock_timestamp());
    v_snapshot := jsonb_set(v_input -> 'snapshot',
        ARRAY['graphData', 'nodes', v_input ->> 'sceneIndex', 'data', 'timeline'], p_timeline);
    v_snapshot := jsonb_set(v_snapshot, '{assets}', p_assets);
    UPDATE sceneturn.projects SET data_json = v_snapshot::json, revision = revision + 1, updated_at = v_now
    WHERE owner_id = p_owner_id AND id = p_project_id AND revision = p_expected_revision;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'REVISION_CONFLICT';
    END IF;
    v_envelope := sceneturn.project_snapshot_envelope(p_owner_id, p_project_id, v_snapshot, p_expected_revision + 1);
    PERFORM sceneturn.record_project_envelope(p_owner_id, p_project_id, v_envelope, v_now);
    INSERT INTO sceneturn.operations(id, owner_id, project_id, operation_id, kind, payload_hash, response_json, created_at)
    VALUES (gen_random_uuid()::text, p_owner_id, p_project_id, p_operation_id, 'timeline.asset', p_payload_hash,
        v_envelope::json, v_now);
    RETURN jsonb_build_object('envelope', v_envelope, 'materialNodeIds', sceneturn.project_material_node_ids(p_owner_id, p_project_id));
END;
$$;

REVOKE EXECUTE ON FUNCTION sceneturn.timeline_input(text, text, integer, text, text, text, text)
FROM PUBLIC, anon, authenticated, service_role;
REVOKE EXECUTE ON FUNCTION public.sceneturn_timeline_prepare(text, text, integer, text, text, text, text),
    public.sceneturn_timeline_save(text, text, integer, text, text, text, text, jsonb, jsonb)
FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.sceneturn_timeline_prepare(text, text, integer, text, text, text, text),
    public.sceneturn_timeline_save(text, text, integer, text, text, text, text, jsonb, jsonb)
TO service_role;
