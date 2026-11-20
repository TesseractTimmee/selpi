"""
Live readings, as SP LINK's display screens show them (Now, Today, Technical, DC/AC history, ...).

The field table (live_fields.json) matches SP LINK 15.40 for memory map
version 31. Values were checked against the SP PRO's Modbus map on 17 Nov 2026.
Reads use the same blocks SP LINK reads, so no range is requested that the unit doesn't serve.
"""
import json
import math
import os
import re

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


def read(reader, fields):
    """Read every block the fields need, plus the scale factors. Returns {address: word}."""
    mem = {}
    for i, w in enumerate(reader.words(SCALE_BASE, len(SCALES))):
        mem[SCALE_BASE + i] = w
    for screen in sorted({f['screen'] for f in fields}):
        for base, n in BLOCKS.get(screen, []):
            for i, w in enumerate(reader.words(base, n)):
                mem[base + i] = w
    return mem


def evaluate(field, mem):
    """(value, text) for one field; value is a number, an enum code, or None if not available."""
    if any(int(a, 16) not in mem for a in field['words']):
        return None, ''
    scales = {name: mem[SCALE_BASE + i] for i, name in enumerate(SCALES)}
    try:
        v = eval(_code(field['formula']), dict(_ENV, M=mem, S=scales))
    except (ArithmeticError, ValueError):
        return None, ''
    if field['kind'] == 'enum':
        label = tables().get(field['table'], {}).get(str(v))
        return v, label if label is not None else 'code %d' % v
    if field['kind'] == 'raw':
        return v, str(v)
    return v, ('%.3f' % v).rstrip('0').rstrip('.') if isinstance(v, float) else str(v)


def snapshot(reader, screens=None):
    fields = fields_for(screens)
    mem = read(reader, fields)
    out = []
    for f in fields:
        v, text = evaluate(f, mem)
        out.append({'key': f['key'], 'label': f['name'], 'splink_label': f['label'], 'screen': f['screen'], 'value': v,
                    'text': text, 'unit': f['unit'], 'words': f['words'], 'note': f.get('note', '')})
    return out
