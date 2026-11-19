"""
SP PRO configuration settings with their meaning, as SP LINK 15.40 shows them (sppro/settings_meta.json), and
SP LINK's validation (sppro/settings_rules.json). Shared by `selpi config show / set` and the web UI.

  entry(name)                       the setting's metadata (kind, values, scale, limits, unit, ...)
  to_display(name, raw, cells)      raw word -> (value, text), e.g. 2346 -> (56.304, '56.3 V') on a 24-cell unit
  from_display(name, text, cells)   what you'd type in SP LINK -> raw word, plus range warnings
  check(raw_config, cells, model)   SP LINK's rules over a whole configuration -> [problem]
  send_check(raw_new, raw_old, unit, link)   the send-time checks (need the unit's live state)
  model(reader)                     the unit's model (low byte of 0xa05d indexes SP LINK's model list)
"""
import json
import os
import re
from types import SimpleNamespace

_HERE = os.path.dirname(os.path.abspath(__file__))
def _load(name):
    with open(os.path.join(_HERE, name), encoding='utf-8') as fh:
        return json.load(fh)


META = {s['name']: s for s in _load('settings_meta.json')['settings']}
RULES = _load('settings_rules.json')
MODEL_ORDER = ['SPMC482', 'SPMC481', 'SPMC241', 'SPLC1202', 'SPMC1201', 'SPMC240', 'SPLC1201', 'SPLC1200', 'SPMC480']
MODELS = {k: SimpleNamespace(**v) for k, v in RULES['models'].items()}
MODEL_WORD = 0xa05d
MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']


class SettingError(ValueError):
    pass


SECTION_TITLES = {'Common': 'COMMON SETTINGS:', 'AppType': 'APPLICATION TYPE SETTINGS:',
                  'BattType': 'BATTERY TYPE SETTINGS:', 'SystemScheduler': 'SYSTEM SCHEDULER SETTINGS:'}
_ADDR = None
_WHERE = None


def address(name):
    """The setting's word address (sppro/layout.json section + slot)."""
    global _ADDR
    if _ADDR is None:
        from sppro import config
        by_slot = {(sec, i): a for sec, i, _, a in config.slots()}
        _ADDR = {n: by_slot.get((SECTION_TITLES.get(e['section']), e['slot'])) for n, e in META.items()}
    return _ADDR.get(entry(name)['name'])


def raw_config(memory):
    """{name: raw} for every known setting from config.read_memory() output."""
    return {n: memory.get(address(n)) for n in META if address(n) is not None and memory.get(address(n)) is not None}


def where(name):
    """Where SP LINK shows it: 'Tab > Sub-tab > Group: Label' (sppro/settings_layout.json), or None."""
    global _WHERE
    if _WHERE is None:
        _WHERE = {}
        path = os.path.join(_HERE, 'settings_layout.json')
        if os.path.exists(path):
            for t in _load('settings_layout.json')['tabs']:
                for st in t['subtabs']:
                    for g in st['groups']:
                        for r in g['rows']:
                            parts = [x for x in (t.get('title'), st.get('title'), g.get('title')) if x]
                            _WHERE[r['setting']] = '%s: %s' % (' > '.join(parts), (r.get('label') or r['setting']).strip())
    return _WHERE.get(entry(name)['name'])


def entry(name: str) -> dict:
    e = META.get(name) or next((v for k, v in META.items() if k.lower() == name.lower()), None)
    if e is None:
        raise SettingError('no setting called %r' % name)
    return e


def model(reader):
    i = reader.words(MODEL_WORD, 1)[0] & 0xff
    return MODEL_ORDER[i] if i < len(MODEL_ORDER) else None


def cells_for(model_name):
    m = MODELS.get(model_name)
    return m.cells if m else None


def _signed(v):
    return v - 65536 if v > 32767 else v


# save helper -> (raw -> display, display -> raw); DC volts take the cell count
_NUM = {
    'fnGetNumericalValue': (lambda r, c: r, lambda d, c: d),
    'fnGetNumericalValueWhichIsStoredInDeciUnits': (lambda r, c: r / 10, lambda d, c: d * 10),
    'fnGetNumericalValueWhichIsStoredInCentiUnits': (lambda r, c: r / 100, lambda d, c: d * 100),
    'fnGetNumericalValueInMiliUnits': (lambda r, c: r / 1000, lambda d, c: d * 1000),
    'fnGetNumericalValueWhichIsStoredInQuinquadeciUnits': (lambda r, c: r / 15, lambda d, c: d * 15),
    'fnGetNumericalValueWhichIsDisplayedInHoursButStoredInMinutes': (lambda r, c: r / 60, lambda d, c: d * 60),
    'fnGetNumericalValueWhichIsStoredInDeciUnitsOffset100': (lambda r, c: r / 10 - 100, lambda d, c: (d + 100) * 10),
    'fnGetNumericalValueWithOffSetOf10000': (lambda r, c: r - 10000, lambda d, c: d + 10000),
    'fnGetNumericalValue_TemperatureCoeficients': (lambda r, c: (r - 10000) / 1000, lambda d, c: d * 1000 + 10000),
    'fnGetNumericalValueAndChangeSign': (lambda r, c: -_signed(r), lambda d, c: -d),
    'fnGetNumericalValueWhichIsStoredInDeciUnitsAndChangeSign': (lambda r, c: -_signed(r) / 10, lambda d, c: -d * 10),
    'fnGetDCVoltNumericalValue': (lambda r, c: r * c / 1000, lambda d, c: d / c * 1000),
    'fnGetInputPowerSettingValue': (lambda r, c: r / 100, lambda d, c: d * 100),
    'fnGetCTRatioValue': (lambda r, c: r * 5, lambda d, c: d / 5),
    'fnGetDayFromDayMonthDTPWBC': (lambda r, c: r, lambda d, c: d),
    'fnGetMonthFromDayMonthDTPWBC': (lambda r, c: r, lambda d, c: d),
}


def _limits(e, cells):
    """(min, max) in display units, None where SP LINK sets it from another setting."""
    if 'per_cell_min' in e:
        mn = e['per_cell_min'] * cells if e.get('per_cell_min') is not None and cells else None
        mx = e['per_cell_max'] * cells if e.get('per_cell_max') is not None and cells else None
        return mn, mx
    return e.get('min'), e.get('max')


def to_display(name, raw, cells=None):
    """(value, text) for a raw word. value is a number, a label, 'HH:MM', or the raw number for kind raw."""
    e = entry(name)
    k = e['kind']
    if k == 'enum':
        label = e['values'].get(str(raw))
        return (label, label) if label is not None else (raw, 'code %d' % raw)
    if k == 'time':
        if e.get('disabled') == raw:
            return 'Disabled', 'Disabled'
        t = '%02d:%02d' % (raw // 60, raw % 60)
        return t, t
    if k == 'daymonth':
        d, m = raw & 0xff, raw >> 8
        t = '%d %s' % (d, MONTHS[m - 1]) if 1 <= m <= 12 else 'raw %d' % raw
        return t, t
    if k == 'number' and e.get('save') in _NUM:
        if e['save'] == 'fnGetDCVoltNumericalValue' and not cells:
            return raw, '%d (mV per cell)' % raw
        v = _NUM[e['save']][0](raw, cells)
        dec = e.get('decimals') or 0
        v = round(v, max(dec, 3))
        unit = e.get('unit') or ''
        return v, ('%.*f %s' % (dec, v, unit)).strip()
    return raw, str(raw)


def from_display(name, text, cells=None):
    """Raw word for a value as SP LINK shows it (label, number in its unit, HH:MM, 'D Mon', 'Disabled'), and a
    list of warnings (outside SP LINK's range). Raises SettingError if it can't be converted."""
    e = entry(name)
    k = e['kind']
    s = str(text).strip()
    warnings = []
    if k == 'enum':
        for code, label in e['values'].items():
            if label.lower() == s.lower():
                return int(code), warnings
        raise SettingError('%s: %r is not one of %s' % (e['name'], s, ', '.join(e['values'].values())))
    if k == 'time':
        if e.get('disabled') is not None and s.lower() == 'disabled':
            return e['disabled'], warnings
        m = re.fullmatch(r'(\d{1,2}):(\d{2})', s)
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise SettingError('%s: give a time as HH:MM%s' % (e['name'], ' or Disabled' if e.get('disabled') else ''))
        return int(m.group(1)) * 60 + int(m.group(2)), warnings
    if k == 'daymonth':
        m = re.fullmatch(r'(\d{1,2})\s+([A-Za-z]{3})', s)
        if not m or m.group(2).title() not in MONTHS:
            raise SettingError('%s: give a date as e.g. "1 Jan"' % e['name'])
        return (MONTHS.index(m.group(2).title()) + 1) << 8 | int(m.group(1)), warnings
    if k == 'number' and e.get('save') in _NUM:
        if e['save'] == 'fnGetDCVoltNumericalValue' and not cells:
            raise SettingError('%s is a DC voltage: the cell count (from the unit model) is needed' % e['name'])
        unit = (e.get('unit') or '').lower()
        s2 = s[:-len(unit)].strip() if unit and s.lower().endswith(unit) else s
        try:
            d = float(s2)
        except ValueError:
            raise SettingError('%s: %r is not a number' % (e['name'], s))
        mn, mx = _limits(e, cells)
        if (mn is not None and d < mn - 1e-9) or (mx is not None and d > mx + 1e-9):
            warnings.append('%s %g is outside SP LINK\'s range %s' % (e['name'], d, e.get('range_text') or '%s - %s' % (mn, mx)))
        raw = round(_NUM[e['save']][1](d, cells))
        if not -32768 <= raw <= 65535:
            raise SettingError('%s: %g is out of range for the stored word' % (e['name'], d))
        return raw & 0xffff, warnings
    try:
        raw = int(s, 0)
    except ValueError:
        raise SettingError('%s has no decoded meaning (kind %s): give the raw number' % (e['name'], k))
    return raw, warnings


# ---- rules ----------------------------------------------------------------------------------------------

def _values(raw_config, cells):
    out = {}
    for name, raw in raw_config.items():
        if name in META and raw is not None:
            out[name] = to_display(name, raw, cells)[0]
    return out


def _env(values, model_name):
    env = dict(values)
    env.update(max=max, min=min, any=any, all=all, int=int, abs=abs,
               is_managed_bms=lambda t: str(t).startswith('BMS'),
               kw=lambda v: v,           # input power settings are already in kW
               models=MODELS, model=model_name,
               model_nominal_v=MODELS[model_name].nominal_v if model_name in MODELS else None)
    env['active'] = lambda n: (values.get('SystemSchedule%dEnable' % n) == 'Enabled'
                               or values.get('SystemSchedule%dDigitalInput' % n) not in (None, 'None'))
    return env


def _expand(expr, n):
    e = expr.replace('{N}', str(n)) if n is not None else expr
    e = e.replace('active(N)', 'active(%d)' % n if n is not None else 'True')
    # "max(SystemSchedule{N}X for active N)" / "any(... for N)": the eight priorities
    e = re.sub(r'(\w+)\(([^()]*?)SystemSchedule\{N\}(\w+)([^()]*?) for active N\)',
               lambda m: '%s([%s]+[0])' % (m.group(1), ', '.join(
                   '%sSystemSchedule%d%s%s' % (m.group(2), i, m.group(3), m.group(4)) for i in range(1, 9))), e)
    e = re.sub(r'any\(([^()]*?)SystemSchedule\{N\}(\w+)([^()]*?) for N\)',
               lambda m: 'any([%s])' % ', '.join('%sSystemSchedule%d%s%s' % (m.group(1), i, m.group(2), m.group(3))
                                                  for i in range(1, 9)), e)
    return e


def _run(rules, env, values, extra=None):
    problems = []
    if extra:
        env = dict(env, **extra)
    for r in rules:
        if r.get('type') == 'unique':
            helper = r['group'].split(':', 1)[1]
            used = {}
            for name, e in META.items():
                if e.get('load') == helper and name in values and values[name] not in r.get('ignore', []):
                    used.setdefault(values[name], []).append(name)
            for v, names in used.items():
                if len(names) > 1:
                    problems.append({'id': r['id'], 'severity': r['severity'],
                                     'message': r['message'].format(value=v, settings=', '.join(names)), 'settings': names})
            continue
        for n in (range(1, 9) if r.get('each') else [None]):
            try:
                if r.get('when') and not eval(_expand(r['when'], n), {'__builtins__': {}}, env):
                    continue
                ok = eval(_expand(r['require'], n), {'__builtins__': {}}, env)
            except (NameError, KeyError, AttributeError, TypeError) as ex:
                # a setting the rule needs isn't in the configuration given (or can't be shown): skip, report
                problems.append({'id': r['id'], 'severity': 'skipped', 'message': 'not checked: %s' % ex})
                continue
            if not ok:
                msg = r['message'].replace('{N}', str(n)) if n else r['message']
                problems.append({'id': r['id'], 'severity': r['severity'], 'message': msg})
    return problems


def check(raw_config, cells=None, model_name=None):
    """SP LINK's configuration rules over a whole configuration {name: raw}. Returns [{id, severity, message}]."""
    values = _values(raw_config, cells)
    return _run(RULES['rules'], _env(values, model_name), values)


def send_check(raw_new, raw_old, unit, link, cells=None, model_name=None):
    """SP LINK's send-time checks. unit: output_mode, hardware_revision, comms_card_revision,
    comms_card_attached, region, factory_export_limit; link: port."""
    values = _values(raw_new, cells)
    changed = {n for n in raw_new if raw_old.get(n) != raw_new.get(n)}
    extra = {'unit': SimpleNamespace(**unit), 'link': SimpleNamespace(**link),
             'changed': lambda v: False}
    # changed(X): the rule text names a setting; evaluate by name
    rules = []
    for r in RULES['send_checks']:
        r = dict(r)
        for k in ('when', 'require'):
            if k in r:
                r[k] = re.sub(r'changed\((\w+)\)', lambda m: 'True' if m.group(1) in changed else 'False', r[k])
        rules.append(r)
    return _run(rules, _env(values, model_name), values, extra)
