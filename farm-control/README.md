# Farm control plane

Opt-in coordinator and Mac-local workers for a USB device farm. Kimaki submits
validated jobs; ordinary phone operations do not create OpenCode conversations.
No code in this package opens or migrates an OpenCode/Kimaki SQLite database.

## Components

* `farm-control api`: authenticated HTTP API backed by a **separate PostgreSQL 16+ database**.
* `farm-control worker`: one process per Mac, local ADB only, one execution at a time.
* `farm-control notify`: optional durable, coalesced Discord summaries. Job completion
  commits independently of notification delivery. Use a **dedicated notification bot**
  so unrelated Kimaki REST traffic cannot share uncoordinated rate-limit buckets.
* `kimaki farm` and `/farm`: project-scoped submission, status, cancellation and inventory.
* `/metrics`, Prometheus alerts and a Grafana dashboard for the job service. Keep existing
  node exporters for CPU, disk, memory and host availability.

The initial operation allowlist is `device-info`, `package-status`, and `reboot`.
These exercise read-only, device-verifiable and side-effecting execution. There is
intentionally no arbitrary shell, Python or agent prompt execution endpoint. Add
operations in `models.py` and `Worker.execute`, with bounded output, timeouts and
reconciliation tests. Business-specific workflows belong in reviewed deterministic
handlers, not arbitrary model-generated scripts.

## Ownership and failure semantics

Enrollment requires the operator to attest `legacy_access_disabled: true`. Before
enrolling a phone, stop its existing agents, remove VM-side/other-user command access
to its ADB transports and verify no job is still executing. A database lease cannot
stop an old agent or fence commands already accepted by Android. On a shared ADB
server, do not remove forwards until all consumers are drained; pilot on a separate
Mac or isolate the pilot's ADB access first.

The coordinator serializes admission in short transactions. A partial unique index
reserves each serial through both `running` and `outcome_unknown`. Global capacity
defaults to four, per-Mac capacity to two; this first worker deliberately executes
one job at a time. **Uncertain outcomes retain capacity** until operator reconciliation.
New UUID tokens and monotonic device epochs fence stale database updates. Workers
verify `ro.serialno`, select a local ADB transport ID, and hold process/device locks.
Never run workers under multiple users or with different state directories on the
same Mac. Only this worker may control its enrolled phones.

Leases last 30 seconds. Heartbeats run every five seconds with five-second HTTP
timeouts; any renewal failure stops new commands and kills the local ADB subprocess.
ADB commands time out after 12 seconds. Killing the client does **not** undo a device
side effect. Lease loss, worker restart, disconnect or timeout produces an uncertain
outcome and quarantine, not automatic replay. A new worker instance cannot release
that reservation by simply reporting a phone online.

The worker also checks a sticky, conservative 20-second deadline against both wall
and monotonic clocks and renews synchronously before each device command. A missed
deadline cannot be revived by a later heartbeat. OS process kills can still orphan
an already running ADB client: before reconciliation, inspect and terminate the
old worker's ADB clients on the owning Mac, including after a forced restart. This
release does not provide OS-level process fencing; quarantine is mandatory.
Locally persisted late results are appended as audited evidence and retained as
`.reconcile` files; they never release quarantine. Inspect `.corrupt` and
`.interrupted` files manually. A worker credential authorizes replacement of that
Mac's instance; protect and revoke compromised credentials before restarting.

An administrator must verify the prior executor stopped and inspect device state
before reconciling an uncertain job. The evidence is audited. Reconcile to failed,
cancelled or succeeded; a retry is a **new job with a new idempotency key**, after
reconciliation. Repeat submissions with the same key return the same job; different
parameters under the same key are rejected. No generic exactly-once claim is made.

Cancellation of a queued job is immediate. A running job observes cancellation at
heartbeat/command boundaries; if an action may have started it remains uncertain.
Reboot success means the command was accepted, not that Android finished booting.

Inventory is independent of identity: serial and assigned Mac are enrolled by an
administrator. Only successful complete scans can report a phone absent. Failed
scans or observations older than 30 seconds report `UNKNOWN`. WiFi is never silently
selected: the worker only inspects already connected TCP transports, verifies their
hardware serial and requires `allow_wifi` on each job. It never enables TCP ADB or
connects to network endpoints. Duplicate USB serials fail the scan closed.
Cancellation racing with the first command can conservatively require reconciliation
even when no device side effect occurred. Read-only status polling does not take the
admission lock; expired states are durably recovered by the five-second sweeper.

## Install without touching production

Use a clean checkout of this repository. Do not push an existing working directory
containing credentials, private farm databases or uploads.

```sh
cd farm-control
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
```

Generate separate random credentials (for example `openssl rand -hex 32`) for each
project client, each Mac, and the administrator. Keep the following settings in an
operator-owned `0600` environment file, **not in git**:

```text
FARM_DATABASE_URL=postgresql://farm:URL_ENCODED_PASSWORD@postgres:5432/farm
POSTGRES_PASSWORD=RANDOM_PASSWORD
FARM_CREDENTIALS=[{"role":"admin","subject":"operator","token":"RANDOM_ADMIN_TOKEN"},{"role":"client","subject":"pilot","token":"RANDOM_PROJECT_TOKEN"},{"role":"worker","subject":"mini-1","token":"RANDOM_MAC_TOKEN"}]
```

Replace every placeholder; real tokens must be at least 32 characters. URL-encode
the database password in the URL. `docker compose --env-file /secure/farm.env up -d --build`
starts PostgreSQL, runs the idempotent schema initialization, then binds the API to
**localhost:8090 only**. PostgreSQL is not published. For a native installation,
set a native PostgreSQL URL, run `farm-control migrate`, then `farm-control api`.
Do not run migrations against an unrelated database. Use a dedicated database role.

Mac access requires either a TLS reverse proxy on a private network, or an SSH
forward to the API's loopback port. Example: `ssh -N -L 8090:127.0.0.1:8090 SERVER`.
Verify TLS certificates; never expose bearer credentials over plaintext LAN HTTP.
The API is a single trusted-farm deployment, not a public multi-tenant service.
Restrict network access, request-body sizes and request rates at the proxy.

Enroll a drained pilot phone using the admin token and `POST /v1/admin/devices`:

```json
{"serial":"EXAMPLE_SERIAL","name":"Pilot phone","project":"pilot","worker":"mini-1","legacy_access_disabled":true}
```

On the Mac, install Python 3.11+ and platform-tools, then install this package in a
venv. Reuse the **existing local ADB server** on port 5040; do not start competing
USB ADB servers. Worker environment:

```text
FARM_API_URL=http://127.0.0.1:8090
FARM_WORKER_TOKEN=RANDOM_MAC_TOKEN
FARM_ADB=/opt/homebrew/bin/adb
FARM_ADB_PORT=5040
FARM_STATE_DIR=/Users/OPERATOR/.local/state/kimaki-farm
```

Run `.venv/bin/farm-control worker`. `deploy/` includes a LaunchAgent and wrapper;
replace `OPERATOR`, use absolute paths, protect the environment file and give the
wrapper executable permissions. Install only after the manual pilot passes.
An Intel Mac may use `/usr/local/bin/adb`. Keep one fixed state directory per host.

## Kimaki integration

Build the CLI using the repository's documented workspace build process; installing
directly from a GitHub URL does not build its workspace dependencies. Point
`KIMAKI_FARM_CONFIG` at a `0600` JSON file with one entry per project:

```json
{"pilot":{"url":"http://127.0.0.1:8090","token":"RANDOM_PROJECT_TOKEN","channels":["GUILD_ID:CHANNEL_ID"]}}
```

Replace IDs with numeric Discord IDs. Project authorization comes from the API
credential, not an agent-supplied project name. Discord channel mapping must match
exactly one project; threads inherit their parent channel's mapping. `/farm`
requires Manage Server at execution time and replies ephemerally. Job IDs and
statuses are displayed, never lease credentials. Normal chats are unchanged.

```sh
kimaki farm devices --project pilot
kimaki farm submit --project pilot --serial EXAMPLE_SERIAL --operation device-info --key pilot-probe-1
kimaki farm status --project pilot --job JOB_UUID
kimaki farm cancel --project pilot --job JOB_UUID
```

Use a stable key for retries of the same submission. Do not put administrator or
Mac credentials in Kimaki's project configuration. The project client can submit
allowlisted jobs including reboot; grant it only to trusted project operators.

## Notifications, monitoring and administration

Set `FARM_DISCORD_TOKEN` and `FARM_DISCORD_CHANNELS={"pilot":"CHANNEL_ID"}` for a
dedicated notification bot, then enable Compose profile `notifications`. Every
project uses a distinct channel. One PostgreSQL advisory lock elects the sender.
Summaries coalesce changes rather than emitting every tool event. The sender follows
Discord `Retry-After`/reset headers, persists backoff, disables inaccessible channels
and uses deterministic nonces of at most 25 characters. Delivery is at-least-once:
Discord nonce deduplication is time-limited, so very late retries may duplicate a
summary. Notifications never control job success.
All 429 responses conservatively pause this dedicated sender globally for the
server-specified delay; this trades some cross-project latency for safe backoff.

`POST /v1/admin/workers/ID/drain` stops new claims; existing work finishes.
`POST /v1/admin/workers/ID/resume` permits claims again. To reconcile, call
`POST /v1/admin/jobs/UUID/reconcile` with `outcome`, `prior_executor_stopped: true`
and a meaningful `evidence` string. Do not acknowledge that assertion until verified.
To change capacity, update `farm_settings.global_limit` and
`farm_workers.concurrency` as an administrator during a drained maintenance window.
To restore notification delivery after correcting credentials/permissions, clear
`farm_outbox.disabled` and reset `failures` for the affected project.

Scrape `/metrics` with the protected admin token. Import the Grafana dashboard and
Prometheus examples in `deploy/`. Alert independently on API reachability, stale
workers, quarantine, queue age, disk/WAL growth and host memory. Grafana on one Mac
is not an independent dead-man monitor for that Mac.

## Backup, migration and rollback

1. Obtain a **consistent off-VM backup** of the existing OpenCode history before
   cutover. A 96 GiB database cannot be copied into 40 GB free. Use SQLite's backup
   API to a destination with room, or stop all writers and copy the complete
   database state. Do not copy just the main file while WAL writers are active.
   Verify the backup on the destination. Do not VACUUM/rebuild the production file
   as part of this migration, and do not export chat contents to PostgreSQL.
2. Back up the new database with `pg_dump -Fc` to a separate host. Rehearse a restore
   into a disposable database. Before resuming from any restored backup, drain all
   workers, mark previously running jobs uncertain and reconcile actual device state.
   A database restore is not proof that physical actions rolled back.
3. Keep the legacy launcher stopped or bounded while taking ownership inventory.
   Shadow observations first. Enroll five drained phones on one Mac; start with
   `device-info` and `package-status`. Do not execute both systems on those phones.
4. Test worker kill, USB unplug, Mac restart, lost API connectivity and coordinator
   restart. Require no overlapping device commands, durable quarantine, rejected
   stale completions and correct UNKNOWN versus USB_OFFLINE observations.
5. Expand per Mac only after the tests pass. Keep history and old code available.
6. Rollback: drain new workers, wait for completion/reconcile uncertain jobs, stop
   workers and remove their command access, then restore legacy access for the same
   explicit device set. Never simply enable both launchers. Preserve new job/event
   records for diagnosis; no migration deletes prior data.

## Tests

```sh
FARM_TEST_DATABASE_URL=postgresql://.../DISPOSABLE_TEST_DB .venv/bin/pytest tests -q
```

The integration fixtures **truncate farm tables in the named test database**. Never
point this variable at production. They exercise racing claims, deduplication,
expired/stale leases, retained capacity, project auth, worker reboot, cancellation,
inventory freshness and durable Discord backoff. Unit tests run without PostgreSQL;
PostgreSQL tests skip explicitly when the variable is absent. The inactive
`deploy/github-actions.yml.example` supplies a fresh PostgreSQL service when copied
to `.github/workflows/farm-control.yml` by a maintainer authorized to edit workflows.
The credential used for this PR lacked GitHub's `workflow` scope, so the template
is included without activation. A real five-phone pilot remains required before production
cutover; mocked ADB tests cannot prove USB hardware behavior.
