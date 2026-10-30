-- #93. The files of removed snapshots, tags and branches, until they are deleted. A rollback, a tag's
-- deletion or a branch's deletion records here what it removes (its roots: the snapshot, tag and changelog
-- files it deletes, as they were) before it removes them, and the request answers once the change itself is
-- made; the files those roots referenced are deleted afterwards (by the request itself on EKS, by the purge
-- function in the serverless form), only those that no snapshot, tag or changelog of any branch references
-- when they are deleted. No foreign key: the record outlives nothing it needs, and a dropped table's record
-- is removed by its purge.
-- applied: the operation took effect. A record not applied (a request that stopped after recording, or one
-- answered 503 to be sent again) becomes due a lease and its operation's delay after it was made, and its
-- purge deletes only what nothing references then. target: what the operation was asked for, which a
-- request sent again finds its record by. due_at: when the record is due, which the backlog counts from.
-- not_before: as in table_purges, a claim holds the row until then; a failed purge sets it to the failure's
-- time.
CREATE TABLE file_purges (
    id               bigserial PRIMARY KEY,
    table_id         uuid NOT NULL,
    table_name       text NOT NULL,
    path             text NOT NULL,
    branch           text NOT NULL,
    operation        text NOT NULL CHECK (operation IN ('ROLLBACK', 'DROP_TAG', 'DROP_BRANCH')),
    target           text NOT NULL,
    created_at       timestamptz NOT NULL,
    created_by       text NOT NULL,
    request_id       text,
    applied          boolean NOT NULL DEFAULT false,
    attempts         integer NOT NULL DEFAULT 0,
    last_attempt_at  timestamptz,
    last_error       text,
    due_at           timestamptz NOT NULL,
    not_before       timestamptz NOT NULL
);

CREATE INDEX file_purges_due ON file_purges (not_before);
CREATE INDEX file_purges_table ON file_purges (table_id);
CREATE INDEX file_purges_open ON file_purges (table_id, branch, operation, target) WHERE NOT applied;

-- A record's roots: each file the operation removes, its kind, name, modification time and content. A
-- branch's deletion also keeps the branch's latest schema (SCHEMA), to read the branch's manifests by.
CREATE TABLE file_purge_roots (
    purge_id  bigint NOT NULL REFERENCES file_purges (id) ON DELETE CASCADE,
    kind      text NOT NULL CHECK (kind IN ('SNAPSHOT', 'TAG', 'CHANGELOG', 'SCHEMA')),
    name      text NOT NULL,
    modified  bigint NOT NULL,
    content   text NOT NULL,
    PRIMARY KEY (purge_id, kind, name, modified)
);

-- The last rollback of each branch (#93): who asked for what, and when. A rollback sent again by the same
-- caller within minutes is that rollback's retry, and is not applied twice.
ALTER TABLE table_branches ADD COLUMN last_rollback jsonb;
