"""
Write one SP PRO configuration setting the way SP LINK does (docs/protocol.md, "Configuration write"):

  download mode 0xa251 = 1, 0xca14 = 0, passcode -> 0xa001, 0xa001 = 0, check 0xa002 == 1 (unlocked),
  write the one word, then always relock: 0xa002 = 0, 0xca14 = 0xffff, 0xa251 = 0.

The passcode is the SP LINK passcode; the SP PRO checks it (a wrong one leaves 0xa002 at 0 and nothing is
written). Guards: exact setting name (configuration settings only), 16-bit value, the unit idle (download mode
off, locked) and on the verified versions, a full backup first, relock even after an error, and a full
read-back afterwards that reports every word that changed.
"""
import json
import os
import struct
import time

from sppro import config, live

DL_MODE, LOCK, CODE, UNLOCKED = 0xa251, 0xca14, 0xa001, 0xa002
LOCKED_VALUE = 0xffff


class WriteError(Exception):
    pass


def find_setting(name: str) -> dict:
    """The configuration setting with exactly this name (case-insensitive)."""
    hits = [s for s in config.settings({}) if s['section'] != 'Service' and s['name'].lower() == name.lower()]
    if not hits:
        try:   # settings SP LINK shows but has no name for (e.g. Port1AccInput, 0xc087): from settings_meta
            from sppro import settings
            e = settings.entry(name)
            a = settings.address(e['name'])
            if a is not None and e['name'].lower() == name.lower():
                return {'name': e['name'], 'section': e['section'], 'address': a, 'value': None}
        except Exception:
            pass
        near = [s['name'] for s in config.settings({}) if name.lower() in s['name'].lower()][:8]
        raise WriteError('no configuration setting called %r%s' % (name, ('; did you mean: ' + ', '.join(near)) if near else ''))
    if len(hits) > 1:
        raise WriteError('%r matches %d settings: %s' % (name, len(hits), ', '.join(h['address'] for h in hits)))
    return dict(hits[0], address=int(hits[0]['address'], 16))


def plan(setting: dict, value: int) -> list:
    """(what, address, value) steps; the passcode step's value is filled in at write time."""
    return [('download mode on', DL_MODE, 1), ('unlock', LOCK, 0), ('passcode', CODE, None), ('clear code', CODE, 0),
            ('SETTING %s' % setting['name'], setting['address'], value),
            ('relock 1', UNLOCKED, 0), ('relock 2', LOCK, LOCKED_VALUE), ('download mode off', DL_MODE, 0)]


def preflight(reader, force=False) -> dict:
    state = {'download_mode': reader.words(DL_MODE, 1)[0], 'lock': reader.words(LOCK, 1)[0],
             'unlocked': reader.words(UNLOCKED, 1)[0]}
    if state['download_mode'] != 0 or state['lock'] != LOCKED_VALUE or state['unlocked'] != 0:
        raise WriteError('SP PRO busy or already unlocked (another session, e.g. SP LINK or a log download?): %s'
                         % state)
    try:
        live.check_versions(reader, writes=True, force=force)
    except live.VersionMismatch as e:
        raise WriteError(str(e))
    return state


def _save(out_dir, stamp, label, memory):
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, '%s_%s' % (stamp, label))
    with open(path + '.SPLC', 'w', newline='') as fh:
        fh.write(config.to_splc(memory))
    with open(path + '.json', 'w') as fh:
        json.dump({'%x' % k: v for k, v in sorted(memory.items())}, fh)
    return path


def write_setting(reader, name: str, value: int, passcode: int, out_dir='config-writes', force=False,
                  fresh_reader=None, log=print) -> dict:
    """Write one setting (see write_settings). fresh_reader(): a new connection for relocking if this one fails."""
    r = write_settings(reader, {name: value}, passcode, out_dir=out_dir, force=force, fresh_reader=fresh_reader, log=log)
    c = r['changes'][0]
    out = {'setting': c['setting'], 'address': c['address'], 'old': c['old'], 'new': c['new'], 'written': r['written'],
           'backup': r['backup']}
    if not r['written']:
        out['note'] = 'already %d: nothing written' % c['old']
        return out
    out.update(confirmed=c['confirmed'], other_changes=r['other_changes'], after=r['after'],
               undo='selpi config set %s %d --yes' % (c['setting'], c['old']))
    return out


def write_settings(reader, values: dict, passcode: int, out_dir='config-writes', force=False, fresh_reader=None,
                   expected=None, log=print) -> dict:
    """Write several settings {name: raw} in one unlock session: preflight, full backup, unlock, every changed word,
    relock always, full read-back. expected: the configuration memory as read earlier in the session (e.g. by the
    web page); if the unit's configuration differs from it now, nothing is written."""
    settings_ = []
    for name, value in values.items():
        if not 0 <= value <= 0xffff:
            raise WriteError('%s: value %r out of range (raw 16-bit: 0..65535)' % (name, value))
        settings_.append((find_setting(name), value))
    preflight(reader, force=force)
    before = config.read_memory(reader)
    if expected is not None:
        moved = sorted(a for a in set(before) | set(expected) if before.get(a) != expected.get(a))
        if moved:
            names = {a: n for _, _, n, a in config.slots() if n}
            raise WriteError('the configuration changed on the unit since it was read (%s): read it again first'
                             % ', '.join('0x%x %s' % (a, names.get(a, '')) for a in moved[:8]))
    stamp = time.strftime('%Y-%m-%d_%H-%M-%S')
    label = settings_[0][0]['name'] if len(settings_) == 1 else '%d-settings' % len(settings_)
    backup = _save(out_dir, stamp, 'before_%s' % label, before)
    log('backup: %s.SPLC / .json (%d words)' % (backup, len(before)))
    todo = [(st, v) for st, v in settings_ if before.get(st['address']) != v]
    changes = [{'setting': st['name'], 'address': st['address'], 'old': before.get(st['address']), 'new': v}
               for st, v in settings_]
    if not todo:
        return {'written': False, 'changes': changes, 'backup': backup}

    steps = plan(todo[0][0], todo[0][1])
    written = False
    try:
        for what, addr, val in steps[:4]:
            reader.write(addr, struct.pack('<H', passcode if what == 'passcode' else val))
            log('  ok %s' % what)
        state = reader.words(UNLOCKED, 1)[0]
        if state != 1:
            raise WriteError('not unlocked (0xa002 = %d: wrong passcode, or this passcode level may not '
                             'change settings): nothing written' % state)
        for st, v in todo:
            reader.write(st['address'], struct.pack('<H', v))
            written = True
            log('  ok %s = %d' % (st['name'], v))
    finally:
        for what, addr, val in steps[5:]:
            try:
                reader.write(addr, struct.pack('<H', val))
                log('  ok %s' % what)
            except Exception as e:  # connection lost mid-way: relock on a fresh connection
                if fresh_reader is None:
                    raise
                log('  %s failed on this connection (%s), retrying on a fresh one' % (what, e))
                fresh_reader().write(addr, struct.pack('<H', val))
                log('  ok %s (fresh connection)' % what)

    after = config.read_memory(fresh_reader() if fresh_reader else reader)
    after_path = _save(out_dir, stamp, 'after_%s' % label, after)
    names = {a: n for _, _, n, a in config.slots() if n}
    mine = {st['address'] for st, _ in settings_}
    others = {('0x%x %s' % (a, names.get(a, ''))).strip(): (before.get(a), v) for a, v in sorted(after.items())
              if before.get(a) != v and a not in mine}
    for c, (st, v) in zip(changes, settings_):
        c['new'] = after.get(st['address'])
        c['confirmed'] = c['new'] == v
    return {'written': written, 'changes': changes, 'other_changes': others, 'backup': backup, 'after': after_path,
            'memory': after}
