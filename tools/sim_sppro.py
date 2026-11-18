#!/usr/bin/env python3
"""
Offline SP PRO simulator for testing selpi's config/logs commands against a capture.

Memory comes from a recorded session (every reply to a query). Writes to a log's range register
run the search the SP PRO would: matching records (by timestamp) within that log's segments.

  python tools/sim_sppro.py CAPTURE.jsonl config DIR
  python tools/sim_sppro.py CAPTURE.jsonl logs SINCE UNTIL DIR
  python tools/sim_sppro.py CAPTURE.jsonl serve PORT [PASSWORD]   raw Q/W frames over TCP (like a serial
      link); with PASSWORD it stays silent until the 0x1f0000 MD5 login succeeds. Battery SoC and power
      drift slowly so pollers see changing values.
"""
import argparse
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sppro import logs  # noqa: E402


def capture_memory(path):
    """{address: 2 bytes} from all query replies in a recorded session."""
    mem = {}
    sessions = []
    cur = None
    for line in open(path):
        r = json.loads(line)
        if r.get('event') == 'connect':
            cur = {'s': b'', 'ready': False}
            sessions.append(cur)
            continue
        if cur is None or r.get('dir') != 's2c' or 'hex' not in r:
            continue
        b = bytes.fromhex(r['hex'])
        if not cur['ready']:
            cur['ready'] = b'READY' in b
            continue
        cur['s'] += b
    for s in sessions:
        buf, i = s['s'], 0
        while i + 8 <= len(buf):
            t, n, a = buf[i], buf[i + 1] + 1, int.from_bytes(buf[i + 2:i + 6], 'little')
            if t == 0x51:
                d = buf[i + 8:i + 8 + n * 2]
                for k in range(len(d) // 2):
                    mem[a + k] = d[2 * k:2 * k + 2]
                i += 8 + n * 2 + 2
            elif t == 0x57:
                i += 8 + n * 2 + 2
            else:
                i += 1
    return mem


BUTTON_REGS = {0xa232: 'generator', 0xa233: 'on', 0xa234: 'silence',
               0xa235: 'generator', 0xa236: 'on', 0xa237: 'silence'}   # port 1 / port 2 (see sppro/buttons.py)


class SimProtocol:
    def __init__(self, mem):
        self.mem = dict(mem)
        self.writes = []
        self.presses = []           # (button, value) for each accepted front panel button press
        self._pressed = {}          # button register -> (value, time pressed)

    def _buttons(self):
        """A pressed button reads back its value for about a second, then 0."""
        import time
        for a, (v, t) in list(self._pressed.items()):
            if time.monotonic() - t > 1.0:
                del self._pressed[a]
        for a in BUTTON_REGS:
            v = self._pressed.get(a, (0, 0))[0]
            self.mem[a] = struct.pack('<H', v)

    clock_offset = -40          # simulated SP PRO clock: local time + this many seconds (set by writes)

    def _clock(self):
        import datetime
        t = datetime.datetime.now() + datetime.timedelta(seconds=self.clock_offset)
        bcd = lambda v: ((v // 10) << 4) | (v % 10)
        vals = [0, t.second, t.minute, t.hour, (t.isoweekday() % 7) + 1, t.day, t.month, t.year % 100]
        for i, v in enumerate(vals):
            self.mem[0x1d0000 + i] = struct.pack('<H', bcd(v))

    def query(self, rng):
        if 0x1d0000 <= rng.address < 0x1d0008:
            self._clock()
        if any(rng.address <= a < rng.address + rng.words for a in BUTTON_REGS):
            self._buttons()
        return b''.join(self.mem.get(rng.address + i, b'\0\0') for i in range(rng.words))

    def write(self, address, data):
        self.writes.append((address, data))
        if address in BUTTON_REGS and len(data) >= 2:
            import time
            self._buttons()
            value = struct.unpack('<H', data[:2])[0]
            if address in self._pressed:              # still being pressed: the unit reports 3
                self._pressed[address] = (3, self._pressed[address][1])
            else:
                self._pressed[address] = (value, time.monotonic())
                self.presses.append((BUTTON_REGS[address], value))
                print('sim: %s button %s press' % (BUTTON_REGS[address], 'long' if value == 2 else 'short'), flush=True)
            self._buttons()
            return
        if address == 0x1d0011 and len(data) >= 14:      # set clock: sec min hour wday day month yy (BCD)
            import datetime
            b = lambda i: (data[2 * i] >> 4) * 10 + (data[2 * i] & 15)
            t = datetime.datetime(2000 + b(6), b(5), b(4), b(2), b(1), b(0))
            self.clock_offset = round((t - datetime.datetime.now()).total_seconds())
            print('sim: clock set, offset now %+d s' % self.clock_offset, flush=True)
        for k in range(len(data) // 2):
            self.mem[address + k] = data[2 * k:2 * k + 2]
        # the range block's 5th word is the result's status word: writing 1 starts the search
        for log in logs.LOGS.values():
            if address == log.range_reg:
                self._search(log, *struct.unpack('<IIH', data)[:2])

    def _u32(self, a):
        return struct.unpack('<I', self.mem.get(a, b'\0\0') + self.mem.get(a + 1, b'\0\0'))[0]

    def _search(self, log, since, until):
        count = struct.unpack('<H', self.mem[log.descriptor])[0]
        segs = [(self._u32(log.descriptor + 5 + 4 * i), self._u32(log.descriptor + 7 + 4 * i)) for i in range(count)]
        hits = []
        for start, end in segs:
            a = start
            while a + log.record_words - 1 <= end:
                if a in self.mem:
                    ts = self._u32(a)
                    if since <= ts <= until:
                        hits.append((ts, a))
                a += log.record_words
        res = struct.pack('<HIIH', 4, 0, 0, 0)
        if hits:
            hits.sort()
            res = struct.pack('<HIIH', 3, hits[0][1], hits[-1][1], len(hits))
        for k in range(6):
            self.mem[log.result_reg + k] = res[2 * k:2 * k + 2]


def crc16(msg):
    from memory import CRC
    return CRC(bytes(msg)).as_bytes()


def serve(sim, port, password=None):
    """Answer raw SP PRO frames on TCP, one client at a time (a stand-in for the USB/serial link)."""
    import hashlib
    import socket
    import time
    challenge = bytes(range(0x10, 0x20))
    logged_in = password is None
    srv = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    srv.bind(('::', port))
    srv.listen(1)
    print('sim: serving on port %d%s' % (port, ' (login required)' if password else ''), flush=True)
    for a in range(8):
        sim.mem[0x1f0000 + a] = challenge[2 * a:2 * a + 2]
    t0 = time.time()
    while True:
        conn, peer = srv.accept()
        print('sim: client', peer[0], flush=True)
        buf = b''
        n_q = n_w = 0
        while True:
            try:
                chunk = conn.recv(4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while len(buf) >= 8:
                t, n, a = chr(buf[0]), buf[1] + 1, int.from_bytes(buf[2:6], 'little')
                if t not in 'QW':
                    buf = buf[1:]
                    continue
                need = 8 if t == 'Q' else 8 + 2 * n + 2
                if len(buf) < need:
                    break
                req, buf = buf[:need], buf[need:]
                # drift: SoC (0xa081, %/256) and battery power (0xa02f, int32) change over time
                k = int(time.time() - t0)
                sim.mem[0xa081] = struct.pack('<H', (60 * 256 + (k * 16) % (30 * 256)) & 0xffff)
                p = struct.pack('<i', -2000 + (k * 37) % 4000)
                sim.mem[0xa02f], sim.mem[0xa030] = p[:2], p[2:]
                if t == 'W':
                    data = req[8:8 + 2 * n]
                    if a == 0x1f0000 and password is not None:
                        md5 = hashlib.md5(challenge + password.encode().ljust(32)).digest()
                        want = b''.join(md5[i + 1:i + 2] + md5[i:i + 1] for i in range(0, 16, 2))
                        logged_in = data == want
                        sim.mem[0x1f0010] = struct.pack('<H', 1 if logged_in else 0)
                        print('sim: login', 'ok' if logged_in else 'FAILED', flush=True)
                    elif logged_in:
                        sim.write(a, data)
                    n_w += 1
                    conn.sendall(req)
                else:
                    if not logged_in and a not in range(0x1f0000, 0x1f0011):
                        continue                    # silent, like the real unit before login
                    body = req[:8] + sim.query(type('R', (), {'address': a, 'words': n})())
                    conn.sendall(body + crc16(body))
                    n_q += 1
        conn.close()
        print('sim: client gone (%d queries, %d writes)' % (n_q, n_w), flush=True)
        logged_in = password is None


def main():
    from commands import config as config_cmd, logs as logs_cmd
    from sppro.reader import Reader
    ap = argparse.ArgumentParser()
    ap.add_argument('capture')
    ap.add_argument('what', choices=['config', 'logs', 'serve'])
    ap.add_argument('rest', nargs='*')
    a = ap.parse_args()
    sim = SimProtocol(capture_memory(a.capture))
    if a.what == 'serve':
        serve(sim, int(a.rest[0]), a.rest[1] if len(a.rest) > 1 else None)
        return
    reader = Reader(lambda: sim)
    if a.what == 'config':
        config_cmd.run_backup(argparse.Namespace(out=a.rest[0], json=False), reader)
    else:
        since, until, out = a.rest
        logs_cmd.run(argparse.Namespace(since=since, until=until, logs='events,alerts,detailed,daily', out=out), reader)
    print('writes:', [('0x%x' % w[0], w[1].hex()) for w in sim.writes])


if __name__ == '__main__':
    main()
