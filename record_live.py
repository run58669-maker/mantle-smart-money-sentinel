"""Live screen recording of the Mantle Sentinel demo.

Layout:
    [ left half: cmd running main.py --demo ]  [ right half: Chrome on the repo ]

Same orchestrator pattern as the TF Resilient Agent record — but the
demo runs main.py --demo (forge a whale-1 outflow, fire the full
RPC → state-diff → LLM → Telegram pipeline once) and the right pane
shows the GitHub source of mantle_rpc.py so a judge can read the
fallback chain code alongside the live output.
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).parent
VIDEO_DIR = ROOT / "video"
OUT = VIDEO_DIR / "segment_live.mp4"

SCREEN_W = 1920
SCREEN_H = 1080
HALF_W = SCREEN_W // 2
RECORD_S = 50
DEMO_DELAY_S = 4
TITLE = f"MantleSentinelDemo_{int(time.time())}"
PY = r"C:\Users\86150\AppData\Local\Python\pythoncore-3.14-64\python.exe"

user32 = ctypes.windll.user32
SWP_SHOWWINDOW = 0x0040
SW_RESTORE = 9
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2


def enum_windows():
    out: list[tuple[int, str]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lp):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 2)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        out.append((hwnd, buf.value))
        return True

    user32.EnumWindows(cb, 0)
    return out


def find_window(predicate):
    for hwnd, title in enum_windows():
        if predicate(title):
            return hwnd, title
    return None, None


def place(hwnd, x, y, w, h, topmost=False):
    user32.ShowWindow(hwnd, SW_RESTORE)
    after = HWND_TOPMOST if topmost else 0
    user32.SetWindowPos(hwnd, after, x, y, w, h, SWP_SHOWWINDOW)


def unset_topmost(hwnd):
    user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, 0x0001 | 0x0002 | SWP_SHOWWINDOW)


def main():
    from playwright.sync_api import sync_playwright

    print("[1/6] navigate chrome to repo source")
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        pg = next((t for t in ctx.pages if "github.com/run58669-maker" in t.url), None) or ctx.new_page()
        pg.bring_to_front()
        pg.goto("https://github.com/run58669-maker/mantle-smart-money-sentinel/blob/main/mantle_rpc.py",
                wait_until="domcontentloaded", timeout=20000)
        time.sleep(2.0)

    print("[2/6] place chrome right half")
    chrome_hwnd, chrome_title = find_window(
        lambda t: "mantle_rpc.py" in t or "mantle-smart-money-sentinel" in t or t.endswith("Google Chrome")
    )
    if not chrome_hwnd:
        print("  ERROR: chrome window not found")
        sys.exit(1)
    print(f"  chrome: '{chrome_title[:60]}' hwnd={chrome_hwnd}")
    place(chrome_hwnd, HALF_W, 0, HALF_W, SCREEN_H, topmost=True)

    print(f"[3/6] spawn cmd '{TITLE}' (delay {DEMO_DELAY_S}s then main.py --demo)")
    win_timeout = r"C:\Windows\System32\timeout.exe"
    inner = (
        f"chcp 65001 >nul && "
        f"title {TITLE} && cd /d {ROOT} && "
        f"echo === Mantle Smart-Money Sentinel  DevNetwork 2026 === && "
        f"echo Starting in {DEMO_DELAY_S} seconds... && "
        f"{win_timeout} /t {DEMO_DELAY_S} /nobreak >nul && "
        f'"{PY}" -X utf8 main.py --demo && '
        f"echo === recording window closes shortly ==="
    )
    CREATE_NEW_CONSOLE = 0x00000010
    subprocess.Popen(f'cmd /K "{inner}"', creationflags=CREATE_NEW_CONSOLE, shell=False)

    print("[4/6] place cmd left half")
    cmd_hwnd = None
    for _ in range(50):
        time.sleep(0.1)
        cmd_hwnd, _ = find_window(lambda t: TITLE in t)
        if cmd_hwnd:
            break
    if not cmd_hwnd:
        print("  ERROR: cmd window not found")
        sys.exit(1)
    place(cmd_hwnd, 0, 0, HALF_W, SCREEN_H, topmost=True)
    time.sleep(0.6)

    print(f"[5/6] ffmpeg gdigrab {RECORD_S}s -> {OUT.name}")
    VIDEO_DIR.mkdir(exist_ok=True)
    log = (VIDEO_DIR / "ffmpeg_live.log").open("w")
    proc = subprocess.Popen(
        [
            "ffmpeg", "-y",
            "-f", "gdigrab", "-framerate", "30", "-t", str(RECORD_S),
            "-i", "desktop",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p",
            str(OUT),
        ],
        stdout=log, stderr=log,
    )

    print("[6/6] recording...")
    rc = proc.wait()
    log.close()
    print(f"  ffmpeg rc={rc}")
    unset_topmost(cmd_hwnd)
    unset_topmost(chrome_hwnd)
    user32.PostMessageW(cmd_hwnd, 0x0010, 0, 0)
    print(f"\n  → {OUT}  ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
