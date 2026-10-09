"""Mac/Linux USB worker. No agent, shell interpolation, or conversation database.

The worker serializes execution locally; scale across Macs first. PostgreSQL still
enforces global/per-Mac limits against duplicate claimers. Only one worker process
per Mac is supported. Do not expose the local ADB server to other executors.
"""
import contextlib
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time
import uuid
from urllib.parse import urlsplit
import httpx

log = logging.getLogger(__name__)


class LeaseLost(Exception):
    pass


class LeaseGuard(threading.Event):
    """A missed deadline is sticky, including after process suspension/Mac sleep."""
    def __init__(self):
        super().__init__()
        self.deadlines = None

    def is_set(self):
        if self.deadlines and (time.monotonic() >= self.deadlines[0] or time.time() >= self.deadlines[1]):
            self.set()
        return super().is_set()

    def renewed(self, started, seconds):
        if self.is_set():
            raise LeaseLost()
        self.deadlines = (started[0] + seconds, started[1] + seconds)
        if self.is_set():
            raise LeaseLost()


@contextlib.contextmanager
def local_lock(path):
    import fcntl
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open('a') as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def parse_devices(output):
    devices = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 2 or fields[0] == 'List' or fields[0].startswith('*'):
            continue
        serial, state = fields[:2]
        attrs = dict(p.split(':', 1) for p in fields[2:] if ':' in p)
        tid = attrs.get('transport_id', '')
        if not tid.isdigit():
            continue
        usb = 'usb' in attrs
        status = 'UNAUTHORIZED' if state == 'unauthorized' else ('USB_READY' if usb and state == 'device' else 'UNKNOWN')
        devices.append({'serial': serial, 'status': status, 'transport_id': int(tid), 'usb': usb, 'state': state})
    return devices


class Worker:
    def __init__(self, url, token, state_dir, adb='adb', adb_port=5040, client=None):
        endpoint = urlsplit(url)
        if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment or not (
            endpoint.scheme == 'https' or (endpoint.scheme == 'http' and endpoint.hostname in ('localhost', '127.0.0.1', '::1'))
        ):
            raise ValueError('Use HTTPS or a loopback SSH tunnel for the farm API')
        self.client = client or httpx.Client(base_url=url, headers={'Authorization': 'Bearer '+token}, timeout=5)
        self.state_dir = Path(state_dir)
        self.adb = adb
        self.adb_port = adb_port
        self.instance = str(uuid.uuid4())
        self.sequence = 0
        self.stopping = threading.Event()

    def api(self, path, body):
        response = self.client.post('/v1/worker/'+path, json=body)
        response.raise_for_status()
        return response.json()

    def argv(self, args):
        return [self.adb, '-H', '127.0.0.1', '-P', str(self.adb_port), *args]

    def env(self):
        # ADB must not inherit coordinator credentials or remote endpoint overrides.
        return {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'LANG', 'SYSTEMROOT') if key in os.environ}

    def inventory(self):
        try:
            listing = self.command(['devices', '-l'], threading.Event(), timeout=5)
            if not listing.startswith('List of devices attached'):
                raise ValueError('Incomplete ADB inventory')
            records = parse_devices(listing)
            result = {}
            wifi_deadline = time.monotonic() + 5
            for d in records:
                serial = d['serial']
                if not d['usb']:
                    # Existing WiFi connections only. Never adb connect or switch tcpip mode.
                    if d['state'] != 'device' or time.monotonic() >= wifi_deadline:
                        continue
                    try:
                        serial = self.command(['-t', str(d['transport_id']), 'shell', 'getprop', 'ro.serialno'],
                                              threading.Event(), timeout=min(3, wifi_deadline-time.monotonic()))
                    except (OSError, RuntimeError, LeaseLost, subprocess.SubprocessError):
                        continue  # A broken fallback must not hide healthy USB devices.
                    d['status'] = 'WIFI_FALLBACK'
                if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', serial):
                    continue
                old = result.get(serial)
                if old and old['status'] == 'USB_READY':
                    if d['status'] == 'USB_READY':
                        raise ValueError('Duplicate USB serial; refuse ambiguous inventory')
                    continue
                result[serial] = {k: d[k] for k in ('status', 'transport_id')}
                result[serial]['serial'] = serial
            return True, list(result.values())
        except (OSError, ValueError, RuntimeError, LeaseLost, subprocess.SubprocessError):
            log.warning('ADB inventory unavailable; report UNKNOWN')
            return False, []

    def publish_scan(self):
        complete, devices = self.inventory()
        self.sequence += 1
        self.api('scan', {'instance': self.instance, 'sequence': self.sequence, 'complete': complete, 'devices': devices})
        return devices if complete else []

    def command(self, args, guard, timeout=12):
        if self.stopping.is_set() or guard.is_set():
            raise LeaseLost()
        # A temp file bounds memory use; output is not copied into logs or API errors.
        import tempfile
        with tempfile.TemporaryFile() as output:
            proc = subprocess.Popen(self.argv(args), env=self.env(), stdout=output, stderr=subprocess.DEVNULL,
                                    start_new_session=True)
            deadline = time.monotonic()+timeout
            try:
                while proc.poll() is None:
                    if self.stopping.is_set() or guard.is_set() or time.monotonic() >= deadline:
                        raise LeaseLost()
                    if output.tell() > 65536:
                        raise LeaseLost()
                    time.sleep(0.1)
                if proc.returncode:
                    raise RuntimeError('ADB command failed')
                if output.tell() > 65536:
                    raise RuntimeError('ADB output exceeded limit')
                output.seek(0)
                return output.read(65536).decode(errors='replace').strip()
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=3)

    def execute(self, job, devices):
        from .models import Submit
        # Validate even a compromised/misconfigured coordinator cannot send arbitrary shell.
        spec = Submit(serial=job['serial'], operation=job['operation'], idempotency_key=job['idempotency_key'], **job['params'])
        guard = LeaseGuard()
        done = threading.Event()
        lease = {'instance': self.instance, 'token': job['token']}
        path = 'jobs/'+job['id']

        renewal_lock = threading.Lock()

        def confirm():
            with renewal_lock:
                if guard.is_set():
                    raise LeaseLost()
                started = (time.monotonic(), time.time())
                answer = self.api(path+'/heartbeat', lease)
                guard.renewed(started, min(20, answer.get('lease_seconds', 30)-5))
                if answer['cancel_requested']:
                    guard.set()
                    return False
                return True

        def renew():
            while not done.is_set():
                try:
                    if not confirm():
                        return
                except Exception:
                    guard.set()
                    return
                done.wait(5)

        thread = threading.Thread(target=renew, daemon=True)
        outcome, result = 'outcome_unknown', {}
        try:
            # Confirm again before each command; deadlines also guard a suspended process.
            if not confirm():
                outcome = 'cancelled'
                return
            thread.start()
            def command(args):
                if not confirm():
                    raise LeaseLost()
                return self.command(args, guard)
            lock_name = hashlib.sha256(spec.serial.encode()).hexdigest()
            with local_lock(self.state_dir/'devices'/lock_name):
                d = next((d for d in devices if d['serial'] == spec.serial), None)
                if not d or (d['status'] != 'USB_READY' and not (d['status'] == 'WIFI_FALLBACK' and spec.allow_wifi)):
                    raise LeaseLost()
                target = ['-t', str(d['transport_id'])]
                hardware = command(target+['shell', 'getprop', 'ro.serialno'])
                if hardware != spec.serial:
                    raise LeaseLost()
                if spec.operation == 'device-info':
                    value = command(target+['shell', 'getprop', 'ro.build.version.sdk'])
                    result = {'serial_verified': True, 'sdk': value[:16]}
                elif spec.operation == 'package-status':
                    # pm path exits 1 for a normally absent package. Listing succeeds
                    # with empty output; exact matching also avoids substring false positives.
                    value = command(target+['shell', 'pm', 'list', 'packages', spec.package])
                    result = {'installed': ('package:'+spec.package) in value.splitlines()}
                elif spec.operation == 'reboot':
                    command(target+['reboot'])
                    result = {'reboot_requested': True}  # Receipt, not proof of successful boot.
                result['transport'] = d['status']
                outcome = 'succeeded'
        except Exception:
            # A disconnect or timeout never automatically replays an action.
            outcome, result = 'outcome_unknown', {'reason': 'execution_interrupted'}
        finally:
            done.set()
            if thread.ident is not None:
                thread.join(timeout=6)
            # Persist even cancellation before execution or an unexpected handler error.
            self.finish(job['id'], {**lease, 'outcome': outcome, 'result': result})

    def finish(self, job_id, record):
        self.persist_result(job_id, record)
        try:
            self.api('jobs/'+job_id+'/complete', record)
            (self.state_dir/'results'/(job_id+'.json')).unlink(missing_ok=True)
        except Exception:
            log.warning('Result acknowledgment unavailable; retained for reconciliation')

    def persist_result(self, job_id, record):
        folder = self.state_dir/'results'
        folder.mkdir(parents=True, mode=0o700, exist_ok=True)
        target = folder/(job_id+'.json')
        temp = target.with_suffix('.tmp')
        with temp.open('w') as f:
            os.chmod(temp, 0o600)
            json.dump(record, f)
            f.flush()
            os.fsync(f.fileno())
        temp.replace(target)
        if os.name == 'posix':
            directory = os.open(folder, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

    def replay_results(self):
        for p in (self.state_dir/'results').glob('*.tmp'):
            p.rename(p.with_suffix('.interrupted'))
        for p in (self.state_dir/'results').glob('*.json'):
            try:
                self.api('jobs/'+p.stem+'/complete', json.loads(p.read_text()))
                p.unlink()
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (400, 404, 409, 422):
                    # Preserve evidence for an operator; never let stale results overwrite a new lease.
                    if e.response.status_code == 409:
                        try:
                            self.api('jobs/'+p.stem+'/evidence', json.loads(p.read_text()))
                        except httpx.HTTPStatusError as evidence_error:
                            if evidence_error.response.status_code not in (400, 404, 409, 422):
                                raise
                    p.rename(p.with_suffix('.reconcile'))
                else:
                    raise
            except (ValueError, TypeError):
                p.rename(p.with_suffix('.corrupt'))
                log.error('Malformed local result retained for operator inspection')

    def run(self):
        os.umask(0o077)
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
        failures = 0
        with local_lock(self.state_dir/'worker.lock'):
            self.api('boot', {'instance': self.instance})
            while not self.stopping.is_set():
                try:
                    self.replay_results()
                    devices = self.publish_scan()
                    job = self.api('claim', {'instance': self.instance})
                    if job:
                        self.execute(job, devices)
                    failures = 0
                except Exception:
                    log.warning('Worker paused after API/ADB error; no new device command issued')
                    failures = min(failures+1, 6)
                self.stopping.wait(min(60, 3 * 2**failures))
