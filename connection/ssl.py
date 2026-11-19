import os
import socket
import ssl
from ssl import SSLSocket

# Select.Live answers in well under a second (login about 2.5 s). Without a timeout a lost reply blocks until
# Select.Live drops the connection about a minute later; with one the request fails fast and sppro.reader.Reader
# retries it on a fresh connection.
DEFAULT_TIMEOUT = 15.0


def create_ssl_connection(hostname, port: int, timeout: float = None) -> SSLSocket:
    if timeout is None:
        timeout = float(os.environ.get('SELPI_CONNECTION_SELECT_LIVE_TIMEOUT') or DEFAULT_TIMEOUT)
    host = hostname.decode() if isinstance(hostname, bytes) else hostname
    context = ssl.create_default_context()
    tcp_socket = socket.create_connection((host, port), timeout=timeout)
    return context.wrap_socket(tcp_socket, server_hostname=host)
