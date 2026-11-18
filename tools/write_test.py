#!/usr/bin/env python3
"""
Single-setting write test (SP PRO), mirroring SP LINK's unlock sequence from the 17 Nov capture.

    write_test.py --code N ADDRESS VALUE            dry run: print every frame, send nothing
    write_test.py --code N ADDRESS VALUE --send     do it

Sequence: preflight reads -> full config backup -> W 0xa251=1 (download mode, as SP LINK) ->
W 0xca14=0 -> W 0xa001=code -> W 0xa001=0 -> Q 0xa002 must be 1 (unlocked) -> W ADDRESS=VALUE ->
always: W 0xa002=0, W 0xca14=0xffff, W 0xa251=0 -> read back ADDRESS + full config, diff.
Every write is echo-checked by memory.Protocol. Requests are never retried mid-sequence.
"""
import argparse
import json
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import Request  # noqa: E402
from sppro import config  # noqa: E402
from sppro import live  # noqa: E402
from sppro.reader import Reader  # noqa: E402

DL_MODE, LOCK, CODE, UNLOCKED = 0xa251, 0xca14, 0xa001, 0xa002


def frame(address, value):
    return Request.create_write(address, struct.pack('<H', value)).get_message().hex()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--code', type=int, required=True)
    ap.add_argument('address', type=lambda s: int(s, 0))
    ap.add_argument('value', type=int)
    ap.add_argument('--send', action='store_true')
    ap.add_argument('--out', default='captures/live')
    a = ap.parse_args()

    names = {addr: name for _, _, name, addr in config.slots() if name}
    if a.address not in names:
        sys.exit('0x%x is not a known config slot' % a.address)
    plan = [('download mode on', DL_MODE, 1), ('unlock step 1', LOCK, 0), ('installer code', CODE, a.code),
            ('clear code', CODE, 0), ('SETTING %s' % names[a.address], a.address, a.value),
            ('relock 1', UNLOCKED, 0), ('relock 2', LOCK, 0xffff), ('download mode off', DL_MODE, 0)]
    print('Frames (hex):')
    for what, addr, val in plan:
        shown = frame(addr, val) if addr != CODE or val == 0 else '<installer code frame>'
        print('  %-28s W 0x%04x = %-6d %s' % (what, addr, val if addr != CODE or val == 0 else 0, shown))
    print('  (check Q 0xa002 == 1 between "clear code" and SETTING; relock always runs)')
    if not a.send:
        return

    r = Reader(retries=1)
    pre = {n: r.words(x, 1)[0] for n, x in (('download_mode', DL_MODE), ('lock', LOCK), ('unlocked', UNLOCKED))}
    print('preflight', pre)
    if pre['download_mode'] != 0 or pre['lock'] != 0xffff:
        sys.exit('SP PRO busy or already unlocked (another session?): not writing')
    problems = live.version_problems(live.versions(r))
    if problems:   # config layout differs from the one this was verified on: never write blind
        sys.exit('unit versions differ from the verified map (%s): not writing' % ', '.join(problems))
    before = config.read_memory(r)
    stamp = time.strftime('%Y-%m-%d_%H-%M-%S')
    os.makedirs(a.out, exist_ok=True)
    with open('%s/write-test-before_%s.json' % (a.out, stamp), 'w') as fh:
        json.dump({'%x' % k: v for k, v in before.items()}, fh)
    print('backup: %d words, current 0x%x = %d' % (len(before), a.address, before.get(a.address)))

    wrote = False
    try:
        for what, addr, val in plan[:4]:
            r.write(addr, struct.pack('<H', val))
            print('  ok', what)
        state = r.words(UNLOCKED, 1)[0]
        print('  Q 0xa002 =', state)
        if state != 1:
            raise RuntimeError('not unlocked (0xa002=%d): setting NOT written' % state)
        r.write(a.address, struct.pack('<H', a.value))
        wrote = True
        print('  ok SETTING written')
    finally:
        relock = Reader(retries=3)
        for what, addr, val in plan[5:]:
            try:
                r.write(addr, struct.pack('<H', val))
                print('  ok', what)
            except Exception as e:  # connection lost: retry on a fresh connection
                print('  %s failed on this connection (%s), retrying fresh' % (what, e))
                relock.write(addr, struct.pack('<H', val))
                print('  ok', what, '(fresh connection)')

    after = config.read_memory(Reader(retries=3))
    with open('%s/write-test-after_%s.json' % (a.out, stamp), 'w') as fh:
        json.dump({'%x' % k: v for k, v in after.items()}, fh)
    diff = {k: (before.get(k), v) for k, v in after.items() if before.get(k) != v}
    print('read back 0x%x = %d' % (a.address, after.get(a.address)))
    print('changed words:', {('0x%x %s' % (k, names.get(k, ''))): ov for k, ov in sorted(diff.items())} or 'none')


if __name__ == '__main__':
    main()
