import logging
import struct
import time

MAX_WORDS = 256


class Reader:
    """
    Word-addressed reads and writes with chunking and reconnect-on-failure.

    Select.Live sometimes never delivers a reply and closes the connection about a minute later;
    every request is therefore retried on a fresh connection a few times before giving up.
    """
    def __init__(self, protocol_factory=None, retries: int = 3):
        if protocol_factory is None:
            from memory import Protocol
            protocol_factory = Protocol
        self.__factory = protocol_factory
        self.__protocol = None
        self.__retries = retries
        # monotonic time after which calls stop retrying and give up, so a single operation (e.g. a whole
        # configuration read of many blocks over a flaky Select.Live link) can't hold the link lock for minutes.
        self.deadline = None
        # Called with this reader just after each (re)connect, to re-establish per-session device state before the
        # next read. Select.Live drops the link mid-transfer and we reconnect transparently; a log download sets
        # this so download mode (0xa251) is re-asserted on the fresh session rather than reading from a unit that
        # has fallen out of it. Reentrancy-guarded: the hook's own writes won't re-trigger it.
        self.on_connect = None
        self.__priming = False

    def __expired(self):
        return self.deadline is not None and time.monotonic() > self.deadline

    def __reconnect(self):
        self.__protocol = self.__factory()
        if self.on_connect is not None and not self.__priming:
            self.__priming = True
            try:
                self.on_connect(self)
            finally:
                self.__priming = False

    def __call(self, what: str, fn):
        last = None
        for attempt in range(1, self.__retries + 1):
            if self.__expired():
                raise TimeoutError('%s: SP PRO link deadline exceeded' % what)
            try:
                if self.__protocol is None:
                    self.__reconnect()
                return fn(self.__protocol)
            except Exception as e:  # connection loss, short read, CRC error, ...
                last = e
                self.__protocol = None
                logging.warning('%s failed (attempt %d/%d): %s', what, attempt, self.__retries, e)
                if self.__expired():
                    break
                time.sleep(min(2 * attempt, 5))
        raise last

    def close(self):
        """Release the link (the ESP proxy serves one client at a time)."""
        if self.__protocol is not None and hasattr(self.__protocol, 'close'):
            try:
                self.__protocol.close()
            except Exception:
                pass
        self.__protocol = None

    def read(self, address: int, words: int) -> bytes:
        from memory import Range
        out = bytearray()
        while words > 0:
            n = min(words, MAX_WORDS)
            out += self.__call('read 0x%x+%d' % (address, n), lambda p: bytes(p.query(Range(address, n))))
            address += n
            words -= n
        return bytes(out)

    def words(self, address: int, words: int, fmt: str = 'H') -> list:
        data = self.read(address, words)
        size = struct.calcsize(fmt)
        return [struct.unpack('<' + fmt, data[i:i + size])[0] for i in range(0, len(data), size)]

    def u32(self, address: int) -> int:
        return struct.unpack('<I', self.read(address, 2))[0]

    def raw(self, frame: bytes, reply_len: int) -> bytes:
        """One proxied frame, one attempt (never resend a client's write); a failure drops the link."""
        try:
            if self.__protocol is None:
                self.__protocol = self.__factory()
            return self.__protocol.raw(frame, reply_len)
        except Exception:
            self.close()
            raise

    def write(self, address: int, data: bytes):
        self.__call('write 0x%x' % address, lambda p: p.write(address, bytes(data)))
