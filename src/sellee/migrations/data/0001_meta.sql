-- Business-data DB, initial schema. A trivial but real table so startup migration is
-- exercised end to end; real entities land in later workstreams' own migrations.
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
