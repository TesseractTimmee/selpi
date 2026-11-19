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
    """Write one setting. fresh_reader(): a new connection for relocking if this one fails mid-way."""
    if not 0 <= value <= 0xffff:
        raise WriteError('value %r out of range (raw 16-bit: 0..65535)' % value)
    setting = find_setting(name)
    preflight(reader, force=force)
    before = config.read_memory(reader)
    old = before.get(setting['address'])
    stamp = time.strftime('%Y-%m-%d_%H-%M-%S')
    backup = _save(out_dir, stamp, 'before_%s' % setting['name'], before)
    log('backup: %s.SPLC / .json (%d words); %s now %s' % (backup, len(before), setting['name'], old))
    if old == value:
        return {'setting': setting['name'], 'address': setting['address'], 'old': old, 'new': old,
                'written': False, 'note': 'already %d: nothing written' % value, 'backup': backup}

    steps = plan(setting, value)
    written = False
    try:
        for what, addr, val in steps[:4]:
            reader.write(addr, struct.pack('<H', passcode if what == 'passcode' else val))
            log('  ok %s' % what)
        state = reader.words(UNLOCKED, 1)[0]
        if state != 1:
            raise WriteError('not unlocked (0xa002 = %d: wrong passcode, or this passcode level may not '
                             'change settings): %s NOT written' % (state, setting['name']))
        reader.write(setting['address'], struct.pack('<H', value))
        written = True
        log('  ok %s = %d' % (setting['name'], value))
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
    after_path = _save(out_dir, stamp, 'after_%s' % setting['name'], after)
    names = {a: n for _, _, n, a in config.slots() if n}
    changed = {a: (before.get(a), v) for a, v in after.items() if before.get(a) != v}
    others = {('0x%x %s' % (a, names.get(a, ''))).strip(): ov for a, ov in sorted(changed.items())
              if a != setting['address']}
    return {'setting': setting['name'], 'address': setting['address'], 'old': old,
            'new': after.get(setting['address']), 'written': written, 'confirmed': after.get(setting['address']) == value,
            'other_changes': others, 'backup': backup, 'after': after_path,
            'undo': 'selpi config set %s %d --yes' % (setting['name'], old)}
