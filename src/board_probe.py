"""Probe a serial console: detect login prompt / shell, print what the board says.

Usage: python board_probe.py COM6 115200
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import boardctl_mcp as b  # noqa: E402


def show(tag, res, limit=2500):
    text = json.dumps(res, ensure_ascii=False)
    print(f"----- {tag} -----")
    print(text[:limit])
    print()


def looks_like_shell(text: str) -> bool:
    tail = text.rstrip()
    return bool(re.search(r"[#$] $", tail + " ")) or tail.endswith("#") or tail.endswith("$")


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "COM6"
    baud = int(sys.argv[2]) if len(sys.argv) > 2 else 115200

    show("serial_list", b.serial_list(), 800)

    r = b.connect(type="serial", serial_port=port, baudrate=baud, wait_after=2.5)
    show(f"connect {port}@{baud}", r)
    if not r.get("ok"):
        return 1
    sid = r["session_id"]

    try:
        # press Enter to elicit whatever prompt the console is sitting at
        r = b.send(session_id=sid, data="\n", newline="none", wait=2.5)
        show("press Enter", r)
        out = r.get("output", "")
        logged_in = looks_like_shell(out)

        if "login:" in out and not logged_in:
            b.send(session_id=sid, data="root", wait=1.5)
            pw = b.expect(session_id=sid, patterns=["Password:", "login:"], timeout=4)
            show("after user root", pw)
            if pw.get("matched") and pw.get("pattern_index") == 0:
                for pwd in ("", "root", "123456"):
                    b.send(session_id=sid, data=pwd, wait=2.0)
                    chk = b.send(session_id=sid, data="\n", newline="none", wait=1.5)
                    if looks_like_shell(chk.get("output", "")) and "incorrect" not in chk.get("output", ""):
                        logged_in = True
                        print(f"[probe] logged in with password: {pwd!r}")
                        break
                    # maybe still at Password: or bounced back to login:
                    if "login:" in chk.get("output", ""):
                        b.send(session_id=sid, data="root", wait=1.5)
                        b.expect(session_id=sid, patterns=["Password:"], timeout=4)
            else:
                logged_in = looks_like_shell(pw.get("output", ""))

        if logged_in:
            for cmd in ("uname -a", "cat /etc/os-release 2>/dev/null | head -n 3", "uptime"):
                r = b.send(session_id=sid, data=cmd, wait=2.0)
                show(f"cmd: {cmd}", r)
        else:
            print("[probe] console state unknown -- output above shows what the board is at.")
    finally:
        show("close", b.close(session_id=sid), 300)
    return 0


if __name__ == "__main__":
    sys.exit(main())
