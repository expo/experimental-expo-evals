CREATE TABLE tasks (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  revision INTEGER NOT NULL,
  base_commit TEXT,
  committed_revision INTEGER,
  files_json TEXT NOT NULL,
  base_files_json TEXT NOT NULL DEFAULT '[]',
  updated_at TEXT NOT NULL,
  updated_by TEXT NOT NULL,
  change_id TEXT NOT NULL,
  commit_token TEXT,
  commit_expires INTEGER
);

CREATE TABLE task_revisions (
  task_id TEXT NOT NULL REFERENCES tasks(id),
  revision INTEGER NOT NULL,
  name TEXT NOT NULL,
  files_json TEXT NOT NULL,
  base_commit TEXT,
  action TEXT NOT NULL CHECK (action IN ('create', 'save', 'commit')),
  created_at TEXT NOT NULL,
  created_by TEXT NOT NULL,
  PRIMARY KEY (task_id, revision)
);

CREATE TABLE runs (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id),
  commit_sha TEXT NOT NULL,
  files_json TEXT NOT NULL,
  agent TEXT NOT NULL,
  model TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  callback_hash TEXT,
  detail_json TEXT,
  error TEXT
);

CREATE INDEX runs_created_at ON runs(created_at DESC);
CREATE UNIQUE INDEX one_active_run ON runs((1)) WHERE status IN ('queued', 'running');
