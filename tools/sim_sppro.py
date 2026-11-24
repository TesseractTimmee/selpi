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

# --- Multiphase (Powerchain) emulation -------------------------------------------------------------------------
# Decoded from the 16.11 SP LINK IL (SP_LINK.mMultiPhase, details/selpi.md "Multi-inverter ... STEP 1/2"). The gateway
# control registers and the discovery sequence are known; the two byte-layouts marked GUESS are what driving SP LINK
# against this sim is meant to confirm/correct - every read of them is logged so a stall tells us what was expected.
MP_REG_STRUCTURE = 0xc0ed      # read (enFrom 0x1d): ActualSP_PROMultiPhaseStructure; gated on commsSW>=200 & memmap>=20
MP_REG_SERIALLIST = 0xdf50     # read (enFrom 0x1c): the list of unit serials
MP_REG_BUILDLIST = 0xdfdf      # write (enFrom 0x1b): "rebuild the serial list"; echo clears SP LINK's wait flag
MP_REG_GATEWAY = 0xdfde        # write (enFrom 0x1f): re-point the link at a unit by index; echo => GatewayAnswered
MP_ID_BROADCAST = 0x2ff        # "spray and pray": address all units
MP_REG_SERIAL = 0xa05e         # unit serial number (uint32, 2 words) - read per unit after re-addressing
MP_REG_COMMISSIONED = 0xca08   # per-unit commissioned-state code; SP LINK needs ==1 to accept + read the phase
MP_REG_PHASEROLE = 0xc0ee      # per-unit phase + role (manager/worker); ==1 => "System Manager" (only the primary)
MP_STRUCTURE_VALUE = 3         # 0xc0ed structure enum: 3 showed L1/L2/L3/Split (Three Phase + Split)
MP_SERIAL_BASE = 10000001      # distinct FAKE serials (unit uid -> MP_SERIAL_BASE+uid); keeps the real one out
# 0xdf50 layout (decoded from DataReceivedRoutine): 64 uint32 slots back-to-back, NO count word; serial[i] is the
# uint32 at 0xdf50+2i; a zero uint32 = empty slot (SP LINK keeps the non-zero ones). SP LINK reads 128 words here.
MP_SERIALLIST_SLOTS = 64
MP_SERIALLIST_WORDS = MP_SERIALLIST_SLOTS * 2


class SimProtocol:
    def __init__(self, mem, multiphase=0, capture_log=None):
        self.mem = dict(mem)
        self.capture_log = capture_log        # file handle: every framed Q/W + reply is logged here (JSONL)
        self.units = None                     # multiphase: {id: per-unit memory dict}; None = single unit
        self.target = 0                       # the unit the link currently points at (set by a 0xdfde write)
        if multiphase:
            self._mp_setup(multiphase)
        self.writes = []
        self.presses = []           # (button, value) for each accepted front panel button press
        self._pressed = {}          # button register -> (value, time pressed)
        self._gen_change = None     # (new status, at) while the generator is starting / stopping
        self._long_gen = []         # times of long generator presses (two = equalise request)
        self.equalise_requested = False
        # configuration write lock (sppro/config_write.py): 0xca14 = 0, passcode -> 0xa001 unlocks (0xa002 = 1)
        self.passcode = 1234        # the SP LINK passcode this simulated unit accepts
        self.blocked_config_writes = []
        self.mem.setdefault(0xca14, struct.pack('<H', 0xffff))
        self.mem.setdefault(0xa002, struct.pack('<H', 0))
        self._lock_open = False     # 0xca14 was written 0 (waiting for the passcode)

    def _mp_setup(self, n):
        """Fake an n-unit Powerchain: one memory per unit + the discovery registers SP LINK reads on connect."""
        base = self.mem
        self.serials = [MP_SERIAL_BASE + uid for uid in range(n)]   # distinct FAKE serials (real one stays out)
        self.units = {}
        for uid in range(n):
            m = dict(base)
            self._seed_u32(m, MP_REG_SERIAL, self.serials[uid])        # this unit's own serial at 0xa05e/0xa05f
            # a fully-commissioned, assigned site: unit uid -> phase/role code uid+1 (1 L1 mgr, 2 L2 mgr, 3 L3 mgr,
            # 4 Split mgr, 5 L1 worker, ...). This is the fixture for testing the ESP/integration per-unit reads;
            # the earlier "factory-default secondaries" seeding (for the SP LINK drag-drop capture) is no longer
            # needed now that step 2 is done.
            m[MP_REG_COMMISSIONED] = struct.pack('<H', 1)
            m[MP_REG_PHASEROLE] = struct.pack('<H', uid + 1)
            m[0xa081] = struct.pack('<H', (50 + uid * 10) * 256)       # Battery SoC (0xa081, %/256) differs per unit
            # discovery registers answered on every unit so they work before/after any re-address
            m[MP_REG_STRUCTURE] = struct.pack('<H', MP_STRUCTURE_VALUE)
            self._seed_serial_list(m, self.serials)
            self.units[uid] = m
        self.mem = self.units[0]
        print('sim: multiphase mode, %d units, serials %s (structure 0x%x=%d)' %
              (n, self.serials, MP_REG_STRUCTURE, MP_STRUCTURE_VALUE), flush=True)

    @staticmethod
    def _seed_u32(mem, addr, value):
        b = struct.pack('<I', value)
        mem[addr], mem[addr + 1] = b[:2], b[2:]

    @staticmethod
    def _seed_serial_list(mem, serials):
        """0xdf50: 64 uint32 slots back-to-back (no count word); non-zero = a unit, zero = empty. Wipe then fill."""
        for w in range(MP_SERIALLIST_WORDS):
            mem[MP_REG_SERIALLIST + w] = b'\0\0'
        for i, s in enumerate(serials):
            SimProtocol._seed_u32(mem, MP_REG_SERIALLIST + i * 2, s)

    def _set_target(self, value):
        """A 0xdfde write re-points the link; route later reads to that unit's memory (broadcast keeps unit 0)."""
        self.target = value
        if self.units is not None:
            self.mem = self.units.get(0 if value == MP_ID_BROADCAST else value, self.units[0])
            print('sim: re-addressed to unit %s%s' %
                  (value, ' (broadcast)' if value == MP_ID_BROADCAST else ''), flush=True)

    def log_frame(self, direction, address, words, data=None, raw=None):
        if not self.capture_log:
            return
        import time
        rec = {'t': round(time.time(), 3), 'dir': direction, 'addr': '0x%x' % address, 'words': words,
               'target': self.target}
        if data is not None:
            rec['data'] = data.hex()
        if raw is not None:
            rec['raw'] = raw.hex()
        if self.units is not None and address in (MP_REG_STRUCTURE, MP_REG_SERIALLIST, MP_REG_BUILDLIST, MP_REG_GATEWAY):
            rec['mp'] = {MP_REG_STRUCTURE: 'structure', MP_REG_SERIALLIST: 'seriallist',
                         MP_REG_BUILDLIST: 'buildlist', MP_REG_GATEWAY: 'gateway'}[address]
        self.capture_log.write(json.dumps(rec) + '\n')
        self.capture_log.flush()

    GEN_REG, OUT_REG = 0xa096, 0xa07d   # Generator Status, Output Mode (see sppro/controls.py)
    TRANSITION = 2.0                     # simulated seconds for Starting -> Running, Stopping -> Not Running

    def _word(self, a):
        return struct.unpack('<H', self.mem.get(a, b'\0\0'))[0]

    def _state(self):
        """Finish a generator start / stop once its transition time has passed."""
        import time
        if self._gen_change and time.monotonic() >= self._gen_change[1]:
            self.mem[self.GEN_REG] = struct.pack('<H', self._gen_change[0])
            self._gen_change = None

    def _press_effect(self, button, value):
        """What an accepted press does, as the SP PRO manual describes it."""
        import time
        self._state()
        now = time.monotonic()
        if button == 'generator' and value == 1:
            g = self._word(self.GEN_REG)
            if g == 0:                                   # Not Running -> Starting -> Running
                self.mem[self.GEN_REG] = struct.pack('<H', 6)
                self._gen_change = (1, now + self.TRANSITION)
            elif g in (1, 6):                            # Running / Starting -> Stopping -> Not Running
                self.mem[self.GEN_REG] = struct.pack('<H', 8)
                self._gen_change = (0, now + self.TRANSITION)
            # anything else (AC Source Present, Disabled, Fault, ...): logged, nothing happens
        elif button == 'generator' and value == 2:
            self._long_gen = [t for t in self._long_gen if now - t < 10] + [now]
            if len(self._long_gen) >= 2:
                self.equalise_requested = True
                self._long_gen = []
                print('sim: equalise requested', flush=True)
        elif button == 'on' and value == 2:
            o = self._word(self.OUT_REG)
            self.mem[self.OUT_REG] = struct.pack('<H', 2 if o == 0 else 0)

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
        self._state()
        return b''.join(self.mem.get(rng.address + i, b'\0\0') for i in range(rng.words))

    def write(self, address, data):
        self.writes.append((address, data))
        if address == MP_REG_GATEWAY and len(data) >= 2:     # re-point the link at a unit (echo => GatewayAnswered)
            self._set_target(struct.unpack('<H', data[:2])[0])
            self.mem[address] = data[:2]
            return
        if address == MP_REG_BUILDLIST:                      # "rebuild serial list"; echo clears SP LINK's wait flag
            print('sim: serial-list rebuild requested', flush=True)
            self.mem[address] = data[:2] if len(data) >= 2 else b'\0\0'
            return
        if address in BUTTON_REGS and len(data) >= 2:
            import time
            self._buttons()
            value = struct.unpack('<H', data[:2])[0]
            if address in self._pressed:              # still being pressed: the unit reports 3
                self._pressed[address] = (3, self._pressed[address][1])
            else:
                self._pressed[address] = (value, time.monotonic())
                self.presses.append((BUTTON_REGS[address], value))
                self._press_effect(BUTTON_REGS[address], value)
                print('sim: %s button %s press' % (BUTTON_REGS[address], 'long' if value == 2 else 'short'), flush=True)
            self._buttons()
            return
        if address in (0xca14, 0xa001, 0xa002) and len(data) >= 2:
            v = struct.unpack('<H', data[:2])[0]
            if address == 0xca14:
                self._lock_open = v == 0
                if v == 0xffff:
                    self.mem[0xa002] = struct.pack('<H', 0)
            elif address == 0xa001 and v != 0:
                # Unlock on the passcode. The config page opens 0xca14=0 first (_lock_open); SP LINK's multiphase
                # Save writes the passcode straight to 0xa001 with no 0xca14 step, so also unlock when it matches.
                ok = v == self.passcode or (self._lock_open and v == self.passcode)
                self.mem[0xa002] = struct.pack('<H', 1 if ok else 0)
                if ok:
                    print('sim: config unlocked (passcode 0x%04x)' % v, flush=True)
            elif address == 0xa002:
                self.mem[0xa002] = struct.pack('<H', v)
            self.mem[address] = data[:2]
            return
        if 0xc000 <= address < 0xc900 and struct.unpack('<H', self.mem.get(0xa002, b'\0\0'))[0] != 1:
            self.blocked_config_writes.append((address, data))   # locked: the unit ignores setting writes
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
                # drift: SoC (0xa081, %/256) and battery power (0xa02f, int32) change over time; in multiphase each
                # unit gets a per-target offset so the phases read differently (so system sum/avg are meaningful)
                k = int(time.time() - t0)
                off = sim.target if sim.units is not None and sim.target != MP_ID_BROADCAST else 0
                sim.mem[0xa081] = struct.pack('<H', (60 * 256 + (k * 16) % (30 * 256) + off * 5 * 256) & 0xffff)
                p = struct.pack('<i', -2000 + (k * 37) % 4000 + off * 1000)
                sim.mem[0xa02f], sim.mem[0xa030] = p[:2], p[2:]
                if t == 'W':
                    data = req[8:8 + 2 * n]
                    sim.log_frame('c2s', a, n, data=data)
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
                    sim.log_frame('s2c', a, n, raw=req)
                else:
                    sim.log_frame('c2s', a, n)
                    if not logged_in and a not in range(0x1f0000, 0x1f0011):
                        continue                    # silent, like the real unit before login
                    body = req[:8] + sim.query(type('R', (), {'address': a, 'words': n})())
                    reply = body + crc16(body)
                    conn.sendall(reply)
                    sim.log_frame('s2c', a, n, raw=reply)
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
    ap.add_argument('--multiphase', type=int, default=0, metavar='N',
                    help='serve: fake an N-unit Powerchain (discovery registers + per-unit memories)')
    ap.add_argument('--capture-log', metavar='FILE', help='serve: log every framed Q/W + reply to FILE (JSONL)')
    a = ap.parse_args()
    log = open(a.capture_log, 'a') if a.capture_log else None
    sim = SimProtocol(capture_memory(a.capture), multiphase=a.multiphase, capture_log=log)
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
