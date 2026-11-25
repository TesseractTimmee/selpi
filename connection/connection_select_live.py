from ssl import SSLZeroReturnError
from exception import ConnectionLostException, ValidationException
import settings
from . import Connection
from .ssl import create_ssl_connection
import re

# Named Select.Live servers (all port 7528, from SP LINK). CONNECTION_SELECT_LIVE_SERVER picks one; anything else
# (or unset) falls back to the manual CONNECTION_SELECT_LIVE_HOST / _PORT (default select.live).
SERVERS = {
    b'select.live': (b'select.live', 7528),
    b'select.live2': (b'splink-api.selectlive.selectronic.com.au', 7528),
    b'select.live2-nonprod': (b'splink-api.nonprod.selectlive.selectronic.com.au', 7528),
}


class ConnectionSelectLive(Connection):
    requires_sppro_login = False     # the LOGIN/USER/CONNECT handshake authenticates; no 0x1f0000 serial login

    def __init__(
            self,
            create_ssl_connection_function: create_ssl_connection=None,
            settings_module: settings=None
    ):
        super().__init__()
        self.__create_ssl_connection = create_ssl_connection_function or create_ssl_connection
        self.__settings_module = settings_module or settings
        self.__socket = None
        self.__write_failures = 0

    def _connect(self):
        # Server: a named preset (Select.Live / Select.Live 2 / nonprod) or a manual host/port. Same protocol.
        server = self.__settings_module.getb(b'CONNECTION_SELECT_LIVE_SERVER')
        if server in SERVERS:
            hostname, port = SERVERS[server]
        else:
            hostname = self.__settings_module.getb(b'CONNECTION_SELECT_LIVE_HOST') or b'select.live'
            port_b = self.__settings_module.getb(b'CONNECTION_SELECT_LIVE_PORT')
            port = int(port_b) if port_b else 7528
        self.__socket = self.__create_ssl_connection(hostname, port)

        response = self._read(1024)
        if response != b'LOGIN\r\n':
            raise ValidationException(
                "Authentication failed, 'LOGIN' prompt was missing, received %s instead"
                % response
            )
        username = self.__settings_module.getb(b'CONNECTION_SELECT_LIVE_USERNAME')
        password = self.__settings_module.getb(b'CONNECTION_SELECT_LIVE_PASSWORD')
        self._write(b'USER:'+username+b':'+password+b'\r\n')
        response = self._read(1024)
        if response != b'OK\r\n':
            raise ValidationException(
                "Authentication failed, username or password not accepted, received %s"
                % response
            )

        device = self.__settings_module.getb(b'CONNECTION_SELECT_LIVE_DEVICE')
        self._write(b'CONNECT:'+device+b'\r\n')
        response = self._read(1024)
        if response != b'READY\r\n':
            devices = self._get_devices()
            raise ValidationException(
                "Authentication failed.\n  Device '%s' not accepted\n  Response: '%s'\n  Available devices: %s"
                % (device.decode('utf-8'), response.decode('utf-8').strip(), devices)
            )

    def _close(self):
        if self.__socket is not None:
            try:
                self.__socket.close()
            except OSError:
                pass
            self.__socket = None

    def _get_devices(self):
        self._write(b'LIST DEVICES\r\n')
        match = re.search(r'^DEVICE:([0-9]+)\r\n', self._read(1024).decode('utf-8'), re.MULTILINE)
        if match:
            return ', '.join(match.groups())
        return ''

    def _read(self, length: int):
        return self.__socket.read(length)

    def _write(self, data: bytes):
        if self.__write_failures > 3:
            raise ConnectionLostException("Too many sequential write failures (%s)" % self.__write_failures)
        try:
            response = self.__socket.write(data)
        except SSLZeroReturnError:
            self.__write_failures = self.__write_failures + 1
            self._connect()
            return self._write(data)
        self.__write_failures = 0
        return response

