"""boardctl -- a single MCP server that lets an AI agent drive Linux dev boards
over serial (UART/USB-serial), SSH and telnet with one consistent session API.

Model for the agent:
    serial_list()                        find COM ports
    connect(type=...)                    open a stateful session -> session_id
    expect(session_id, patterns)         wait until a regex shows up (login:, #, ...)
    send(session_id, "cmd")              send a line and collect the reply
    read(session_id)                     collect async output (boot logs, etc.)
    control_lines(...)                   toggle DTR/RTS (board reset / bootloader)
    ssh_exec / sftp_upload / sftp_download   one-shot SSH helpers
    sessions() / close(session_id)       bookkeeping

Sessions keep a ring buffer (1 MB) of raw bytes; expect() scans output since its
last successful match, read()/send() consume a separate cursor, so boot spam read
earlier is never lost for expect().
"""

from __future__ import annotations

import atexit
import logging
import os
import queue
import re
import socket
import threading
import time
import uuid
from logging.handlers import RotatingFileHandler
from typing import Literal, Optional

import paramiko
import serial
import serial.tools.list_ports
from cryptography.hazmat.primitives import hashes
from mcp.server.fastmcp import FastMCP
from paramiko.ssh_exception import IncompatiblePeer

# --------------------------------------------------------------------------- logging
_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "boardctl.log")
_log = logging.getLogger("boardctl")
_log.setLevel(logging.DEBUG)
if not _log.handlers:
    _h = RotatingFileHandler(_LOG_PATH, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _log.addHandler(_h)

# --------------------------------------------------------------------------- constants
MAX_BUFFER = 1_000_000  # per-session ring buffer ceiling (bytes)
TRIM_TO = 600_000       # keep this many bytes after a trim
MAX_SESSIONS = 16


def _guide_path() -> str:
    """Absolute path of AI_GUIDE.md, for repo (src/../) and flat layouts alike."""
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.join(os.path.dirname(here), "AI_GUIDE.md")
    return repo if os.path.isfile(repo) else os.path.join(here, "AI_GUIDE.md")


_AI_GUIDE = _guide_path()

mcp = FastMCP(
    "boardctl",
    instructions=(
        "Board console control (serial/SSH/telnet) for AI agents. Sessions are stateful; "
        "output is a raw byte stream -- wait for markers with expect(), never assume "
        "request-response.\n"
        "Core rules:\n"
        "- Full operating guide, read it before first use: " + _AI_GUIDE + "\n"
        "- SHARE PROACTIVELY: right after connect(), offer share(session_id) and tell the "
        "user they can open the very same console in their own terminal app "
        "(WindTerm/Xshell/MobaXterm/PuTTY, Telnet 127.0.0.1:<listen_port>). "
        "Users usually don't know this feature exists.\n"
        "- SERIAL PORTS ARE EXCLUSIVE: one COM port = one session. Check sessions() before "
        "connect(), close stale sessions first; 'access denied' on a port means someone "
        "still holds it (old session, share_console.py, or the user's terminal app).\n"
        "- Long commands: send(..., wait=0) then expect([\"MARK-0\", \"ERROR\"]); put $? in "
        "the marker (echo \"MARK-$?\") so it can never match the command echo.\n"
        "- SSH long tasks: keepalive=30. Boards offering only ssh-rsa (old Dropbear) are "
        "handled automatically.\n"
        "- control_lines() hard-resets boards -- only on purpose. close(session_id) when done."
    ),
)

SESSIONS: dict[str, "Session"] = {}
SESSIONS_LOCK = threading.Lock()


def _new_sid() -> str:
    return uuid.uuid4().hex[:10]


# --------------------------------------------------------------------------- session base
class Session:
    """A stateful console session with a byte ring buffer and regex expect."""

    kind = "base"
    default_newline = "lf"

    def __init__(self, desc: str, encoding: str = "utf-8"):
        self.id = _new_sid()
        self.desc = desc
        self.encoding = encoding
        self._buf = bytearray()
        self._read_off = 0    # cursor consumed by read()/send()
        self._expect_off = 0  # cursor scanned by expect()
        self._taps: dict[str, object] = {}  # live mirrors, see add_tap()
        self._wlock = threading.Lock()      # serializes transport writes
        self._cond = threading.Condition()
        self.alive = True
        self.error: Optional[str] = None
        self.created = time.time()
        self.last_rx = time.time()
        self._thread: Optional[threading.Thread] = None

    # -- reader thread hooks -------------------------------------------------
    def _start(self) -> None:
        self._thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._thread.start()

    def _reader_loop(self) -> None:  # pragma: no cover - overridden
        self._die("no transport")

    def _send_raw(self, payload: bytes) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def _close_io(self) -> None:  # pragma: no cover - overridden
        pass

    # -- buffer plumbing -----------------------------------------------------
    def _feed(self, data: bytes) -> None:
        if not data:
            return
        with self._cond:
            self._buf.extend(data)
            self.last_rx = time.time()
            if len(self._buf) > MAX_BUFFER:
                cut = len(self._buf) - TRIM_TO
                del self._buf[:cut]
                self._read_off = max(0, self._read_off - cut)
                self._expect_off = max(0, self._expect_off - cut)
                _log.warning("session %s: buffer trimmed (%d bytes dropped)", self.id, cut)
            self._cond.notify_all()
            taps = list(self._taps.values())
        for cb in taps:
            try:
                cb(data)  # mirrors must not block the reader thread
            except Exception:
                pass

    def add_tap(self, cb) -> str:
        """Register a callback(data: bytes) that mirrors board output live."""
        tid = uuid.uuid4().hex[:8]
        with self._cond:
            self._taps[tid] = cb
        return tid

    def remove_tap(self, tid: str) -> None:
        with self._cond:
            self._taps.pop(tid, None)

    def tail(self, n: int = 4000) -> bytes:
        """Last n bytes of raw output (history replay for late joiners)."""
        with self._cond:
            return bytes(self._buf[-n:])

    def _die(self, err: Optional[str] = None) -> None:
        with self._cond:
            self.alive = False
            if err and not self.error:
                self.error = err
            self._cond.notify_all()

    def _decode(self, b: bytes) -> str:
        return b.decode(self.encoding, errors="replace")

    @staticmethod
    def _cap(text: str, max_output: int) -> dict:
        """Keep the tail of long output so agent context does not blow up."""
        if max_output and len(text) > max_output:
            return {
                "output": text[-max_output:],
                "truncated": True,
                "dropped_chars": len(text) - max_output,
            }
        return {"output": text, "truncated": False, "dropped_chars": 0}

    # -- public API ----------------------------------------------------------
    def read(self, timeout: float = 2.0, settle: float = 0.2) -> bytes:
        """Return output since the last read.

        Waits up to `timeout` for the first bytes, then keeps absorbing chunks
        while they keep arriving (quiet gap of `settle` s or deadline reached).
        """
        with self._cond:
            deadline = time.monotonic() + max(0.0, timeout)
            while self._read_off >= len(self._buf) and self.alive:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return b""
                self._cond.wait(remaining)
            while self.alive:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                cur = len(self._buf)
                got_more = self._cond.wait_for(
                    lambda: len(self._buf) > cur, timeout=min(settle, remaining))
                if not got_more:
                    break
            data = bytes(self._buf[self._read_off:])
            self._read_off = len(self._buf)
            return data

    def expect(self, patterns: list[str], timeout: float = 10.0, max_output: int = 4000) -> dict:
        """Wait until one of the regexes appears; consume output up to the match."""
        rxs = [re.compile(p.encode(self.encoding), re.DOTALL) for p in patterns]
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cond:
            while True:
                chunk = bytes(self._buf[self._expect_off:])
                for i, rx in enumerate(rxs):
                    m = rx.search(chunk)
                    if m:
                        start = self._expect_off
                        end = start + m.end()
                        self._expect_off = end
                        self._read_off = max(self._read_off, end)
                        out = Session._cap(self._decode(bytes(self._buf[start:end])), max_output)
                        return {"matched": True, "pattern_index": i, "pattern": patterns[i],
                                **out, "alive": self.alive}
                if not self.alive:
                    out = Session._cap(self._decode(chunk), max_output)
                    return {"matched": False, "pattern": None, **out, "alive": False,
                            "error": self.error or "session closed"}
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    out = Session._cap(self._decode(chunk), max_output)
                    return {"matched": False, "pattern": None, **out, "alive": True, "timeout": True}
                self._cond.wait(remaining)

    def send(self, data: str, newline: str = "auto") -> None:
        if newline == "auto":
            newline = self.default_newline
        payload = data.encode(self.encoding, errors="replace")
        if newline == "lf":
            payload += b"\n"
        elif newline == "crlf":
            payload += b"\r\n"
        elif newline == "cr":
            payload += b"\r"
        elif newline != "none":
            raise ValueError(f"newline must be auto/lf/crlf/cr/none, got {newline!r}")
        self._send_raw(payload)

    def status(self) -> dict:
        with self._cond:
            return {
                "session_id": self.id,
                "type": self.kind,
                "desc": self.desc,
                "alive": self.alive,
                "error": self.error,
                "buffered_bytes": len(self._buf) - self._read_off,
                "created_ago_s": round(time.time() - self.created, 1),
                "idle_s": round(time.time() - self.last_rx, 1),
            }

    def close(self) -> None:
        try:
            self._close_io()
        except Exception as e:  # best effort
            _log.warning("session %s: close error %s", self.id, e)
        finally:
            self._die("closed by user")
        _log.info("session %s closed", self.id)


# --------------------------------------------------------------------------- serial
_PARITY = {"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN, "O": serial.PARITY_ODD,
           "M": serial.PARITY_MARK, "S": serial.PARITY_SPACE}
_BYTESIZE = {5: serial.FIVEBITS, 6: serial.SIXBITS, 7: serial.SEVENBITS, 8: serial.EIGHTBITS}
_STOPBITS = {1: serial.STOPBITS_ONE, 1.5: serial.STOPBITS_ONE_POINT_FIVE, 2: serial.STOPBITS_TWO}
_FLOW = {"none": {}, "rtscts": {"rtscts": True}, "dsrdtr": {"dsrdtr": True}, "xonxoff": {"xonxoff": True}}


class SerialSession(Session):
    kind = "serial"
    default_newline = "lf"

    def __init__(self, serial_port: str, baudrate: int, bytesize: int, parity: str,
                 stopbits: float, flow: str, dtr: Optional[bool], rts: Optional[bool],
                 encoding: str):
        super().__init__(desc=f"{serial_port} @ {baudrate}", encoding=encoding)
        kwargs = dict(baudrate=baudrate, bytesize=_BYTESIZE[bytesize],
                      parity=_PARITY[parity], stopbits=_STOPBITS[stopbits],
                      timeout=0.1, write_timeout=3.0, exclusive=True, **_FLOW[flow])
        self.ser = serial.serial_for_url(serial_port, **kwargs)
        try:
            if dtr is not None:
                self.ser.dtr = dtr
            if rts is not None:
                self.ser.rts = rts
        except Exception:
            self.ser.close()  # don't orphan a half-open port
            raise

    def _reader_loop(self) -> None:
        try:
            while self.alive and self.ser.is_open:
                try:
                    # read(in_waiting or 1): return the first byte the moment it
                    # arrives, then drain the rest immediately. read(4096) would
                    # hold interactive echo for the full 0.1 s timeout instead.
                    data = self.ser.read(self.ser.in_waiting or 1)
                except (serial.SerialException, OSError) as e:
                    self._die(f"serial read error: {e}")
                    return
                if data:
                    self._feed(data)
        finally:
            self._die()

    def _send_raw(self, payload: bytes) -> None:
        with self._wlock:  # bridge keystrokes and agent sends share one port
            # no flush(): pyserial's flush() ignores write_timeout and can spin
            # forever on a wedged adapter, deadlocking every sender on _wlock
            self.ser.write(payload)

    def _close_io(self) -> None:
        self.ser.close()


# --------------------------------------------------------------------------- ssh
class _LegacyRSAKey(paramiko.RSAKey):
    """RSA host key that also accepts ssh-rsa (RSA/SHA-1) signatures.

    paramiko >=5 removed ssh-rsa outright; boards of the 2014-2017 Yocto era
    (old Dropbear/OpenSSH) often offer nothing else. Never enabled globally --
    _legacy_transport_factory scopes it to a single Transport instance.
    """

    HASHES = {**paramiko.RSAKey.HASHES, "ssh-rsa": hashes.SHA1}


def _legacy_transport_factory():
    """Transport factory re-enabling ssh-rsa host keys on that instance only."""
    def factory(sock, disabled_algorithms=None):
        t = paramiko.Transport(sock, disabled_algorithms=disabled_algorithms)
        t._key_info = {**paramiko.Transport._key_info, "ssh-rsa": _LegacyRSAKey}
        opts = t.get_security_options()
        opts.key_types = list(opts.key_types) + ["ssh-rsa"]
        return t
    return factory


class SSHSession(Session):
    kind = "ssh"
    default_newline = "lf"

    def __init__(self, host: str, port: int, username: str, password: Optional[str],
                 key_path: Optional[str], key_passphrase: Optional[str],
                 connect_timeout: float, encoding: str,
                 legacy_algos: bool = False, keepalive: float = 0):
        super().__init__(desc=f"ssh://{username}@{host}:{port}", encoding=encoding)
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            self.client.connect(host, port=port, username=username, password=password,
                                key_filename=key_path, passphrase=key_passphrase,
                                timeout=connect_timeout, banner_timeout=30, auth_timeout=30,
                                allow_agent=False, look_for_keys=False,
                                transport_factory=_legacy_transport_factory() if legacy_algos else None)
        except Exception:
            self.client.close()  # connect() leaves its half-open Transport behind on failure
            raise
        try:
            if keepalive and keepalive > 0:
                self.client.get_transport().set_keepalive(keepalive)
            self.chan = self.client.invoke_shell(term="vt100", width=512, height=200)
            self.chan.settimeout(0.5)
        except Exception:
            self.client.close()  # don't orphan a half-open SSH session
            raise

    def _reader_loop(self) -> None:
        try:
            while self.alive:
                try:
                    data = self.chan.recv(8192)
                except socket.timeout:
                    continue
                except OSError as e:
                    self._die(f"ssh channel error: {e}")
                    return
                if not data:
                    self._die("connection closed by remote")
                    return
                self._feed(data)
        finally:
            self._die()

    def _send_raw(self, payload: bytes) -> None:
        with self._wlock:
            while payload and self.alive:
                n = self.chan.send(payload)
                if n <= 0:
                    self._die("ssh send failed")
                    break
                payload = payload[n:]
            if payload:
                # dead session: surface it (serial/telnet raise in this spot too)
                self._die(self.error or "ssh session closed")
                raise OSError(f"ssh send failed: {self.error or 'session closed'}")

    def _close_io(self) -> None:
        try:
            self.chan.close()
        except Exception:
            pass
        self.client.close()


# --------------------------------------------------------------------------- telnet
_TN_IAC, _TN_DONT, _TN_DO, _TN_WONT, _TN_WILL, _TN_SB, _TN_SE = 255, 254, 253, 252, 251, 250, 240
_TN_ECHO, _TN_SGA, _TN_TTYPE = 1, 3, 24


class _TelnetCodec:
    """Minimal telnet server-side negotiation shared by TelnetSession and the
    console bridge: strips IAC sequences from peer input, answers WILL ECHO/SGA,
    refuses everything else, replies to TTYPE requests with "vt100".

    accept_do: options to accept when the peer sends DO (replying WILL instead
    of WONT). The bridge accepts ECHO/SGA so terminals keep local echo off --
    the board already echoes; TelnetSession keeps the historical WONT behavior.
    """

    def __init__(self, send: object, accept_do: frozenset = frozenset()):
        # send(bytes) pushes negotiation replies back to the peer
        self._send = send
        self._accept_do = accept_do
        self.state = 0  # 0 data / 1 got IAC / 2 got cmd / 3 in SB / 4 SB got IAC
        self.cmd = 0
        self.sb = bytearray()

    def _respond_option(self, cmd: int, opt: int) -> None:
        if cmd == _TN_WILL:
            reply = _TN_DO if opt in (_TN_ECHO, _TN_SGA) else _TN_DONT
        elif cmd == _TN_DO:
            reply = _TN_WILL if opt in self._accept_do else _TN_WONT
        elif cmd == _TN_DONT:
            reply = _TN_WONT
        else:  # WONT
            reply = _TN_DONT
        try:
            self._send(bytes([_TN_IAC, reply, opt]))
        except OSError:
            pass

    def _handle_sub(self, sb: bytearray) -> None:
        # terminal-type negotiation: IAC SB TTYPE SEND IAC SE -> reply IS "vt100"
        if len(sb) >= 2 and sb[0] == _TN_TTYPE and sb[1] == 1:
            try:
                self._send(bytes([_TN_IAC, _TN_SB, _TN_TTYPE, 0]) + b"vt100" +
                           bytes([_TN_IAC, _TN_SE]))
            except OSError:  # peer vanished mid-negotiation
                pass

    def feed(self, data: bytes) -> bytes:
        out = bytearray()
        st, cmd, sb = self.state, self.cmd, self.sb
        for b in data:
            if st == 0:
                if b == _TN_IAC:
                    st = 1
                else:
                    out.append(b)
            elif st == 1:
                if b == _TN_IAC:          # escaped 0xFF literal
                    out.append(_TN_IAC)
                    st = 0
                elif b in (_TN_WILL, _TN_WONT, _TN_DO, _TN_DONT):
                    cmd = b
                    st = 2
                elif b == _TN_SB:
                    sb.clear()
                    st = 3
                else:                     # NOP/AYT/BRK... ignore
                    st = 0
            elif st == 2:
                self._respond_option(cmd, b)
                st = 0
            elif st == 3:
                if b == _TN_IAC:
                    st = 4
                elif len(sb) <= 65_536:   # bound junk SB payloads
                    sb.append(b)
            elif st == 4:
                if b == _TN_SE:
                    self._handle_sub(sb)
                    st = 0
                elif b == _TN_IAC:
                    sb.append(_TN_IAC)    # IAC IAC = escaped 0xff, stay in SB
                    st = 3
                else:
                    if len(sb) <= 65_536:
                        sb.append(_TN_IAC)
                        sb.append(b)
                    st = 3
        self.state, self.cmd = st, cmd
        return bytes(out)


class TelnetSession(Session):
    kind = "telnet"
    default_newline = "crlf"

    def __init__(self, host: str, port: int, connect_timeout: float, encoding: str):
        super().__init__(desc=f"telnet://{host}:{port}", encoding=encoding)
        self.sock = socket.create_connection((host, port), timeout=connect_timeout)
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.sock.settimeout(0.5)
        except OSError:
            self.sock.close()
            raise
        self._codec = _TelnetCodec(self._send_reply)

    def _send_reply(self, b: bytes) -> None:
        with self._wlock:
            self.sock.sendall(b)

    def _telnet_feed(self, data: bytes) -> None:
        out = self._codec.feed(data)
        if out:
            self._feed(out)

    def _reader_loop(self) -> None:
        try:
            while self.alive:
                try:
                    data = self.sock.recv(4096)
                except socket.timeout:
                    continue
                except OSError as e:
                    self._die(f"telnet socket error: {e}")
                    return
                if not data:
                    self._die("connection closed by remote")
                    return
                self._telnet_feed(data)
        finally:
            self._die()

    def _send_raw(self, payload: bytes) -> None:
        with self._wlock:
            self.sock.sendall(payload.replace(b"\xff", b"\xff\xff"))

    def _close_io(self) -> None:
        self.sock.close()


# --------------------------------------------------------------------------- console bridge
BRIDGES: dict[str, "ConsoleBridge"] = {}
BRIDGE_LOCK = threading.Lock()


class ConsoleBridge:
    """Expose one session on 127.0.0.1:<port> as a mini-telnet server, so a
    human can watch and type in any terminal app (Xshell / MobaXterm / WindTerm
    / PuTTY) while the agent drives the same console through MCP.

    Board output is mirrored to all clients; client input goes to the board;
    late joiners receive the last few KB of history. Bound to localhost only.
    """

    MAX_CLIENTS = 4
    HISTORY = 4000

    def __init__(self, session: Session, port: int = 0):
        self.sess = session
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.clients: dict[tuple, dict] = {}
        self.srv = socket.socket()
        try:
            self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.srv.bind(("127.0.0.1", port))
            self.srv.listen(self.MAX_CLIENTS)
            self.srv.settimeout(0.5)
        except Exception:
            self.srv.close()  # don't leak the socket on bind/listen failure
            raise
        self.port = self.srv.getsockname()[1]
        self.tap_id = session.add_tap(self._on_board_data)
        threading.Thread(target=self._accept_loop, daemon=True).start()

    # board -> clients
    def _on_board_data(self, data: bytes) -> None:
        if b"\xff" in data:
            data = data.replace(b"\xff", b"\xff\xff")  # telnet-escape
        with self.lock:
            clients = list(self.clients.values())
        for c in clients:
            try:
                c["q"].put_nowait(data)
            except queue.Full:  # slow terminal: drop oldest, keep it live
                try:
                    c["q"].get_nowait()
                    c["q"].put_nowait(data)
                except (queue.Empty, queue.Full):
                    pass

    def _accept_loop(self) -> None:
        while not self.stop.is_set() and self.sess.alive:
            try:
                conn, addr = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self.lock:
                if len(self.clients) >= self.MAX_CLIENTS:
                    try:
                        conn.close()
                    except OSError:
                        pass
                    continue
                entry = {"sock": conn, "q": queue.Queue(maxsize=1024),
                         "wlock": threading.Lock()}
                self.clients[addr] = entry
            conn.settimeout(0.5)
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            try:  # reap half-open clients instead of holding slots forever
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                conn.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 15_000, 3_000))  # Windows
            except (AttributeError, OSError):
                try:
                    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 15)
                    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 3)
                except OSError:
                    pass
            # proactively move the terminal into remote-echo, char-at-a-time
            # mode (WILL ECHO, WILL SGA) so keystrokes go out instantly and the
            # board's echo is shown instead of client-side local echo
            entry["q"].put(bytes([_TN_IAC, _TN_WILL, _TN_ECHO, _TN_IAC, _TN_WILL, _TN_SGA]))
            history = self.sess.tail(self.HISTORY)
            if history:
                entry["q"].put(history.replace(b"\xff", b"\xff\xff"))
            threading.Thread(target=self._client_writer, args=(addr, entry), daemon=True).start()
            threading.Thread(target=self._client_reader, args=(addr, entry), daemon=True).start()
        self.close()

    def _client_writer(self, addr: tuple, entry: dict) -> None:
        q = entry["q"]
        while not self.stop.is_set():
            try:
                data = q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                with entry["wlock"]:
                    entry["sock"].sendall(data)
            except socket.timeout:  # peer stopped reading: drop it, free the slot
                self._drop_client(addr)
                return
            except OSError:
                return

    def _client_reader(self, addr: tuple, entry: dict) -> None:
        sock = entry["sock"]
        codec = _TelnetCodec(self._make_reply(entry), accept_do=frozenset({_TN_ECHO, _TN_SGA}))
        pend = b""  # trailing CR/NUL may pair with the next chunk
        while not self.stop.is_set():
            try:
                data = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            if pend:
                data = pend + data
                pend = b""
            if data.endswith(b"\r") or data.endswith(b"\x00"):
                pend = data[-1:]
                data = data[:-1]
            out = codec.feed(data) if data else b""
            if not self._forward(out):
                break
        if pend and not self.stop.is_set():  # flush the held-back byte
            self._forward(codec.feed(pend))
        self._drop_client(addr)

    def _make_reply(self, entry: dict):
        def reply(b: bytes) -> None:
            with entry["wlock"]:
                entry["sock"].sendall(b)
        return reply

    def _forward(self, out: bytes) -> bool:
        if not out:
            return True
        # terminals send CR LF / CR NUL for Enter; boards want CR (or LF)
        out = out.replace(b"\r\n", b"\r").replace(b"\r\x00", b"\r").replace(b"\n", b"\r")
        try:
            self.sess._send_raw(out)
            return True
        except Exception:
            return False

    def _drop_client(self, addr: tuple) -> None:
        with self.lock:
            entry = self.clients.pop(addr, None)
        if entry:
            try:
                entry["sock"].close()
            except OSError:
                pass

    def close(self) -> None:
        self.stop.set()
        try:
            self.srv.close()
        except OSError:
            pass
        with self.lock:
            entries = list(self.clients.values())
            self.clients.clear()
        for e in entries:
            try:
                e["sock"].close()
            except OSError:
                pass
        try:
            self.sess.remove_tap(self.tap_id)
        except Exception:
            pass
        with BRIDGE_LOCK:   # self-deregister (e.g. accept loop ended on session death)
            if BRIDGES.get(self.sess.id) is self:
                BRIDGES.pop(self.sess.id, None)


atexit.register(lambda: [br.close() for br in list(BRIDGES.values())])


# --------------------------------------------------------------------------- helpers
def _get(sid: str) -> Session:
    s = SESSIONS.get(sid)
    if s is None:
        raise LookupError(f"unknown session_id {sid!r}; call sessions() to list open sessions")
    return s


def _register(s: Session) -> None:
    evict: list[Session] = []
    try:
        with SESSIONS_LOCK:
            if sum(1 for x in SESSIONS.values() if x.alive) >= MAX_SESSIONS:
                # under pressure, reclaim registry slots held by dead sessions --
                # close() also releases the COM ports / sockets they still hold
                evict = [x for x in SESSIONS.values() if not x.alive]
                for x in evict:
                    SESSIONS.pop(x.id, None)
            if sum(1 for x in SESSIONS.values() if x.alive) >= MAX_SESSIONS:
                raise RuntimeError(
                    f"too many open sessions (max {MAX_SESSIONS}); close some first")
            SESSIONS[s.id] = s
    finally:
        # close() may join threads (paramiko stop_thread); keep it out of the lock
        for x in evict:
            try:
                x.close()
            except Exception:
                pass


def _ssh_client(host: str, port: int, username: str, password: Optional[str],
                key_path: Optional[str], key_passphrase: Optional[str],
                connect_timeout: float, legacy_algos: bool = False) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(host, port=port, username=username, password=password,
                       key_filename=key_path, passphrase=key_passphrase,
                       timeout=connect_timeout, banner_timeout=30, auth_timeout=30,
                       allow_agent=False, look_for_keys=False,
                       transport_factory=_legacy_transport_factory() if legacy_algos else None)
    except Exception:
        client.close()  # connect() leaves its half-open Transport behind on failure
        raise
    return client


def _connect_client(host: str, port: int, username: str, password: Optional[str],
                    key_path: Optional[str], key_passphrase: Optional[str],
                    connect_timeout: float, legacy_algos: bool):
    """Open an SSHClient, auto-retrying once with the ssh-rsa shim if the
    server only offers pre-SHA-2 host keys (old Dropbear/OpenSSH boards).

    Returns (client, legacy_used).
    """
    try:
        return _ssh_client(host, port, username, password, key_path,
                           key_passphrase, connect_timeout, legacy_algos), legacy_algos
    except IncompatiblePeer:
        if legacy_algos:
            raise
        _log.info("ssh %s: server only offers legacy host keys; retrying with ssh-rsa shim", host)
        return _ssh_client(host, port, username, password, key_path,
                           key_passphrase, connect_timeout, True), True


def _cap_bytes(b: bytes, limit: int = 32_768) -> str:
    text = b.decode("utf-8", errors="replace")
    return text[-limit:] if len(text) > limit else text


def _sftp_error(e: Exception) -> dict:
    msg = f"{e.__class__.__name__}: {e}"
    if "EOF" in str(e) or "subsystem" in str(e).lower():
        msg += (" (board likely has no SFTP subsystem -- dropbear needs the "
                "openssh-sftp-server package; transfer via ssh_exec/console instead)")
    return {"ok": False, "error": msg}


atexit.register(lambda: [s.close() for s in list(SESSIONS.values())])


# --------------------------------------------------------------------------- tools
@mcp.tool()
def serial_list() -> dict:
    """List serial (COM) ports available on this machine.

    Returns ports with device name (use as serial_port in connect()), friendly
    description and hardware id -- useful to spot USB-serial adapters of the board.
    """
    ports = [{"device": p.device, "description": p.description, "hwid": p.hwid}
             for p in serial.tools.list_ports.comports()]
    _log.info("serial_list -> %s", [p["device"] for p in ports])
    return {"ok": True, "ports": ports}


@mcp.tool()
def connect(
    type: Literal["serial", "ssh", "telnet"],
    serial_port: Optional[str] = None,
    baudrate: int = 115200,
    bytesize: int = 8,
    parity: Literal["N", "E", "O", "M", "S"] = "N",
    stopbits: float = 1,
    flow: Literal["none", "rtscts", "dsrdtr", "xonxoff"] = "none",
    dtr: Optional[bool] = None,
    rts: Optional[bool] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    key_path: Optional[str] = None,
    key_passphrase: Optional[str] = None,
    connect_timeout: float = 10.0,
    wait_after: float = 1.5,
    encoding: str = "utf-8",
    legacy_algos: bool = False,
    keepalive: float = 0,
) -> dict:
    """Open a stateful console session to a Linux dev board and return a session_id.

    Exactly one of the transports:
    - type="serial": serial_port is a COM port (e.g. "COM3") or pyserial URL
      ("loop://", "socket://ip:port", "rfc2217://..."). baudrate default 115200.
      COM ports are exclusive: check sessions() and close old sessions on the
      same port before connecting. dtr/rts force initial control-line states
      (board reset / bootloader entry).
    - type="ssh": host, username, and password OR key_path (+key_passphrase).
      Interactive login shell (invoke_shell), so prompts/confirmations work.
      legacy_algos=True re-enables ssh-rsa (SHA-1) host keys for pre-2015
      boards; it also auto-enables once when the server offers nothing newer.
      keepalive sends SSH keepalives every N seconds (0 = off) so idle
      connections survive long operations.
    - type="telnet": host (+port, default 23).

    Collects initial output (banner/login prompt) for wait_after seconds and
    returns it as "output". Reuse session_id in send/read/expect/close.
    """
    legacy_used = False
    s: Optional[Session] = None
    try:
        if type == "serial":
            if not serial_port:
                return {"ok": False, "error": "type='serial' requires serial_port (e.g. 'COM3')"}
            s = SerialSession(serial_port, baudrate, bytesize, parity, stopbits,
                              flow, dtr, rts, encoding)
        elif type == "ssh":
            if not (host and username):
                return {"ok": False, "error": "type='ssh' requires host and username"}
            if not (password or key_path):
                return {"ok": False, "error": "type='ssh' requires password or key_path"}

            def _open_ssh(legacy: bool) -> Session:
                return SSHSession(host, port or 22, username, password, key_path,
                                  key_passphrase, connect_timeout, encoding,
                                  legacy_algos=legacy, keepalive=keepalive)
            try:
                s = _open_ssh(legacy_algos)
                legacy_used = legacy_algos
            except IncompatiblePeer:
                if legacy_algos:
                    raise
                _log.info("connect ssh %s: only legacy host keys offered; retrying with ssh-rsa shim", host)
                s = _open_ssh(True)
                legacy_used = True
        elif type == "telnet":
            if not host:
                return {"ok": False, "error": "type='telnet' requires host"}
            s = TelnetSession(host, port or 23, connect_timeout, encoding)
        else:
            return {"ok": False, "error": f"unknown type {type!r}"}
        _register(s)
        s._start()
        initial = s.read(timeout=max(0.0, wait_after))
        out = Session._cap(s._decode(initial), 8000)
        _log.info("connect %s ok: %s (%s)", s.id, s.desc, type)
        res = {"ok": True, "session_id": s.id, "type": s.kind, "desc": s.desc, **out}
        if legacy_used:
            res["legacy_algos"] = True
        return res
    except Exception as e:
        if s is not None:
            # never leak an opened transport (full registry, failed start, ...)
            with SESSIONS_LOCK:
                SESSIONS.pop(s.id, None)
            try:
                s.close()
            except Exception:
                pass
        _log.warning("connect(%s) failed: %s: %s", type, e.__class__.__name__, e)
        return {"ok": False, "error": f"{e.__class__.__name__}: {e}"}


@mcp.tool()
def send(
    session_id: str,
    data: str,
    newline: Literal["auto", "lf", "crlf", "cr", "none"] = "auto",
    wait: float = 1.0,
    max_output: int = 8000,
) -> dict:
    """Send text/commands to an open session and collect the reply.

    Appends a newline by default (auto: LF for serial/ssh, CRLF for telnet;
    use newline="none" for raw bytes e.g. interactive keys like "\\x03" for Ctrl+C).
    Waits `wait` seconds for output before returning it. For slow commands call
    with wait=0 and follow up with expect()/read().
    """
    try:
        s = _get(session_id)
        s.send(data, newline)
        raw = s.read(timeout=max(0.0, wait))
        out = Session._cap(s._decode(raw), max_output)
        return {"ok": True, "session_id": session_id, **out}
    except Exception as e:
        return {"ok": False, "error": f"{e.__class__.__name__}: {e}"}


@mcp.tool()
def read(session_id: str, timeout: float = 2.0, max_output: int = 16000) -> dict:
    """Read new output from an open session (boot logs, async messages).

    Waits up to `timeout` seconds for at least some data; returns immediately
    with whatever is buffered once any arrives. Output longer than max_output
    chars is trimmed to its tail.
    """
    try:
        s = _get(session_id)
        raw = s.read(timeout=max(0.0, timeout))
        out = Session._cap(s._decode(raw), max_output)
        return {"ok": True, "session_id": session_id, **out}
    except Exception as e:
        return {"ok": False, "error": f"{e.__class__.__name__}: {e}"}


@mcp.tool()
def expect(
    session_id: str,
    patterns: list[str],
    timeout: float = 10.0,
    max_output: int = 4000,
) -> dict:
    """Wait until one of the regex patterns appears in the session output.

    The killer feature for console automation: wait for login prompts
    (["login:"]), shells (["root@board:~#", "~ #", "\\$"]), U-Boot
    (["Hit any key to stop"]) or completion markers (["BUILD OK", "ERROR"]).
    Scans output since the last successful expect (independent of read()).
    Returns matched=true/false, which pattern_index matched and the consumed
    output (tail-capped at max_output chars).
    """
    try:
        s = _get(session_id)
        res = s.expect(patterns, timeout=timeout, max_output=max_output)
        return {"ok": True, "session_id": session_id, **res}
    except Exception as e:
        return {"ok": False, "error": f"{e.__class__.__name__}: {e}"}


@mcp.tool()
def control_lines(session_id: str, dtr: Optional[bool] = None, rts: Optional[bool] = None) -> dict:
    """Set serial control lines DTR/RTS on a serial session (board reset, bootloader entry).

    Example: control_lines(session_id, dtr=False, rts=True) then back, to enter
    the ROM bootloader of ESP32/STM32-class boards. Only valid for serial sessions.
    """
    try:
        s = _get(session_id)
        if not isinstance(s, SerialSession):
            return {"ok": False, "error": "control_lines only works on serial sessions"}
        if dtr is not None:
            s.ser.dtr = dtr
        if rts is not None:
            s.ser.rts = rts
        return {"ok": True, "session_id": session_id,
                "dtr": bool(s.ser.dtr), "rts": bool(s.ser.rts)}
    except Exception as e:
        return {"ok": False, "error": f"{e.__class__.__name__}: {e}"}


@mcp.tool()
def sessions() -> dict:
    """List all open board sessions with their status (use this to rediscover session_ids).

    Sessions exposed to a terminal app via share() carry a "shared_port" field.
    """
    with BRIDGE_LOCK:
        shared = {sid: br.port for sid, br in BRIDGES.items()}
    with SESSIONS_LOCK:
        items = [s.status() for s in SESSIONS.values()]
    for it in items:
        if it["session_id"] in shared:
            it["shared_port"] = shared[it["session_id"]]
    return {"ok": True, "count": len(items), "sessions": items}


@mcp.tool()
def close(session_id: str) -> dict:
    """Close an open board session (releases the COM port / network connection).

    Also stops any active share() bridge for the session.
    """
    try:
        s = _get(session_id)
        with BRIDGE_LOCK:
            br = BRIDGES.pop(session_id, None)
        if br:
            br.close()
        s.close()
        with SESSIONS_LOCK:
            SESSIONS.pop(session_id, None)
        return {"ok": True, "session_id": session_id, "closed": True}
    except Exception as e:
        return {"ok": False, "error": f"{e.__class__.__name__}: {e}"}


@mcp.tool()
def share(session_id: str, port: int = 0) -> dict:
    """Share a live session on 127.0.0.1:<port> (mini-telnet) so a human can
    watch and type in any terminal app (Xshell / MobaXterm / WindTerm / PuTTY)
    while the agent works on the same console through MCP.

    Offer this proactively right after connect() -- users usually don't know
    the feature exists. Board output is mirrored to all clients (up to 4);
    client input goes to the board; each client receives the last ~4 KB of
    history on connect. Bound to localhost only. Returns listen_port -- point
    the terminal's Telnet session at 127.0.0.1:<listen_port>. Sharing stops
    with unshare() or when the session is closed.
    """
    try:
        s = _get(session_id)
        if not s.alive:
            return {"ok": False, "error": f"session {session_id} is dead "
                    f"({s.error or 'closed'}); reconnect first"}
        with BRIDGE_LOCK:
            existing = BRIDGES.get(session_id)
            if existing and existing.stop.is_set():   # stale bridge from a died session
                BRIDGES.pop(session_id, None)
                existing = None
            if existing:
                res = {"ok": True, "session_id": session_id,
                       "listen_port": existing.port, "clients": len(existing.clients),
                       "note": "already shared"}
                if port and port != existing.port:
                    res["note"] += f" (requested port {port} ignored)"
                return res
            br = ConsoleBridge(s, port)
            BRIDGES[session_id] = br
        _log.info("session %s shared on 127.0.0.1:%d", session_id, br.port)
        return {"ok": True, "session_id": session_id, "listen_port": br.port,
                "clients": 0, "how": "connect a Telnet session to 127.0.0.1:<listen_port>"}
    except Exception as e:
        return {"ok": False, "error": f"{e.__class__.__name__}: {e}"}


@mcp.tool()
def unshare(session_id: str) -> dict:
    """Stop sharing a session (closes the local bridge port)."""
    try:
        with BRIDGE_LOCK:
            br = BRIDGES.pop(session_id, None)
        if br is None:
            return {"ok": False, "error": "session is not shared"}
        br.close()
        _log.info("session %s unshared", session_id)
        return {"ok": True, "session_id": session_id}
    except Exception as e:
        return {"ok": False, "error": f"{e.__class__.__name__}: {e}"}


@mcp.tool()
def ssh_exec(
    host: str,
    username: str,
    command: str,
    password: Optional[str] = None,
    key_path: Optional[str] = None,
    key_passphrase: Optional[str] = None,
    port: int = 22,
    timeout: float = 20.0,
    legacy_algos: bool = False,
) -> dict:
    """One-shot SSH command execution: connect, run `command`, return stdout/stderr/exit_code.

    Non-interactive -- no prompts. For interactive flows use connect(type='ssh')
    with send/expect instead. `timeout` is a wall-clock limit for the whole
    command: on expiry ok=False with whatever output arrived so far (the
    command may keep running on the board). Old boards offering only ssh-rsa
    host keys are handled automatically (retry with the legacy shim) and
    marked with "legacy_algos": true.
    """
    client = None
    try:
        client, legacy_used = _connect_client(host, port, username, password,
                                              key_path, key_passphrase, 10.0, legacy_algos)
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        chan = stdout.channel
        deadline = time.monotonic() + max(0.1, timeout)
        out_b, err_b = bytearray(), bytearray()
        timed_out = False
        while True:
            # drain what arrived; keep buffering bounded (tail semantics)
            while chan.recv_ready() and time.monotonic() < deadline:
                out_b += chan.recv(65_536)
                if len(out_b) > 512_000:
                    del out_b[:-262_144]
            while chan.recv_stderr_ready() and time.monotonic() < deadline:
                err_b += chan.recv_stderr(65_536)
                if len(err_b) > 512_000:
                    del err_b[:-262_144]
            if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            time.sleep(min(0.05, remaining))
        if not timed_out:
            # grace drain: in-order processing means post-exit-status data is
            # usually already queued; give the race window one last pass
            time.sleep(0.05)
            while chan.recv_ready():
                out_b += chan.recv(65_536)
                if len(out_b) > 512_000:
                    del out_b[:-262_144]
            while chan.recv_stderr_ready():
                err_b += chan.recv_stderr(65_536)
                if len(err_b) > 512_000:
                    del err_b[:-262_144]
        out = _cap_bytes(bytes(out_b))
        err = _cap_bytes(bytes(err_b))
        if timed_out:
            return {"ok": False, "exit_code": None, "stdout": out, "stderr": err,
                    "error": f"TimedOut: command did not exit within {timeout}s "
                             f"(may still be running on the board)"}
        rc = chan.recv_exit_status()
        res = {"ok": True, "exit_code": rc, "stdout": out, "stderr": err}
        if legacy_used:
            res["legacy_algos"] = True
        return res
    except Exception as e:
        return {"ok": False, "error": f"{e.__class__.__name__}: {e}"}
    finally:
        if client:
            try:
                client.close()
            except Exception:
                pass


@mcp.tool()
def sftp_upload(
    host: str,
    username: str,
    local_path: str,
    remote_path: str,
    password: Optional[str] = None,
    key_path: Optional[str] = None,
    key_passphrase: Optional[str] = None,
    port: int = 22,
    legacy_algos: bool = False,
) -> dict:
    """Upload a local file to the board via SFTP (one-shot).

    Example: push a freshly built binary to /tmp/app on the board, then run it
    over a connect(type='ssh') session. Old boards offering only ssh-rsa host
    keys are handled automatically (retry with the legacy shim).
    """
    client = None
    try:
        if not os.path.isfile(local_path):
            return {"ok": False, "error": f"local file not found: {local_path}"}
        client, _ = _connect_client(host, port, username, password,
                                    key_path, key_passphrase, 10.0, legacy_algos)
        sftp = client.open_sftp()
        sftp.get_channel().settimeout(30)  # abort if the board stalls mid-transfer
        sftp.put(local_path, remote_path)
        st = sftp.stat(remote_path)
        sftp.close()
        return {"ok": True, "remote_path": remote_path, "size": st.st_size}
    except Exception as e:
        return _sftp_error(e)
    finally:
        if client:
            try:
                client.close()
            except Exception:
                pass


@mcp.tool()
def sftp_download(
    host: str,
    username: str,
    remote_path: str,
    local_path: str,
    password: Optional[str] = None,
    key_path: Optional[str] = None,
    key_passphrase: Optional[str] = None,
    port: int = 22,
    legacy_algos: bool = False,
) -> dict:
    """Download a file from the board to this machine via SFTP (one-shot).

    Example: pull back /var/log/messages or test results for analysis. Old
    boards offering only ssh-rsa host keys are handled automatically (retry
    with the legacy shim).
    """
    client = None
    try:
        client, _ = _connect_client(host, port, username, password,
                                    key_path, key_passphrase, 10.0, legacy_algos)
        sftp = client.open_sftp()
        sftp.get_channel().settimeout(30)  # abort if the board stalls mid-transfer
        sftp.get(remote_path, local_path)
        sftp.close()
        return {"ok": True, "local_path": local_path, "size": os.path.getsize(local_path)}
    except Exception as e:
        return _sftp_error(e)
    finally:
        if client:
            try:
                client.close()
            except Exception:
                pass


@mcp.resource("boardctl://ai-guide")
def ai_guide() -> str:
    """Full operating guide for agents (AI_GUIDE.md, kept next to the server)."""
    try:
        with open(_AI_GUIDE, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return "AI_GUIDE.md not found (expected at: %s)" % _AI_GUIDE


def main() -> None:
    _log.info("boardctl MCP server starting (stdio)")
    mcp.run()


if __name__ == "__main__":
    main()
