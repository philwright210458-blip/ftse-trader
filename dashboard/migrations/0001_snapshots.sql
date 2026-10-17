CREATE TABLE snapshot_revisions (
  revision TEXT PRIMARY KEY,
  generated_at TEXT NOT NULL UNIQUE,
  received_at TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE snapshot_current (
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  revision TEXT NOT NULL REFERENCES snapshot_revisions(revision),
  generated_at TEXT NOT NULL
);
CREATE INDEX snapshot_revision_time ON snapshot_revisions(generated_at);
