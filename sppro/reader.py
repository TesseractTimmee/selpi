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
    def __init__(self, protocol_factory=None, retries: int = 3, idle_reconnect: float = None,
                 max_words: int = MAX_WORDS):
        if protocol_factory is None:
            from memory import Protocol
            protocol_factory = Protocol
        self.__factory = protocol_factory
        self.__protocol = None
        self.__retries = retries
        # Largest single read. A local link happily serves the whole block in one request (MAX_WORDS). Over
        # Select.Live a big block (e.g. the Now screen's 85 words, or Technical's 154) is one slow cloud
        # round-trip that stalls easily; splitting it into smaller reads keeps each request sub-second, so a
        # stall costs one chunk instead of the whole screen. Set small only for Select.Live.
        self.__max_words = max_words if max_words and max_words > 0 else MAX_WORDS
        # monotonic time after which calls stop retrying and give up, so a single operation (e.g. a whole
        # configuration read of many blocks over a flaky Select.Live link) can't hold the link lock for minutes.
        self.deadline = None
        # Select.Live closes a connection after about a minute idle. When the poll interval is longer than that,
        # the first read of each poll lands on a socket Select.Live has already dropped: it returns nothing, and
        # the retries soft-reset a dead session (~6 s wasted) before the next read reconnects -- so the first
        # block of the poll (now the Now/power block, since scales are cached) is lost every cycle. When
        # idle_reconnect is set, pre-empt it: if the link has been idle this long, close the (dying) session
        # cleanly before the next call so it reconnects fresh. Only set for Select.Live; a local link
        # (serial/TCP/ESP) does not idle-close, so it stays None there. A fast poll (< this) never trips it.
        self.__idle_reconnect = idle_reconnect
        self.__last = None            # time.monotonic() of the last successful call

    def __expired(self):
        return self.deadline is not None and time.monotonic() > self.deadline

    def __call(self, what: str, fn):
        if self.__idle_reconnect is not None and self.__protocol is not None and self.__last is not None \
                and time.monotonic() - self.__last > self.__idle_reconnect:
            self.close()              # stale (Select.Live idle-closed) session: drop it so the call below reconnects
        last = None
        for attempt in range(1, self.__retries + 1):
            if self.__expired():
                raise TimeoutError('%s: SP PRO link deadline exceeded' % what)
            try:
                if self.__protocol is None:
                    self.__protocol = self.__factory()
                out = fn(self.__protocol)
                self.__last = time.monotonic()
                return out
            except Exception as e:  # connection loss, short read, CRC error, ...
                last = e
                # A stalled read (timeout / short read) usually means the Select.Live tunnel went briefly quiet,
                # not that it broke. Keep the session and retry on it (as SP LINK does) rather than reconnecting:
                # a reconnect opens a NEW Select.Live session while the old one lingers (~60 s), so the relay can
                # leave the device bound to the dying session and the stall feeds itself. Drop the link only on a
                # genuine error, or on the final attempt (so the next call starts fresh).
                transient = isinstance(e, (TimeoutError, BufferError))
                if transient and attempt < self.__retries and self.__protocol is not None \
                        and hasattr(self.__protocol, 'reset'):
                    try:
                        self.__protocol.reset()
                    except Exception:
                        self.__protocol = None
                else:
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
            n = min(words, self.__max_words)
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
