class Connection:
    # Whether the SP PRO expects the 0x1f0000 MD5 serial login before it answers. True for a direct serial / TCP
    # link; False for Select.Live, where the LOGIN/USER/CONNECT handshake authenticates and the tunnel then carries
    # reads with no serial login (SP LINK never sends one over Select.Live). Gating the login on this stops a brief
    # Select.Live stall (a silent read) from being mistaken for a login prompt and firing extra reads/writes at an
    # already-struggling link.
    requires_sppro_login = True

    def __init__(self):
        self.__connected = False

    def connect(self):
        if self.__connected:
            return
        self._connect()
        self.__connected = True

    def _connect(self):
        raise NotImplementedError

    def read(self, length: int):
        self.connect()
        return self._read(length)

    def _read(self, length: int):
        raise NotImplementedError

    def write(self, data: bytes):
        self.connect()
        return self._write(data)

    def _write(self, data: bytes):
        raise NotImplementedError

    def close(self):
        """Drop the link (reconnects on next use)."""
        if self.__connected:
            self._close()
            self.__connected = False

    def _close(self):
        pass


