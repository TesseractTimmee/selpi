"""
The web UI's configuration page: SP LINK's settings screens, values in SP LINK's terms, its validation, and a
guarded write (sppro/settings.py, sppro/config_write.py).

  layout()                    static: tabs / sub-tabs / groups / rows + each setting's kind, labels, limits, units
  read(reader)                a session read: values + problems already present; remembered under a read id
  check(reader, rid, changes) proposed {name: text as SP LINK shows it} -> raw values, range warnings, and the
                              rule / send-check problems the changes would cause
  write(reader, rid, ...)     re-reads the unit (refuses if anything changed since the read), checks again, then
                              writes every changed setting in one unlock session (one backup, one read-back)

Writes need SELPI_ALLOW_CONFIG_WRITES=true (the app option allow_config_writes), the SP LINK passcode each time
(never stored), verified unit versions, and a read from this session no older than READ_MAX_AGE.
"""
import json
import os
import threading
import time
import uuid

from sppro import buttons, config, config_write, controls, live, settings

READ_MAX_AGE = 30 * 60          # s: a write needs a read this recent
READS = {}                      # read id -> {'at', 'memory', 'model', 'cells'}
_LOCK = threading.Lock()
_LAYOUT = None


def writes_allowed():
    return os.environ.get('SELPI_ALLOW_CONFIG_WRITES', '').lower() == 'true'


def writes_available():
    # the release gate (config_write.writes_available) AND the user-facing option
    return config_write.writes_available() and writes_allowed()


def layout():
    """Everything the page needs to draw SP LINK's screens (no unit access)."""
    global _LAYOUT
    if _LAYOUT is None:
        keep = ('kind', 'values', 'unit', 'min', 'max', 'step', 'decimals', 'range_text', 'per_cell_min',
                'per_cell_max', 'disabled', 'words', 'unnamed_in_splink', 'section', 'directions', 'pf_mode', 'offset',
                'date_part', 'installer_only', 'min_version')
        meta = {n: {k: e[k] for k in keep if k in e} for n, e in settings.META.items()}
        lay = settings._load('settings_layout.json')
        tabs, loose = [], []
        for t in lay['tabs']:
            if not t.get('title'):      # groups SP LINK places outside its tabs (the shunts): a tab after Battery
                loose.append(dict(t, id='shunts', title='Shunts'))
            else:
                tabs.append(t)
        at = next((i + 1 for i, t in enumerate(tabs) if t['id'] == 'tabBattery'), len(tabs))
        tabs[at:at] = loose
        # SP LINK's day/month date pickers store two words: one row (day box + month list) instead of two
        months = dict(settings.date_pairs())
        for t in tabs:
            for st in t['subtabs']:
                for g in st['groups']:
                    names = {r['setting'] for r in g['rows']}
                    merged = {d: m for d, m in months.items() if d in names and m in names}
                    g['rows'] = [dict(r, month=merged[r['setting']]) if r['setting'] in merged else r
                                 for r in g['rows'] if r['setting'] not in merged.values()]
        for n in meta:
            if settings.factory_only(n):
                meta[n]['factory_only'] = True
        _LAYOUT = {'tabs': tabs, 'settings': meta, 'hidden_tabs': ['Factory Mode Settings'],
                   'hidden_group_categories': ['legacy', 'unreleased', 'advanced']}
    return dict(_LAYOUT, writes_allowed=writes_allowed(), writes_available=writes_available())


def limits(cells, battery=None, values=None):
    """{name: [min, max, decimals]} for the number settings, in SP LINK's display units: the page's number boxes
    hold values to these as SP LINK's do (DC voltages per cell x the unit's cells, by Battery Type; maximums SP LINK
    takes from other settings; None = not known)."""
    out = {}
    for n, e in settings.META.items():
        if e['kind'] in ('number', 'pf'):
            dec = e.get('decimals') or 0
            mn, mx = settings._limits(e, cells, battery, values)
            out[n] = [None if mn is None else round(mn, dec), None if mx is None else round(mx, dec), dec]
    return out


def _forget_old():
    now = time.time()
    for rid in [r for r, v in READS.items() if now - v['at'] > READ_MAX_AGE]:
        READS.pop(rid, None)


def _values(raw, cells):
    out = {}
    for name, v in raw.items():
        value, text = settings.to_display(name, v, cells)
        if isinstance(value, float):    # show the figure SP LINK shows (the text is already rounded to its decimals)
            try:
                value = float(text.split()[0])
            except (ValueError, IndexError):
                pass
        out[name] = {'raw': v, 'value': value, 'text': text}
    return out


def read(reader):
    memory = config.read_memory(reader)
    model = settings.model(reader)
    cells = settings.cells_for(model)
    config_version = live.versions(reader).get('config_settings')
    raw = settings.raw_config(memory)
    rid = uuid.uuid4().hex[:12]
    with _LOCK:
        _forget_old()
        READS[rid] = {'at': time.time(), 'memory': memory, 'model': model, 'cells': cells,
                      'config_version': config_version}
    problems = [p for p in settings.check(raw, cells, model) if p['severity'] != 'skipped']
    shown = {n: settings.to_display(n, v, cells)[0] for n, v in raw.items()}
    return {'read_id': rid, 'model': model, 'cells': cells, 'config_version': config_version,
            'values': _values(raw, cells), 'problems': problems,
            'limits': limits(cells, shown.get('BatteryType'), shown),
            'version_problems': live.version_problems(live.versions(reader)),
            'map_mismatch': live.versions(reader).get('memory_map') != live.VERIFIED['memory_map'],
            'writes_allowed': writes_allowed(),
            'writes_available': writes_available(),
            'read_max_age': READ_MAX_AGE}


def _session(rid):
    with _LOCK:
        _forget_old()
        s = READS.get(rid)
    if not s:
        raise ValueError('no current read of the configuration (or it is older than %d minutes): read it again'
                         % (READ_MAX_AGE // 60))
    return s


def _unit_state(reader, raw_now, cells):
    v = live.versions(reader)
    region = settings.to_display('Region', raw_now['Region'], cells)[0] if 'Region' in raw_now else 'None'
    app = settings.to_display('ApplicationType', raw_now['ApplicationType'], cells)[0] if 'ApplicationType' in raw_now else None
    unit = {'output_mode': controls.output_state(reader)['mode'], 'hardware_revision': reader.words(0xa060, 1)[0],
            'comms_card_revision': v.get('comms_card_revision'), 'comms_card_attached': bool(v.get('comms_card_attached')),
            'region': region if app == 'Solar Hybrid (AS4777.2:2020)' else 'None', 'factory_export_limit': None}
    return unit, {'port': buttons.port(reader)}


def _evaluate(reader, s_sess, changes):
    """Convert {name: text} and work out what the change would cause. Returns (result, raw_changes)."""
    s = s_sess
    cells, model = s['cells'], s['model']
    raw_now = settings.raw_config(s['memory'])
    out, raw_changes = {}, {}
    # limits follow the Battery Type and other settings as they will be after the change
    planned = dict(raw_now)
    for name, text in (changes or {}).items():
        try:
            planned[settings.entry(name)['name']] = settings.from_display(name, text, cells)[0]
        except settings.SettingError:
            pass
    shown = {n: settings.to_display(n, v, cells)[0] for n, v in planned.items()}
    for name, text in (changes or {}).items():
        try:
            e = settings.entry(name)
            if e.get('words') == 2:
                raise settings.SettingError('%s spans two words: not writable here' % name)
            ro = settings.read_only_reason(e['name'])
            if ro:
                raise settings.SettingError('%s is %s; selpi keeps it read-only (change it in SP LINK)' % (e['name'], ro))
            if not settings.available(e['name'], s_sess['config_version']):
                raise settings.SettingError('%s needs SP PRO config version %d; this unit is %s'
                                            % (e['name'], e['min_version'], s_sess['config_version']))
            raw, warnings = settings.from_display(e['name'], text, cells, shown.get('BatteryType'), shown)
            out[e['name']] = {'raw': raw, 'text': settings.to_display(e['name'], raw, cells)[1],
                              'old': raw_now.get(e['name']), 'warnings': warnings}
            if raw != raw_now.get(e['name']):
                raw_changes[e['name']] = raw
        except settings.SettingError as ex:
            out[name] = {'error': str(ex)}
    raw_new = dict(raw_now, **raw_changes)
    key = lambda p: (p['id'], p['message'])
    before = {key(p) for p in settings.check(raw_now, cells, model) if p['severity'] != 'skipped'}
    caused = [p for p in settings.check(raw_new, cells, model) if p['severity'] != 'skipped' and key(p) not in before]
    send = []
    if raw_changes and reader is not None:
        unit, link = _unit_state(reader, raw_now, cells)
        send = [p for p in settings.send_check(raw_new, raw_now, unit, link, cells, model) if p['severity'] != 'skipped']
    return {'changes': out, 'problems': caused, 'send_problems': send,
            'limits': limits(cells, shown.get('BatteryType'), shown)}, raw_changes


def check(reader, rid, changes):
    s = _session(rid)
    result, raw_changes = _evaluate(reader, s, changes)
    result['writable'] = bool(raw_changes)
    return result


def write(reader, rid, changes, passcode, allow_out_of_range=False, ignore_rules=(), force=False, out_dir=None):
    if not config_write.writes_available():
        raise PermissionError('configuration writes are not enabled in this release (not yet validated on a real '
                              'SP PRO); this build is read-only')
    if not writes_allowed():
        raise PermissionError('configuration writes are off (app option allow_config_writes)')
    s = _session(rid)
    result, raw_changes = _evaluate(reader, s, changes)
    errors = [c['error'] for c in result['changes'].values() if 'error' in c]
    if errors:
        raise ValueError('; '.join(errors))
    if not raw_changes:
        raise ValueError('nothing to write: every value is already set')
    if not allow_out_of_range and any(c.get('warnings') for c in result['changes'].values()):
        raise ValueError("a value is outside SP LINK's range (confirm out-of-range values to write anyway)")
    blocking = [p for p in result['problems'] + result['send_problems']
                if p['severity'] == 'error' and p['id'] not in set(ignore_rules or ())]
    if blocking:
        raise ValueError('SP LINK would refuse this: %s' % '; '.join(p['message'] for p in blocking))
    try:
        passcode = int(str(passcode).strip())
    except ValueError:
        raise ValueError('the SP LINK passcode is a number')
    r = config_write.write_settings(reader, raw_changes, passcode, out_dir=out_dir or 'config-writes', force=force,
                                    expected=s['memory'], log=lambda *_: None)
    with _LOCK:
        READS.pop(rid, None)        # the configuration changed: the page must read again
    cells = s['cells']
    return {'written': r['written'], 'backup': os.path.basename(r['backup']),
            'changes': [{'setting': c['setting'], 'old': settings.to_display(c['setting'], c['old'], cells)[1],
                         'new': settings.to_display(c['setting'], c['new'], cells)[1], 'confirmed': c.get('confirmed')}
                        for c in r['changes']],
            'other_changes': r.get('other_changes', {})}
