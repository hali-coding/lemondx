"""RFC 6455 WebSockets, both halves, on the standard library.

An interactive terminal needs both: the daemon offers interactive exec and
console attachment only over a WebSocket, and the browser speaks nothing else.
server.py bridges one to the other.

Only what that bridge needs is here -- text and binary messages, the control
frames the protocol requires (ping, pong, close), and fragment reassembly. No
extensions and no compression, so no negotiation to get wrong.

A connection reads through a file object and writes through a callable, rather
than owning a socket, because the two ends arrive differently: the browser's
socket is already wrapped in the request handler's buffered reader (bytes may
be sitting in that buffer, so the raw socket must not be read behind its back),
while the daemon's is a socket this module connects itself.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
import threading

_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

CONTINUATION, TEXT, BINARY, CLOSE, PING, PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
_CONTROL = (CLOSE, PING, PONG)

# A terminal never legitimately sends more in one message; a paste is far less.
MAX_MESSAGE = 8 * 1024 * 1024

NORMAL_CLOSURE = 1000
GOING_AWAY = 1001


class WebSocketError(Exception):
    """The connection failed, or the peer broke the protocol."""


def accept_key(key):
    """The ``Sec-WebSocket-Accept`` value answering a client's key."""
    digest = hashlib.sha1(key.encode("ascii", "ignore") + _GUID).digest()
    return base64.b64encode(digest).decode("ascii")


def is_upgrade(headers):
    """Is this request asking to become a WebSocket?"""
    return ("websocket" in (headers.get("Upgrade") or "").lower()
            and "upgrade" in (headers.get("Connection") or "").lower())


def _apply_mask(payload, key):
    """XOR a payload with a 4-byte key, whole-buffer at a time.

    A per-byte loop is visible on the 64 KiB frames a busy terminal produces.
    """
    if not payload:
        return payload
    repeated = (key * (len(payload) // 4 + 1))[:len(payload)]
    masked = int.from_bytes(payload, "big") ^ int.from_bytes(repeated, "big")
    return masked.to_bytes(len(payload), "big")


def shutdown_closer(sock):
    """A closer that actually frees a reader blocked on this socket.

    Closing a socket does not wake a thread already parked in ``recv`` on it;
    shutting it down does. Both ends of the bridge depend on that, so the pair
    lives here rather than in each caller.
    """
    def close():
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass                    # already gone, or never connected
        try:
            sock.close()
        except OSError:
            pass
    return close


class WebSocket:
    """One connection.

    Reads are single-threaded by construction -- each direction of the bridge
    has one reader. Writes take a lock, because a reader must be able to answer
    a ping or a close while the other direction is still sending.
    """

    def __init__(self, reader, sendall, mask=False, closer=None):
        self._reader = reader
        self._sendall = sendall
        self._mask = mask                 # clients must mask, servers must not
        self._closer = closer
        self._send_lock = threading.Lock()
        self.closed = False

    # -- reading -----------------------------------------------------------

    def _read(self, count):
        if not count:
            return b""
        try:
            data = self._reader.read(count)
        except (OSError, ValueError) as exc:
            # ValueError: the reader was closed under us to unblock this read.
            raise WebSocketError("read failed: %s" % exc) from exc
        if not data or len(data) < count:
            raise WebSocketError("connection closed mid-frame")
        return data

    def _read_frame(self):
        head = self._read(2)
        fin = bool(head[0] & 0x80)
        opcode = head[0] & 0x0F
        masked = bool(head[1] & 0x80)
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read(8))[0]
        if length > MAX_MESSAGE:
            raise WebSocketError("frame of %d bytes is too large" % length)
        key = self._read(4) if masked else b""
        payload = self._read(length)
        return fin, opcode, _apply_mask(payload, key) if masked else payload

    def recv(self):
        """The next message as ``(opcode, payload)``, or None once closed.

        Pings are answered and fragments reassembled here, so callers see only
        whole text and binary messages.
        """
        opcode = None
        chunks = []
        total = 0
        while True:
            fin, frame_opcode, payload = self._read_frame()

            if frame_opcode in _CONTROL:
                if frame_opcode == CLOSE:
                    self._send_frame(CLOSE, payload[:2])      # echo the code back
                    self.closed = True
                    return None
                if frame_opcode == PING:
                    self._send_frame(PONG, payload)
                continue                                      # a pong needs no reply

            if frame_opcode != CONTINUATION:
                opcode = frame_opcode
            chunks.append(payload)
            total += len(payload)
            if total > MAX_MESSAGE:
                raise WebSocketError("message of over %d bytes" % MAX_MESSAGE)
            if fin:
                return opcode if opcode is not None else BINARY, b"".join(chunks)

    # -- writing -----------------------------------------------------------

    def _send_frame(self, opcode, payload):
        length = len(payload)
        header = bytearray([0x80 | opcode])
        flag = 0x80 if self._mask else 0
        if length < 126:
            header.append(flag | length)
        elif length < 65536:
            header.append(flag | 126)
            header += struct.pack("!H", length)
        else:
            header.append(flag | 127)
            header += struct.pack("!Q", length)
        if self._mask:
            key = os.urandom(4)
            header += key
            payload = _apply_mask(payload, key)

        with self._send_lock:
            if self.closed and opcode != CLOSE:
                return
            try:
                self._sendall(bytes(header) + payload)
            except OSError as exc:
                self.closed = True
                raise WebSocketError("write failed: %s" % exc) from exc

    def send(self, payload):
        self._send_frame(BINARY, payload)

    def send_text(self, text):
        self._send_frame(TEXT, text.encode("utf-8"))

    def close(self, code=NORMAL_CLOSURE):
        """Say goodbye if we still can, then break the connection.

        Breaking it is the point: it is what unblocks a reader parked in
        ``recv()`` on the other thread.
        """
        if not self.closed:
            self.closed = True
            try:
                self._send_frame(CLOSE, struct.pack("!H", code))
            except WebSocketError:
                pass
        if self._closer:
            try:
                self._closer()
            except OSError:
                pass


def server_handshake(headers, write):
    """Answer an upgrade request with the 101, taking over the connection."""
    key = headers.get("Sec-WebSocket-Key")
    if not key:
        raise WebSocketError("upgrade request carried no Sec-WebSocket-Key")
    version = (headers.get("Sec-WebSocket-Version") or "").strip()
    if version and version != "13":
        raise WebSocketError("unsupported WebSocket version %s" % version)
    write(("HTTP/1.1 101 Switching Protocols\r\n"
           "Upgrade: websocket\r\n"
           "Connection: Upgrade\r\n"
           "Sec-WebSocket-Accept: %s\r\n"
           "\r\n" % accept_key(key)).encode("ascii"))


def connect_unix(socket_path, path, timeout=15):
    """Open a WebSocket to ``path`` on a daemon listening on a unix socket.

    The handshake is read through the same buffered reader the frames will use,
    so no reply bytes can be stranded between the two.
    """
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        "GET %s HTTP/1.1\r\n"
        "Host: localhost\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: %s\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n" % (path, key)
    )

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    reader = None
    try:
        sock.connect(socket_path)
        sock.sendall(request.encode("ascii"))
        reader = sock.makefile("rb")

        status = reader.readline(8192).decode("latin-1").strip()
        if " 101" not in status:
            raise WebSocketError("upgrade refused: %s" % (status or "no response"))
        accepted = ""
        while True:
            line = reader.readline(8192).decode("latin-1")
            if line in ("\r\n", "\n", ""):
                break
            name, _, value = line.partition(":")
            if name.strip().lower() == "sec-websocket-accept":
                accepted = value.strip()
        if accepted != accept_key(key):
            raise WebSocketError("upgrade answered with a bad Sec-WebSocket-Accept")
    except OSError as exc:
        sock.close()
        raise WebSocketError("cannot open %s: %s" % (socket_path, exc)) from exc
    except WebSocketError:
        sock.close()
        raise

    # A session lasts as long as someone is typing; nothing may time out now.
    sock.settimeout(None)
    return WebSocket(reader, sock.sendall, mask=True, closer=shutdown_closer(sock))
