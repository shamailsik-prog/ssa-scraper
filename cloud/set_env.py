#!/usr/bin/env python3
"""Set one key in the service's .env from stdin, so the value never appears on a command line.

Usage: printf '%s' "$VALUE" | python3 set_env.py KEY [/path/to/.env]
"""

import re
import sys


def main() -> int:
    if len(sys.argv) < 2 or not re.fullmatch(r"[A-Z0-9_]+", sys.argv[1]):
        print("usage: set_env.py KEY [.env]", file=sys.stderr)
        return 2
    key = sys.argv[1]
    path = sys.argv[2] if len(sys.argv) > 2 else "/opt/ssa-scraper/.env"
    value = sys.stdin.read().strip()
    if any(c in value for c in "\r\n"):
        print("value must be a single line", file=sys.stderr)
        return 2
    text = open(path, encoding="utf-8").read()
    line = f"{key}={value}"
    text, n = re.subn(rf"^{re.escape(key)}=.*$", lambda _m: line, text, flags=re.M)
    if n == 0:
        text += ("" if text.endswith("\n") else "\n") + line + "\n"
    open(path, "w", encoding="utf-8").write(text)
    print(f"{key} set")
    return 0


if __name__ == "__main__":
    sys.exit(main())
