"""Short transactions; the settings row serializes admission, not device work.

At 120 devices this deliberately favors inspectable invariants over claim throughput.
Every ownership mutation takes this gate before touching worker/device/job rows.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import json
import uuid
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

LEASE_SECONDS = 30
FRESH_SECONDS = 30


class Conflict(Exception):
    pass


class NotFound(Exception):
    pass


class Store:
    def __init__(self, dsn):
        self.dsn = dsn

    def connect(self):
        return psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=5,
                              options="-c statement_timeout=5000 -c lock_timeout=3000 -c client_encoding=UTF8")

    def migrate(self):
        with self.connect() as c:
            c.execute(Path(__file__).with_name("schema.sql").read_text())

    @contextmanager
    def transaction(self):
        # Commit recovery independently: a rejected stale heartbeat must not undo it.
        with self.connect() as recovery:
            recovery.execute("SELECT id FROM farm_settings WHERE id=1 FOR UPDATE")
            self.expire(recovery)
        with self.connect() as c:
            c.execute("SELECT id FROM farm_settings WHERE id=1 FOR UPDATE")
            yield c

    def emit(self, c, job, kind, detail=None):
        c.execute("INSERT INTO farm_events(job,serial,kind,detail) VALUES (%s,%s,%s,%s)",
                  (job['id'], job['serial'], kind, Jsonb(detail or {})))
        c.execute("""INSERT INTO farm_outbox(project,version) VALUES (%s,1)
                     ON CONFLICT(project) DO UPDATE SET version=farm_outbox.version+1""", (job['project'],))

    def expire(self, c):
        jobs = c.execute("""UPDATE farm_jobs SET state='outcome_unknown',updated_at=clock_timestamp()
                            WHERE state='running' AND lease_until < clock_timestamp() RETURNING *""").fetchall()
        for j in jobs:
            c.execute("UPDATE farm_devices SET quarantined=true WHERE serial=%s", (j['serial'],))
            self.emit(c, j, 'lease_expired')

    def boot(self, worker, instance):
        with self.transaction() as c:
            row = c.execute("SELECT * FROM farm_workers WHERE id=%s", (worker,)).fetchone()
            if not row:
                raise NotFound("Worker must be enrolled by an administrator")
            if row['instance'] != instance:
                jobs = c.execute("""UPDATE farm_jobs SET state='outcome_unknown',updated_at=clock_timestamp()
                                    WHERE worker=%s AND state='running' RETURNING *""", (worker,)).fetchall()
                for j in jobs:
                    c.execute("UPDATE farm_devices SET quarantined=true WHERE serial=%s", (j['serial'],))
                    self.emit(c, j, 'worker_restarted')
                c.execute("UPDATE farm_devices SET status='UNKNOWN',transport_id=NULL WHERE worker=%s", (worker,))
                c.execute("""UPDATE farm_workers SET instance=%s,scan_sequence=-1,last_scan=NULL,
                             last_seen=clock_timestamp() WHERE id=%s""", (instance, worker))

    def check_instance(self, c, worker, instance):
        row = c.execute("SELECT * FROM farm_workers WHERE id=%s AND instance=%s", (worker, instance)).fetchone()
        if not row:
            raise Conflict("Worker instance changed; stop this process and inspect the owning Mac")
        return row

    def scan(self, worker, scan):
        with self.transaction() as c:
            row = self.check_instance(c, worker, scan.instance)
            if scan.sequence <= row['scan_sequence']:
                raise Conflict("Stale inventory sequence")
            c.execute("UPDATE farm_workers SET scan_sequence=%s,last_seen=clock_timestamp() WHERE id=%s",
                      (scan.sequence, worker))
            # A failed bridge scan is UNKNOWN, never a mass phone-offline event.
            default = 'USB_OFFLINE' if scan.complete else 'UNKNOWN'
            c.execute("""UPDATE farm_devices SET status=%s,transport_id=NULL,observed_at=clock_timestamp()
                         WHERE worker=%s""", (default, worker))
            if scan.complete:
                c.execute("UPDATE farm_workers SET last_scan=clock_timestamp() WHERE id=%s", (worker,))
                for d in scan.devices:
                    c.execute("""UPDATE farm_devices SET status=%s,transport_id=%s,observed_at=clock_timestamp()
                                 WHERE serial=%s AND worker=%s""", (d.status, d.transport_id, d.serial, worker))

    def enroll(self, data):
        with self.transaction() as c:
            c.execute("INSERT INTO farm_workers(id) VALUES (%s) ON CONFLICT DO NOTHING", (data.worker,))
            old = c.execute("SELECT * FROM farm_devices WHERE serial=%s", (data.serial,)).fetchone()
            if old:
                raise Conflict("Device already enrolled; drain/reconcile before changing ownership")
            c.execute("""INSERT INTO farm_devices(serial,project,name,worker,enabled)
                         VALUES (%s,%s,%s,%s,true)""", (data.serial, data.project, data.name, data.worker))

    def submit(self, project, data):
        params = data.model_dump(exclude={'serial', 'operation', 'idempotency_key'})
        with self.transaction() as c:
            old = c.execute("SELECT * FROM farm_jobs WHERE project=%s AND idempotency_key=%s",
                            (project, data.idempotency_key)).fetchone()
            if old:
                if (old['serial'], old['operation'], old['params']) != (data.serial, data.operation, params):
                    raise Conflict("Idempotency key already used for different parameters")
                return old
            device = c.execute("SELECT * FROM farm_devices WHERE serial=%s AND project=%s",
                               (data.serial, project)).fetchone()
            if not device or not device['enabled']:
                raise NotFound("Device is not enrolled/enabled for this project")
            job = c.execute("""INSERT INTO farm_jobs(id,project,serial,operation,params,idempotency_key)
                               VALUES (%s,%s,%s,%s,%s,%s) RETURNING *""",
                            (uuid.uuid4(), project, data.serial, data.operation, Jsonb(params), data.idempotency_key)).fetchone()
            self.emit(c, job, 'queued')
            return job

    def claim(self, worker, instance):
        with self.transaction() as c:
            w = self.check_instance(c, worker, instance)
            c.execute("UPDATE farm_workers SET last_seen=clock_timestamp() WHERE id=%s", (worker,))
            if w['draining']:
                return None
            active = c.execute("SELECT count(*) AS n FROM farm_jobs WHERE state IN ('running','outcome_unknown')").fetchone()['n']
            limit = c.execute("SELECT global_limit FROM farm_settings WHERE id=1").fetchone()['global_limit']
            local = c.execute("SELECT count(*) AS n FROM farm_jobs WHERE state IN ('running','outcome_unknown') AND worker=%s", (worker,)).fetchone()['n']
            if active >= limit or local >= w['concurrency']:
                return None
            j = c.execute("""SELECT j.* FROM farm_jobs j JOIN farm_devices d ON d.serial=j.serial
                WHERE j.state='queued' AND d.worker=%s AND d.enabled AND NOT d.quarantined
                AND d.project=j.project AND d.observed_at > clock_timestamp() - interval '30 seconds'
                AND (d.status='USB_READY' OR (d.status='WIFI_FALLBACK' AND (j.params->>'allow_wifi')::boolean))
                AND NOT EXISTS (SELECT 1 FROM farm_jobs a WHERE a.serial=j.serial AND a.state IN ('running','outcome_unknown'))
                ORDER BY j.created_at LIMIT 1 FOR UPDATE OF j SKIP LOCKED""", (worker,)).fetchone()
            if not j:
                return None
            epoch = c.execute("UPDATE farm_devices SET epoch=epoch+1 WHERE serial=%s RETURNING epoch", (j['serial'],)).fetchone()['epoch']
            j = c.execute("""UPDATE farm_jobs SET state='running',worker=%s,instance=%s,token=%s,epoch=%s,
                             lease_until=clock_timestamp()+interval '30 seconds',updated_at=clock_timestamp()
                             WHERE id=%s RETURNING *""", (worker, instance, uuid.uuid4(), epoch, j['id'])).fetchone()
            self.emit(c, j, 'claimed', {'epoch': epoch, 'worker': worker})
            return j

    def lease(self, c, worker, job_id, request):
        self.check_instance(c, worker, request.instance)
        j = c.execute("""SELECT * FROM farm_jobs WHERE id=%s AND worker=%s AND instance=%s
                         AND token=%s AND state='running' AND lease_until > clock_timestamp()""",
                      (job_id, worker, request.instance, request.token)).fetchone()
        if not j:
            raise Conflict("Lease lost; stop device commands and reconcile")
        return j

    def heartbeat(self, worker, job_id, data):
        with self.transaction() as c:
            j = self.lease(c, worker, job_id, data)
            c.execute("UPDATE farm_workers SET last_seen=clock_timestamp() WHERE id=%s", (worker,))
            c.execute("UPDATE farm_jobs SET lease_until=clock_timestamp()+interval '30 seconds' WHERE id=%s", (job_id,))
            return {'cancel_requested': j['cancel_requested'], 'lease_seconds': LEASE_SECONDS}

    def complete(self, worker, job_id, data):
        with self.transaction() as c:
            # A completion retry after a lost HTTP acknowledgment is safe and deterministic.
            old = c.execute("SELECT * FROM farm_jobs WHERE id=%s AND worker=%s AND instance=%s AND token=%s",
                            (job_id, worker, data.instance, data.token)).fetchone()
            if old and old['state'] == data.outcome and old['result'] == data.result:
                return old
            j = self.lease(c, worker, job_id, data)
            j = c.execute("""UPDATE farm_jobs SET state=%s,result=%s,updated_at=clock_timestamp()
                             WHERE id=%s RETURNING *""", (data.outcome, Jsonb(data.result), job_id)).fetchone()
            if data.outcome == 'outcome_unknown':
                c.execute("UPDATE farm_devices SET quarantined=true WHERE serial=%s", (j['serial'],))
            self.emit(c, j, data.outcome)
            return j

    def get(self, project, job_id):
        with self.connect() as c:
            row = c.execute("SELECT * FROM farm_jobs WHERE id=%s AND project=%s", (job_id, project)).fetchone()
            if not row:
                raise NotFound("Job not found in this project")
            if row['state'] == 'running' and row['lease_until'] < datetime.now(timezone.utc):
                row['state'] = 'outcome_unknown'  # The sweeper persists recovery independently.
            return row

    def evidence(self, worker, job_id, data):
        # Old instances can append bounded evidence, never change state or release a reservation.
        with self.transaction() as c:
            job = c.execute('SELECT * FROM farm_jobs WHERE id=%s AND worker=%s AND instance=%s AND token=%s',
                            (job_id, worker, data.instance, data.token)).fetchone()
            if not job:
                raise NotFound('Original execution not found')
            detail = {'outcome': data.outcome, 'result': data.result}
            if not c.execute("SELECT 1 FROM farm_events WHERE job=%s AND kind='late_evidence' AND detail=%s",
                             (job_id, Jsonb(detail))).fetchone():
                self.emit(c, job, 'late_evidence', detail)

    def cancel(self, project, job_id):
        with self.transaction() as c:
            j = c.execute("SELECT * FROM farm_jobs WHERE id=%s AND project=%s", (job_id, project)).fetchone()
            if not j:
                raise NotFound("Job not found in this project")
            if j['state'] == 'queued':
                c.execute("UPDATE farm_jobs SET state='cancelled',updated_at=clock_timestamp() WHERE id=%s", (job_id,))
            elif j['state'] == 'running':
                if j['cancel_requested']:
                    return
                c.execute("UPDATE farm_jobs SET cancel_requested=true WHERE id=%s", (job_id,))
            else:
                return
            self.emit(c, j, 'cancel_requested')

    def reconcile(self, job_id, data, actor='operator'):
        with self.transaction() as c:
            j = c.execute("SELECT * FROM farm_jobs WHERE id=%s", (job_id,)).fetchone()
            if not j or j['state'] != 'outcome_unknown':
                raise Conflict("Only uncertain outcomes can be reconciled")
            c.execute("UPDATE farm_jobs SET state=%s,updated_at=clock_timestamp() WHERE id=%s", (data.outcome, job_id))
            c.execute("UPDATE farm_devices SET quarantined=false WHERE serial=%s", (j['serial'],))
            self.emit(c, j, 'reconciled', {'outcome': data.outcome, 'evidence': data.evidence, 'actor': actor})

    def devices(self, project):
        with self.connect() as c:
            return c.execute("""SELECT serial,name,worker,enabled,quarantined,observed_at,
                CASE WHEN observed_at IS NULL OR observed_at < clock_timestamp()-interval '30 seconds'
                THEN 'UNKNOWN' ELSE status END AS status FROM farm_devices WHERE project=%s ORDER BY name""", (project,)).fetchall()
