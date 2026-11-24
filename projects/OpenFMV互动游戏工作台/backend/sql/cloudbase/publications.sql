CREATE OR REPLACE FUNCTION sceneturn.publication_record(p_value sceneturn.publications)
RETURNS jsonb
LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $$
    SELECT jsonb_build_object(
        'slug', p_value.slug, 'title', p_value.title, 'description', p_value.description,
        'created_at', p_value.created_at, 'poster_asset_id', p_value.poster_asset_id,
        'trailer_asset_id', p_value.trailer_asset_id, 'published', p_value.published
    );
$$;

CREATE OR REPLACE FUNCTION sceneturn.publication_game_ready(p_transfer sceneturn.transfer_jobs)
RETURNS boolean
LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $$
    SELECT p_transfer.kind = 'game' AND p_transfer.status = 'ready'
        AND p_transfer.source_revision IS NOT NULL AND p_transfer.confirmation_hash IS NOT NULL;
$$;

CREATE OR REPLACE FUNCTION sceneturn.publication_visible(p_slug text)
RETURNS sceneturn.publications
LANGUAGE plpgsql STABLE SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_publication sceneturn.publications%ROWTYPE;
BEGIN
    SELECT p.* INTO v_publication
    FROM sceneturn.publications AS p
    JOIN sceneturn.transfer_jobs AS t ON p.transfer_id = t.id
    WHERE p.slug = p_slug AND p.published AND sceneturn.publication_game_ready(t);
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'PUBLICATION_NOT_FOUND';
    END IF;
    RETURN v_publication;
END;
$$;

CREATE OR REPLACE FUNCTION sceneturn.publication_game_member(p_transfer sceneturn.transfer_jobs, p_name text)
RETURNS jsonb
LANGUAGE plpgsql STABLE SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_proof jsonb;
BEGIN
    IF NOT sceneturn.publication_game_ready(p_transfer) THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'GAME_VERSION_NOT_FOUND';
    END IF;
    IF p_name IS NULL OR (p_name NOT IN ('index.html', 'game.json') AND NOT EXISTS (
        SELECT 1 FROM jsonb_array_elements(p_transfer.private_json::jsonb -> 'files') AS f
        WHERE f ->> 'path' = p_name
    )) THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'GAME_FILE_NOT_FOUND';
    END IF;
    SELECT jsonb_build_object('path', p_name, 'bytes', f -> 'bytes', 'sha256', f -> 'sha256') INTO v_proof
    FROM jsonb_array_elements(p_transfer.private_json::jsonb -> 'fileManifest') AS f
    WHERE f ->> 'path' = p_name;
    IF v_proof IS NULL THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'GAME_FILE_PROOF_MISSING';
    END IF;
    RETURN jsonb_build_object(
        'id', p_transfer.id, 'status', p_transfer.status, 'kind', p_transfer.kind, 'private_json',
        jsonb_build_object('directory', p_transfer.private_json -> 'directory',
            'files', jsonb_build_array(jsonb_build_object('path', p_name)),
            'fileManifest', jsonb_build_array(v_proof), 'archiveSha256', p_transfer.private_json -> 'archiveSha256')
    );
END;
$$;

CREATE OR REPLACE FUNCTION sceneturn.publication_asset(
    p_transfer sceneturn.transfer_jobs, p_asset_id text, p_kind text
) RETURNS jsonb
LANGUAGE plpgsql STABLE SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_asset jsonb;
BEGIN
    SELECT jsonb_build_object('relpath', relpath, 'mime_type', mime_type) INTO v_asset
    FROM sceneturn.assets
    WHERE id = p_asset_id AND owner_id = p_transfer.owner_id AND project_id = p_transfer.project_id
        AND starts_with(mime_type, p_kind || '/') AND mime_type <> 'image/svg+xml';
    IF v_asset IS NULL THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'PUBLICATION_MEDIA_NOT_FOUND';
    END IF;
    RETURN v_asset;
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_publication_list()
RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM sceneturn.require_gateway_access();
    RETURN (
        SELECT COALESCE(jsonb_agg(p.record ORDER BY p.created_at DESC, p.slug), '[]'::jsonb)
        FROM (
            SELECT sceneturn.publication_record(p) AS record, p.created_at, p.slug
            FROM sceneturn.publications AS p
            JOIN sceneturn.transfer_jobs AS t ON p.transfer_id = t.id
            WHERE p.published AND sceneturn.publication_game_ready(t)
            ORDER BY p.created_at DESC, p.slug LIMIT 100
        ) AS p
    );
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_publication_read(p_slug text)
RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM sceneturn.require_gateway_access();
    RETURN sceneturn.publication_record(sceneturn.publication_visible(p_slug));
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_publication_game(p_slug text, p_name text)
RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_transfer sceneturn.transfer_jobs%ROWTYPE;
    v_publication sceneturn.publications%ROWTYPE;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    v_publication := sceneturn.publication_visible(p_slug);
    SELECT * INTO STRICT v_transfer FROM sceneturn.transfer_jobs WHERE id = v_publication.transfer_id;
    RETURN sceneturn.publication_game_member(v_transfer, p_name);
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_publication_media(p_slug text, p_role text)
RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_transfer sceneturn.transfer_jobs%ROWTYPE;
    v_publication sceneturn.publications%ROWTYPE;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    v_publication := sceneturn.publication_visible(p_slug);
    SELECT * INTO STRICT v_transfer FROM sceneturn.transfer_jobs WHERE id = v_publication.transfer_id;
    IF p_role = 'poster' THEN
        RETURN sceneturn.publication_asset(v_transfer, v_publication.poster_asset_id, 'image');
    ELSIF p_role = 'trailer' THEN
        RETURN sceneturn.publication_asset(v_transfer, v_publication.trailer_asset_id, 'video');
    END IF;
    RAISE SQLSTATE 'PT404' USING MESSAGE = 'PUBLICATION_MEDIA_NOT_FOUND';
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_publication_owner(p_owner_id text, p_transfer_id text)
RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM sceneturn.require_gateway_access();
    IF NOT EXISTS (
        SELECT 1 FROM sceneturn.transfer_jobs WHERE id = p_transfer_id AND owner_id = p_owner_id
            AND kind IN ('import', 'backup', 'game') AND status <> 'awaiting_upload'
    ) THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'TRANSFER_NOT_FOUND';
    END IF;
    RETURN (SELECT sceneturn.publication_record(p) FROM sceneturn.publications AS p WHERE transfer_id = p_transfer_id);
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_publication_prepare(p_owner_id text, p_transfer_id text, p_title text)
RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_transfer sceneturn.transfer_jobs%ROWTYPE;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    SELECT * INTO v_transfer FROM sceneturn.transfer_jobs
    WHERE id = p_transfer_id AND owner_id = p_owner_id
        AND kind IN ('import', 'backup', 'game') AND status <> 'awaiting_upload';
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'TRANSFER_NOT_FOUND';
    END IF;
    IF p_title IS NULL OR btrim(p_title) = '' OR length(p_title) > 160 THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'PUBLICATION_TITLE_REQUIRED';
    END IF;
    RETURN sceneturn.publication_game_member(v_transfer, 'index.html');
END;
$$;

CREATE OR REPLACE FUNCTION public.sceneturn_publication_save(
    p_owner_id text, p_transfer_id text, p_new_slug uuid, p_published boolean, p_title text, p_description text,
    p_poster_asset_id text, p_trailer_asset_id text, p_expected_archive_sha256 text, p_expected_index_sha256 text
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_transfer sceneturn.transfer_jobs%ROWTYPE;
    v_publication sceneturn.publications%ROWTYPE;
    v_game jsonb;
BEGIN
    PERFORM sceneturn.require_gateway_access();
    SELECT * INTO v_transfer FROM sceneturn.transfer_jobs
    WHERE id = p_transfer_id AND owner_id = p_owner_id
        AND kind IN ('import', 'backup', 'game') AND status <> 'awaiting_upload'
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'TRANSFER_NOT_FOUND';
    END IF;
    IF p_published IS FALSE THEN
        UPDATE sceneturn.publications SET published = false
        WHERE transfer_id = p_transfer_id RETURNING * INTO v_publication;
        IF NOT FOUND THEN
            RETURN NULL;
        END IF;
        RETURN sceneturn.publication_record(v_publication);
    END IF;
    IF p_title IS NULL OR btrim(p_title) = '' OR length(p_title) > 160 THEN
        RAISE SQLSTATE 'PT422' USING MESSAGE = 'PUBLICATION_TITLE_REQUIRED';
    END IF;

    v_game := sceneturn.publication_game_member(v_transfer, 'index.html') -> 'private_json';
    IF p_published IS DISTINCT FROM true OR p_expected_archive_sha256 IS NULL OR p_expected_index_sha256 IS NULL
        OR (v_game ->> 'archiveSha256') IS DISTINCT FROM p_expected_archive_sha256
        OR (v_game -> 'fileManifest' -> 0 ->> 'sha256') IS DISTINCT FROM p_expected_index_sha256 THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'GAME_FILE_CHANGED';
    END IF;
    IF p_poster_asset_id IS NOT NULL THEN
        PERFORM sceneturn.publication_asset(v_transfer, p_poster_asset_id, 'image');
    END IF;
    IF p_trailer_asset_id IS NOT NULL THEN
        PERFORM sceneturn.publication_asset(v_transfer, p_trailer_asset_id, 'video');
    END IF;

    INSERT INTO sceneturn.publications (slug, transfer_id, title, description, poster_asset_id, trailer_asset_id, published, created_at)
    VALUES (p_new_slug::text, p_transfer_id, p_title, p_description, p_poster_asset_id, p_trailer_asset_id, true,
        extract(epoch FROM clock_timestamp()))
    ON CONFLICT (transfer_id) DO UPDATE
        SET title = EXCLUDED.title, description = EXCLUDED.description, poster_asset_id = EXCLUDED.poster_asset_id,
            trailer_asset_id = EXCLUDED.trailer_asset_id, published = true
    RETURNING * INTO v_publication;
    RETURN sceneturn.publication_record(v_publication);
END;
$$;

REVOKE EXECUTE ON FUNCTION
    sceneturn.publication_record(sceneturn.publications),
    sceneturn.publication_game_ready(sceneturn.transfer_jobs),
    sceneturn.publication_visible(text),
    sceneturn.publication_game_member(sceneturn.transfer_jobs, text),
    sceneturn.publication_asset(sceneturn.transfer_jobs, text, text)
FROM PUBLIC, anon, authenticated, service_role;

REVOKE EXECUTE ON FUNCTION
    public.sceneturn_publication_list(),
    public.sceneturn_publication_read(text),
    public.sceneturn_publication_game(text, text),
    public.sceneturn_publication_media(text, text),
    public.sceneturn_publication_owner(text, text),
    public.sceneturn_publication_prepare(text, text, text),
    public.sceneturn_publication_save(text, text, uuid, boolean, text, text, text, text, text, text)
FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION
    public.sceneturn_publication_list(),
    public.sceneturn_publication_read(text),
    public.sceneturn_publication_game(text, text),
    public.sceneturn_publication_media(text, text),
    public.sceneturn_publication_owner(text, text),
    public.sceneturn_publication_prepare(text, text, text),
    public.sceneturn_publication_save(text, text, uuid, boolean, text, text, text, text, text, text)
TO service_role;
