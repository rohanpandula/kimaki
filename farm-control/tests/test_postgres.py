"""Real PostgreSQL tests. FARM_TEST_DATABASE_URL must name a disposable DB."""
import concurrent.futures
import os
import uuid
import httpx
import pytest
from fastapi.testclient import TestClient
from farm_control.db import Store, Conflict
from farm_control.models import Enroll, Submit, Scan, Observation, Lease, Complete, Reconcile
from farm_control.api import create_app
from farm_control.notifier import Notifier


@pytest.fixture
def store():
    dsn = os.getenv('FARM_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('Set FARM_TEST_DATABASE_URL to a disposable PostgreSQL database')
    s = Store(dsn)
    s.migrate()
    with s.connect() as c:
        c.execute('TRUNCATE farm_events,farm_jobs,farm_devices,farm_workers,farm_outbox RESTART IDENTITY CASCADE')
        c.execute('UPDATE farm_settings SET global_limit=4')
        c.execute('UPDATE farm_sender SET blocked_until=clock_timestamp()')
    return s


def ready(s, serial='PHONE', worker='mini-1', project='test', instance=None):
    instance = instance or uuid.uuid4()
    s.enroll(Enroll(serial=serial, worker=worker, project=project, name=serial, legacy_access_disabled=True))
    s.boot(worker, instance)
    s.scan(worker, Scan(instance=instance, sequence=0, complete=True, devices=[Observation(serial=serial, status='USB_READY', transport_id=1)]))
    return instance


def submit(s, key='one', serial='PHONE'):
    return s.submit('test', Submit(serial=serial, operation='device-info', idempotency_key=key))


def test_submission_dedup_and_racing_claims(store):
    i = ready(store)
    a = submit(store)
    assert submit(store)['id'] == a['id']
    with pytest.raises(Conflict):
        store.submit('test', Submit(serial='PHONE', operation='reboot', idempotency_key='one'))
    submit(store, 'two')
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.claim('mini-1', i), range(8)))
    assert sum(r is not None for r in results) == 1


def test_expired_heartbeat_quarantines_without_other_traffic(store):
    i = ready(store)
    a = submit(store)
    j = store.claim('mini-1', i)
    with store.connect() as c:
        c.execute("UPDATE farm_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s", (j['id'],))
    with pytest.raises(Conflict):
        store.heartbeat('mini-1', j['id'], Lease(instance=i, token=j['token']))
    with store.connect() as c:
        assert c.execute('SELECT state FROM farm_jobs WHERE id=%s', (a['id'],)).fetchone()['state'] == 'outcome_unknown'
        assert c.execute('SELECT quarantined FROM farm_devices').fetchone()['quarantined']
        assert c.execute("SELECT count(*) AS n FROM farm_events WHERE kind='lease_expired'").fetchone()['n'] == 1
    submit(store, 'next')
    assert store.claim('mini-1', i) is None
    store.reconcile(j['id'], Reconcile(outcome='failed', prior_executor_stopped=True, evidence='Operator verified no executor remains'))
    assert store.claim('mini-1', i) is not None


def test_unknown_reserves_global_capacity(store):
    i = ready(store)
    j = submit(store)
    store.claim('mini-1', i)
    with store.connect() as c:
        c.execute('UPDATE farm_settings SET global_limit=1')
        c.execute("UPDATE farm_jobs SET lease_until=clock_timestamp()-interval '1 second'")
    k = ready(store, serial='OTHER', worker='mini-2')
    submit(store, 'other', 'OTHER')
    assert store.claim('mini-2', k) is None


def test_failed_scan_stale_sequence_and_wifi_opt_in(store):
    i = ready(store)
    submit(store)
    store.scan('mini-1', Scan(instance=i, sequence=1, complete=False, devices=[]))
    assert store.devices('test')[0]['status'] == 'UNKNOWN'
    assert store.claim('mini-1', i) is None
    with pytest.raises(Conflict):
        store.scan('mini-1', Scan(instance=i, sequence=0, complete=True, devices=[]))
    store.scan('mini-1', Scan(instance=i, sequence=2, complete=True, devices=[Observation(serial='PHONE',status='WIFI_FALLBACK',transport_id=1)]))
    assert store.claim('mini-1', i) is None
    store.submit('test', Submit(serial='PHONE',operation='device-info',idempotency_key='wifi',allow_wifi=True))
    assert store.claim('mini-1', i)['idempotency_key'] == 'wifi'


def test_reboot_instance_fences_old_worker_and_keeps_unknown(store):
    i = ready(store)
    submit(store)
    j = store.claim('mini-1', i)
    new = uuid.uuid4()
    store.boot('mini-1', new)
    with pytest.raises(Conflict):
        store.complete('mini-1', j['id'], Complete(instance=i,token=j['token'],outcome='succeeded'))
    assert store.get('test', j['id'])['state'] == 'outcome_unknown'


def test_complete_retry_and_cancel_queued(store):
    i = ready(store)
    submit(store)
    j = store.claim('mini-1', i)
    result = Complete(instance=i,token=j['token'],outcome='succeeded',result={'sdk': '35'})
    store.complete('mini-1',j['id'],result)
    store.complete('mini-1',j['id'],result)
    other = submit(store, 'next')
    store.cancel('test',other['id'])
    assert store.get('test',other['id'])['state'] == 'cancelled'


def test_api_scopes_and_public_job_redaction(store):
    ready(store)
    credentials = [{'role':'client','subject':'test','token':'c'*32}, {'role':'worker','subject':'mini-1','token':'w'*32},
                   {'role':'client','subject':'other','token':'o'*32}]
    with TestClient(create_app(store,credentials)) as client:
        body={'serial':'PHONE','operation':'device-info','idempotency_key':'api'}
        assert client.post('/v1/jobs',json=body).status_code == 401
        c={'Authorization':'Bearer '+'c'*32}
        job=client.post('/v1/jobs',json=body,headers=c).json()
        assert 'token' not in job and 'instance' not in job
        assert client.post('/v1/worker/boot',json={'instance':str(uuid.uuid4())},headers=c).status_code == 403
        assert client.get('/v1/jobs/'+job['id'],headers={'Authorization':'Bearer '+'o'*32}).status_code == 404


def test_outbox_coalesces_and_respects_retry_after(store):
    ready(store)
    submit(store)
    submit(store,'two')
    requests=[]
    def handle(request):
        requests.append(request)
        return httpx.Response(429,json={'retry_after':60,'global':True})
    notifier=Notifier(store,'test',{'test':'123'},client=httpx.Client(base_url='https://example.test',transport=httpx.MockTransport(handle)))
    notifier.once()
    notifier.once()
    assert len(requests)==1
    with store.connect() as c:
        assert c.execute('SELECT delivered_version FROM farm_outbox').fetchone()['delivered_version']==0


def test_late_evidence_never_releases_quarantine(store):
    i = ready(store)
    submit(store)
    job = store.claim('mini-1', i)
    store.boot('mini-1', uuid.uuid4())
    result = Complete(instance=i, token=job['token'], outcome='succeeded', result={'sdk':'35'})
    store.evidence('mini-1', job['id'], result)
    store.evidence('mini-1', job['id'], result)
    assert store.get('test', job['id'])['state'] == 'outcome_unknown'
    with store.connect() as c:
        assert c.execute('SELECT quarantined FROM farm_devices').fetchone()['quarantined']
        assert c.execute("SELECT count(*) AS n FROM farm_events WHERE kind='late_evidence'").fetchone()['n'] == 1


def test_admin_unknown_worker_and_auth_scheme(store):
    credentials = [{'role':'admin','subject':'operator','token':'a'*32}]
    with TestClient(create_app(store, credentials)) as client:
        assert client.get('/metrics', headers={'Authorization':'a'*32}).status_code == 401
        assert client.get('/metrics', headers={'Authorization':b'Bearer \xff'}).status_code == 401
        headers = {'Authorization':'Bearer '+'a'*32}
        assert client.post('/v1/admin/workers/missing/drain', headers=headers).status_code == 404
        assert client.post('/v1/admin/workers/missing/resume', headers=headers).status_code == 404
