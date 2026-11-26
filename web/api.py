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
    POST /api/v1/identify?unit=N                        flash unit N's front panel LEDs to locate it (Powerchain)

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

import settings
from sppro import field_meta, live, multiphase

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULTS = json.load(open(os.path.join(HERE, '..', 'sppro', 'default_fields.json'), encoding='utf-8'))
SCREEN_NAMES = {
    'NowSubTab': 'now', 'TodaySubTab': 'today', 'TechnicalDataSubTab': 'tech', 'DCHistorySubTab': 'DC history',
    'ACHistorySubTab': 'AC history', 'ACHistorySubTab2': 'AC history 2', 'ServiceReadings1': 'service',
    'ServiceSettings': 'service settings', 'Now_Today_Tech_KacoModelsCapacityAndOEM': 'unit',
}
VALUE_WAIT = 20        # seconds a values request waits for the link before answering from cache
POLL_DEADLINE = 30     # bound the whole read so a slow/flaky link can't hold the lock for minutes; blocks that
                       # don't come back in time are skipped (partial poll) and served from cache instead
                       # how long a last-good value may still be served after a failed/busy poll is
                       # settings.stale_grace() (0 = never, for a fast 'unknown' on a reliable link)


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
        sig = (tuple(f['words']), f['formula'], f.get('table'), f['kind'])   # model number vs rating share reg+formula, differ by table
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
                 'default': k in DEFAULTS, 'note': f.get('note', ''), 'feature': f.get('feature'),
                 'needs_map_version': f.get('needs_map_version'),
                 'aggregatable': multiphase.aggregation(f['unit'], f['key']) is not None}   # rolls up to the System device
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
        self.identity = {}        # static unit identity for the HA device (serial, model number), read once
        self.features = None      # feature set present on this unit (bms, ac_solar, battery) - gates feature-tagged fields
        self.map_version = None   # unit's memory-map version - gates fields with a needs_map_version
        self.mp_units = []        # cached Powerchain unit descriptors (from sppro.unit_list); [] = single/unknown
        self.multi = False        # more than one unit on the bus
        self.backup_running = False
        self.proxy = None         # web.proxy.Proxy when the SP LINK proxy is on
        self.last_control = {}    # control -> (time pressed, state code before the press)

    def info(self):
        conn = os.environ.get('SELPI_CONNECTION_TYPE', '')
        if self.firmware is None or not self.identity:
            # firmware + serial + model number for the HA device record; static, read once, in one round trip
            # (each read is a cloud round trip on Select.Live). values() sets self.firmware from version_number.
            try:
                vals = self.values(['version_number', 'unit_serial_number', 'unit_model_number',
                                    'unit_model_ratings', 'unit_rev_number']).get('values', {})
                self.identity = {'serial': vals.get('unit_serial_number'),
                                 'model_number': vals.get('unit_model_number'),
                                 'model_ratings': vals.get('unit_model_ratings'),   # e.g. "48V DC, 7.5kW, 240V AC"
                                 'hw': vals.get('unit_rev_number')}                 # "Hardware Revision" -> hw_version
            except Exception as e:
                logging.debug('identity read failed: %s', e)
        if self.features is None:   # probe which feature groups this unit has (BMS, AC solar, battery); gate their fields
            try:
                self.features = self.sppro.use('features', live.present_features, wait=VALUE_WAIT, deadline=POLL_DEADLINE)
            except Exception as e:
                logging.debug('feature probe failed: %s', e)   # retry next info()
        if self.map_version is None and self.sppro.versions:   # check_versions already read the version block on the
            self.map_version = self.sppro.versions.get('memory_map')   # first poll; reuse it, no extra round trip
        server = os.environ.get('SELPI_CONNECTION_SELECT_LIVE_SERVER', '')
        if conn == 'SelectLive':
            label = {'select.live2': 'Select.Live 2',
                     'select.live2-nonprod': 'Select.Live 2 (test)'}.get(server, 'Select.Live')
        elif conn == 'TCP':
            label = 'ESP (proxy)'        # the add-on's TCP link is the ESPHome sppro proxy
        else:
            label = conn or 'Unknown'
        units = self.units()             # discover + cache the Powerchain units (sets self.multi)
        return {'id': self.install_id, 'connection': conn, 'connection_label': label,
                'clock_sync': os.environ.get('SELPI_CLOCK_SYNC', '').lower() == 'true',
                'scan_interval': 60 if conn == 'SelectLive' else 10,
                'stale_grace': settings.stale_grace(),   # add-on serves last-good this long after a failed poll
                'cache_ttl': settings.cache_ttl(),        # config/settings page cache-first window
                'model': self.identity.get('model_number') or 'SP PRO', 'manufacturer': 'Selectronic',
                'model_ratings': self.identity.get('model_ratings'),   # hardware rating for the device hw_version
                'serial': self.identity.get('serial'), 'firmware': self.firmware,
                'versions': self.sppro.versions, 'version_problems': self.sppro.version_problems,
                'units': units, 'multi': self.multi,          # units first: discovery sets self.multi
                'buttons': sorted(API_BUTTONS) + ['on'], 'controls': sorted(CONTROLS),
                'identify': True,     # per-inverter flash-LEDs (POST /api/v1/identify?unit=N); benign, no passcode
                'hw': self.identity.get('hw'),          # Hardware Revision -> HA device hw_version
                'map_version': self.map_version,        # unit's memory-map version (for the field map gate)
                'features': sorted(self.features) if self.features is not None else None}   # present feature groups

    def units(self):
        """The Powerchain units for the integration to build (sub-)devices from; a single inverter yields one unit.
        Discovery is cached on the shared link (sppro.unit_list); a slow link keeps the last-known list. Updates
        self.mp_units / self.multi, which the per-unit reads use (so the Api owns its own multiphase view)."""
        try:
            self.mp_units = self.sppro.unit_list(wait=VALUE_WAIT, deadline=POLL_DEADLINE)
            self.multi = len(self.mp_units) > 1
        except Exception as e:
            logging.debug('unit discovery failed: %s', e)
        out = [{'index': u['index'], 'serial': u['serial'], 'phase': u['phase'], 'role': u['role'],
                'label': u['label'], 'model': u['model']} for u in self.mp_units]
        if self.multi:                 # a synthetic 'system' unit: the add-on computes the whole-system totals
            out.append({'index': 'system', 'serial': None, 'phase': None, 'role': 'system',
                        'label': 'System', 'model': None})
        return out

    def _unit(self, index):
        """Resolve a unit index to its descriptor (None = the primary / single / the 'system' pseudo-unit)."""
        if index in (None, '', 'system'):
            return None
        try:
            index = int(index)
        except (TypeError, ValueError):
            return None
        for u in self.mp_units:
            if u['index'] == index:
                return u
        return None

    def values(self, keys, unit=None):
        """Current values. `unit` (a Powerchain unit index) re-addresses the link to that inverter first and caches
        its values separately; None = the primary / single inverter (unchanged behaviour)."""
        asked = {live.KEY_ALIASES.get(k, k): k for k in keys}   # old (renamed) keys still answered
        keys = [k for k in asked if k in self.fields]
        fields = [self.fields[k] for k in keys]
        if self.features is not None or self.map_version is not None:   # gate: skip absent features / fields newer than the unit's map
            fields = [f for f in fields if live.field_available(f, self.features, self.map_version)]
        if not self.mp_units:     # discovery hasn't run in this process yet (e.g. after an add-on restart): populate
            self.units()          # mp_units/multi lazily, so per-unit and 'system' reads work without a /units call
        u = self._unit(unit)
        if u is None and self.multi:         # multi: 'no unit' = the primary (L1 manager); always address a known unit
            u = (self.mp_units or [None])[0]
        ck = lambda k: (unit, k)         # cache is namespaced per unit (unit=None for the primary / single)
        grace = settings.stale_grace()   # how long a last-good value may be served after a failed poll (0 = never)
        stale = False
        fresh_at = None                  # the timestamp written to the cache this cycle (fresh reads keep it exactly)
        try:
            def read(r):
                if unit == 'system':
                    # whole-system totals: read every unit (coherent snapshot under the one lock) and combine
                    # per field (sum power/energy/current, avg voltage/SoC/freq/temp); see sppro.multiphase.
                    cols = {f['key']: [] for f in fields}
                    for mu in self.mp_units:
                        multiphase.change_destination(r, mu['index'], mu['serial'])
                        mem = live.read(r, fields, unit=mu['index'])
                        for f in fields:
                            cols[f['key']].append(live.evaluate(f, mem)[0])
                    return {f['key']: (multiphase.aggregate(f['unit'], cols[f['key']], f['key']), None) for f in fields}
                if u is not None and self.multi:
                    multiphase.change_destination(r, u['index'], u['serial'])   # verify serial, then read this unit
                mem = live.read(r, fields, unit=(u['index'] if (u is not None and self.multi) else None))
                return {f['key']: live.evaluate(f, mem) for f in fields}
            got = self.sppro.use('HA poll%s' % ('' if unit is None else ' u%s' % unit), read,
                                  wait=VALUE_WAIT, deadline=POLL_DEADLINE,
                                  key='poll:%s:' % unit + ','.join(sorted(keys))) if fields else {}
            fresh_at = time.time()
            for k, (v, text) in got.items():
                if v is not None:    # a skipped block returns None: keep the last good cache, don't overwrite it
                    self.cache[ck(k)] = (text if self.fields[k]['kind'] in ('enum', 'bitfield') else v, fresh_at)
            if unit is None and got.get('version_number', (None,))[0] is not None:
                self.firmware = got['version_number'][1]
            if any(v is None for v, _ in got.values()):   # a partial poll (some blocks skipped): mark it stale
                stale = True
        except self.Busy:
            # link busy/failed: only serve the last good values while within the stale-grace window; with
            # grace 0 (a reliable local link) serve nothing — let the poll fail so entities go 'unknown' at once.
            stale = True
            now = time.time()
            if not any(ck(k) in self.cache and now - self.cache[ck(k)][1] <= grace for k in keys):
                raise
        now = time.time()
        # values read this cycle are always returned; anything older only within the stale-grace window
        out = {asked[k]: self.cache[ck(k)][0] for k in keys
               if ck(k) in self.cache and (self.cache[ck(k)][1] == fresh_at or now - self.cache[ck(k)][1] <= grace)}
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
                'link_down': getattr(self.sppro, 'link_down', False),   # circuit breaker tripped: repeated reconnect failures
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
                             deadline=30, check_versions=False)
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
            out = self.sppro.use('control %s' % name, act, wait=60, deadline=45, check_versions=False)
        except controls.ControlError as e:
            raise ConflictError(str(e))
        result = {'control': name, 'state': state, 'pressed': out.get('pressed', True)}
        for k in ('status', 'mode', 'note', 'days_to_equalise_before', 'days_to_equalise_after'):
            if k in out:
                result[k] = out[k]
        return result

    def identify(self, unit='', duration=''):
        """Flash a unit's front panel LEDs to locate it physically (SP LINK's double-click identify).

        `unit` = a Powerchain unit index; omit for a single inverter. A benign momentary write (0xa389): no
        installer passcode and no configuration change, so it is allowed ahead of settings write-enablement.
        """
        from sppro import controls
        if unit == 'system':
            raise ValueError('identify is per inverter, not the computed System device')
        if not self.mp_units:
            self.units()                       # populate mp_units/multi (e.g. after an add-on restart)
        u = self._unit(unit)
        if u is None and self.multi and unit not in (None, ''):
            raise ValueError('unknown unit %r' % unit)
        if u is None and self.multi:
            u = (self.mp_units or [None])[0]    # multi + no unit = the primary (L1 manager)
        try:
            duration_ms = int(duration) if duration != '' else multiphase.IDENTIFY_DEFAULT_MS
        except ValueError:
            raise ValueError('duration must be an integer number of milliseconds')

        def act(r):
            v = live.versions(r).get('memory_map')
            if v is not None and v < multiphase.IDENTIFY_MIN_MAP_VERSION:
                raise controls.ControlError('identify needs memory map version >= %d (unit reports %d)'
                                            % (multiphase.IDENTIFY_MIN_MAP_VERSION, v))
            return multiphase.identify(r, u, self.multi, duration_ms)
        logging.warning('identify (flash LEDs) via the API: unit %s', unit or '(primary)')
        try:
            out = self.sppro.use('identify u%s' % (unit or ''), act, wait=30, deadline=30, check_versions=False)
        except (controls.ControlError, RuntimeError) as e:
            raise ConflictError(str(e))
        return {'identified': True, 'unit': out['index'], 'register': '0x%x' % out['register'],
                'value': out['value'], 'duration_ms': out['duration_ms']}

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
                if method == 'GET' and path == '/api/v1/units':
                    return self._send(200, {'units': api.units(), 'multi': api.multi})
                if method == 'GET' and path == '/api/v1/values':
                    keys = [k for k in q.get('keys', '').split(',') if k]
                    return self._send(200, api.values(keys, q.get('unit')))
                if method == 'GET' and path == '/api/v1/diagnostics':
                    return self._send(200, api.diagnostics())
                if method == 'POST' and path == '/api/v1/backup':
                    return self._send(200, api.backup())
                if method == 'POST' and path == '/api/v1/button':
                    return self._send(200, api.button(q.get('name', ''), q.get('confirm', '')))
                if method == 'POST' and path == '/api/v1/control':
                    return self._send(200, api.control(q.get('name', ''), q.get('state', ''), q.get('confirm', '')))
                if method == 'POST' and path == '/api/v1/identify':
                    return self._send(200, api.identify(q.get('unit', ''), q.get('duration', '')))
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
