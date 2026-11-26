"""
Live readings, as SP LINK's display screens show them (Now, Today, Technical, DC/AC history, ...).

The field table (live_fields.json) matches SP LINK 15.40 for memory map
version 31. Values were checked against the SP PRO's Modbus map on 17 Nov 2026.
Reads use the same blocks SP LINK reads, so no range is requested that the unit doesn't serve.
"""
import json
import logging
import math
import os
import re
import time

from . import field_meta

_HERE = os.path.dirname(os.path.abspath(__file__))
TABLE = json.load(open(os.path.join(_HERE, 'live_fields.json')))

# Field display names, HA/ESP hints and the drop list live in field_meta (shared verbatim with the ESPHome
# component via tools/sync_fields.sh) so the app, the HA integration and the ESP firmware present fields the
# same way. The clean name is: override > curated default (default_fields.json) > SP LINK label, Title-cased.
_DEFAULT_NAMES = json.load(open(os.path.join(_HERE, 'default_fields.json')))

FIELDS = TABLE['fields']
for _f in FIELDS:
    _f['name'] = field_meta.field_name(_f['key'], _f['name'], _DEFAULT_NAMES.get(_f['key']))
# keys renamed in 0.4.7 (SP LINK's "DaystoEqualise" split badly into daysto_...): old key -> new key
KEY_ALIASES = {'daysto_recharge': 'days_to_recharge', 'daysto_equalise': 'days_to_equalise'}
BLOCKS = {screen: [(int(a, 16), n) for a, n in v] for screen, v in TABLE['blocks'].items()}
SCALES = ['AcVolts', 'AcCurrent', 'DcVolts', 'DcCurrent', 'Temperature', 'InternalVoltages']
SCALE_BASE = 0xa028
_tables = None

# Version block (SP LINK request type 17): the unit reports which settings/memory-map/log formats its
# firmware uses. SP LINK 15.40 handles any older value by branching, and refuses to connect if any is
# newer than these. Our field table and the write test were verified at exactly these values.
VERSION_BASE, VERSION_WORDS = 0xa007, 25
VERSIONS = {'config_settings': 0, 'service_settings': 1, 'memory_map': 2, 'event_log_format': 3,
            'detailed_log_format': 4, 'daily_log_format': 5, 'string_inverter_support': 14,
            'comms_card_attached': 15, 'comms_card_revision': 18, 'comms_card_software': 20}
VERIFIED = {'config_settings': 48, 'service_settings': 14, 'memory_map': 31, 'event_log_format': 3,
            'detailed_log_format': 3, 'daily_log_format': 5}


def versions(reader):
    w = reader.words(VERSION_BASE, VERSION_WORDS)
    return {k: w[i] for k, i in VERSIONS.items()}


FACTORY_BATTERYLESS = 0xa021    # AddFactoryLockedBatterylessMode (1 = batteryless unit). Pinned from the Select.Live
                                # capture: reads 0 on this (battery) unit; its shadow is 0xa390.
FACTORY_EXPORT_LIMIT = 0xa0aa   # AddBatteryPlusKacoSupportLimitShadow = SP LINK's ActualSP_PROFactoryExportLimitValue
                                # (deci kW, 0xFFFF = blank / no factory cap). The stored copy is 0xca0e.


def batteryless(reader):
    """The factory batteryless flag (1 = batteryless unit; SP LINK then hides the battery settings). None on error."""
    try:
        return reader.words(FACTORY_BATTERYLESS, 1)[0]
    except Exception:
        return None


def factory_export_limit(reader):
    """The factory export limit in kW (SP LINK reads it at connect), or None when blank / no cap (0xFFFF). Comes over
    Select.Live too (it is in the connect handshake)."""
    try:
        w = reader.words(FACTORY_EXPORT_LIMIT, 1)[0]
        return None if w == 0xffff else w / 10
    except Exception:
        return None


def version_problems(v):
    """Differences from the verified versions, e.g. ['memory_map 30 (verified 31)']. Empty = matches."""
    return ['%s %d (verified %d)' % (k, v[k], want) for k, want in VERIFIED.items() if v.get(k) != want]


class VersionMismatch(Exception):
    pass


def check_versions(reader, writes=False, force=False):
    """Compare the unit's version block with the verified map. Reading commands warn on stderr. Writes require only
    the MEMORY-MAP version to match (that is the setting-address layout; this build carries one map's addresses) --
    config/service/log-format differences only change which settings are meaningful, which the per-setting
    availability check (settings.available) and the rules already handle. --force overrides even a map mismatch.
    Returns the problems (empty = matches)."""
    import sys
    v = versions(reader)
    problems = version_problems(v)
    if problems:
        text = 'this unit differs from the verified map (%s)' % ', '.join(problems)
        if writes and not force and v.get('memory_map') != VERIFIED['memory_map']:
            raise VersionMismatch("this unit's memory map is %s but this build maps version %d; the setting addresses "
                                  "may differ, so not writing (--force to override)"
                                  % (v.get('memory_map'), VERIFIED['memory_map']))
        print('WARNING: %s; values may be wrong' % text, file=sys.stderr)
    return problems


SCREENS = {  # short names for the command line
    'now': 'NowSubTab', 'today': 'TodaySubTab', 'tech': 'TechnicalDataSubTab', 'dc': 'DCHistorySubTab',
    'ac': 'ACHistorySubTab', 'ac2': 'ACHistorySubTab2', 'service': 'ServiceReadings1',
    'settings': 'ServiceSettings', 'unit': 'Now_Today_Tech_KacoModelsCapacityAndOEM',
}


def tables():
    global _tables
    if _tables is None:
        _tables = json.load(open(os.path.join(_HERE, 'splink_tables.json')))
    return _tables


def s16(x):
    return x - 65536 if x > 32767 else x


def u32(lo, hi):
    return lo | (hi << 16)


def s32(x):
    return x - (1 << 32) if x >= 1 << 31 else x


_ENV = {'s16': s16, 'u32': u32, 's32': s32, 'abs': abs, 'sqrt': math.sqrt, 'max': max, '__builtins__': {}}
_compiled = {}


def _code(formula):
    if formula not in _compiled:
        expr = re.sub(r'\{0x([0-9a-f]+)\}', r'M[0x\1]', formula)
        expr = re.sub(r'\{scale:(\w+)\}', r'S["\1"]', expr)
        _compiled[formula] = compile(expr, formula, 'eval')
    return _compiled[formula]


def fields_for(screens=None):
    want = None if not screens else {SCREENS.get(s, s) for s in screens}
    return [f for f in FIELDS if want is None or f['screen'] in want]


SCALE_TTL = 600   # seconds. The scale block (0xa028) holds the unit's calibration factors (AcVolts / AcCurrent /
                  # DcVolts / DcCurrent / Temperature / InternalVoltages) -- commissioning constants that do not
                  # change at runtime. SP LINK reads them once; we cache them on the reader (per Powerchain unit)
                  # and refresh only this often instead of re-reading them every poll. Re-reading every poll is
                  # wasted work and, where that one block is flaky over Select.Live, a retry-storm (3 x ~2 s) that
                  # stalls the whole poll -- which is what blocks any faster cadence.


def _scales_into(reader, mem, unit, block):
    """Put the scale registers (SCALE_BASE..+len(SCALES)) into mem. Cached on the reader object so they persist
    across polls and reset on reconnect (a fresh reader re-reads them), keyed per Powerchain unit. The block is
    read from the link only when there is no cached set yet, or the cache has aged past SCALE_TTL; otherwise the
    constant scales are served from cache with no round trip."""
    store = getattr(reader, '_scale_cache', None)
    if store is None:
        try:
            store = reader._scale_cache = {}
        except AttributeError:                      # a reader that can't hold the attribute: just read every time
            block(SCALE_BASE, len(SCALES))
            return
    cached, at = store.get(unit, (None, 0.0))
    now = time.monotonic()
    if cached is None or now - at >= SCALE_TTL:
        if cached is not None:
            store[unit] = (cached, now)             # one refresh attempt per TTL, even if this read fails
        block(SCALE_BASE, len(SCALES))
        got = {SCALE_BASE + i: mem[SCALE_BASE + i] for i in range(len(SCALES)) if SCALE_BASE + i in mem}
        if len(got) == len(SCALES):                 # only trust (and cache) a complete read
            cached = got
            store[unit] = (cached, now)
    if cached:
        for a, w in cached.items():
            mem.setdefault(a, w)


def read(reader, fields, unit=None):
    """Read every block the fields need, plus the scale factors. Returns {address: word}. A block that fails
    (a flaky/slow Select.Live link, or the operation's deadline running out) is skipped rather than failing the
    whole poll, so a slow poll returns partial data — the fields on the blocks that did come back — instead of
    nothing. The reader reconnects per block, so a skipped block doesn't disturb the next one. The scale block
    (constant calibration) is cached per reader+unit, not re-read every poll (see _scales_into)."""
    mem = {}

    def block(base, n):
        try:
            for i, w in enumerate(reader.words(base, n)):
                mem[base + i] = w
        except Exception as e:                      # link loss / short read / deadline: skip this block
            logging.debug('live read 0x%x+%d skipped: %s', base, n, e)

    _scales_into(reader, mem, unit, block)
    for screen in sorted({f['screen'] for f in fields}):
        for base, n in BLOCKS.get(screen, []):
            block(base, n)
    return mem


def evaluate(field, mem):
    """(value, text) for one field; value is a number, an enum code, or None if not available."""
    if any(int(a, 16) not in mem for a in field['words']):
        return None, ''
    scales = {name: mem.get(SCALE_BASE + i) for i, name in enumerate(SCALES)}   # None if the scale block was skipped
    try:
        v = eval(_code(field['formula']), dict(_ENV, M=mem, S=scales))
    except (ArithmeticError, ValueError, KeyError, TypeError):   # TypeError/KeyError: a scale/word is missing
        return None, ''
    if field['kind'] == 'enum':
        label = tables().get(field['table'], {}).get(str(v))
        return v, label if label is not None else 'code %d' % v
    if field['kind'] == 'bitfield':   # a status word: value = the int, text = the set bits' names (SP LINK's *ValueToList)
        t = tables().get(field['table'], {})
        iv = int(v)
        names = [t[str(b)] for b in range(16) if (iv & (1 << b)) and str(b) in t]
        return iv, ', '.join(names)
    if field['kind'] == 'raw':
        return v, str(v)
    return v, ('%.3f' % v).rstrip('0').rstrip('.') if isinstance(v, float) else str(v)



_BY_KEY = None
def _by_key():
    global _BY_KEY
    if _BY_KEY is None:
        _BY_KEY = {f['key']: f for f in FIELDS}
    return _BY_KEY

# Feature presence: SP LINK hides whole groups of live fields when the feature is absent (no BMS battery, no AC
# solar). We detect it from cheap probe fields and gate the tagged fields the same way.
FEATURE_PROBE = {'ac_solar': ('number_of_kacos', lambda v: bool(v) and v > 0),
                 'bms': ('tech_bms_model', lambda v: bool(v) and v > 0)}  # bms model 0 = None (no managed BMS)

def present_features(reader):
    """The feature set this unit has, from probe reads (so feature-gated fields can be hidden when absent).
    'battery' comes from the factory batteryless flag (a batteryless unit has no battery readings at all)."""
    probes = [_by_key()[k] for k, _ in FEATURE_PROBE.values() if k in _by_key()]
    mem = read(reader, probes)
    feats = set()
    for feat, (key, ok) in FEATURE_PROBE.items():
        f = _by_key().get(key)
        if f is not None:
            v, _t = evaluate(f, mem)
            if v is not None and ok(v):
                feats.add(feat)
    if batteryless(reader) == 0:   # 1 = batteryless unit; None (read failed) -> don't claim the feature
        feats.add('battery')
    return feats


def field_available(f, features=None, map_version=None):
    """Whether a field applies to this unit. False when its feature group is absent, or when the unit's memory-map
    version is below the field's needs_map_version (SP LINK's own per-field firmware gate). None = unknown -> keep."""
    feat = f.get('feature')
    if feat and features is not None and feat not in features:
        return False
    need = f.get('needs_map_version')
    if need and map_version is not None and map_version < need:
        return False
    return True


def snapshot(reader, screens=None, features=None, map_version=None):
    fields = fields_for(screens)
    if features is not None or map_version is not None:
        fields = [f for f in fields if field_available(f, features, map_version)]
    mem = read(reader, fields)
    out = []
    for f in fields:
        v, text = evaluate(f, mem)
        out.append({'key': f['key'], 'label': f['name'], 'splink_label': f['label'], 'screen': f['screen'], 'value': v,
                    'text': text, 'unit': f['unit'], 'words': f['words'], 'note': f.get('note', '')})
    return out
