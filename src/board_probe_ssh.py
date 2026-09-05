"""Probe a board over SSH: exercises boardctl's ssh path against real hardware.

Tries a list of passwords (or one explicit password), then runs an interactive
shell command and an SFTP round trip. Old boards that only offer ssh-rsa host
keys (Dropbear 2014-2017) are handled by the built-in legacy shim automatically.

Usage:
    python board_probe_ssh.py 192.168.5.10              # try common passwords
    python board_probe_ssh.py 192.168.5.10 mypass       # explicit password
"""

import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import boardctl_mcp as b  # noqa: E402

logging.getLogger("paramiko").setLevel(logging.CRITICAL)  # keep probe output clean

COMMON_PASSWORDS = ["", "root", "123456"]


def show(tag, res, limit=1500):
    print(f"----- {tag} -----")
    print(json.dumps(res, ensure_ascii=False)[:limit])
    print()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    host = sys.argv[1]
    candidates = [sys.argv[2]] if len(sys.argv) > 2 else COMMON_PASSWORDS

    used = None
    for pw in candidates:
        r = b.ssh_exec(host=host, username="root", password=pw,
                       command="echo SSH-OK", timeout=15)
        print(f"[probe] password {pw!r} -> {'ok' if r.get('ok') else str(r.get('error', ''))[:120]}")
        if r.get("ok"):
            used = pw
            break
    if used is None:
        print("[probe] no password worked. Set one from the serial console first, e.g.:")
        print("        send('echo root:root | chpasswd')")
        return 1
    print(f"[probe] ssh works with password {used!r}")

    r = b.connect(type="ssh", host=host, username="root", password=used,
                  wait_after=2.0, keepalive=30)
    show("connect(ssh)", r)
    if not r.get("ok"):
        return 1
    sid = r["session_id"]
    try:
        show("uname -a", b.send(session_id=sid, data="uname -a", wait=2.0))

        tmp_up = os.path.join(os.environ.get("TEMP", "."), "boardctl_probe_up.bin")
        tmp_down = tmp_up + ".down"
        payload = b"boardctl-ssh-probe-payload-0123456789"
        with open(tmp_up, "wb") as f:
            f.write(payload)
        show("sftp_upload", b.sftp_upload(host=host, username="root", password=used,
                                          local_path=tmp_up, remote_path="/tmp/probe_up.bin"))
        show("sftp_download", b.sftp_download(host=host, username="root", password=used,
                                              remote_path="/tmp/probe_up.bin",
                                              local_path=tmp_down))
        got = open(tmp_down, "rb").read() if os.path.exists(tmp_down) else b""
        print(f"[probe] sftp roundtrip {'OK' if got == payload else 'MISMATCH'}")
        show("cleanup", b.ssh_exec(host=host, username="root", password=used,
                                   command="rm -f /tmp/probe_up.bin"))
    finally:
        show("close", b.close(session_id=sid), 300)
    return 0


if __name__ == "__main__":
    sys.exit(main())
