CREATE OR REPLACE FUNCTION public.sceneturn_model_registry(p_owner_id uuid, p_project_id uuid)
RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_project sceneturn.projects%ROWTYPE;
    v_connections jsonb;
    v_wan_workspace text;
    v_codex_image boolean := false;
    v_now double precision := extract(epoch FROM statement_timestamp());
BEGIN
    PERFORM sceneturn.require_gateway_access();
    IF NOT EXISTS (SELECT 1 FROM sceneturn.users WHERE id = p_owner_id::text) THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'MODEL_OWNER_NOT_FOUND';
    END IF;
    IF p_project_id IS NOT NULL THEN
        SELECT * INTO v_project FROM sceneturn.projects WHERE id = p_project_id::text AND owner_id = p_owner_id::text;
        IF NOT FOUND THEN
            RAISE SQLSTATE 'PT404' USING MESSAGE = 'PROJECT_NOT_FOUND';
        END IF;
        v_codex_image := EXISTS (
            SELECT 1 FROM sceneturn.codex_connections AS bridge
            WHERE bridge.owner_id = p_owner_id::text AND bridge.project_id = p_project_id::text AND bridge.revoked_at IS NULL
            AND bridge.expires_at > v_now AND bridge.last_seen_at >= v_now - 30
            AND bridge.capabilities_json::jsonb @> '["native_image_v1"]'::jsonb
        );
    END IF;
    SELECT workspace_id INTO v_wan_workspace FROM sceneturn.provider_settings WHERE owner_id = p_owner_id::text;
    SELECT jsonb_object_agg(registry.provider_id, jsonb_build_object(
        'revision', coalesce(connection.revision, 0),
        'config', jsonb_strip_nulls(jsonb_build_object(
            'region', connection.config_json -> 'region',
            'workspaceId', connection.config_json -> 'workspaceId',
            'entitledModelIds', connection.config_json -> 'entitledModelIds',
            'prices', connection.config_json -> 'prices'
        )),
        'configured', CASE registry.provider_id
            WHEN 'deepseek' THEN EXISTS (
                SELECT 1 FROM sceneturn.text_provider_settings WHERE id = 'platform' AND api_key_encrypted <> ''
            )
            WHEN 'aliyun-wan' THEN EXISTS (
                SELECT 1 FROM sceneturn.provider_settings WHERE owner_id = p_owner_id::text AND api_key_encrypted <> ''
            )
            WHEN 'sensetime' THEN (SELECT count(*) FROM sceneturn.image_accounts WHERE owner_id = p_owner_id::text) = 3
            ELSE coalesce(connection.api_key_encrypted <> '', false)
        END
    )) INTO v_connections
    FROM (VALUES ('deepseek'), ('aliyun-qwen'), ('sensetime'), ('volcengine-ark'), ('aliyun-wan')) AS registry(provider_id)
    LEFT JOIN sceneturn.model_provider_connections AS connection ON connection.provider_id = registry.provider_id AND (
        (registry.provider_id IN ('deepseek', 'aliyun-qwen') AND connection.scope_key = 'platform')
        OR (registry.provider_id IN ('sensetime', 'volcengine-ark', 'aliyun-wan') AND connection.scope_key = p_owner_id::text
            AND connection.owner_id = p_owner_id::text)
    );
    RETURN jsonb_build_object('connections', v_connections, 'wanWorkspace', v_wan_workspace, 'codexImage', v_codex_image,
        'projectDefaults', v_project.data_json -> 'metadata' -> 'modelDefaults', 'projectRevision', v_project.revision);
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_model_owner_update(
    p_owner_id uuid, p_record_id uuid, p_provider_id text, p_expected_revision integer,
    p_config jsonb, p_encrypted_key text, p_update_wan_workspace boolean
) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_existing sceneturn.model_provider_connections%ROWTYPE;
    v_workspace text;
    v_now double precision;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    IF p_provider_id IS NULL OR p_provider_id NOT IN ('aliyun-wan', 'volcengine-ark') THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'MODEL_PROVIDER_UNSUPPORTED';
    END IF;
    IF p_expected_revision IS NULL OR p_expected_revision < 0 OR p_record_id IS NULL OR
        jsonb_typeof(p_config) IS DISTINCT FROM 'object' OR
        (p_config - ARRAY['region', 'workspaceId', 'entitledModelIds', 'prices']) <> '{}'::jsonb OR
        (p_config ->> 'region') IS DISTINCT FROM 'cn-beijing' OR
        (p_config ? 'workspaceId' AND coalesce(p_config ->> 'workspaceId', '') !~ '^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$') OR
        (p_config ? 'entitledModelIds' AND jsonb_typeof(p_config -> 'entitledModelIds') IS DISTINCT FROM 'array') OR
        (p_config ? 'prices' AND jsonb_typeof(p_config -> 'prices') IS DISTINCT FROM 'object') OR
        (p_encrypted_key IS NOT NULL AND (length(p_encrypted_key) NOT BETWEEN 100 AND 4096
            OR p_encrypted_key !~ '^gAAAAA[A-Za-z0-9_-]+={0,2}$')) THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'MODEL_SETTINGS_INVALID';
    END IF;
    PERFORM pg_advisory_xact_lock(8314610783521762916);
    IF NOT EXISTS (SELECT 1 FROM sceneturn.users WHERE id = p_owner_id::text) THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'MODEL_OWNER_NOT_FOUND';
    END IF;
    SELECT * INTO v_existing FROM sceneturn.model_provider_connections
        WHERE scope_key = p_owner_id::text AND provider_id = p_provider_id FOR UPDATE;
    IF (v_existing.owner_id IS NOT NULL AND v_existing.owner_id <> p_owner_id::text)
        OR coalesce(v_existing.revision, 0) <> p_expected_revision THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'MODEL_CONFIGURATION_CONFLICT';
    END IF;
    v_workspace := p_config ->> 'workspaceId';
    v_now := extract(epoch FROM clock_timestamp());
    IF p_provider_id = 'aliyun-wan' AND (p_encrypted_key IS NOT NULL OR p_update_wan_workspace IS TRUE) THEN
        IF v_workspace IS NULL THEN
            RAISE SQLSTATE 'PT422' USING MESSAGE = 'MODEL_WORKSPACE_REQUIRED';
        END IF;
        UPDATE sceneturn.provider_settings SET api_key_encrypted = coalesce(p_encrypted_key, api_key_encrypted),
            workspace_id = v_workspace, region = 'cn-beijing', updated_at = v_now WHERE owner_id = p_owner_id::text;
        IF NOT FOUND THEN
            IF p_encrypted_key IS NULL THEN
                RAISE SQLSTATE 'PT422' USING MESSAGE = 'MODEL_KEY_REQUIRED';
            END IF;
            INSERT INTO sceneturn.provider_settings(owner_id, api_key_encrypted, workspace_id, region, model, updated_at)
            VALUES (p_owner_id::text, p_encrypted_key, v_workspace, 'cn-beijing', 'wan3.0-video', v_now);
        END IF;
    END IF;
    IF v_existing.id IS NULL THEN
        INSERT INTO sceneturn.model_provider_connections(id, owner_id, scope_key, provider_id, api_key_encrypted, config_json, revision, updated_at)
        VALUES (p_record_id::text, p_owner_id::text, p_owner_id::text, p_provider_id, p_encrypted_key, p_config, 1, v_now);
    ELSE
        UPDATE sceneturn.model_provider_connections SET api_key_encrypted = coalesce(p_encrypted_key, api_key_encrypted),
            config_json = p_config, revision = p_expected_revision + 1, updated_at = v_now WHERE id = v_existing.id;
    END IF;
    RETURN p_expected_revision + 1;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.sceneturn_model_registry(uuid, uuid),
    public.sceneturn_model_owner_update(uuid, uuid, text, integer, jsonb, text, boolean) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.sceneturn_model_registry(uuid, uuid),
    public.sceneturn_model_owner_update(uuid, uuid, text, integer, jsonb, text, boolean) TO service_role;
