-- #69. The files of dropped managed tables, until they are deleted. A drop removes the table from the
-- catalog and records its root here in the same transaction, so the table is gone for every client at
-- once; its files are deleted afterwards (by the dropping request on EKS, by the purge function in the
-- serverless form) and the row goes when they are. No foreign key: the table's row is gone.
-- not_before: a claim holds the row until then (longer than any purge runs); a failed purge sets it to
-- the failure's time, so the next sweep tries again.
CREATE TABLE table_purges (
    id               bigserial PRIMARY KEY,
    table_id         uuid NOT NULL,
    table_name       text NOT NULL,
    path             text NOT NULL,
    dropped_at       timestamptz NOT NULL,
    dropped_by       text NOT NULL,
    request_id       text,
    attempts         integer NOT NULL DEFAULT 0,
    last_attempt_at  timestamptz,
    last_error       text,
    not_before       timestamptz NOT NULL
);

CREATE INDEX table_purges_due ON table_purges (not_before);

-- A consistency check answered in pages (#69): where it is and the report so far, between one page and
-- the next, under the id its page token names. Kept a day at most; the last page deletes it.
CREATE TABLE consistency_checks (
    id          uuid PRIMARY KEY,
    table_id    uuid NOT NULL,
    progress    text NOT NULL,
    version     integer NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
