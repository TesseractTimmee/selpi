"""
SP LINK proxy: a TCP port that looks like an SP PRO's serial link, sharing the app's own link.

SP LINK (or anything that speaks raw Q/W frames) connects here. Each complete, CRC-valid frame is run on
the shared link between the app's own requests (Inverter.use) and the reply goes back unchanged, so the
two never interleave mid-frame. Works over every connection the app has (serial, TCP/ESP, Select.Live;
on Select.Live every frame is a cloud round trip, so SP LINK may be slow or time out).

Rules (same as the ESP firmware's proxy):
- one client at a time; only addresses in `allowed` may connect;
- writes are refused (dropped without a reply, as the unit does for a bad frame) unless the client is in
  `write`; login writes (0x1f0000) always pass, and with `log_downloads` so do the writes a log download
  needs (0xa251 = 0/1 and the four 5-word log search ranges);
- invalid bytes are skipped until a frame header lines up again.
"""
import ipaddress
import logging
import socket
import struct
import threading
import time

from memory.crc import CRC

LOGIN_ADDRESS = 0x1f0000
LOG_RANGES = (0xa2d4, 0xa293, 0xa321, 0xa374)
DEFAULT_ALLOWED = ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8', 'fc00::/7', '::1/128')


def nets(items):
    out = []
    for x in items or ():
        x = str(x).strip()
        if x:
            out.append(ipaddress.ip_network(x, strict=False))
    return out


def in_nets(ip, networks):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if a.version == 6 and a.ipv4_mapped:
        a = a.ipv4_mapped
    return any(a in n for n in networks if n.version == a.version)


def request_len(buf):
    """Bytes in the frame starting at buf[0] (needs buf[0:2]): Q = 8, W = 8 + data + 2."""
    return 8 if buf[0:1] == b'Q' else 8 + 2 * (buf[1] + 1) + 2


def reply_len(frame):
    """A query answers with header + data + CRC; a write echoes the whole frame."""
    return 8 + 2 * (frame[1] + 1) + 2 if frame[0:1] == b'Q' else len(frame)


def frame_address(frame):
    return struct.unpack('<I', frame[2:6])[0]


def valid(frame):
    return CRC(frame[:8]).as_int() == 0 and CRC(frame).as_int() == 0


def is_log_download_write(frame):
    a, words = frame_address(frame), frame[1] + 1
    if a == 0xa251:
        return words == 1 and frame[9] == 0 and frame[8] <= 1
    return words == 5 and a in LOG_RANGES


class Proxy:
    def __init__(self, inverter, busy_exc, port, allowed=DEFAULT_ALLOWED, write=(), log_downloads=True,
                 host='::', frame_wait=15):
        self.inverter, self.Busy = inverter, busy_exc
        self.port, self.host, self.frame_wait = port, host, frame_wait
        self.allowed, self.write, self.log_downloads = nets(allowed), nets(write), log_downloads
        self.lock = threading.Lock()
        self.client = None            # peer address of the connected client
        self.status = {'client': None, 'may_write': False, 'since': None, 'frames': 0, 'refused_writes': 0,
                       'failed': 0, 'last_error': None}

    def start(self):
        fam = socket.AF_INET6 if ':' in self.host else socket.AF_INET
        srv = socket.socket(fam, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if fam == socket.AF_INET6:
            srv.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        srv.bind((self.host, self.port))
        srv.listen(2)
        self.port = srv.getsockname()[1]
        threading.Thread(target=self._accept, args=(srv,), daemon=True, name='splink-proxy').start()
        logging.info('SP LINK proxy on port %d (allowed %s; may write: %s; log downloads %s)', self.port,
                     ', '.join(map(str, self.allowed)) or 'nobody', ', '.join(map(str, self.write)) or 'nobody',
                     'on' if self.log_downloads else 'off')
        return self

    def _accept(self, srv):
        while True:
            conn, addr = srv.accept()
            ip = addr[0]
            if not in_nets(ip, self.allowed):
                logging.warning('SP LINK proxy: rejected %s (not in allowed)', ip)
                conn.close()
                continue
            with self.lock:
                if self.client is not None:
                    logging.warning('SP LINK proxy: rejected %s (%s is connected)', ip, self.client)
                    conn.close()
                    continue
                self.client = ip
            threading.Thread(target=self._serve, args=(conn, ip), daemon=True, name='splink-client').start()

    def _serve(self, conn, ip):
        may_write = bool(self.write) and in_nets(ip, self.write)
        self.status.update(client=ip, may_write=may_write, since=time.time())
        self.inverter.keep_open += 1          # don't drop the link between SP LINK's frames
        logging.info('SP LINK proxy: client %s connected (%s)', ip, 'may write' if may_write else 'read-only')
        try:   # warm the shared link (connect, version check, login) before SP LINK's first frame
            self.inverter.use('SP LINK connect (%s)' % ip, lambda r: None, wait=self.frame_wait)
        except Exception as e:
            logging.warning('SP LINK proxy: link not ready for %s: %s', ip, e)
        buf = bytearray()
        try:
            conn.settimeout(600)                 # SP LINK idle for 10 min = gone
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                buf += data
                while len(buf) >= 2:
                    if buf[0] not in b'QW':
                        del buf[0]
                        continue
                    need = request_len(buf)
                    if len(buf) < need:
                        break
                    frame = bytes(buf[:need])
                    if not valid(frame):
                        del buf[0]               # resync on the next header byte
                        continue
                    del buf[:need]
                    reply = self.handle(frame, ip, may_write)
                    if reply:
                        conn.sendall(reply)
        except (OSError, socket.timeout) as e:
            logging.info('SP LINK proxy: client %s: %s', ip, e)
        finally:
            conn.close()
            self.inverter.keep_open -= 1
            with self.lock:
                self.client = None
            self.status.update(client=None, may_write=False, since=None)
            logging.info('SP LINK proxy: client %s gone (%d frames)', ip, self.status['frames'])

    def handle(self, frame, ip, may_write):
        """Run one valid frame on the shared link; None = no reply (refused or failed)."""
        address = frame_address(frame)
        if frame[0:1] == b'W' and address != LOGIN_ADDRESS and not may_write and \
                not (self.log_downloads and is_log_download_write(frame)):
            self.status['refused_writes'] += 1
            logging.warning('SP LINK proxy: refused write to 0x%x from %s (read-only)', address, ip)
            return None
        try:
            reply = self.inverter.use('SP LINK (%s)' % ip, lambda r: r.raw(frame, reply_len(frame)),
                                      wait=self.frame_wait)
            self.status['frames'] += 1
            return reply
        except self.Busy as e:
            self.status['failed'] += 1
            self.status['last_error'] = str(e)
        except Exception as e:  # silent unit, link loss: SP LINK retries like on a serial line
            self.status['failed'] += 1
            self.status['last_error'] = '%s: %s' % (type(e).__name__, e)
            logging.info('SP LINK proxy: %s frame 0x%x failed: %s', frame[0:1].decode(), address, e)
        return None
