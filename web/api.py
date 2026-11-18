"""
selpi HA API: what the selpi Home Assistant integration talks to (the Traccar pattern: the add-on owns
the SP PRO link, the integration only asks it for values). Separate port from the ingress UI, with a
bearer token, because HA core doesn't come through the ingress proxy.

    GET  /api/v1/info              unit id, connection, suggested poll interval, versions, firmware
    GET  /api/v1/fields            field catalog: key, name, unit, kind, enum options, HA hints, default
    GET  /api/v1/values?keys=a,b   current values (enum fields as their label); reads only the screens needed
    GET  /api/v1/diagnostics       link status, clock check, last backup
    POST /api/v1/backup            start a config backup

Read-only towards the SP PRO (same reads as the Live page). Values are cached; if the link is busy
(a backup or log download), the last values come back with "stale": true.
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

from sppro import live

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULTS = json.load(open(os.path.join(HERE, '..', 'sppro', 'default_fields.json'), encoding='utf-8'))
SCREEN_NAMES = {
    'NowSubTab': 'now', 'TodaySubTab': 'today', 'TechnicalDataSubTab': 'tech', 'DCHistorySubTab': 'DC history',
    'ACHistorySubTab': 'AC history', 'ACHistorySubTab2': 'AC history 2', 'ServiceReadings1': 'service',
    'ServiceSettings': 'service settings', 'Now_Today_Tech_KacoModelsCapacityAndOEM': 'unit',
}
# unit -> (HA device class, display precision); same choices as the ESPHome component
UNITS = {
    'kW': ('power', 3), 'kWh': ('energy', 2), 'kvar': ('reactive_power', 3), 'kVA': ('apparent_power', 3),
    'V': ('voltage', 1), 'A': ('current', 1), 'Hz': ('frequency', 2), '°C': ('temperature', 1),
    'h': ('duration', 1), '%': (None, 1), '': (None, 2),
}
VALUE_WAIT = 20        # seconds a values request waits for the link before answering from cache
STALE_AFTER = 600      # cache older than this isn't served


def _slug(name):
    return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')


def _hints(f):
    """HA entity hints for a field: device_class, state_class, precision, category."""
    if f['kind'] == 'enum':
        return {'device_class': 'enum', 'state_class': None, 'precision': None, 'category': None}
    if f['kind'] == 'raw':
        return {'device_class': None, 'state_class': 'measurement', 'precision': 0, 'category': 'diagnostic'}
    dc, prec = UNITS.get(f['unit'], (None, 2))
    if f['unit'] == '%' and 'soc' in f['key']:
        dc = 'battery'
    sc = 'measurement'
    if f['unit'] == 'kWh':
        net = 'net' in f['key']
        sc = 'total_increasing' if ('today' in f['key'] or f['key'].endswith('_acc')) and not net else 'total'
    elif f['unit'] == 'h' and f['key'].endswith('_acc'):
        sc = 'total_increasing'
    return {'device_class': dc, 'state_class': sc, 'precision': prec, 'category': None}


def catalog():
    """Every field once (exact repeats on other screens dropped), defaults first with their HA names."""
    by_key = {f['key']: f for f in live.FIELDS}
    order = [k for k in DEFAULTS if k in by_key] + [f['key'] for f in live.FIELDS if f['key'] not in DEFAULTS]
    seen, names, out = set(), set(), []
    tables = live.tables()
    for k in order:
        f = by_key[k]
        sig = (tuple(f['words']), f['formula'])
        if sig in seen:
            continue
        seen.add(sig)
        name = DEFAULTS.get(k) or f['name']
        if _slug(name) in names:
            name = '%s (%s)' % (name, SCREEN_NAMES.get(f['screen'], f['screen']))
        if _slug(name) in names:
            name = '%s (%s)' % (name, k)
        names.add(_slug(name))
        entry = {'key': k, 'name': name, 'unit': f['unit'], 'kind': f['kind'], 'screen': f['screen'],
                 'default': k in DEFAULTS, 'note': f.get('note', '')}
        entry.update(_hints(f))
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

    def info(self):
        conn = os.environ.get('SELPI_CONNECTION_TYPE', '')
        if self.firmware is None:
            try:
                self.values(['version_number'])
            except Exception as e:
                logging.debug('firmware read failed: %s', e)
        return {'id': self.install_id, 'connection': conn, 'scan_interval': 60 if conn == 'SelectLive' else 10,
                'model': 'SP PRO', 'manufacturer': 'Selectronic', 'firmware': self.firmware,
                'versions': self.sppro.versions, 'version_problems': self.sppro.version_problems}

    def values(self, keys):
        keys = [k for k in keys if k in self.fields]
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
        out = {k: self.cache[k][0] for k in keys if k in self.cache and now - self.cache[k][1] < STALE_AFTER}
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
                return self._send(404, {'error': 'not found'})
            except api.Busy as e:
                return self._send(503, {'error': str(e)})
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
