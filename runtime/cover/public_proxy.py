"""Loopback HTTP proxy restricting downloader connections to public addresses."""

import ipaddress
import select
import socket
import socketserver
import threading
from urllib.parse import parse_qsl, urlsplit

MAX_BYTES = 256 * 1024 * 1024


def public_address(host: str, port: int) -> tuple:
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses:
        raise ValueError("Address does not resolve")
    for family, kind, proto, _, address in addresses:
        if not ipaddress.ip_address(address[0]).is_global:
            raise ValueError("Private or reserved address is not allowed")
    return addresses[0]


def validate_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Only public HTTP and HTTPS URLs are supported")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URLs containing credentials are not supported")
    credential_keys = {
        "token",
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "password",
        "signature",
        "sig",
    }
    if any(
        key.casefold() in credential_keys or key.casefold().startswith("x-amz-")
        for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
    ):
        raise ValueError("Authenticated or signed URLs are not supported")
    if any(c.isspace() for c in url):
        raise ValueError("URL contains whitespace")
    public_address(
        parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
    )


class PublicProxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), ProxyHandler)
        self.total_bytes = 0
        self.lock = threading.Lock()

    def count(self, size: int) -> bool:
        with self.lock:
            self.total_bytes += size
            return self.total_bytes <= MAX_BYTES


class ProxyHandler(socketserver.StreamRequestHandler):
    rbufsize = 0

    def handle(self):
        self.connection.settimeout(30)
        line = self.rfile.readline(16385)
        if not line or len(line) > 16384:
            return
        parts = line.decode("latin1").strip().split(" ")
        if len(parts) != 3:
            return
        method, target, version = parts
        headers = []
        while True:
            header = self.rfile.readline(16385)
            if not header or len(header) > 16384:
                return
            if header in (b"\r\n", b"\n"):
                break
            if not header.lower().startswith(
                (b"proxy-authorization:", b"proxy-connection:")
            ):
                headers.append(header)
        parsed = urlsplit(("http://" if method == "CONNECT" else "") + target)
        try:
            if (
                parsed.username is not None
                or parsed.password is not None
                or not parsed.hostname
            ):
                raise ValueError("Invalid target")
            address = public_address(
                parsed.hostname, parsed.port or (443 if method == "CONNECT" else 80)
            )
            remote = socket.socket(address[0], address[1], address[2])
            remote.settimeout(30)
            remote.connect(address[4])
        except (OSError, ValueError):
            self.wfile.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
            return
        with remote:
            if method == "CONNECT":
                self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            else:
                if parsed.scheme not in ("http", "https"):
                    return
                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query
                remote.sendall(
                    f"{method} {path} {version}\r\n".encode("latin1")
                    + b"".join(headers)
                    + b"Connection: close\r\n\r\n"
                )
            sockets = [self.connection, remote]
            while True:
                try:
                    readable, _, _ = select.select(sockets, [], [], 30)
                    if not readable:
                        return
                    for source in readable:
                        data = source.recv(65536)
                        if not data or not self.server.count(len(data)):
                            return
                        (
                            remote if source is self.connection else self.connection
                        ).sendall(data)
                except (OSError, ValueError):
                    return
