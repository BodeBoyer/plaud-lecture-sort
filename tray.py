"""System-tray runner: syncs every 15 min, toast per new lecture, menu for
manual sync / notes / new docs. Started at logon by `lecture_sort.py setup`.
Run with pythonw.exe so no console window appears."""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import threading
import traceback
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lecture_sort as ls  # noqa: E402

INTERVAL_S = 15 * 60
_lock = threading.Lock()
_last = "never"


def _icon_image(color: str) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((6, 6, 58, 58), radius=12, fill=color)
    d.rectangle((18, 20, 46, 24), fill="white")
    d.rectangle((18, 30, 46, 34), fill="white")
    d.rectangle((18, 40, 36, 44), fill="white")
    return img


def sync(icon: pystray.Icon) -> None:
    global _last
    if not _lock.acquire(blocking=False):
        return
    try:
        icon.icon = _icon_image("#e0a800")
        cfg = json.loads(ls.CONFIG.read_text(encoding="utf-8"))
        state = ls.load_state()
        res = ls.run(7, False, cfg, state)
        for rid, cls, how in res:
            if cls in ("short", "pending"):
                continue
            name = cfg["classes"].get(cls, {}).get("name", "Unsorted")
            icon.notify(f"{name}  ({how})", "Lecture sorted")
        n = sum(1 for _, c, _ in res if c not in ("short", "pending"))
        pend = sum(1 for _, c, _ in res if c == "pending")
        _last = f"{dt.datetime.now():%H:%M}  {n} new" + (f", {pend} pending" if pend else "")
        icon.icon = _icon_image("#2e7d32")
    except SystemExit as e:  # plaud not logged in / missing credentials
        _last = f"{dt.datetime.now():%H:%M}  ERROR"
        icon.icon = _icon_image("#c62828")
        icon.notify(str(e)[:200], "Lecture sort needs attention")
    except Exception:  # noqa: BLE001
        _last = f"{dt.datetime.now():%H:%M}  ERROR"
        icon.icon = _icon_image("#c62828")
        with ls.LOG.open("a", encoding="utf-8") as f:
            f.write(traceback.format_exc())
        icon.notify("see lecture_sort.log", "Lecture sort error")
    finally:
        icon.title = f"Lecture sort - last: {_last}"
        _lock.release()


def _loop(icon: pystray.Icon) -> None:
    while True:
        sync(icon)
        threading.Event().wait(INTERVAL_S)


def _open(path: Path):
    return lambda: os.startfile(path) if path.exists() else None


def main() -> None:
    icon = pystray.Icon("lecture-sort", _icon_image("#2e7d32"), "Lecture sort")
    icon.menu = pystray.Menu(
        pystray.MenuItem(lambda _: f"Last sync: {_last}", None, enabled=False),
        pystray.MenuItem("Sync now", lambda: threading.Thread(target=sync, args=(icon,), daemon=True).start()),
        pystray.MenuItem("Open notes folder", _open(ls.NOTES)),
        pystray.MenuItem("New docs to add to NotebookLM", _open(ls.NEW_DOCS)),
        pystray.MenuItem("Open log", _open(ls.LOG)),
        pystray.MenuItem("Quit", lambda: icon.stop()),
    )
    threading.Thread(target=_loop, args=(icon,), daemon=True).start()
    icon.run()


if __name__ == "__main__":
    ls.NOTES.mkdir(exist_ok=True)
    main()
