-- Service-identity grants (issue #21). A grant allows or denies one privilege level on the whole
-- catalog, one database, or one object in it. Levels nest: MANAGE includes WRITE includes READ.
-- An ALLOW of a level permits that level and below; a DENY of a level forbids that level and
-- above, and wins over every ALLOW. A READ allow on a table may name the only columns readable.
-- As M0 requires, every grant names its purpose, environment, approver, approval reference and
-- expiry; an expired grant no longer counts.
CREATE TABLE catalog_grants (
    id             bigserial PRIMARY KEY,
    principal      text NOT NULL REFERENCES principals (name),
    scope          text NOT NULL CHECK (scope IN ('CATALOG', 'DATABASE', 'TABLE', 'VIEW', 'FUNCTION')),
    database_name  text,
    object_name    text,
    privilege      text NOT NULL CHECK (privilege IN ('READ', 'WRITE', 'MANAGE')),
    effect         text NOT NULL CHECK (effect IN ('ALLOW', 'DENY')),
    columns        text[],
    purpose        text NOT NULL,
    environment    text NOT NULL,
    approved_by    text NOT NULL,
    approval       text NOT NULL,
    expires_at     timestamptz NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    created_by     text NOT NULL,
    CHECK ((scope = 'CATALOG') = (database_name IS NULL)),
    CHECK ((scope IN ('CATALOG', 'DATABASE')) = (object_name IS NULL)),
    CHECK (columns IS NULL OR (scope = 'TABLE' AND privilege = 'READ' AND effect = 'ALLOW'))
);

CREATE UNIQUE INDEX catalog_grants_unique
    ON catalog_grants (principal, scope, coalesce(database_name, ''), coalesce(object_name, ''),
                       privilege, effect);
CREATE INDEX catalog_grants_principal ON catalog_grants (principal);

-- The policy version: one row, advanced by every grant change, recorded with every decision.
CREATE TABLE catalog_policy (
    id       integer PRIMARY KEY CHECK (id = 1),
    version  bigint NOT NULL
);
INSERT INTO catalog_policy (id, version) VALUES (1, 0);
