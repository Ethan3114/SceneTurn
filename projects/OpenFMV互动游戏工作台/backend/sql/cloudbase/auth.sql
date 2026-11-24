CREATE OR REPLACE FUNCTION public.sceneturn_auth_register(
    p_user_id uuid,
    p_email text,
    p_password_hash text,
    p_token_hash text,
    p_session_seconds integer
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $auth$
DECLARE
    now_epoch double precision;
    registered_email text;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    IF p_session_seconds IS NULL OR p_session_seconds NOT BETWEEN 300 AND 2592000 THEN
        RAISE EXCEPTION 'Invalid session duration' USING ERRCODE = '22023';
    END IF;

    now_epoch := EXTRACT(EPOCH FROM pg_catalog.clock_timestamp())::double precision;
    INSERT INTO sceneturn.users (id, email, password_hash, created_at)
    VALUES (p_user_id::text, pg_catalog.lower(p_email), p_password_hash, now_epoch)
    ON CONFLICT (email) DO NOTHING
    RETURNING email INTO registered_email;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    INSERT INTO sceneturn.sessions (token_hash, user_id, expires_at, created_at)
    VALUES (p_token_hash, p_user_id::text, now_epoch + p_session_seconds, now_epoch);

    RETURN pg_catalog.jsonb_build_object('id', p_user_id, 'email', registered_email);
END;
$auth$;

CREATE OR REPLACE FUNCTION public.sceneturn_auth_login_lookup(
    p_source_hash text,
    p_email text
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $auth$
DECLARE
    now_epoch double precision;
    lock_until double precision;
    account jsonb;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    now_epoch := EXTRACT(EPOCH FROM pg_catalog.clock_timestamp())::double precision;
    SELECT expires_at INTO lock_until
    FROM sceneturn.login_failures
    WHERE source_hash = p_source_hash AND failure_count >= 5 AND expires_at > now_epoch;
    IF FOUND THEN
        RETURN pg_catalog.jsonb_build_object('status', 'locked', 'retry_after', lock_until - now_epoch);
    END IF;

    SELECT pg_catalog.jsonb_build_object('id', id, 'password_hash', password_hash)
    INTO account
    FROM sceneturn.users
    WHERE email = pg_catalog.lower(p_email);

    RETURN pg_catalog.jsonb_build_object('status', 'lookup', 'account', account);
END;
$auth$;

CREATE OR REPLACE FUNCTION public.sceneturn_auth_login_finish(
    p_source_hash text,
    p_email text,
    p_verified_user_id text,
    p_verified_password_hash text,
    p_token_hash text,
    p_session_seconds integer
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $auth$
DECLARE
    now_epoch double precision;
    failures integer;
    lock_until double precision;
    account jsonb;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    IF p_source_hash IS NULL OR p_session_seconds IS NULL OR p_session_seconds NOT BETWEEN 300 AND 2592000 THEN
        RAISE EXCEPTION 'Invalid login request' USING ERRCODE = '22023';
    END IF;

    PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(p_source_hash, 49917243583));
    now_epoch := EXTRACT(EPOCH FROM pg_catalog.clock_timestamp())::double precision;
    SELECT failure_count, expires_at INTO failures, lock_until
    FROM sceneturn.login_failures
    WHERE source_hash = p_source_hash AND expires_at > now_epoch
    FOR UPDATE;
    IF failures >= 5 THEN
        RETURN pg_catalog.jsonb_build_object('status', 'locked', 'retry_after', lock_until - now_epoch);
    END IF;

    DELETE FROM sceneturn.login_failures
    WHERE source_hash IN (
        SELECT source_hash
        FROM sceneturn.login_failures
        WHERE expires_at <= now_epoch
        ORDER BY expires_at
        LIMIT 128
        FOR UPDATE SKIP LOCKED
    );

    IF p_verified_user_id IS NOT NULL AND p_verified_password_hash IS NOT NULL AND p_token_hash IS NOT NULL THEN
        SELECT pg_catalog.jsonb_build_object('id', id, 'email', email)
        INTO account
        FROM sceneturn.users
        WHERE id = p_verified_user_id AND email = pg_catalog.lower(p_email) AND password_hash = p_verified_password_hash
        FOR SHARE;
    END IF;

    IF account IS NOT NULL THEN
        DELETE FROM sceneturn.login_failures WHERE source_hash = p_source_hash;
        INSERT INTO sceneturn.sessions (token_hash, user_id, expires_at, created_at)
        VALUES (p_token_hash, p_verified_user_id, now_epoch + p_session_seconds, now_epoch);
        RETURN pg_catalog.jsonb_build_object('status', 'accepted', 'user', account);
    END IF;

    failures := COALESCE(failures, 0) + 1;
    INSERT INTO sceneturn.login_failures (source_hash, failure_count, expires_at)
    VALUES (p_source_hash, failures, now_epoch + 900)
    ON CONFLICT (source_hash) DO UPDATE
    SET failure_count = EXCLUDED.failure_count, expires_at = EXCLUDED.expires_at;
    IF failures >= 5 THEN
        RETURN pg_catalog.jsonb_build_object('status', 'locked', 'retry_after', 900);
    END IF;
    RETURN pg_catalog.jsonb_build_object('status', 'invalid');
END;
$auth$;

CREATE OR REPLACE FUNCTION public.sceneturn_auth_session_user(
    p_token_hash text
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $auth$
DECLARE
    account jsonb;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    SELECT pg_catalog.jsonb_build_object('id', u.id, 'email', u.email)
    INTO account
    FROM sceneturn.sessions AS s
    JOIN sceneturn.users AS u ON u.id = s.user_id
    WHERE s.token_hash = p_token_hash AND s.expires_at > EXTRACT(EPOCH FROM pg_catalog.clock_timestamp());
    RETURN account;
END;
$auth$;

CREATE OR REPLACE FUNCTION public.sceneturn_auth_logout(
    p_token_hash text
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $auth$
BEGIN
    PERFORM sceneturn.require_gateway_access();
    DELETE FROM sceneturn.sessions
    WHERE token_hash = p_token_hash AND expires_at > EXTRACT(EPOCH FROM pg_catalog.clock_timestamp());
    RETURN FOUND;
END;
$auth$;

REVOKE EXECUTE ON FUNCTION
    public.sceneturn_auth_register(uuid, text, text, text, integer),
    public.sceneturn_auth_login_lookup(text, text),
    public.sceneturn_auth_login_finish(text, text, text, text, text, integer),
    public.sceneturn_auth_session_user(text),
    public.sceneturn_auth_logout(text)
FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION
    public.sceneturn_auth_register(uuid, text, text, text, integer),
    public.sceneturn_auth_login_lookup(text, text),
    public.sceneturn_auth_login_finish(text, text, text, text, text, integer),
    public.sceneturn_auth_session_user(text),
    public.sceneturn_auth_logout(text)
TO service_role;
