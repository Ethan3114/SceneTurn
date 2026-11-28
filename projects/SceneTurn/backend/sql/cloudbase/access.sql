CREATE TABLE sceneturn.gateway_credentials (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    token_sha256 bytea NOT NULL CHECK (length(token_sha256) = 32)
);
ALTER TABLE sceneturn.gateway_credentials ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE sceneturn.gateway_credentials FROM PUBLIC, anon, authenticated, service_role;

CREATE FUNCTION sceneturn.require_gateway_access()
RETURNS void
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    claims jsonb := COALESCE(NULLIF(current_setting('request.jwt.claims', true), ''), '{}')::jsonb;
    headers jsonb := COALESCE(NULLIF(current_setting('request.headers', true), ''), '{}')::jsonb;
    scope_token text := headers ->> 'x-sceneturn-database-scope';
BEGIN
    IF (claims ->> 'role') IS DISTINCT FROM 'service_role'
       OR scope_token IS NULL OR length(scope_token) NOT BETWEEN 43 AND 128 THEN
        RAISE EXCEPTION 'Forbidden' USING ERRCODE = '42501';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM sceneturn.gateway_credentials
        WHERE singleton AND token_sha256 = sha256(convert_to(scope_token, 'UTF8'))
    ) THEN
        RAISE EXCEPTION 'Forbidden' USING ERRCODE = '42501';
    END IF;
END;
$function$;
REVOKE EXECUTE ON FUNCTION sceneturn.require_gateway_access() FROM PUBLIC, anon, authenticated, service_role;

CREATE FUNCTION public.sceneturn_schema_revision()
RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    PERFORM sceneturn.require_gateway_access();
    RETURN (SELECT version_num FROM sceneturn.alembic_version);
END;
$function$;
REVOKE EXECUTE ON FUNCTION public.sceneturn_schema_revision() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.sceneturn_schema_revision() TO service_role;
