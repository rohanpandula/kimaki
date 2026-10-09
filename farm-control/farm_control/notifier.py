"""One durable summary sender, independent of job success.

Use a dedicated notification bot. Sharing its token with another REST client would
share Discord buckets without sharing this sender's learned backoff state.
"""
import hashlib
import logging
import math
import time
import httpx

log = logging.getLogger(__name__)


def nonce(project, version):
    return hashlib.sha256(f'{project}:{version}'.encode()).hexdigest()[:25]


def retry_delay(response):
    values = [1.0]
    for name in ('Retry-After', 'X-RateLimit-Reset-After'):
        try:
            values.append(float(response.headers.get(name, 0)))
        except (TypeError, ValueError):
            pass
    if response.status_code == 429:
        try:
            values.append(float(response.json().get('retry_after', 0)))
        except (ValueError, TypeError, AttributeError):
            pass
    return max(v for v in values if math.isfinite(v)) + 0.1


class Notifier:
    def __init__(self, store, token, channels, client=None, interval=30):
        if len(set(channels.values())) != len(channels):
            raise ValueError('Use a distinct Discord channel per project')
        if any(not str(v).isdigit() for v in channels.values()):
            raise ValueError('Discord channel IDs must be numeric')
        self.store, self.channels, self.interval = store, channels, max(10, interval)
        self.client = client or httpx.Client(base_url='https://discord.com/api/v10',
            headers={'Authorization': 'Bot '+token, 'User-Agent': 'DiscordBot (https://github.com/rohanpandula/kimaki, 0.1)'}, timeout=10)

    def once(self):
        # Session advisory lock prevents overlapping senders, including during HTTP waits.
        with self.store.connect() as lock:
            lock.autocommit = True
            if not lock.execute('SELECT pg_try_advisory_lock(19182611) AS acquired').fetchone()['acquired']:
                return
            try:
                self.send_due(lock)
            finally:
                lock.execute('SELECT pg_advisory_unlock(19182611)')

    def send_due(self, c):
        row = c.execute("""SELECT * FROM farm_outbox WHERE version>delivered_version AND NOT disabled
            AND next_attempt <= clock_timestamp()
            AND (SELECT blocked_until <= clock_timestamp() FROM farm_sender WHERE id=1)
            ORDER BY next_attempt LIMIT 1""").fetchone()
        if not row:
            return
        channel = self.channels.get(row['project'])
        if not channel:
            c.execute("UPDATE farm_outbox SET next_attempt=clock_timestamp()+interval '5 minutes' WHERE project=%s", (row['project'],))
            return
        counts = c.execute('SELECT state,count(*) AS n FROM farm_jobs WHERE project=%s GROUP BY state ORDER BY state', (row['project'],)).fetchall()
        content = 'Farm status: ' + ', '.join(f'{r["state"]}: {r["n"]}' for r in counts)
        body = {'content': content[:1900], 'allowed_mentions': {'parse': []},
                'nonce': nonce(row['project'], row['version']), 'enforce_nonce': True}
        try:
            response = self.client.post('/channels/'+str(channel)+'/messages', json=body)
        except httpx.HTTPError:
            self.defer(c, row, min(300, 2**min(row['failures']+1, 8)))
            return
        if response.status_code == 429:
            wait = retry_delay(response)
            self.defer(c, row, wait, count_failure=False)
            # Conservatively pause the sole sender globally, including unknown/malformed scopes.
            c.execute("UPDATE farm_sender SET blocked_until=clock_timestamp()+%s*interval '1 second' WHERE id=1", (wait,))
            return
        if 400 <= response.status_code < 500 and response.status_code != 408:
            c.execute('UPDATE farm_outbox SET disabled=true WHERE project=%s', (row['project'],))
            if response.status_code == 401:
                c.execute('UPDATE farm_outbox SET disabled=true')
            log.error('Notification delivery disabled after HTTP %s; check bot credentials/channel access', response.status_code)
            return
        if not response.is_success:
            self.defer(c, row, min(300, 2**min(row['failures']+1, 8)))
            return
        wait = self.interval
        if response.headers.get('X-RateLimit-Remaining') == '0':
            wait = max(wait, retry_delay(response))
        c.execute("""UPDATE farm_outbox SET delivered_version=%s,failures=0,
            next_attempt=clock_timestamp()+%s*interval '1 second' WHERE project=%s""",
            (row['version'], wait, row['project']))

    def defer(self, c, row, delay, count_failure=True):
        c.execute("""UPDATE farm_outbox SET failures=failures+%s,disabled=(%s AND failures>=19),
            next_attempt=clock_timestamp()+%s*interval '1 second' WHERE project=%s""", (int(count_failure), count_failure, delay, row['project']))
        log.warning('Notification deferred; project=%s', row['project'])

    def run(self):
        while True:
            try:
                self.once()
            except Exception:
                log.error('Notification sender unavailable; jobs continue independently')
            time.sleep(1)
