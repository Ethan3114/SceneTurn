REVOKE ALL ON SCHEMA sceneturn FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON ALL TABLES IN SCHEMA sceneturn FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA sceneturn FROM PUBLIC, anon, authenticated, service_role;

DO $setup$
DECLARE
    table_name text;
BEGIN
    FOR table_name IN
        SELECT c.relname
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'sceneturn' AND c.relkind IN ('r', 'p')
    LOOP
        EXECUTE 'ALTER TABLE sceneturn.' || pg_catalog.quote_ident(table_name) || ' ENABLE ROW LEVEL SECURITY';
    END LOOP;
END;
$setup$;
