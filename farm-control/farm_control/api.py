"""Authenticated pull API. Mac credentials never receive PostgreSQL credentials."""
from contextlib import asynccontextmanager
import asyncio
import hmac
import json
import logging
import os
import re
from uuid import UUID
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.concurrency import run_in_threadpool
from .db import Store, Conflict, NotFound
from .models import Boot, Claim, Complete, Enroll, Lease, Reconcile, Scan, Submit

log = logging.getLogger(__name__)


def public_job(row):
    return {k: row[k] for k in ('id', 'serial', 'operation', 'state', 'result', 'created_at', 'updated_at', 'cancel_requested')}


def create_app(store=None, credentials=None):
    store = store or Store(os.environ['FARM_DATABASE_URL'])
    credentials = credentials if credentials is not None else json.loads(os.environ['FARM_CREDENTIALS'])
    # [{"token": "...", "role": "client|worker|admin", "subject": "project-or-worker"}]
    if not isinstance(credentials, list) or not credentials or any(
        not isinstance(c, dict) or not isinstance(c.get('token'), str) or
        not re.fullmatch(r'[!-~]{32,512}', c['token']) or
        c.get('role') not in ('admin', 'worker', 'client') or
        not isinstance(c.get('subject'), str) or not re.fullmatch(r'[a-z0-9_-]{1,64}', c['subject'])
        for c in credentials
    ):
        raise ValueError("Use distinct random credentials of at least 32 characters")
    if len({c['token'] for c in credentials}) != len(credentials):
        raise ValueError("Credentials must be unique")

    @asynccontextmanager
    async def lifespan(app):
        async def sweep():
            while True:
                try:
                    def expire():
                        with store.transaction():
                            pass
                    await run_in_threadpool(expire)
                except Exception:
                    log.error('Lease sweep failed; check PostgreSQL availability')
                await asyncio.sleep(5)
        task = asyncio.create_task(sweep())
        yield
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    app = FastAPI(title='Kimaki farm control', lifespan=lifespan)

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        return JSONResponse(status_code=409, content={'detail': str(exc)})

    @app.exception_handler(NotFound)
    async def not_found(request, exc):
        return JSONResponse(status_code=404, content={'detail': str(exc)})

    def auth(authorization: str = Header(default='')):
        if not authorization.startswith('Bearer '):
            raise HTTPException(401, 'Invalid farm credential')
        supplied = authorization[7:].encode('utf-8')
        for credential in credentials:
            if hmac.compare_digest(supplied, credential['token'].encode('utf-8')):
                return credential
        raise HTTPException(401, 'Invalid farm credential')

    def role(expected):
        def require(identity=Depends(auth)):
            if identity['role'] != expected:
                raise HTTPException(403, 'Credential cannot perform this operation')
            return identity['subject']
        return require

    client = role('client')
    worker = role('worker')
    admin = role('admin')

    @app.get('/health')
    def health():
        with store.connect() as c:
            c.execute('SELECT 1 FROM farm_settings WHERE id=1')
        return {'status': 'ok'}

    @app.post('/v1/jobs')
    def submit(data: Submit, project=Depends(client)):
        return public_job(store.submit(project, data))

    @app.get('/v1/jobs/{job_id}')
    def status(job_id: UUID, project=Depends(client)):
        return public_job(store.get(project, job_id))

    @app.post('/v1/jobs/{job_id}/cancel')
    def cancel(job_id: UUID, project=Depends(client)):
        store.cancel(project, job_id)
        return {'cancel_requested': True}

    @app.get('/v1/devices')
    def devices(project=Depends(client)):
        return store.devices(project)

    @app.post('/v1/worker/boot')
    def boot(data: Boot, subject=Depends(worker)):
        store.boot(subject, data.instance)
        return {'lease_seconds': 30}

    @app.post('/v1/worker/scan')
    def scan(data: Scan, subject=Depends(worker)):
        store.scan(subject, data)
        return {'accepted': True}

    @app.post('/v1/worker/claim')
    def claim(data: Claim, subject=Depends(worker)):
        return store.claim(subject, data.instance)

    @app.post('/v1/worker/jobs/{job_id}/heartbeat')
    def heartbeat(job_id: UUID, data: Lease, subject=Depends(worker)):
        return store.heartbeat(subject, job_id, data)

    @app.post('/v1/worker/jobs/{job_id}/complete')
    def complete(job_id: UUID, data: Complete, subject=Depends(worker)):
        return store.complete(subject, job_id, data)

    @app.post('/v1/worker/jobs/{job_id}/evidence')
    def evidence(job_id: UUID, data: Complete, subject=Depends(worker)):
        store.evidence(subject, job_id, data)
        return {'recorded': True}

    @app.post('/v1/admin/devices')
    def enroll(data: Enroll, _=Depends(admin)):
        store.enroll(data)
        return {'enrolled': True}

    @app.post('/v1/admin/jobs/{job_id}/reconcile')
    def reconcile(job_id: UUID, data: Reconcile, _=Depends(admin)):
        store.reconcile(job_id, data, actor=_)
        return {'reconciled': True}

    @app.post('/v1/admin/workers/{worker_id}/drain')
    def drain(worker_id: str, _=Depends(admin)):
        with store.transaction() as c:
            if c.execute('UPDATE farm_workers SET draining=true WHERE id=%s', (worker_id,)).rowcount == 0:
                raise NotFound('Worker not found')
        return {'draining': True}

    @app.post('/v1/admin/workers/{worker_id}/resume')
    def resume(worker_id: str, _=Depends(admin)):
        with store.transaction() as c:
            if c.execute('UPDATE farm_workers SET draining=false WHERE id=%s', (worker_id,)).rowcount == 0:
                raise NotFound('Worker not found')
        return {'draining': False}

    @app.get('/metrics', response_class=PlainTextResponse)
    def metrics(_=Depends(admin)):
        # Bounded labels; no phone serials, usernames, result contents or credentials.
        with store.connect() as c:
            jobs = c.execute('SELECT state,count(*) AS n FROM farm_jobs GROUP BY state').fetchall()
            unknown = c.execute('SELECT count(*) AS n FROM farm_devices WHERE quarantined').fetchone()['n']
            age = c.execute("SELECT coalesce(extract(epoch FROM clock_timestamp()-min(created_at)),0) AS age FROM farm_jobs WHERE state='queued'").fetchone()['age']
            backlog = c.execute('SELECT count(*) AS n FROM farm_outbox WHERE version>delivered_version').fetchone()['n']
            disabled = c.execute('SELECT count(*) AS n FROM farm_outbox WHERE disabled').fetchone()['n']
            stale = c.execute("SELECT count(*) AS n FROM farm_workers WHERE last_seen IS NULL OR last_seen<clock_timestamp()-interval '30 seconds'").fetchone()['n']
        lines = ['# TYPE farm_jobs gauge']
        for row in jobs:
            lines.append(f'farm_jobs{{state="{row["state"]}"}} {row["n"]}')
        lines += [f'farm_quarantined_devices {unknown}', f'farm_queue_oldest_seconds {age}',
                  f'farm_notification_backlog {backlog}', f'farm_notification_disabled {disabled}', f'farm_stale_workers {stale}']
        return '\n'.join(lines) + '\n'

    return app
