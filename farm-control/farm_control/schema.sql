-- Separate PostgreSQL database. Never run against a Kimaki/OpenCode database.
CREATE TABLE IF NOT EXISTS farm_settings (
    id integer PRIMARY KEY CHECK (id = 1),
    global_limit integer NOT NULL CHECK (global_limit BETWEEN 1 AND 120)
);
INSERT INTO farm_settings VALUES (1, 4) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS farm_workers (
    id text PRIMARY KEY,
    instance uuid,
    last_seen timestamptz,
    last_scan timestamptz,
    scan_sequence bigint NOT NULL DEFAULT -1,
    concurrency integer NOT NULL DEFAULT 2 CHECK (concurrency BETWEEN 1 AND 32),
    draining boolean NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS farm_devices (
    serial text PRIMARY KEY,
    project text NOT NULL,
    name text NOT NULL,
    worker text NOT NULL REFERENCES farm_workers(id),
    enabled boolean NOT NULL DEFAULT false,
    quarantined boolean NOT NULL DEFAULT false,
    epoch bigint NOT NULL DEFAULT 0,
    status text NOT NULL DEFAULT 'UNKNOWN',
    observed_at timestamptz,
    transport_id integer
);
CREATE TABLE IF NOT EXISTS farm_jobs (
    id uuid PRIMARY KEY,
    project text NOT NULL,
    serial text NOT NULL REFERENCES farm_devices(serial),
    operation text NOT NULL CHECK (operation IN ('device-info','package-status','reboot')),
    params jsonb NOT NULL,
    idempotency_key text NOT NULL,
    state text NOT NULL DEFAULT 'queued' CHECK (state IN ('queued','running','succeeded','failed','cancelled','outcome_unknown')),
    worker text REFERENCES farm_workers(id),
    instance uuid,
    token uuid,
    epoch bigint,
    lease_until timestamptz,
    cancel_requested boolean NOT NULL DEFAULT false,
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(project, idempotency_key)
);
-- An uncertain outcome still reserves the physical device until reconciliation.
CREATE UNIQUE INDEX IF NOT EXISTS farm_one_owner ON farm_jobs(serial)
    WHERE state IN ('running', 'outcome_unknown');
CREATE INDEX IF NOT EXISTS farm_queue ON farm_jobs(created_at) WHERE state = 'queued';
CREATE TABLE IF NOT EXISTS farm_events (
    id bigserial PRIMARY KEY,
    job uuid REFERENCES farm_jobs(id),
    serial text,
    kind text NOT NULL,
    detail jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS farm_outbox (
    project text PRIMARY KEY,
    version bigint NOT NULL DEFAULT 0,
    delivered_version bigint NOT NULL DEFAULT 0,
    failures integer NOT NULL DEFAULT 0,
    disabled boolean NOT NULL DEFAULT false,
    next_attempt timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS farm_sender (
    id integer PRIMARY KEY CHECK (id = 1),
    blocked_until timestamptz NOT NULL DEFAULT clock_timestamp()
);
INSERT INTO farm_sender(id) VALUES (1) ON CONFLICT DO NOTHING;
