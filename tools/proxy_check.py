#!/usr/bin/env python3
"""Talk raw SP PRO frames to the ESP's proxy port: query a block, try a refused write.
  python tools/proxy_check.py HOST PORT [SOURCE_ADDRESS]"""
import socket
import struct
import sys
import time

sys.path.insert(0, '.')
from memory import CRC  # noqa: E402


def q(addr, words):
    m = b'Q' + bytes([words - 1]) + struct.pack('<I', addr)
    return m + CRC(m).as_bytes()


def w(addr, data):
    m = b'W' + bytes([len(data) // 2 - 1]) + struct.pack('<I', addr)
    m += CRC(m).as_bytes() + data
    return m + CRC(m).as_bytes()


def recv(s, n, wait=3):
    s.settimeout(wait)
    buf = b''
    try:
        while len(buf) < n:
            c = s.recv(n - len(buf))
            if not c:
                break
            buf += c
    except socket.timeout:
        pass
    return buf


s = socket.create_connection((sys.argv[1], int(sys.argv[2])),
                             source_address=(sys.argv[3], 0) if len(sys.argv) > 3 else None)
for addr, n in ((0xa081, 1), (0xa007, 25), (0xa058, 85)):
    t = time.time()
    s.sendall(q(addr, n))
    r = recv(s, 8 + 2 * n + 2)
    ok = len(r) == 8 + 2 * n + 2 and CRC(r).as_int() == 0
    print('Q 0x%x x%d -> %d bytes, crc %s, %.0f ms, first words %s' % (
        addr, n, len(r), 'ok' if ok else 'BAD', (time.time() - t) * 1000,
        list(struct.unpack('<%dH' % min(n, 6), r[8:8 + 2 * min(n, 6)])) if ok else '-'))
s.sendall(w(0xc185, struct.pack('<H', 2183)))
r = recv(s, 12, wait=2)
print('W 0xc185 = 2183:', 'no reply (refused)' if not r else ('echoed (written)' if len(r) == 12 else 'reply %r' % r))
