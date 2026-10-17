CREATE TABLE scan_revisions (revision TEXT PRIMARY KEY, source_sequence INTEGER NOT NULL, commit_sha TEXT NOT NULL, published_at TEXT NOT NULL, received_at TEXT NOT NULL, payload TEXT NOT NULL);
--> statement-breakpoint
CREATE TABLE scan_current (singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision TEXT NOT NULL REFERENCES scan_revisions(revision));
--> statement-breakpoint
CREATE INDEX scan_revision_source ON scan_revisions(source_sequence,published_at);
