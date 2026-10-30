-- Initial colvenna-catalog metadata schema.
-- Catalog objects keep the epoch-millisecond timestamps of the Paimon REST contract; authentication
-- and audit records use timestamptz.

CREATE TABLE catalog_databases (
    name        text PRIMARY KEY,
    id          uuid NOT NULL UNIQUE,
    location    text NOT NULL,
    options     jsonb NOT NULL DEFAULT '{}'::jsonb,
    owner       text,
    created_at  bigint NOT NULL,
    created_by  text NOT NULL,
    updated_at  bigint NOT NULL,
    updated_by  text NOT NULL
);

-- Table registry. schema_json is the Paimon Schema (without the path option) that answers GetTable;
-- it is updated under the same table lock as the schema-N file in the table directory.
CREATE TABLE catalog_tables (
    id             uuid PRIMARY KEY,
    database_name  text NOT NULL REFERENCES catalog_databases (name),
    name           text NOT NULL,
    path           text NOT NULL,
    external       boolean NOT NULL,
    table_type     text NOT NULL,
    schema_id      bigint NOT NULL,
    schema_json    text NOT NULL,
    state          text NOT NULL DEFAULT 'ACTIVE' CHECK (state IN ('ACTIVE', 'FENCED')),
    state_reason   text,
    owner          text,
    created_at     bigint NOT NULL,
    created_by     text NOT NULL,
    updated_at     bigint NOT NULL,
    updated_by     text NOT NULL,
    UNIQUE (database_name, name)
);

CREATE UNIQUE INDEX catalog_tables_managed_path ON catalog_tables (path) WHERE NOT external;

-- Latest published snapshot per table branch. The row lock serializes commits on one branch.
CREATE TABLE table_branches (
    table_id                 uuid NOT NULL REFERENCES catalog_tables (id) ON DELETE CASCADE,
    branch                   text NOT NULL,
    snapshot_id              bigint,
    snapshot_uuid            text,
    snapshot_json            text,
    record_count             bigint NOT NULL DEFAULT 0,
    file_size_in_bytes       bigint NOT NULL DEFAULT 0,
    file_count               bigint NOT NULL DEFAULT 0,
    last_file_creation_time  bigint NOT NULL DEFAULT 0,
    updated_at               bigint NOT NULL,
    updated_by               text NOT NULL,
    PRIMARY KEY (table_id, branch)
);

-- Commit intents are committed on their own before the snapshot file is published, so the outcome
-- of a commit can be decided after a server crash or a lost response.
CREATE TABLE commit_intents (
    id                  bigserial PRIMARY KEY,
    table_id            uuid NOT NULL,
    branch              text NOT NULL,
    snapshot_id         bigint NOT NULL,
    snapshot_uuid       text,
    base_snapshot_uuid  text,
    commit_user         text,
    commit_identifier   bigint,
    statistics_json     text,
    principal           text NOT NULL,
    request_id          text NOT NULL,
    state               text NOT NULL CHECK (state IN ('PENDING', 'PUBLISHED', 'ABORTED')),
    resolution          text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    resolved_at         timestamptz
);

CREATE INDEX commit_intents_pending ON commit_intents (table_id, branch) WHERE state = 'PENDING';
CREATE INDEX commit_intents_snapshot ON commit_intents (table_id, branch, snapshot_id);

-- Partitions and statistics of tables whose partitions live in the catalog
-- (metastore.partitioned-table=true).
CREATE TABLE table_partitions (
    table_id                 uuid NOT NULL REFERENCES catalog_tables (id) ON DELETE CASCADE,
    branch                   text NOT NULL,
    spec_name                text NOT NULL,
    spec_json                text NOT NULL,
    record_count             bigint NOT NULL,
    file_size_in_bytes       bigint NOT NULL,
    file_count               bigint NOT NULL,
    last_file_creation_time  bigint NOT NULL,
    total_buckets            integer NOT NULL,
    done                     boolean NOT NULL DEFAULT false,
    options                  jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at               bigint NOT NULL,
    created_by               text NOT NULL,
    updated_at               bigint NOT NULL,
    updated_by               text NOT NULL,
    PRIMARY KEY (table_id, branch, spec_name)
);

CREATE TABLE catalog_views (
    database_name  text NOT NULL REFERENCES catalog_databases (name),
    name           text NOT NULL,
    id             uuid NOT NULL UNIQUE,
    schema_json    text NOT NULL,
    owner          text,
    created_at     bigint NOT NULL,
    created_by     text NOT NULL,
    updated_at     bigint NOT NULL,
    updated_by     text NOT NULL,
    PRIMARY KEY (database_name, name)
);

CREATE TABLE catalog_functions (
    database_name  text NOT NULL REFERENCES catalog_databases (name),
    name           text NOT NULL,
    id             uuid NOT NULL UNIQUE,
    function_json  text NOT NULL,
    owner          text,
    created_at     bigint NOT NULL,
    created_by     text NOT NULL,
    updated_at     bigint NOT NULL,
    updated_by     text NOT NULL,
    PRIMARY KEY (database_name, name)
);

-- Service identities and API keys. A token is returned in plain text only when it is issued; only its
-- SHA-256 is stored.
CREATE TABLE principals (
    name         text PRIMARY KEY,
    description  text,
    is_admin     boolean NOT NULL DEFAULT false,
    created_at   timestamptz NOT NULL DEFAULT now(),
    created_by   text NOT NULL,
    disabled_at  timestamptz,
    disabled_by  text
);

CREATE TABLE api_tokens (
    id            uuid PRIMARY KEY,
    principal     text NOT NULL REFERENCES principals (name),
    token_hash    bytea NOT NULL UNIQUE,
    description   text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    created_by    text NOT NULL,
    expires_at    timestamptz NOT NULL,
    revoked_at    timestamptz,
    revoked_by    text
);

CREATE INDEX api_tokens_principal ON api_tokens (principal);

CREATE TABLE audit_events (
    id             bigserial PRIMARY KEY,
    event_time     timestamptz NOT NULL DEFAULT now(),
    request_id     text NOT NULL,
    principal      text,
    action         text NOT NULL,
    resource_type  text NOT NULL,
    resource       text,
    outcome        text NOT NULL CHECK (outcome IN ('SUCCEEDED', 'FAILED', 'DENIED')),
    http_status    integer NOT NULL,
    detail         jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX audit_events_time ON audit_events (event_time);
CREATE INDEX audit_events_resource ON audit_events (resource_type, resource, event_time);
