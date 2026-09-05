"""End-to-end tests for boardctl MCP server.

Runs the real server over stdio (exactly how ZCode talks to it) and exercises
every tool. No real hardware needed:
- serial: pyserial "loop://" URL (virtual loopback)
- telnet: in-process fake telnetd (negotiation + login + shell)
- ssh:    in-process paramiko fake server (password auth, shell, exec, sftp)

Run:  python test_boardctl.py
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import tempfile
import threading
import time
from datetime import timedelta

import paramiko

_HERE = os.path.dirname(os.path.abspath(__file__))
# works both with a src/ + tests/ repo layout and with all files side by side
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if not os.path.isfile(os.path.join(_SRC, "boardctl_mcp.py")):
    _SRC = _HERE
sys.path.insert(0, _SRC)
import boardctl_mcp as b  # noqa: E402

SERVER = os.path.join(_SRC, "boardctl_mcp.py")

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, info: str = "") -> None:
    RESULTS.append((name, bool(cond), info))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"   | {info}" if not cond and info else ""))


# ---------------------------------------------------------------- fake console brain
class Console:
    """Shared login/shell state machine for the fake telnet/ssh servers."""

    def __init__(self, write, close):
        self.write = write
        self.close = close
        self.state = "login"
        self.linebuf = b""
        self.closed = False

    def start(self) -> None:
        self.write(b"BoardOS 2.0 (fake)\r\n")
        self.write(b"board login: ")

    def start_shell(self) -> None:
        """SSH sessions are already authenticated: go straight to the shell."""
        self.state = "shell"
        self.write(b"Welcome to BoardOS!\r\nroot@board:~# ")

    def feed(self, data: bytes) -> None:
        self.linebuf += data
        while b"\n" in self.linebuf:
            line, _, rest = self.linebuf.partition(b"\n")
            self.linebuf = rest
            self.handle(line.strip(b"\r"))

    def handle(self, line: bytes) -> None:
        cmd = line.decode("utf-8", "replace").strip()
        if self.state == "login":
            if cmd == "root":
                self.state = "password"
                self.write(b"Password: ")
            else:
                self.write(f"login '{cmd}' incorrect\r\nboard login: ".encode())
            return
        if self.state == "password":
            if cmd == "boardpw":
                self.state = "shell"
                self.write(b"\r\nWelcome to BoardOS!\r\nroot@board:~# ")
            else:
                self.state = "login"
                self.write(b"\r\nLogin incorrect\r\nboard login: ")
            return
        # shell state
        self.write(line + b"\r\n")  # echo like a real tty
        if cmd in ("exit", "logout"):
            self.write(b"goodbye\r\n")
            self.closed = True
            self.close()
            return
        if cmd.startswith("echo "):
            self.write(cmd[5:].encode() + b"\r\n")
        elif cmd == "uname -a":
            self.write(b"Linux board 5.15.0-fake #1 SMP x86_64 GNU/Linux\r\n")
        elif cmd == "cat /etc/os-release":
            self.write(b"NAME=BoardOS\r\nVERSION=2.0 (fake)\r\n")
        elif cmd:
            self.write(f"{cmd}: command not found\r\n".encode())
        self.write(b"root@board:~# ")


def strip_iac(data: bytes) -> tuple[bytes, bytes]:
    """Remove telnet negotiation from client->server input.

    Returns (clean_output, pending_tail): pending_tail holds an incomplete
    IAC/SB sequence so sequences split across recv chunks are handled.
    """
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        if b == 0xFF:
            if i + 1 >= n:
                return bytes(out), data[i:]           # lone IAC: hold
            nxt = data[i + 1]
            if nxt in (0xFB, 0xFC, 0xFD, 0xFE):
                if i + 2 >= n:
                    return bytes(out), data[i:]       # IAC cmd without option
                i += 3
                continue
            if nxt == 0xFF:
                out.append(0xFF)
                i += 2
                continue
            if nxt == 0xFA:                            # SB ... IAC SE
                j = data.find(b"\xff\xf0", i)
                if j == -1:
                    return bytes(out), data[i:]       # unterminated SB: hold all
                i = j + 2
                continue
            i += 2
            continue
        out.append(b)
        i += 1
    return bytes(out), b""


# ---------------------------------------------------------------- fake telnetd
def fake_telnet_server():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(2)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def run():
        srv.settimeout(0.5)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            conn.settimeout(10)
            conn.sendall(b"\xff\xfb\x01\xff\xfb\x03")  # WILL ECHO, WILL SGA
            con = Console(lambda b: conn.sendall(b), conn.close)
            con.start()
            try:
                pending = b""
                while not con.closed:
                    data = conn.recv(1024)
                    if not data:
                        break
                    clean, pending = strip_iac(pending + data)
                    if clean:
                        con.feed(clean)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
        srv.close()

    threading.Thread(target=run, daemon=True).start()
    return port, stop


# ---------------------------------------------------------------- fake sshd
class StubSFTPHandle(paramiko.SFTPHandle):
    def stat(self):
        try:
            f = self.readfile or self.writefile
            return paramiko.SFTPAttributes.from_stat(os.fstat(f.fileno()))
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)


class StubSFTPServer(paramiko.SFTPServerInterface):
    ROOT = "/tmp"

    def _realpath(self, path):
        return os.path.join(self.ROOT, path.lstrip("/").replace("..", "_"))

    def open(self, path, flags, attr):
        path = self._realpath(path)
        try:
            flags |= getattr(os, "O_BINARY", 0)
            pyflags = flags & (os.O_APPEND | os.O_CREAT | os.O_TRUNC | os.O_EXCL)
            if flags & os.O_WRONLY:
                pyflags |= os.O_WRONLY
            if flags & os.O_RDWR:
                pyflags |= os.O_RDWR
            if pyflags & os.O_RDWR:
                mode = "r+b"
            elif pyflags & os.O_WRONLY:
                mode = "ab" if pyflags & os.O_APPEND else "wb"
            else:
                mode = "rb"
            f = open(path, mode)
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)
        h = StubSFTPHandle(flags)
        h.filename = path
        h.readfile = f
        h.writefile = f
        return h

    def stat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(os.stat(self._realpath(path)))
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)


class BoardSSHServer(paramiko.ServerInterface):
    def __init__(self):
        self._sf = []

    def get_allowed_auths(self, username):
        return "password"

    def check_auth_password(self, username, password):
        if username == "root" and password == "boardpw":
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chan):
        return paramiko.OPEN_SUCCEEDED

    def check_channel_pty_request(self, *args):
        return True

    def check_channel_shell_request(self, channel):
        threading.Thread(target=self._shell, args=(channel,), daemon=True).start()
        return True

    def check_channel_exec_request(self, channel, command):
        threading.Thread(target=self._exec, args=(channel, command), daemon=True).start()
        return True

    def check_channel_subsystem_request(self, channel, name):
        if name == "sftp":
            s = paramiko.SFTPServer(channel, "sftp", self, StubSFTPServer)
            self._sf.append(s)
            s.start()
            return True
        return False

    def _shell(self, channel):
        con = Console(lambda b: channel.sendall(b), channel.close)
        con.start_shell()
        try:
            while not con.closed:
                data = channel.recv(1024)
                if not data:
                    break
                con.feed(data)
        except OSError:
            pass
        if not con.closed:
            try:
                channel.close()
            except OSError:
                pass

    def _exec(self, channel, command):
        time.sleep(0.1)  # let the transport thread send the exec success reply first
        cmd = command.decode("utf-8", "replace") if isinstance(command, bytes) else command
        if cmd == "slow-stream":  # for wall-clock timeout tests
            try:
                for i in range(6):
                    time.sleep(0.5)
                    channel.sendall(f"chunk-{i}\n".encode())
            except OSError:
                pass  # client gave up (timeout test closes the transport)
            try:
                channel.send_exit_status(0)
            except OSError:
                pass
            channel.close()
            return
        if cmd.startswith("echo "):
            channel.sendall(cmd[5:].encode() + b"\n")
            rc = 0
        else:
            channel.sendall(f"{cmd}: command not found\n".encode())
            rc = 127
        channel.send_exit_status(rc)
        channel.close()


def fake_ssh_server(root: str, server_key=None, advertise=None):
    """Fake sshd. server_key: host key object (default: fresh RSA key);
    advertise: restrict the host-key algorithms the server offers, e.g.
    ("ssh-rsa",) to emulate a 2015-era Dropbear."""
    StubSFTPServer.ROOT = root
    key = server_key or paramiko.RSAKey.generate(2048)
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(2)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def run():
        srv.settimeout(0.5)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            try:
                t = paramiko.Transport(conn)
                t.add_server_key(key)
                if advertise:
                    t._preferred_keys = advertise
                t.start_server(server=BoardSSHServer())
                while t.is_active() and not stop.is_set():
                    time.sleep(0.1)
                t.close()
            except Exception:
                pass
            try:
                conn.close()
            except OSError:
                pass
        srv.close()

    threading.Thread(target=run, daemon=True).start()
    return port, stop


# ---------------------------------------------------------------- MCP harness
async def call(s, name, args, timeout_s=90):
    res = await asyncio.wait_for(s.call_tool(name, args), timeout_s)
    sc = getattr(res, "structuredContent", None)
    if isinstance(sc, dict):
        if set(sc.keys()) == {"result"} and isinstance(sc["result"], dict):
            sc = sc["result"]
        return sc
    for c in (res.content or []):
        if getattr(c, "type", "") == "text":
            try:
                return json.loads(c.text)
            except Exception:
                return {"raw": c.text}
    return {"raw": str(res)}


async def main() -> int:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    tmp = tempfile.mkdtemp(prefix="boardctl_test_")
    tport, tstop = fake_telnet_server()
    sport, sstop = fake_ssh_server(tmp)
    legacy_key = b._LegacyRSAKey(key=paramiko.RSAKey.generate(2048).key)
    lport, lstop = fake_ssh_server(tmp, server_key=legacy_key, advertise=("ssh-rsa",))
    print(f"fake telnetd on 127.0.0.1:{tport}, fake sshd on 127.0.0.1:{sport}, "
          f"legacy sshd (ssh-rsa only) on 127.0.0.1:{lport}")

    params = StdioServerParameters(command=sys.executable, args=[SERVER], env=dict(os.environ))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w, read_timeout_seconds=timedelta(seconds=120)) as s:
            await s.initialize()

            tools = await s.list_tools()
            names = {t.name for t in tools.tools}
            for n in ["connect", "send", "read", "expect", "close", "sessions",
                      "serial_list", "ssh_exec", "sftp_upload", "sftp_download",
                      "control_lines", "share", "unshare"]:
                check(f"tool:{n}", n in names, f"have {sorted(names)}")

            # ---- resources: AI guide reachable via MCP ----------------------
            ress = await s.list_resources()
            uris = {str(r.uri) for r in ress.resources}
            check("resource:ai-guide listed", "boardctl://ai-guide" in uris, str(uris)[:200])
            rres = await s.read_resource("boardctl://ai-guide")
            text = rres.contents[0].text if getattr(rres, "contents", None) else ""
            check("resource:ai-guide readable",
                  "expect" in text and "share" in text and "主动提起" in text, text[:120])

            res = await call(s, "serial_list", {})
            check("serial_list", res.get("ok") and isinstance(res.get("ports"), list), str(res)[:200])

            # ---- serial over loop:// --------------------------------------
            res = await call(s, "connect", {"type": "serial", "serial_port": "loop://",
                                            "baudrate": 115200, "wait_after": 0.5})
            check("connect:serial loop://", res.get("ok"), str(res)[:200])
            sid = res.get("session_id", "")
            res = await call(s, "send", {"session_id": sid, "data": "ping-serial", "wait": 0.6})
            check("serial:send echoes", "ping-serial" in res.get("output", ""), str(res)[:200])
            res = await call(s, "expect", {"session_id": sid, "patterns": [r"ping-.*ial"], "timeout": 2})
            check("serial:expect regex", res.get("matched") is True, str(res)[:200])
            res = await call(s, "expect", {"session_id": sid, "patterns": ["never-appears"], "timeout": 1.5})
            check("serial:expect timeout -> matched=false",
                  res.get("ok") and res.get("matched") is False, str(res)[:200])
            res = await call(s, "control_lines", {"session_id": sid, "dtr": True, "rts": False})
            check("serial:control_lines", res.get("ok") and res.get("dtr") is True, str(res)[:200])
            res = await call(s, "close", {"session_id": sid})
            check("serial:close", res.get("ok"), str(res)[:150])

            # ---- telnet ----------------------------------------------------
            res = await call(s, "connect", {"type": "telnet", "host": "127.0.0.1",
                                            "port": tport, "wait_after": 1.2})
            check("connect:telnet", res.get("ok"), str(res)[:200])
            check("telnet:banner+login prompt", "login" in res.get("output", ""), str(res)[:300])
            tid = res.get("session_id", "")
            res = await call(s, "expect", {"session_id": tid, "patterns": ["login:"], "timeout": 5})
            check("telnet:expect login", res.get("matched") is True, str(res)[:200])
            await call(s, "send", {"session_id": tid, "data": "root", "wait": 0.8})
            res = await call(s, "expect", {"session_id": tid, "patterns": ["Password:"], "timeout": 5})
            check("telnet:expect password", res.get("matched") is True, str(res)[:200])
            await call(s, "send", {"session_id": tid, "data": "boardpw", "wait": 0.8})
            res = await call(s, "expect", {"session_id": tid, "patterns": [r"root@board:~#"], "timeout": 5})
            check("telnet:login complete", res.get("matched") is True, str(res)[:200])
            res = await call(s, "send", {"session_id": tid, "data": "echo hello-telnet", "wait": 1.2})
            check("telnet:command output", "hello-telnet" in res.get("output", ""), str(res)[:300])
            res = await call(s, "close", {"session_id": tid})
            check("telnet:close", res.get("ok"), str(res)[:150])

            # ---- ssh interactive shell -------------------------------------
            res = await call(s, "connect", {"type": "ssh", "host": "127.0.0.1", "port": sport,
                                            "username": "root", "password": "boardpw",
                                            "wait_after": 2.0})
            check("connect:ssh", res.get("ok"), str(res)[:250])
            check("ssh:prompt in initial output", "root@board:~#" in res.get("output", ""), str(res)[:300])
            shid = res.get("session_id", "")
            res = await call(s, "send", {"session_id": shid, "data": "uname -a", "wait": 1.2})
            check("ssh:uname -a", "Linux board 5.15.0-fake" in res.get("output", ""), str(res)[:300])
            res = await call(s, "expect", {"session_id": shid, "patterns": [r"~#"], "timeout": 3})
            check("ssh:expect prompt", res.get("matched") is True, str(res)[:200])
            res = await call(s, "send", {"session_id": shid, "data": "cat /etc/os-release", "wait": 1.0})
            check("ssh:os-release", "BoardOS" in res.get("output", ""), str(res)[:300])
            res = await call(s, "close", {"session_id": shid})
            check("ssh:close", res.get("ok"), str(res)[:150])

            # ---- one-shot ssh_exec -----------------------------------------
            res = await call(s, "ssh_exec", {"host": "127.0.0.1", "port": sport, "username": "root",
                                             "password": "boardpw", "command": "echo exec-ok"})
            check("ssh_exec ok", res.get("ok") and res.get("exit_code") == 0
                  and "exec-ok" in res.get("stdout", ""), str(res)[:250])
            res = await call(s, "ssh_exec", {"host": "127.0.0.1", "port": sport, "username": "root",
                                             "password": "boardpw", "command": "nonsense-cmd"})
            check("ssh_exec rc=127", res.get("ok") and res.get("exit_code") == 127, str(res)[:250])

            # ---- sftp round trip -------------------------------------------
            payload = b"boardctl-sftp-payload-\x00\x01\x02-345"
            up = os.path.join(tmp, "up.bin")
            down = os.path.join(tmp, "down.bin")
            with open(up, "wb") as f:
                f.write(payload)
            base = {"host": "127.0.0.1", "port": sport, "username": "root", "password": "boardpw"}
            res = await call(s, "sftp_upload", {**base, "local_path": up, "remote_path": "/test_up.bin"})
            check("sftp_upload", res.get("ok") and res.get("size") == len(payload), str(res)[:250])
            res = await call(s, "sftp_download", {**base, "remote_path": "/test_up.bin", "local_path": down})
            got = open(down, "rb").read() if os.path.exists(down) else b""
            check("sftp_download roundtrip", res.get("ok") and got == payload,
                  str(res)[:250] + f" got={got[:40]!r}")
            res = await call(s, "sftp_upload", {**base, "local_path": os.path.join(tmp, "missing.bin"),
                                                "remote_path": "/x.bin"})
            check("sftp_upload missing local -> clean error", res.get("ok") is False, str(res)[:200])

            # ---- legacy sshd: ssh-rsa host key only (old Dropbear style) ---
            res = await call(s, "ssh_exec", {"host": "127.0.0.1", "port": lport,
                                             "username": "root", "password": "boardpw",
                                             "command": "echo legacy-exec-ok"})
            check("ssh_exec:legacy auto-retry", res.get("ok")
                  and "legacy-exec-ok" in res.get("stdout", "")
                  and res.get("legacy_algos") is True, str(res)[:250])
            res = await call(s, "connect", {"type": "ssh", "host": "127.0.0.1", "port": lport,
                                            "username": "root", "password": "boardpw",
                                            "legacy_algos": True, "keepalive": 15,
                                            "wait_after": 2.0})
            check("connect:ssh legacy explicit", res.get("ok"), str(res)[:250])
            check("ssh legacy:prompt", "root@board:~#" in res.get("output", ""), str(res)[:300])
            lid = res.get("session_id", "")
            res = await call(s, "send", {"session_id": lid, "data": "uname -a", "wait": 1.2})
            check("ssh legacy:uname", "Linux board 5.15.0-fake" in res.get("output", ""), str(res)[:300])
            res = await call(s, "close", {"session_id": lid})
            check("ssh legacy:close", res.get("ok"), str(res)[:150])

            # ---- share: human terminal bridge -------------------------------
            res = await call(s, "connect", {"type": "serial", "serial_port": "loop://",
                                            "baudrate": 115200, "wait_after": 0.3})
            check("connect:serial for share", res.get("ok"), str(res)[:200])
            ssid = res.get("session_id", "")
            await call(s, "send", {"session_id": ssid, "data": "early-marker", "wait": 0.5})
            res = await call(s, "share", {"session_id": ssid})
            check("share:ok", res.get("ok") and res.get("listen_port"), str(res)[:200])
            bport = res.get("listen_port", 0)
            res = await call(s, "share", {"session_id": ssid})
            check("share:re-share same port", res.get("ok") and res.get("listen_port") == bport,
                  str(res)[:200])

            cli = socket.create_connection(("127.0.0.1", bport), timeout=5)

            async def cli_recv_until(token: bytes, budget: float = 4.0) -> bytes:
                acc = b""
                dl = time.monotonic() + budget
                while token not in acc and time.monotonic() < dl:
                    try:
                        chunk = await asyncio.to_thread(cli.recv, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    acc += chunk
                return acc

            hist = await cli_recv_until(b"early-marker", 3)
            check("share:history replay", b"early-marker" in hist, repr(hist[:120]))
            cli.sendall(b"\xff\xfd\x01")  # IAC DO ECHO -> expect IAC WILL ECHO
            neg = await cli_recv_until(b"\xff\xfb\x01", 2)
            check("share:telnet negotiation", b"\xff\xfb\x01" in neg, repr(neg[:60]))
            cli.sendall(b"typed-by-human\r\n")
            got = await cli_recv_until(b"typed-by-human", 4)
            check("share:client input echoed back", b"typed-by-human" in got, repr(got[:120]))
            res = await call(s, "expect", {"session_id": ssid, "patterns": [r"typed-by-human"],
                                           "timeout": 2})
            check("share:mcp sees human input", res.get("matched") is True, str(res)[:200])
            await call(s, "send", {"session_id": ssid, "data": "hello-from-mcp", "wait": 1.0})
            got = await cli_recv_until(b"hello-from-mcp", 4)
            check("share:client sees mcp output", b"hello-from-mcp" in got, repr(got[:120]))
            res = await call(s, "sessions", {})
            shared_ok = any(x.get("session_id") == ssid and x.get("shared_port") == bport
                            for x in res.get("sessions", []))
            check("share:sessions shows shared_port", shared_ok, str(res)[:300])
            res = await call(s, "unshare", {"session_id": ssid})
            check("unshare:ok", res.get("ok"), str(res)[:150])
            cli.settimeout(2)
            try:
                data = await asyncio.to_thread(cli.recv, 4096)
                closed = data == b""
            except OSError:
                closed = True
                data = b""
            check("unshare:client disconnected", closed, f"data={data!r}" if not closed else "")
            cli.close()
            res = await call(s, "close", {"session_id": ssid})
            check("share:close session", res.get("ok"), str(res)[:150])

            # ---- session cap: full registry rejected cleanly, then recovers -
            cap_ids = []
            for _ in range(16):
                res = await call(s, "connect", {"type": "serial", "serial_port": "loop://",
                                                "baudrate": 115200, "wait_after": 0.1})
                if res.get("ok"):
                    cap_ids.append(res["session_id"])
            check("cap:16 sessions open", len(cap_ids) == 16, str(len(cap_ids)))
            res = await call(s, "connect", {"type": "serial", "serial_port": "loop://",
                                            "baudrate": 115200, "wait_after": 0.1})
            check("cap:17th rejected", res.get("ok") is False
                  and "too many" in str(res.get("error", "")), str(res)[:150])
            res = await call(s, "sessions", {})
            check("cap:no orphan on reject", res.get("count") == 16, str(res)[:200])
            for sid in cap_ids:
                await call(s, "close", {"session_id": sid})
            res = await call(s, "connect", {"type": "serial", "serial_port": "loop://",
                                            "baudrate": 115200, "wait_after": 0.1})
            check("cap:recovers after close-all", res.get("ok") is True, str(res)[:150])
            await call(s, "close", {"session_id": res.get("session_id", "")})

            # ---- ssh_exec wall-clock timeout on a streaming command ----------
            res = await call(s, "ssh_exec", {"host": "127.0.0.1", "port": sport,
                                             "username": "root", "password": "boardpw",
                                             "command": "slow-stream", "timeout": 1.0})
            check("ssh_exec:wall-clock timeout", res.get("ok") is False
                  and "TimedOut" in str(res.get("error", ""))
                  and "chunk-0" in str(res.get("stdout", "")), str(res)[:250])

            # ---- error paths ------------------------------------------------
            res = await call(s, "connect", {"type": "ssh", "host": "127.0.0.1", "port": sport,
                                            "username": "root", "password": "wrong"})
            check("ssh:wrong password -> clean error", res.get("ok") is False and "error" in res,
                  str(res)[:200])
            res = await call(s, "connect", {"type": "serial", "serial_port": "COM99"})
            check("serial:nonexistent COM -> clean error", res.get("ok") is False, str(res)[:200])
            res = await call(s, "connect", {"type": "telnet", "host": "127.0.0.1", "port": 1,
                                            "connect_timeout": 3})
            check("telnet:refused -> clean error", res.get("ok") is False, str(res)[:200])
            res = await call(s, "send", {"session_id": "no-such-id", "data": "x"})
            check("send:unknown session -> clean error", res.get("ok") is False, str(res)[:200])
            res = await call(s, "sessions", {})
            check("sessions:all closed", res.get("ok") and res.get("count") == 0, str(res)[:300])

    tstop.set()
    sstop.set()
    lstop.set()

    fails = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} checks passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
