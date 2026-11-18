from . import Connection
import settings
import socket
from exception import ConnectionLostException
import logging

class ConnectionTCP(Connection):
    def _connect(self):
        hostname = settings.getb(b'CONNECTION_TCP_HOSTNAME')
        port = int(settings.getb(b'CONNECTION_TCP_PORT'))
        self.__sock = socket.create_connection((hostname, port), timeout=10)
        # short reads: the SP PRO stays silent until logged in, and Protocol logs in after a few empty reads
        self.__sock.settimeout(float(settings.getb(b'CONNECTION_TCP_TIMEOUT') or 1.0))

    def _read(self, length: int):
        try:
            data = self.__sock.recv(length)
        except socket.timeout:
            return b''
        if not data:
            raise ConnectionLostException('TCP connection closed')
        return data

    def _write(self, data: bytes):
        attempts = 0
        while attempts < 3:
            try:
                return self.__sock.send(data)
            except BrokenPipeError:
                logging.debug("BrokenPipeError, retrying connection")
                self._connect()
                attempts = attempts + 1
        raise ConnectionLostException("Too many sequential write failures (%s)" % attempts)

    def _close(self):
        try:
            self.__sock.close()
        except OSError:
            pass
