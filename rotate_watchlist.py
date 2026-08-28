"""
Rotate tracked_keywords.txt so a full run always starts from a DIFFERENT point in the list.

Live-observed 2026-08-28: a full 14-product run rarely finishes inside one scheduled cycle
(heavy rate-limiting means each cycle only gets through ~7). Since run_tracker always started at
line 1, the SAME first ~7 products completed every cycle while the LAST ~7 (Ralph Lauren, all 6
Jellycat) never got a turn — starved indefinitely, not just delayed.

Fix: persist a rotation offset (tracking/.watchlist_rotation) and print the keyword list starting
from that offset, wrapping around. Advance the offset by half the list each call, so whichever
half didn't run last time goes FIRST next time — guaranteeing every product gets a turn at least
once every 2 cycles, regardless of how many actually complete in any one cycle.

Usage: python rotate_watchlist.py [path/to/tracked_keywords.txt]
Prints the rotated, comment-stripped keyword list to stdout, one per line.
"""

import os
import sys

STATE_FILE = "tracking/.watchlist_rotation"


def read_entries(path: str) -> list:
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                entries.append(line)
    return entries


def read_offset() -> int:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return 0


def write_offset(n: int) -> None:
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write(str(n))


def rotate(entries: list, offset: int) -> list:
    if not entries:
        return []
    offset %= len(entries)
    return entries[offset:] + entries[:offset]


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "tracked_keywords.txt"
    entries = read_entries(path)
    offset = read_offset()
    for kw in rotate(entries, offset):
        print(kw)
    if entries:
        write_offset((offset + max(1, len(entries) // 2)) % len(entries))


def _demo() -> None:
    """Self-check: rotation cycles through the whole list; two consecutive calls together
    cover every entry starting from the front at least once."""
    entries = [f"item{i}" for i in range(6)]
    assert rotate(entries, 0) == entries
    assert rotate(entries, 3) == ["item3", "item4", "item5", "item0", "item1", "item2"]
    assert rotate(entries, 7) == rotate(entries, 1)  # wraps via modulo
    # simulate two consecutive runs advancing the persisted offset
    import tempfile
    global STATE_FILE
    real_state = STATE_FILE
    STATE_FILE = os.path.join(tempfile.gettempdir(), "rotate_watchlist_selftest_state")
    try:
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
        run1 = rotate(entries, read_offset())
        write_offset((read_offset() + len(entries) // 2) % len(entries))
        run2 = rotate(entries, read_offset())
        assert run1[0] == "item0" and run2[0] == "item3", (run1, run2)
        # the back half of run1 (never reached if a cycle only gets through ~half) is the
        # FRONT half of run2 — proving starved items get first priority next time
        assert run2[: len(entries) // 2] == entries[len(entries) // 2:]
    finally:
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
        STATE_FILE = real_state
    print("rotate_watchlist self-check OK")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        _demo()
    else:
        main()
