"""
selpi HA API: what the selpi Home Assistant integration talks to (the Traccar pattern: the add-on owns
the SP PRO link, the integration only asks it for values). Separate port from the ingress UI, with a
bearer token, because HA core doesn't come through the ingress proxy.

    GET  /api/v1/info              unit id, connection, suggested poll interval, versions, firmware
    GET  /api/v1/fields            field catalog: key, name, unit, kind, enum options, HA hints, default
    GET  /api/v1/values?keys=a,b   current values (enum fields as their label); reads only the screens needed
    GET  /api/v1/diagnostics       link status, clock check, last backup
    POST /api/v1/backup            start a config backup
    POST /api/v1/button?name=generator|silence   press a front panel button (short press)
    POST /api/v1/button?name=on&confirm=outage   long press of the ON button: SP PRO ON <-> IDLE (IDLE = no AC output)
    POST /api/v1/control?name=generator&state=on|off   start / stop the generator if it isn't already (state-aware)
    POST /api/v1/control?name=output&state=on|idle     inverter output; idle (no AC output) needs confirm=outage
    POST /api/v1/control?name=equalise                 request an equalise charge (two long Generator presses)

Read-only towards the SP PRO (same reads as the Live page), except front panel button presses (generator
start/stop, silence alarm, and the ON button only with confirm=outage) and the controls built on them
(sppro/controls.py: they read the unit's state and press only when needed; 409 when the unit can't). Values are cached; if the link is busy (a backup or log download), the
last values come back with "stale": true.
"""
import hmac
import json
import logging
import os
import re
import socket
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from sppro import field_meta, live

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULTS = json.load(open(os.path.join(HERE, '..', 'sppro', 'default_fields.json'), encoding='utf-8'))
SCREEN_NAMES = {
    'NowSubTab': 'now', 'TodaySubTab': 'today', 'TechnicalDataSubTab': 'tech', 'DCHistorySubTab': 'DC history',
    'ACHistorySubTab': 'AC history', 'ACHistorySubTab2': 'AC history 2', 'ServiceReadings1': 'service',
    'ServiceSettings': 'service settings', 'Now_Today_Tech_KacoModelsCapacityAndOEM': 'unit',
}
VALUE_WAIT = 20        # seconds a values request waits for the link before answering from cache
STALE_AFTER = 600      # cache older than this isn't served


def _slug(name):
    return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')


def catalog():
    """Every field once (exact repeats + same-quantity twins dropped), defaults first with their HA names."""
    by_key = {f['key']: f for f in live.FIELDS}
    order = [k for k in DEFAULTS if k in by_key] + [f['key'] for f in live.FIELDS if f['key'] not in DEFAULTS]
    seen, names, out = set(), set(), []
    tables = live.tables()
    for k in order:
        if k in field_meta.HA_DROP:
            continue
        f = by_key[k]
        sig = (tuple(f['words']), f['formula'])
        if sig in seen:
            continue
        seen.add(sig)
        name = f['name']        # single source (overrides > curated default > SP LINK), already Title Case
        if _slug(name) in names:
            name = '%s (%s)' % (name, SCREEN_NAMES.get(f['screen'], f['screen']))
        if _slug(name) in names:
            name = '%s (%s)' % (name, k)
        names.add(_slug(name))
        entry = {'key': k, 'name': name, 'unit': f['unit'], 'kind': f['kind'], 'screen': f['screen'],
                 'default': k in DEFAULTS, 'note': f.get('note', '')}
        entry.update(field_meta.hints(f))
        if f['kind'] == 'enum':
            entry['options'] = sorted(set(tables.get(f['table'], {}).values()))
        out.append(entry)
    return out


class Api:
    def __init__(self, sppro, busy_exc, backup_fn, list_backups, clock, install_id):
        self.sppro, self.Busy, self.backup_fn, self.list_backups = sppro, busy_exc, backup_fn, list_backups
        self.clock, self.install_id = clock, install_id
        self.catalog = catalog()
        self.fields = {f['key']: f for f in live.FIELDS}
        self.cache = {}           # key -> (value, read at)
        self.firmware = None
        self.backup_running = False
        self.proxy = None         # web.proxy.Proxy when the SP LINK proxy is on
        self.last_control = {}    # control -> (time pressed, state code before the press)

    def info(self):
        conn = os.environ.get('SELPI_CONNECTION_TYPE', '')
        if self.firmware is None:
            try:
                self.values(['version_number'])
            except Exception as e:
                logging.debug('firmware read failed: %s', e)
        return {'id': self.install_id, 'connection': conn, 'scan_interval': 60 if conn == 'SelectLive' else 10,
                'model': 'SP PRO', 'manufacturer': 'Selectronic', 'firmware': self.firmware,
                'versions': self.sppro.versions, 'version_problems': self.sppro.version_problems,
                'buttons': sorted(API_BUTTONS) + ['on'], 'controls': sorted(CONTROLS)}

    def values(self, keys):
        asked = {live.KEY_ALIASES.get(k, k): k for k in keys}   # old (renamed) keys still answered
        keys = [k for k in asked if k in self.fields]
        fields = [self.fields[k] for k in keys]
        stale = False
        try:
            def read(r):
                mem = live.read(r, fields)
                return {f['key']: live.evaluate(f, mem) for f in fields}
            got = self.sppro.use('HA poll', read, wait=VALUE_WAIT) if fields else {}
            now = time.time()
            for k, (v, text) in got.items():
                self.cache[k] = (text if self.fields[k]['kind'] == 'enum' and v is not None else v, now)
            if 'version_number' in got and got['version_number'][0] is not None:
                self.firmware = got['version_number'][1]
        except self.Busy:
            stale = True
            if not any(k in self.cache for k in keys):
                raise
        now = time.time()
        out = {asked[k]: self.cache[k][0] for k in keys if k in self.cache and now - self.cache[k][1] < STALE_AFTER}
        return {'at': datetime.now().isoformat(timespec='seconds'), 'stale': stale, 'values': out,
                'version_problems': self.sppro.version_problems}

    def diagnostics(self):
        backups = self.list_backups()
        last = backups[0] if backups else None
        at = None
        if last:
            try:
                at = datetime.strptime(last['name'], '%Y-%m-%d_%H-%M-%S').astimezone().isoformat()
            except ValueError:
                pass
        last_ok = self.sppro.last_ok
        return {'last_ok': last_ok and last_ok.astimezone().isoformat(timespec='seconds'), 'last_error': self.sppro.last_error, 'busy': self.sppro.busy,
                'clock': self.clock, 'backup_running': self.backup_running,
                'last_backup': last and {'name': last['name'], 'reason': last.get('reason'), 'at': at},
                'proxy': self.proxy and dict(self.proxy.status, port=self.proxy.port)}

    def button(self, name, confirm=''):
        """Press a front panel button: generator / silence (short), or on (long; needs confirm='outage')."""
        long = False
        if name == 'on':
            if confirm != 'outage':
                raise ValueError('the ON button switches the SP PRO between ON and IDLE (no AC output): '
                                 'add confirm=outage')
            long = True
        elif name not in API_BUTTONS:
            raise ValueError('unknown button %r (one of %s, on)' % (name, ', '.join(sorted(API_BUTTONS))))
        from sppro import buttons
        logging.warning('front panel button press via the API: %s%s', name, ' (long)' if long else '')
        out = self.sppro.use('button %s' % name, lambda r: buttons.press(r, name, long=long), wait=30,
                             check_versions=False)
        return {'pressed': name, 'register': '0x%x' % out['register'], 'read_back': out['read_back']}

    def control(self, name, state='', confirm=''):
        """State-aware controls: generator on/off, output on/idle (idle needs confirm=outage), equalise."""
        from sppro import controls
        if name not in CONTROLS:
            raise ValueError('unknown control %r (one of %s)' % (name, ', '.join(sorted(CONTROLS))))
        allowed = {'generator': ('on', 'off'), 'output': ('on', 'idle'), 'equalise': ('',)}[name]
        if state not in allowed:
            raise ValueError('%s: state must be %s' % (name, ' or '.join(allowed) or 'empty'))
        if name == 'output' and state == 'idle' and confirm != 'outage':
            raise ValueError('output idle cuts the AC output (an outage): add confirm=outage')
        read_state = {'generator': controls.generator_state, 'output': controls.output_state}.get(name)

        def act(r):
            # a press the unit hasn't shown yet (same state as before it, within 30 s): don't press again
            if read_state and name in self.last_control:
                at, code = self.last_control[name]
                if time.monotonic() - at < CONTROL_SETTLE and read_state(r)['code'] == code:
                    raise controls.ControlError('the last %s press is still being processed; try again shortly' % name)
            before = read_state(r)['code'] if read_state else None
            if name == 'generator':
                out = controls.set_generator(r, state == 'on')
            elif name == 'output':
                out = controls.set_output(r, state == 'on')
            else:
                out = controls.equalise(r)
            if read_state and out.get('pressed'):
                self.last_control[name] = (time.monotonic(), before)
            return out
        if self.sppro.version_problems:
            raise ConflictError('SP PRO versions not verified (%s): not pressing'
                                % ', '.join(self.sppro.version_problems))
        logging.warning('control via the API: %s %s', name, state)
        try:
            out = self.sppro.use('control %s' % name, act, wait=60, check_versions=False)
        except controls.ControlError as e:
            raise ConflictError(str(e))
        result = {'control': name, 'state': state, 'pressed': out.get('pressed', True)}
        for k in ('status', 'mode', 'note', 'days_to_equalise_before', 'days_to_equalise_after'):
            if k in out:
                result[k] = out[k]
        return result

    def backup(self):
        if self.backup_running:
            return {'started': False, 'error': 'a backup is already running'}

        def run():
            self.backup_running = True
            try:
                self.backup_fn('ha')
            except Exception as e:
                logging.warning('HA backup failed: %s', e)
            finally:
                self.backup_running = False
        threading.Thread(target=run, daemon=True).start()
        return {'started': True}


API_BUTTONS = {'generator', 'silence'}   # short presses; 'on' (long, ON <-> IDLE) needs confirm=outage
CONTROLS = {'generator', 'output', 'equalise'}   # state-aware (sppro/controls.py)
CONTROL_SETTLE = 30                       # s: a press not yet visible in the unit's state blocks another


class ConflictError(Exception):
    """The SP PRO can't do it now (e.g. generator with the grid present): HTTP 409."""


def serve(api, port, token, host='::'):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'selpi-api'

        def log_message(self, fmt, *args):
            logging.debug('api %s %s', self.address_string(), fmt % args)

        def _send(self, code, body):
            data = json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorised(self):
            got = self.headers.get('Authorization', '')
            if token and hmac.compare_digest(got.encode(), ('Bearer ' + token).encode()):
                return True
            logging.warning('api: refused %s (bad or missing token)', self.client_address[0])
            self._send(401, {'error': 'bad or missing token'})
            return False

        def _handle(self, method):
            if not self._authorised():
                return
            url = urlparse(self.path)
            path = url.path.rstrip('/')
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if method == 'GET' and path == '/api/v1/info':
                    return self._send(200, api.info())
                if method == 'GET' and path == '/api/v1/fields':
                    return self._send(200, {'fields': api.catalog})
                if method == 'GET' and path == '/api/v1/values':
                    keys = [k for k in q.get('keys', '').split(',') if k]
                    return self._send(200, api.values(keys))
                if method == 'GET' and path == '/api/v1/diagnostics':
                    return self._send(200, api.diagnostics())
                if method == 'POST' and path == '/api/v1/backup':
                    return self._send(200, api.backup())
                if method == 'POST' and path == '/api/v1/button':
                    return self._send(200, api.button(q.get('name', ''), q.get('confirm', '')))
                if method == 'POST' and path == '/api/v1/control':
                    return self._send(200, api.control(q.get('name', ''), q.get('state', ''), q.get('confirm', '')))
                return self._send(404, {'error': 'not found'})
            except api.Busy as e:
                return self._send(503, {'error': str(e)})
            except PermissionError as e:
                return self._send(403, {'error': str(e)})
            except ConflictError as e:
                return self._send(409, {'error': str(e)})
            except ValueError as e:
                return self._send(400, {'error': str(e)})
            except Exception as e:
                logging.debug('api error', exc_info=True)
                return self._send(502, {'error': str(e)})

        def do_GET(self):
            self._handle('GET')

        def do_POST(self):
            self._handle('POST')

    class Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6 if ':' in host else socket.AF_INET
        daemon_threads = True

    srv = Server((host, port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    logging.info('HA API on port %d', port)
    return srv
