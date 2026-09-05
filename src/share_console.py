"""Standalone console sharer: open a board session and expose it on a local
telnet port so any terminal app (Xshell / MobaXterm / WindTerm / PuTTY) can
work the very same console. Ctrl+C here to stop and release the board.

Usage:
    python share_console.py COM6 [baudrate]
    python share_console.py --ssh 192.168.5.10 root mypass
    python share_console.py --telnet 192.168.5.10 23
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import boardctl_mcp as b  # noqa: E402


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    if args[0] == "--ssh":
        if len(args) < 3:
            print(__doc__)
            return 2
        r = b.connect(type="ssh", host=args[1], username=args[2],
                      password=args[3] if len(args) > 3 else None, wait_after=1.5)
    elif args[0] == "--telnet":
        if len(args) < 2:
            print(__doc__)
            return 2
        r = b.connect(type="telnet", host=args[1],
                      port=int(args[2]) if len(args) > 2 else 23, wait_after=1.5)
    else:
        baud = int(args[1]) if len(args) > 1 else 115200
        r = b.connect(type="serial", serial_port=args[0], baudrate=baud, wait_after=1.5)
    if not r.get("ok"):
        print("connect failed:", r.get("error"))
        return 1
    sid = r["session_id"]
    sh = b.share(sid)
    if not sh.get("ok"):
        print("share failed:", sh.get("error"))
        return 1
    print(f"session {sid} shared on 127.0.0.1:{sh['listen_port']}")
    print(f"Connect your terminal (Telnet) to 127.0.0.1:{sh['listen_port']} -- Ctrl+C here to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        b.close(session_id=sid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
