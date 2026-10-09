import json
import threading
import contextlib
import uuid
import httpx
import pytest
from pydantic import ValidationError
from farm_control.models import Submit, Scan
from farm_control.notifier import nonce, retry_delay
from farm_control.worker import Worker, LeaseGuard, LeaseLost, parse_devices


def test_parameters_cannot_inject_shell():
    with pytest.raises(ValidationError):
        Submit(serial='x', operation='package-status', package='com.app;reboot', idempotency_key='a')
    with pytest.raises(ValidationError):
        Submit(serial='x', operation='shell', idempotency_key='a')
    with pytest.raises(ValidationError):
        Submit(serial='x', operation='reboot', idempotency_key='a', command='anything')


def test_discord_nonce_and_retry_headers():
    assert len(nonce('a', 100)) <= 25
    assert nonce('a', 100) == nonce('a', 100)
    assert nonce('a', 101) != nonce('a', 100)
    r = httpx.Response(429, json={'retry_after': 8.25}, headers={'Retry-After': '7'})
    assert retry_delay(r) >= 8.25
    assert retry_delay(httpx.Response(429, text='invalid')) >= 1


def test_inventory_does_not_call_wifi_usb():
    rows = parse_devices('List of devices attached\nUSB device usb:1-2 transport_id:4\n1.2.3.4:5555 device transport_id:5\nDARK unauthorized usb:1-3 transport_id:6\n')
    assert [r['status'] for r in rows] == ['USB_READY', 'UNKNOWN', 'UNAUTHORIZED']
    assert rows[0]['transport_id'] == 4


def test_fail_closed_before_spawning_adb(tmp_path, monkeypatch):
    w = Worker('http://localhost', 'test', tmp_path)
    guard = threading.Event()
    guard.set()
    monkeypatch.setattr('subprocess.Popen', lambda *a, **k: pytest.fail('must not spawn'))
    with pytest.raises(LeaseLost):
        w.command(['reboot'], guard)


def test_local_results_survive_api_failure(tmp_path):
    w = Worker('http://localhost', 'test', tmp_path)
    record = {'outcome': 'outcome_unknown', 'result': {'reason': 'lost_lease'}}
    w.persist_result('job', record)
    assert json.loads((tmp_path/'results/job.json').read_text()) == record
    assert not (tmp_path/'results/job.tmp').exists()


@pytest.fixture
def execution(tmp_path, monkeypatch):
    worker = Worker('http://localhost', 'test', tmp_path)
    job = {'id': str(uuid.uuid4()), 'token': str(uuid.uuid4()), 'serial':'PHONE',
           'operation':'package-status', 'idempotency_key':'test', 'params': {'package':'com.example.app','allow_wifi':False}}
    calls = []
    def api(path, body):
        calls.append((path, body))
        return {'cancel_requested':False}
    worker.api = api
    monkeypatch.setattr('farm_control.worker.local_lock', lambda _: contextlib.nullcontext())
    return worker, job, [{'serial':'PHONE','status':'USB_READY','transport_id':1}], calls


def test_absent_package_is_success_without_quarantine(execution):
    worker, job, devices, calls = execution
    worker.command = lambda args, guard: 'PHONE' if args[-1]=='ro.serialno' else ''
    worker.execute(job, devices)
    completed = [body for path,body in calls if path.endswith('/complete')][0]
    assert completed['outcome']=='succeeded'
    assert completed['result']['installed'] is False


def test_wrong_identity_never_executes_requested_action(execution):
    worker, job, devices, calls = execution
    commands=[]
    def command(args, guard):
        commands.append(args)
        return 'DIFFERENT_PHONE'
    worker.command=command
    worker.execute(job,devices)
    assert len(commands)==1
    assert calls[-1][1]['outcome']=='outcome_unknown'


def test_cancellation_before_execution_issues_no_adb(execution):
    worker,job,devices,calls=execution
    def api(path,body):
        calls.append((path,body))
        return {'cancel_requested':True}
    worker.api=api
    worker.command=lambda *args: pytest.fail('no ADB after cancel')
    worker.execute(job,devices)
    assert calls[-1][1]['outcome']=='cancelled'


def test_completion_ack_failure_retains_evidence(execution):
    worker,job,devices,calls=execution
    def api(path,body):
        if path.endswith('/complete'):
            raise httpx.ConnectError('test')
        return {'cancel_requested':False}
    worker.api=api
    worker.command=lambda args,guard: 'PHONE' if args[-1]=='ro.serialno' else 'package:com.example.app'
    worker.execute(job,devices)
    record=json.loads((worker.state_dir/'results'/(job['id']+'.json')).read_text())
    assert record['outcome']=='succeeded'
    assert record['result']['installed'] is True


def test_lease_loss_kills_active_adb_process(tmp_path,monkeypatch):
    worker=Worker('http://localhost','test',tmp_path)
    guard=threading.Event()
    killed=[]
    class Process:
        pid=12345
        returncode=None
        def poll(self): return None if not killed else -9
        def wait(self,timeout): return -9
    def spawn(*args,**kwargs):
        guard.set()
        return Process()
    monkeypatch.setattr('subprocess.Popen',spawn)
    monkeypatch.setattr('signal.SIGKILL',9,raising=False)
    monkeypatch.setattr('os.killpg',lambda pid,sig:killed.append(pid),raising=False)
    with pytest.raises(LeaseLost):worker.command(['reboot'],guard)
    assert killed==[12345]


def test_sleep_deadline_is_sticky(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr('time.monotonic', lambda: 1.0)
    monkeypatch.setattr('time.time', lambda: clock[0])
    guard = LeaseGuard()
    guard.renewed((1.0, 100.0), 20)
    clock[0] = 121.0  # Wall time advances even when the monotonic clock suspends.
    assert guard.is_set()
    with pytest.raises(LeaseLost):
        guard.renewed((1.0, 121.0), 20)


def test_bad_wifi_does_not_hide_usb(tmp_path):
    worker = Worker('http://localhost', 'test', tmp_path)
    def command(args, guard, timeout):
        if args == ['devices', '-l']:
            return 'List of devices attached\nUSB device usb:1 transport_id:1\n1.2.3.4:5555 device transport_id:2'
        raise LeaseLost()
    worker.command = command
    complete, devices = worker.inventory()
    assert complete and devices == [{'serial':'USB','status':'USB_READY','transport_id':1}]


def test_adb_does_not_inherit_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv('FARM_WORKER_TOKEN', 'secret')
    monkeypatch.setenv('ADB_SERVER_SOCKET', 'tcp:remote:5037')
    env = Worker('http://localhost', 'test', tmp_path).env()
    assert 'FARM_WORKER_TOKEN' not in env and 'ADB_SERVER_SOCKET' not in env


def test_corrupt_evidence_does_not_block_other_results(tmp_path):
    worker = Worker('http://localhost', 'test', tmp_path)
    worker.persist_result('valid', {'outcome':'succeeded'})
    (tmp_path/'results/broken.json').write_text('{')
    worker.api = lambda *args: {}
    worker.replay_results()
    assert not (tmp_path/'results/valid.json').exists()
    assert (tmp_path/'results/broken.corrupt').exists()


def test_precancel_ack_failure_is_durable(execution):
    worker, job, devices, calls = execution
    def api(path, body):
        if path.endswith('/complete'):
            raise httpx.ConnectError('test')
        return {'cancel_requested':True}
    worker.api = api
    worker.command = lambda *args: pytest.fail('no command after cancellation')
    worker.execute(job, devices)
    record = json.loads((worker.state_dir/'results'/(job['id']+'.json')).read_text())
    assert record['outcome'] == 'cancelled'


def test_credential_schema_fails_at_startup():
    from farm_control.api import create_app
    for credentials in ([], [{'role':'worker', 'token':'x'*32}], [{'role':'admin','subject':'a','token':'é'*32}]):
        with pytest.raises(ValueError):
            create_app(store=object(), credentials=credentials)


@pytest.mark.parametrize('evidence_status', [200, 404])
def test_late_evidence_replay_preserves_receipt(tmp_path, evidence_status):
    worker = Worker('http://localhost', 'test', tmp_path)
    worker.persist_result('stale', {'outcome':'succeeded'})
    worker.persist_result('valid', {'outcome':'succeeded'})
    paths = []
    def api(path, body):
        paths.append(path)
        status = 409 if path == 'jobs/stale/complete' else evidence_status if path.endswith('/evidence') else 200
        httpx.Response(status, request=httpx.Request('POST', 'http://localhost')).raise_for_status()
        return {}
    worker.api = api
    worker.replay_results()
    assert 'jobs/stale/evidence' in paths
    assert (tmp_path/'results/stale.reconcile').exists()
    assert not (tmp_path/'results/valid.json').exists()
