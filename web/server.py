"""
selpi web UI: live values, all settings, config backups + diffs, log downloads. Read-only towards the
SP PRO except what a log download needs (download mode 0xa251 and the log search ranges), and configuration
writes from the Configuration page when the app option allow_config_writes is on (web/settings_ui.py).

Runs as the Home Assistant add-on (ingress) or locally:
    SELPI_DATA_DIR=/tmp/selpi .venv/bin/python -m web.server --port 8099
Connection settings come from the usual SELPI_* environment (TCP to the ESP, or Select.Live).
"""
import argparse
import csv
import io
import json
import logging
import os
import re
import threading
import time
import traceback
import uuid
import zipfile
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import settings
from sppro import clock, config, config_write, live, logs, multiphase
from sppro.reader import Reader

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get('SELPI_DATA_DIR', os.path.join(HERE, '..', 'data'))
BACKUPS = os.path.join(DATA, 'backups')
LOG_FILES = {'events': 'EventData', 'detailed': 'DetailedData', 'daily': 'DailySummaryData'}
# HA ingress connects from the Supervisor; anything else is refused unless SELPI_WEB_ALLOW_ANY=1,
# and then only for the read-only endpoints in ANON_READ_PATHS (never writes/controls/config reads).
INGRESS_PEERS = {'172.30.32.2', '127.0.0.1', '::1'}
ANON_READ_PATHS = {'/api/live', '/api/status'}   # safe to expose to a non-ingress collector (e.g. Grafana)

# One policy for the shared SP PRO link: every operation is time-bounded (so nothing — not even the automatic
# clock sync or backup — can hold the lock indefinitely and wedge polls/pages), and reads wait a bounded time
# for the lock instead of failing the instant it is in use.
READ_WAIT = 30            # s a read waits for the link before giving up (then Busy)
BREAKER_FAILS = 3         # consecutive link failures before the circuit breaker trips (fail fast)
BREAKER_COOLDOWN = 20     # s to serve Busy instantly between recovery probes while the breaker is tripped
CLOCK_DEADLINE = 25       # the automatic ops must stay short so they can't block everything
BACKUP_DEADLINE = 90
LOG_DEADLINE = 240        # a manual log download may legitimately run longer than a poll, but still bounded so a
                          # stalled link can't hold the lock (and block polls) indefinitely (was 120, which cut
                          # real downloads short over a slow Select.Live link)
LIVE_DEADLINE = 25
SETTINGS_DEADLINE = 20
_LIVE_FEATURES = {}       # unit key -> present feature set; static per unit, so probe once, not every 15 s Live refresh


def _gate(r, unit_key):
    """(features, map_version) for gating the Live page, reusing the link's cached version block and a one-off
    feature probe per unit (the Live page auto-refreshes every 15 s; don't re-probe each time)."""
    if unit_key not in _LIVE_FEATURES:
        _LIVE_FEATURES[unit_key] = live.present_features(r)
    return _LIVE_FEATURES[unit_key], (SPPRO.versions or {}).get('memory_map')
CONFIG_DEADLINE = 20
WRITE_DEADLINE = 60
# How long a kept-open link may sit idle before it is dropped. On Select.Live, keep it comfortably above the
# poll interval so steady polling reuses one logged-in session instead of reconnecting (and re-logging in) on
# the 60 s boundary — connection/login churn is what trips Select.Live. A failure still drops the session at
# once (so SP LINK can take the device), and it closes after this once polling stops.
SELECTLIVE_IDLE = 150
TCP_IDLE = 60


class Busy(Exception):
    pass


class _Flight:
    """Shared outcome for single-flight coalescing: identical concurrent reads wait on one and reuse its result."""
    __slots__ = ('event', 'result', 'error')

    def __init__(self):
        self.event = threading.Event()
        self.result = None
        self.error = None


class Inverter:
    """One shared SP PRO link; every use holds the lock, so requests never interleave. Identical concurrent reads
    are coalesced (pass key=): a second caller with the same key waits for the first and reuses its result instead
    of running a duplicate over the (slow) link."""

    def __init__(self):
        self.lock = threading.Lock()
        self.reader = None
        self.versions = None
        self.version_problems = []
        self.last_ok = None
        self.last_error = None
        self.busy = None          # what holds the lock (shown in the page)
        self.keep_open = 0        # > 0 while an SP LINK proxy client is connected: keep the link up
        self._inflight = {}       # key -> _Flight: coalesces duplicate in-flight reads
        self._inflight_lock = threading.Lock()
        # circuit breaker: once the link has failed repeatedly (e.g. Select.Live won't reconnect — the device
        # session is still held server-side), serve Busy instantly instead of letting every caller burn the full
        # ~25 s retry stack. One probe is let through per cooldown to test recovery; a success clears it.
        self._fails = 0           # consecutive failures
        self._backoff_until = 0.0 # monotonic; > now = breaker tripped
        self._breaker_lock = threading.Lock()
        self.units = None         # cached multiphase unit list (sppro.multiphase.detect); None until discovered
        self.multi = False        # more than one unit on the bus

    def use(self, what, fn, wait=False, check_versions=True, deadline=None, key=None):
        """Run fn(reader) holding the link lock. wait: False = fail at once if busy, True = wait as long as it
        takes, a number = wait that many seconds for the lock. deadline: bound the whole operation so a stalled
        link can't hold the lock (and block polls/pages) indefinitely; the reader stops retrying past it and
        raises. key: coalesce identical in-flight reads — a second caller with the same key waits for the first
        and reuses its result (or re-raises its error) rather than running a duplicate. Use key only for
        idempotent reads, never writes."""
        if key is None:
            return self._run(what, fn, wait, check_versions, deadline)
        with self._inflight_lock:
            flight = self._inflight.get(key)
            lead = flight is None
            if lead:
                flight = self._inflight[key] = _Flight()
        if not lead:                                      # a duplicate: wait for the in-flight one, reuse its outcome
            budget = (wait if type(wait) in (int, float) else 60) + (deadline or 0) + 5
            if not flight.event.wait(timeout=budget):
                raise Busy('busy: %s' % self.busy)
            if flight.error is not None:
                raise flight.error
            return flight.result
        try:
            flight.result = self._run(what, fn, wait, check_versions, deadline)
            return flight.result
        except Exception as e:
            flight.error = e
            raise
        finally:
            with self._inflight_lock:
                self._inflight.pop(key, None)
            flight.event.set()

    def _breaker_ok(self):
        """False while the link is in a failure backoff (fail fast). When the cooldown has elapsed, let exactly
        one caller through as a recovery probe and re-arm the backoff so the others keep failing fast until it
        succeeds."""
        with self._breaker_lock:
            now = time.monotonic()
            if self._backoff_until and now < self._backoff_until:
                return False
            if self._backoff_until:                        # cooldown elapsed: this caller probes; re-arm for others
                self._backoff_until = now + BREAKER_COOLDOWN
            return True

    def _breaker_record(self, ok):
        with self._breaker_lock:
            if ok:
                if self._fails >= BREAKER_FAILS:
                    logging.info('SP PRO link recovered after %d failures', self._fails)
                self._fails = 0
                self._backoff_until = 0.0
            else:
                self._fails += 1
                if self._fails == BREAKER_FAILS:           # just tripped: log it once
                    logging.warning('SP PRO link down after %d failures (%s); backing off, probing every %ds',
                                    self._fails, self.last_error, BREAKER_COOLDOWN)
                if self._fails >= BREAKER_FAILS:
                    self._backoff_until = time.monotonic() + BREAKER_COOLDOWN

    @property
    def link_down(self):
        """True while the circuit breaker is tripped (repeated link/reconnect failures)."""
        return self._fails >= BREAKER_FAILS

    def unit_list(self, refresh=False, wait=False, deadline=None):
        """Discover and cache the units on the bus (>= 1; single inverter = one). Re-reads on refresh.
        Sets self.multi. detect() re-addresses over the link, so it runs under the lock like any read."""
        if self.units is not None and not refresh:
            return self.units
        us = self.use('discover units', multiphase.detect, wait=wait, deadline=deadline, key='units')
        self.units = us
        self.multi = len(us) > 1
        return us

    def read_unit(self, unit, fn, **kw):
        """Run read fn(reader) against a specific unit (re-addressing first when multi). kw -> use()."""
        return self.use(kw.pop('what', 'unit read'), multiphase.for_unit(unit, self.multi, fn), **kw)

    def _run(self, what, fn, wait, check_versions, deadline):
        if not self._breaker_ok():          # link is in a known-bad backoff: fail fast, don't burn the retry stack
            raise Busy('busy: %s' % (self.last_error or 'link down, reconnecting'))
        got = self.lock.acquire(blocking=False) if wait is False else \
            self.lock.acquire() if wait is True else self.lock.acquire(timeout=wait)
        if not got:
            raise Busy('busy: %s' % self.busy)
        self.busy = what
        try:
            if self.reader is None:
                self.reader = Reader()
            self.reader.deadline = (time.monotonic() + deadline) if deadline else None
            if self.versions is None and check_versions:
                self.versions = live.versions(self.reader)
                self.version_problems = live.version_problems(self.versions)
            out = fn(self.reader)
            self.last_ok, self.last_error = datetime.now(), None
            self._breaker_record(True)
            return out
        except Exception as e:
            self.last_error = '%s: %s' % (what, e)
            if self.reader is not None:
                self.reader.close()     # tear the dead link down cleanly (prompt FIN) so Select.Live frees the
                self.reader = None      # device session fast, rather than leaving the socket to GC; reconnect next time
            # keep self.versions cached: the firmware version block is constant, so don't re-read it after a failure
            self._breaker_record(False)
            raise
        finally:
            if self.reader is not None:
                self.reader.deadline = None
                # free the ESP's single proxy slot for SP LINK between jobs (Select.Live stays connected)
                if os.environ.get('SELPI_CONNECTION_TYPE') == 'TCP' and not self.keep_open:
                    self.reader.close()
            self.busy = None
            self.lock.release()


    def close_if_idle(self, idle_s=None):
        """Drop a kept-open link (Select.Live) once nothing has used it for idle_s. Default idle is connection-aware
        (SELECTLIVE_IDLE for Select.Live, so steady polling keeps one session rather than reconnecting every poll)."""
        if idle_s is None:
            idle_s = SELECTLIVE_IDLE if os.environ.get('SELPI_CONNECTION_TYPE') == 'SelectLive' else TCP_IDLE
        if self.reader is None or self.last_ok is None or self.keep_open:
            return
        if (datetime.now() - self.last_ok).total_seconds() < idle_s or not self.lock.acquire(blocking=False):
            return
        try:
            if self.reader is not None:
                self.reader.close()
                self.reader = None
                logging.info('closed idle SP PRO link')
        finally:
            self.lock.release()


CLOCK = {}


def clock_syncer(max_drift):
    """Hourly: set the SP PRO clock from this machine's (HA's NTP) time when it is out by more than max_drift."""
    lead = 2.0 if os.environ.get('SELPI_CONNECTION_TYPE') == 'SelectLive' else 1.0
    time.sleep(30)
    while True:
        try:
            before, after = SPPRO.use('clock check', lambda r: clock.sync(r, max_drift, lead), wait=READ_WAIT, deadline=CLOCK_DEADLINE)
            CLOCK.update(checked=datetime.now(), drift=before, set_to=after, error=None)
            logging.info('clock check: drift %+d s%s', before, '' if after is None else ', set (now %+d s)' % after)
        except Exception as e:
            CLOCK.update(checked=datetime.now(), error=str(e))
            logging.warning('clock check failed: %s', e)
        time.sleep(3600)


def idle_closer():
    while True:
        time.sleep(15)
        SPPRO.close_if_idle()


SPPRO = Inverter()
JOBS = {}


# ------------------------------------------------------------------------------------------- backups

def backup_now(reason='manual'):
    memory = SPPRO.use('config backup', config.read_memory, wait=READ_WAIT, deadline=BACKUP_DEADLINE)
    stamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    folder = os.path.join(BACKUPS, stamp)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, 'settings.json'), 'w') as fh:
        json.dump({'created': stamp, 'reason': reason, 'versions': SPPRO.versions,
                   'settings': config.settings(memory),
                   'memory': {'%x' % k: v for k, v in sorted(memory.items())}}, fh, indent=1)
    for name, text in (('Config_%s.SPLC' % stamp, config.to_splc(memory)),
                       ('ServiceSettings_%s.CSV' % stamp, config.to_service_csv(memory))):
        with open(os.path.join(folder, name), 'w', newline='') as fh:
            fh.write(text)
    logging.info('backup %s (%s)', stamp, reason)
    return stamp


def list_backups():
    if not os.path.isdir(BACKUPS):
        return []
    out = []
    for name in sorted(os.listdir(BACKUPS), reverse=True):
        meta = os.path.join(BACKUPS, name, 'settings.json')
        if not os.path.exists(meta):
            continue
        with open(meta) as fh:
            reason = json.load(fh).get('reason', '')
        out.append({'name': name, 'reason': reason, 'files': sorted(os.listdir(os.path.join(BACKUPS, name)))})
    return out


def load_backup(name):
    if not re.fullmatch(r'[0-9_-]+', name or ''):
        raise ValueError('bad backup name')
    with open(os.path.join(BACKUPS, name, 'settings.json')) as fh:
        return json.load(fh)


def delete_backup(name):
    if not re.fullmatch(r'[0-9_-]+', name or ''):
        raise ValueError('bad backup name')
    folder = os.path.join(BACKUPS, name)
    if not os.path.isdir(folder):
        raise ValueError('no such backup')
    for f in os.listdir(folder):
        os.remove(os.path.join(folder, f))
    os.rmdir(folder)
    logging.info('deleted backup %s', name)


def diff(a, b):
    sa = {s['address']: s for s in load_backup(a)['settings']}
    sb = {s['address']: s for s in load_backup(b)['settings']}
    out = []
    for addr in sorted(set(sa) | set(sb), key=lambda x: int(x, 16)):
        va, vb = sa.get(addr, {}).get('value'), sb.get(addr, {}).get('value')
        if va != vb:
            s = sa.get(addr) or sb.get(addr)
            out.append({'name': s['name'], 'section': s['section'], 'address': addr, 'a': va, 'b': vb})
    return out


def prune(keep):
    items = [b['name'] for b in list_backups() if b['reason'] == 'scheduled']
    for name in items[keep:]:
        folder = os.path.join(BACKUPS, name)
        for f in os.listdir(folder):
            os.remove(os.path.join(folder, f))
        os.rmdir(folder)
        logging.info('pruned backup %s', name)


def scheduler(at, keep):
    """Daily config backup at HH:MM (local time)."""
    hh, mm = (int(x) for x in at.split(':'))
    while True:
        now = datetime.now()
        nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        time.sleep((nxt - now).total_seconds())
        try:
            backup_now('scheduled')
            prune(keep)
        except Exception:
            logging.exception('scheduled backup failed')


# ---------------------------------------------------------------------------------------------- jobs

def start_job(kind, fn):
    jid = uuid.uuid4().hex[:12]
    job = {'id': jid, 'kind': kind, 'state': 'running', 'message': 'starting', 'started': time.time()}
    JOBS[jid] = job

    def run():
        try:
            job['result'] = fn(job)
            job['state'] = 'done'
            job['message'] = 'done'
        except Exception as e:
            logging.exception('%s failed', kind)
            job['state'], job['message'] = 'failed', str(e)
    threading.Thread(target=run, daemon=True).start()
    return job


def logs_job(since, until, keys):
    def fn(job):
        def work(reader):
            mode = reader.words(logs.DOWNLOAD_MODE, 1)[0]
            if mode != 0:   # someone else (SP LINK / Select.Live / installer) is downloading
                raise Busy('the SP PRO is already in log download mode (0xa251=%d); try later' % mode)
            job['message'] = 'reading scale factors'
            scales = logs.live_scales(reader)
            job['message'] = 'searching and downloading %s' % ', '.join(keys)

            def progress(key, index, phase, done, total):
                title = logs.LOGS[key].title
                part = (done / total) if total else (0.0 if phase == 'search' else 1.0)
                job['progress'] = {'log': title, 'log_index': index + 1, 'logs': len(keys), 'phase': phase,
                                   'done': done, 'total': total,
                                   'percent': round(100 * (index + part) / len(keys), 1)}
                job['message'] = ('%s (%d of %d): SP PRO is searching…' % (title, index + 1, len(keys))
                                  if phase == 'search' else
                                  '%s (%d of %d): %d of %d records' % (title, index + 1, len(keys), done, total))
            return scales, logs.download(reader, keys, since, until, progress)
        job['message'] = 'waiting for the SP PRO link' + (' (busy: %s)' % SPPRO.busy if SPPRO.busy else '')
        scales, records = SPPRO.use('log download', work, wait=READ_WAIT, deadline=LOG_DEADLINE)
        job['message'] = 'building the CSV files'
        job['progress'] = dict(job.get('progress') or {}, percent=100)
        merged = {}
        for key, recs in records.items():
            header, rows = logs.rows(key, recs, scales)
            target = 'events' if key == 'alerts' else key
            merged.setdefault(target, (header, []))[1].extend(rows)
        stamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        buf = io.BytesIO()
        counts = {}
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
            for key, (header, rows) in merged.items():
                rows.sort(key=lambda r: float(r[0]))
                text = io.StringIO()
                w = csv.writer(text, quoting=csv.QUOTE_ALL, lineterminator='\r\n')
                w.writerow(header)
                w.writerows(rows)
                z.writestr('%s_%s.CSV' % (LOG_FILES[key], stamp), text.getvalue())
                counts[LOG_FILES[key]] = len(rows)
        job['file'] = buf.getvalue()
        job['filename'] = 'sppro-logs_%s.zip' % stamp
        return counts
    return start_job('logs', fn)


# ---------------------------------------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = 'selpi'

    def log_message(self, fmt, *args):
        logging.debug('%s %s', self.address_string(), fmt % args)

    def _send(self, code, body, ctype='application/json', headers=None):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _allowed(self):
        peer = self.client_address[0]
        if peer.startswith('::ffff:'):   # IPv4 peer on the dual-stack socket
            peer = peer[7:]
        if peer in INGRESS_PEERS:         # HA Supervisor / loopback: full access
            return True
        if os.environ.get('SELPI_WEB_ALLOW_ANY') == '1':
            # escape hatch for a non-ingress collector: read-only, and only the safe endpoints.
            path = urlparse(self.path).path.rstrip('/') or '/'
            if self.command == 'GET' and path in ANON_READ_PATHS:
                return True
            logging.warning('refused %s %s from %s (SELPI_WEB_ALLOW_ANY is read-only: GET %s)',
                            self.command, path, peer, ', '.join(sorted(ANON_READ_PATHS)))
            self._send(403, {'error': 'read-only: only GET %s are available to non-ingress clients'
                             % ' / '.join(sorted(ANON_READ_PATHS))})
            return False
        logging.warning('refused request from %s (not HA ingress)', peer)
        self._send(403, {'error': 'only via Home Assistant ingress'})
        return False

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        return json.loads(self.rfile.read(n) or b'{}') if n else {}

    def do_GET(self):
        if not self._allowed():
            return
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        path = url.path.rstrip('/') or '/'
        try:
            if path == '/':
                with open(os.path.join(HERE, 'index.html'), 'rb') as fh:
                    return self._send(200, fh.read(), 'text/html; charset=utf-8')
            if path == '/api/status':
                return self._send(200, {
                    'connection': os.environ.get('SELPI_CONNECTION_TYPE', ''),
                    'target': (os.environ.get('SELPI_CONNECTION_TCP_HOSTNAME', '') + ':' +
                               os.environ.get('SELPI_CONNECTION_TCP_PORT', ''))
                    if os.environ.get('SELPI_CONNECTION_TYPE') == 'TCP' else 'Select.Live',
                    'versions': SPPRO.versions, 'version_problems': SPPRO.version_problems,
                    'busy': SPPRO.busy, 'last_ok': SPPRO.last_ok, 'last_error': SPPRO.last_error,
                    'backups': len(list_backups()),
                    'backup_time': os.environ.get('SELPI_BACKUP_TIME', ''),
                    'clock_sync': os.environ.get('SELPI_CLOCK_SYNC', '').lower() == 'true', 'clock': CLOCK,
                    'cache_ttl': settings.cache_ttl(), 'stale_grace': settings.stale_grace(),
                    'jobs': [{k: v for k, v in j.items() if k != 'file'} for j in JOBS.values()][-5:]})
            if path == '/api/units':
                units = list(SPPRO.unit_list(wait=READ_WAIT, deadline=LIVE_DEADLINE))
                if SPPRO.multi:   # a synthetic whole-site unit; its values are the roll-up the add-on computes
                    units = units + [{'index': 'system', 'label': 'System', 'serial': None,
                                      'phase': None, 'role': 'system'}]
                return self._send(200, {'multi': SPPRO.multi, 'units': units})
            if path == '/api/live':
                screens = [s for s in q.get('screens', 'now,today').split(',') if s in live.SCREENS] or None
                unit = q.get('unit')
                if unit == 'system' and SPPRO.multi:
                    # whole-site totals: read every unit under the one lock (coherent snapshot), then combine each
                    # field (sum power/energy/current, avg voltage/SoC/freq); non-aggregatable fields are dropped.
                    def read_system(r):
                        cols, template = {}, None
                        for mu in SPPRO.unit_list():
                            multiphase.change_destination(r, mu['index'], mu['serial'])
                            feats, mapv = _gate(r, mu['index'])
                            rows = live.snapshot(r, screens, features=feats, map_version=mapv)
                            if template is None:
                                template = rows
                            for row in rows:
                                cols.setdefault(row['key'], []).append(row['value'])
                        out = []
                        for row in (template or []):
                            agg = multiphase.aggregate(row['unit'], cols.get(row['key'], []), row['key'])
                            if agg is None:
                                continue
                            text = ('%.3f' % agg).rstrip('0').rstrip('.') if isinstance(agg, float) else str(agg)
                            out.append({**row, 'value': agg, 'text': text})
                        return out
                    rows = SPPRO.use('live read system', read_system, wait=READ_WAIT, deadline=LIVE_DEADLINE,
                                     key='live:system:' + ','.join(screens or []))
                    return self._send(200, {'rows': rows, 'unit': 'system',
                                            'version_problems': SPPRO.version_problems,
                                            'at': datetime.now().isoformat(timespec='seconds')})
                u = None
                if unit not in (None, '', 'system'):
                    try:
                        ui = int(unit)
                    except ValueError:
                        ui = None
                    u = next((x for x in (SPPRO.units or []) if x['index'] == ui), None)
                read = multiphase.for_unit(u, SPPRO.multi,
                                           lambda r: live.snapshot(r, screens, *_gate(r, unit)))
                rows = SPPRO.use('live read', read, wait=READ_WAIT, deadline=LIVE_DEADLINE,
                                 key='live:%s:' % unit + ','.join(screens or []))
                return self._send(200, {'rows': rows, 'unit': unit, 'version_problems': SPPRO.version_problems,
                                        'at': datetime.now().isoformat(timespec='seconds')})
            if path == '/api/settings':
                ttl = settings.cache_ttl()   # 0 (local link) = always read fresh; >0 (Select.Live) = serve a recent read
                memory = config.cached_memory(ttl) if (ttl > 0 and not q.get('refresh')) else None
                cached = memory is not None
                if memory is None:
                    try:
                        memory = SPPRO.use('settings read', config.read_memory, wait=READ_WAIT,
                                           deadline=SETTINGS_DEADLINE, key='settings')
                    except Exception:
                        memory = config.cached_memory()      # link stalled/busy: fall back to the last good read
                        cached = True
                        if memory is None:
                            raise
                return self._send(200, {'settings': config.settings(memory), 'cached': cached,
                                        'at': datetime.now().isoformat(timespec='seconds')})
            if path == '/api/config/layout':
                from web import settings_ui
                return self._send(200, settings_ui.layout())
            if path == '/api/config/read':
                from web import settings_ui
                if settings.cache_ttl() > 0 and not q.get('refresh'):
                    hit = settings_ui.cached(settings.cache_ttl())   # reopen from a recent read: no link read over a slow link
                    if hit is not None:
                        return self._send(200, hit)
                try:
                    return self._send(200, SPPRO.use('configuration read', settings_ui.read, wait=READ_WAIT,
                                                     deadline=CONFIG_DEADLINE, key='config'))
                except Exception:
                    hit = settings_ui.cached()           # link stalled/busy: fall back to the last good config read
                    if hit is not None:
                        return self._send(200, hit)
                    raise
            if path == '/api/backups':
                return self._send(200, {'backups': list_backups()})
            m = re.fullmatch(r'/api/backups/([0-9_-]+)/([A-Za-z0-9_.-]+)', path)
            if m:
                f = os.path.join(BACKUPS, m.group(1), m.group(2))
                if not os.path.isfile(f):
                    return self._send(404, {'error': 'no such file'})
                with open(f, 'rb') as fh:
                    return self._send(200, fh.read(), 'application/octet-stream',
                                      {'Content-Disposition': 'attachment; filename="%s"' % m.group(2)})
            if path == '/api/diff':
                return self._send(200, {'changes': diff(q['a'], q['b'])})
            m = re.fullmatch(r'/api/jobs/([0-9a-f]+)(/file)?', path)
            if m and m.group(1) in JOBS:
                job = JOBS[m.group(1)]
                if m.group(2):
                    if 'file' not in job:
                        return self._send(404, {'error': 'not ready'})
                    return self._send(200, job['file'], 'application/zip',
                                      {'Content-Disposition': 'attachment; filename="%s"' % job['filename']})
                return self._send(200, {k: v for k, v in job.items() if k != 'file'})
            return self._send(404, {'error': 'not found'})
        except Busy as e:
            return self._send(409, {'error': str(e)})
        except Exception as e:
            logging.debug(traceback.format_exc())
            return self._send(500, {'error': str(e)})

    def do_POST(self):
        if not self._allowed():
            return
        path = urlparse(self.path).path.rstrip('/')
        try:
            body = self._body()
            if path == '/api/backups':
                return self._send(200, start_job('backup', lambda job: backup_now('manual')))
            if path == '/api/config/check':
                from web import settings_ui
                return self._send(200, SPPRO.use('configuration check', lambda r: settings_ui.check(
                    r, body.get('read_id'), body.get('changes') or {}), wait=READ_WAIT, deadline=CONFIG_DEADLINE))
            if path == '/api/config/write':
                from web import settings_ui
                logging.warning('configuration write from the web UI: %s', ', '.join(sorted((body.get('changes') or {}))))
                try:
                    out = SPPRO.use('configuration write', lambda r: settings_ui.write(
                        r, body.get('read_id'), body.get('changes') or {}, body.get('passcode', ''),
                        allow_out_of_range=bool(body.get('allow_out_of_range')),
                        ignore_rules=body.get('ignore_rules') or [],
                        out_dir=os.path.join(DATA, 'config-writes')), wait=READ_WAIT, deadline=WRITE_DEADLINE)
                except PermissionError as e:
                    return self._send(403, {'error': str(e)})
                except config_write.WriteError as e:
                    return self._send(409, {'error': str(e)})
                return self._send(200, out)
            if path == '/api/logs':
                since = datetime.fromisoformat(body['since'])
                until = datetime.fromisoformat(body['until'])
                if len(body['until']) <= 10:   # a date: include the whole day
                    until = until.replace(hour=23, minute=59, second=59)
                keys = [k for k in body.get('logs', []) if k in logs.LOGS] or ['events', 'alerts']
                job = logs_job(since, until, keys)
                return self._send(200, {k: v for k, v in job.items() if k != 'file'})
            return self._send(404, {'error': 'not found'})
        except Exception as e:
            return self._send(400, {'error': str(e)})

    def do_DELETE(self):
        if not self._allowed():
            return
        path = urlparse(self.path).path.rstrip('/')
        try:
            m = re.fullmatch(r'/api/backups/([0-9_-]+)', path)
            if m:
                delete_backup(m.group(1))
                return self._send(200, {'deleted': m.group(1)})
            return self._send(404, {'error': 'not found'})
        except Exception as e:
            return self._send(400, {'error': str(e)})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=int(os.environ.get('SELPI_WEB_PORT', 8099)))
    ap.add_argument('--host', default='::')
    a = ap.parse_args()
    logging.basicConfig(level=os.environ.get('SELPI_LOG_LEVEL', 'INFO').upper(),
                        format='%(asctime)s %(levelname)s %(message)s')
    os.makedirs(BACKUPS, exist_ok=True)
    threading.Thread(target=idle_closer, daemon=True).start()
    if os.environ.get('SELPI_CLOCK_SYNC', '').lower() == 'true':
        drift = int(os.environ.get('SELPI_CLOCK_MAX_DRIFT', 10))
        threading.Thread(target=clock_syncer, args=(drift,), daemon=True).start()
        logging.info('clock sync on (hourly, max drift %d s)', drift)
    at = os.environ.get('SELPI_BACKUP_TIME', '')
    if at:
        keep = int(os.environ.get('SELPI_BACKUP_KEEP', 30))
        threading.Thread(target=scheduler, args=(at, keep), daemon=True).start()
        logging.info('daily backup at %s, keeping %d', at, keep)
    proxy = None
    proxy_port = int(os.environ.get('SELPI_PROXY_PORT') or 0)
    if proxy_port:
        from web import proxy as splink_proxy
        split = lambda v: [x for x in (v or '').replace(';', ',').split(',') if x.strip()]
        allowed = split(os.environ.get('SELPI_PROXY_ALLOWED')) or list(splink_proxy.DEFAULT_ALLOWED)
        proxy = splink_proxy.Proxy(SPPRO, Busy, proxy_port, allowed, split(os.environ.get('SELPI_PROXY_WRITE')),
                                   os.environ.get('SELPI_PROXY_LOG_DOWNLOADS', 'true').lower() == 'true',
                                   host=a.host).start()
    api_port = int(os.environ.get('SELPI_API_PORT') or 0)
    if api_port:
        token = os.environ.get('SELPI_API_TOKEN', '')
        if not token:
            logging.warning('SELPI_API_PORT set but no SELPI_API_TOKEN: HA API not started')
        else:
            from web import api
            install_id = os.environ.get('SELPI_INSTALL_ID') or 'selpi'
            the_api = api.Api(SPPRO, Busy, backup_now, list_backups, CLOCK, install_id)
            the_api.proxy = proxy
            api.serve(the_api, api_port, token, a.host)
    ThreadingHTTPServer.address_family = __import__('socket').AF_INET6 if ':' in a.host else __import__('socket').AF_INET
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    logging.info('selpi web on port %d (%s)', a.port, os.environ.get('SELPI_CONNECTION_TYPE'))
    srv.serve_forever()


if __name__ == '__main__':
    main()
