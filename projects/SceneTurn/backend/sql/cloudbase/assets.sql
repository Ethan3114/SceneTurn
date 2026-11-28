CREATE OR REPLACE FUNCTION sceneturn.private_asset_descriptor(p_asset sceneturn.assets) RETURNS jsonb
LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $$
    SELECT (to_jsonb(p_asset) - 'owner_id' - 'metadata_json') || jsonb_build_object(
        'metadata_json', CASE WHEN p_asset.mime_type LIKE 'text/%'
            THEN jsonb_build_object('encoding', p_asset.metadata_json ->> 'encoding') ELSE '{}'::jsonb END
    );
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_asset_upload_target(p_owner_id text, p_project_id text) RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM sceneturn.require_gateway_access();
    IF NOT EXISTS (SELECT 1 FROM sceneturn.projects WHERE id = p_project_id AND owner_id = p_owner_id) THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'PROJECT_NOT_FOUND';
    END IF;
    RETURN true;
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_asset_get(p_owner_id text, p_asset_id text) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_asset sceneturn.assets%ROWTYPE;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    SELECT asset.* INTO v_asset FROM sceneturn.assets AS asset
    JOIN sceneturn.projects AS project ON project.id = asset.project_id AND project.owner_id = p_owner_id
    WHERE asset.id = p_asset_id AND asset.owner_id = p_owner_id;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'ASSET_NOT_FOUND';
    END IF;
    RETURN sceneturn.private_asset_descriptor(v_asset);
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_asset_register(
    p_owner_id text, p_project_id text, p_asset_id uuid, p_name text, p_relpath text,
    p_poster_relpath text, p_mime_type text, p_bytes integer, p_sha256 text,
    p_duration_ms integer, p_width integer, p_height integer, p_metadata_json jsonb
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_project sceneturn.projects%ROWTYPE;
    v_asset sceneturn.assets%ROWTYPE;
    v_snapshot jsonb;
    v_envelope jsonb;
    v_time double precision;
    v_asset_type text := split_part(p_mime_type, '/', 1);
    v_content_url text := '/api/v1/assets/' || p_asset_id::text || '/content';
    v_poster_url text := CASE WHEN p_poster_relpath <> '' THEN '/api/v1/assets/' || p_asset_id::text || '/poster' END;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    IF p_asset_id IS NULL OR p_name IS NULL OR length(p_name) NOT BETWEEN 1 AND 200 OR
        strpos(p_name, '/') > 0 OR strpos(p_name, chr(92)) > 0 OR
        p_relpath IS NULL OR p_relpath !~ '^media/[a-f0-9]{32}\.[a-z0-9]{2,5}$' OR
        p_poster_relpath IS NULL OR (p_poster_relpath <> '' AND p_poster_relpath <> split_part(p_relpath, '.', 1) || '-poster.jpg') OR
        p_mime_type IS NULL OR p_mime_type !~ '^(text|video|image|audio)/[a-z0-9-]{1,40}$' OR
        p_bytes IS NULL OR p_bytes < 0 OR (p_bytes = 0 AND v_asset_type <> 'text') OR
        p_sha256 IS NULL OR p_sha256 !~ '^[a-f0-9]{64}$' OR
        p_duration_ms IS NULL OR p_duration_ms < 0 OR p_width IS NULL OR p_width < 0 OR p_height IS NULL OR p_height < 0 OR
        jsonb_typeof(p_metadata_json) IS DISTINCT FROM 'object' OR
        (v_asset_type = 'text' AND (p_metadata_json ->> 'encoding' IS NULL OR p_metadata_json ->> 'encoding' !~ '^[a-z0-9-]{1,32}$')) THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'ASSET_REGISTRATION_INVALID';
    END IF;
    PERFORM pg_advisory_xact_lock(8314610783521762916);
    SELECT * INTO v_project FROM sceneturn.projects WHERE id = p_project_id AND owner_id = p_owner_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'PROJECT_NOT_FOUND';
    END IF;
    v_time := extract(epoch FROM clock_timestamp());
    INSERT INTO sceneturn.assets(id, owner_id, project_id, name, relpath, poster_relpath, mime_type, bytes,
        sha256, duration_ms, width, height, metadata_json, created_at)
    VALUES (p_asset_id::text, p_owner_id, p_project_id, p_name, p_relpath, p_poster_relpath, p_mime_type,
        p_bytes, p_sha256, p_duration_ms, p_width, p_height, p_metadata_json::json, v_time)
    RETURNING * INTO v_asset;
    v_snapshot := v_project.data_json::jsonb;
    v_snapshot := v_snapshot || jsonb_build_object(
        'updatedAt', sceneturn.project_timestamp(v_time),
        'assets', COALESCE(v_snapshot -> 'assets', '[]'::jsonb) || jsonb_build_array(jsonb_build_object(
            'id', p_asset_id, 'type', v_asset_type, 'name', p_name, 'path', v_content_url,
            'relativePath', 'assets/' || p_asset_id::text, 'importedAt', sceneturn.project_timestamp(v_time),
            'metadata', p_metadata_json || jsonb_build_object(
                'duration', p_duration_ms / 1000.0, 'width', p_width, 'height', p_height, 'poster', v_poster_url
            )
        ))
    );
    UPDATE sceneturn.projects SET data_json = v_snapshot::json, updated_at = v_time, revision = revision + 1
    WHERE id = p_project_id AND owner_id = p_owner_id;
    v_envelope := sceneturn.project_snapshot_envelope(p_owner_id, p_project_id, v_snapshot, v_project.revision + 1);
    PERFORM sceneturn.record_project_envelope(p_owner_id, p_project_id, v_envelope, v_time);
    RETURN jsonb_build_object('asset', sceneturn.private_asset_descriptor(v_asset), 'envelope', v_envelope,
        'materialNodeIds', sceneturn.project_material_node_ids(p_owner_id, p_project_id));
END;
$$;

REVOKE EXECUTE ON FUNCTION
    sceneturn.private_asset_descriptor(sceneturn.assets),
    public.sceneturn_asset_upload_target(text, text),
    public.sceneturn_asset_get(text, text),
    public.sceneturn_asset_register(text, text, uuid, text, text, text, text, integer, text, integer, integer, integer, jsonb)
FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION
    public.sceneturn_asset_upload_target(text, text),
    public.sceneturn_asset_get(text, text),
    public.sceneturn_asset_register(text, text, uuid, text, text, text, text, integer, text, integer, integer, integer, jsonb)
TO service_role;
