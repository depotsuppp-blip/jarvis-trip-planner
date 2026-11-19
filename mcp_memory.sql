-- Jarvis's long-term memory as a knowledge graph (mcp_memory.py).
--
-- mcp_memory.py runs this file itself the first time it finds the tables
-- missing; `python mcp_memory.py --migrate` runs it by hand, and it can be
-- pasted into the Neon SQL editor as it is. It is idempotent: every statement
-- is IF NOT EXISTS, so running it again changes nothing.
--
-- WHY A SCHEMA OF ITS OWN. The database is shared with the Trip Planner web
-- app, whose `prisma db push` manages the `public` schema: there it silently
-- DROPS a table schema.prisma does not know while the table is empty, and
-- refuses to run at all once that table holds rows. Prisma does not look at
-- other schemas, so jarvis_memory is invisible to it - and so is the vector
-- extension, created here too.
--
-- ONE MEMORY PER PERSON. `scope` is the caller ("owner", or "user:<id>" for
-- another person signed in to the web app); every query filters on it.

BEGIN;

-- Two Jarvis processes (voice and web) may start on an empty database at once.
SELECT pg_advisory_xact_lock(7305916402615023);

CREATE SCHEMA IF NOT EXISTS jarvis_memory;
CREATE EXTENSION IF NOT EXISTS vector SCHEMA jarvis_memory;

-- The `vector` type is wherever the extension lives: here when this file
-- created it, elsewhere (usually public) when it was there already.
DO $$
DECLARE
    home text;
BEGIN
    SELECT n.nspname INTO home
    FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
    WHERE e.extname = 'vector';
    PERFORM set_config('search_path', 'jarvis_memory, ' || quote_ident(home), true);
END
$$;

-- Nodes: a person, place, thing, date, preference... The person themselves is "me".
CREATE TABLE IF NOT EXISTS jarvis_memory.entities (
    id           uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    scope        varchar(160) NOT NULL,
    name         varchar(120) NOT NULL,
    name_key     text         NOT NULL,   -- the name case- and space-folded: one node per name
    type         varchar(40)  NOT NULL DEFAULT '',
    description  text         NOT NULL DEFAULT '',
    embedding    vector(1536),            -- text-embedding-3-small of "name (type): description"
    created_at   timestamptz  NOT NULL DEFAULT now(),
    updated_at   timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT entities_scope_name_key UNIQUE (scope, name_key)
);

-- Edges: (source) relation_type (target), e.g. Nok sister_of me.
CREATE TABLE IF NOT EXISTS jarvis_memory.relations (
    id             uuid             PRIMARY KEY DEFAULT gen_random_uuid(),
    scope          varchar(160)     NOT NULL,
    source_id      uuid             NOT NULL REFERENCES jarvis_memory.entities (id) ON DELETE CASCADE,
    target_id      uuid             NOT NULL REFERENCES jarvis_memory.entities (id) ON DELETE CASCADE,
    relation_type  varchar(60)      NOT NULL,
    weight         double precision NOT NULL DEFAULT 1,  -- +1 each time the same fact is remembered again
    created_at     timestamptz      NOT NULL DEFAULT now(),
    updated_at     timestamptz      NOT NULL DEFAULT now(),
    CONSTRAINT relations_unique UNIQUE (source_id, target_id, relation_type),
    CONSTRAINT relations_not_to_itself CHECK (source_id <> target_id)
);

-- relations_unique already indexes source_id first; the other direction of a hop:
CREATE INDEX IF NOT EXISTS relations_target ON jarvis_memory.relations (target_id);

-- TEMPORAL FACTS (added 2026-11-19; a database made before then is upgraded by these lines).
-- A fact that ended is not deleted: "me building Jarvis" becomes 'completed' when the person
-- says they finished it, or 'archived' when it no longer holds ("I left Sony"), and the new
-- fact is 'active' beside it. recall shows the status and the date it last changed, so the
-- brain can tell the latest from the history.
ALTER TABLE jarvis_memory.relations ADD COLUMN IF NOT EXISTS status     varchar(16) NOT NULL DEFAULT 'active';
ALTER TABLE jarvis_memory.relations ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE jarvis_memory.relations ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'relations_status_known' AND conrelid = 'jarvis_memory.relations'::regclass
    ) THEN
        ALTER TABLE jarvis_memory.relations
            ADD CONSTRAINT relations_status_known CHECK (status IN ('active', 'completed', 'archived'));
    END IF;
END
$$;

-- No approximate (HNSW / IVFFlat) index on purpose: a search only ever looks at
-- one person's rows (at most 2000, mcp_memory.MAX_ENTITIES), found through
-- entities_scope_name_key, and an exact scan of those is fast. An approximate
-- index filtered by scope afterwards can return fewer than the rows asked for.

COMMIT;
