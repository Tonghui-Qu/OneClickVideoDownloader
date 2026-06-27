#!/usr/bin/python3
"""Native messaging host for OneClick Downloader.

Chrome launches this script on demand and exchanges messages over
stdin/stdout using the native messaging protocol:
a 4-byte little-endian length header followed by a UTF-8 JSON body.
"""

import datetime
import json
import os
import shutil
import struct
import subprocess
import sys
import traceback
from pathlib import Path

# Dual log locations: /tmp always works regardless of HOME; the home
# copy is convenient. Boot line is written the instant we start so we
# can tell whether Chrome executed this script at all.
BOOT_LOG = "/tmp/oneclick-downloader.log"


def log(message):
    line = datetime.datetime.now().strftime("%H:%M:%S ") + str(message).rstrip() + "\n"
    for target in (BOOT_LOG, str(Path.home() / "oneclick-downloader.log")):
        try:
            with open(target, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass


try:
    log(f"BOOT argv={sys.argv} home={os.environ.get('HOME')} cwd={os.getcwd()} py={sys.version.split()[0]}")
except Exception:
    pass

DOWNLOAD_DIR = str(Path.home() / "Downloads")

# Chrome spawns this process with an essentially empty PATH, so we must
# rebuild it to include BOTH Homebrew tools (yt-dlp, deno, ffmpeg) AND
# system tools. In particular yt-dlp needs /usr/bin/security to read the
# Chrome Safe Storage key from the Keychain and decrypt cookies.
EXTRA_PATHS = [
    "/opt/homebrew/bin",
    "/usr/local/bin",
    str(Path.home() / ".deno/bin"),
    str(Path.home() / ".local/bin"),
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
]

YTDLP_CANDIDATES = [
    "/opt/homebrew/bin/yt-dlp",
    "/usr/local/bin/yt-dlp",
    str(Path.home() / ".local/bin/yt-dlp"),
]


def build_env():
    env = os.environ.copy()
    existing = env.get("PATH", "")
    env["PATH"] = os.pathsep.join([p for p in EXTRA_PATHS if Path(p).is_dir()] + [existing])
    return env


def find_ytdlp():
    found = shutil.which("yt-dlp")
    if found:
        return found
    for candidate in YTDLP_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None


def read_message():
    raw_len = sys.stdin.buffer.read(4)
    if len(raw_len) < 4:
        return None
    length = struct.unpack("=I", raw_len)[0]
    data = sys.stdin.buffer.read(length).decode("utf-8")
    return json.loads(data)


def send_message(obj):
    try:
        data = json.dumps(obj).encode("utf-8")
        sys.stdout.buffer.write(struct.pack("=I", len(data)))
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        log(f"SENT: {obj}")
    except Exception:
        log("SEND FAILED:\n" + traceback.format_exc())


def resolve_dir(requested):
    """Pick the download directory. Falls back to ~/Downloads when the
    requested folder is missing (e.g. deleted, renamed, or on an unmounted
    drive). Returns (path, fell_back)."""
    if requested:
        p = Path(os.path.expanduser(requested))
        if p.is_dir():
            return str(p), False
    return DOWNLOAD_DIR, bool(requested)


def pick_folder():
    """Show a native macOS folder chooser and return the selected path."""
    env = build_env()
    script = 'POSIX path of (choose folder with prompt "Select a download folder")'
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180,
        )
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": str(e)}

    if result.returncode == 0:
        path = result.stdout.decode("utf-8").strip().rstrip("/")
        return {"success": True, "path": path}

    err = result.stderr.decode("utf-8", "replace")
    if "User canceled" in err or "-128" in err:
        return {"success": False, "canceled": True}
    return {"success": False, "error": err.strip() or "Folder picker failed"}


def download(url, requested_dir=None):
    ytdlp = find_ytdlp()
    if not ytdlp:
        return {"success": False, "error": "yt-dlp not found. Run: pip install -U yt-dlp"}

    target_dir, fell_back = resolve_dir(requested_dir)
    env = build_env()

    cmd = [ytdlp]
    ffmpeg = shutil.which("ffmpeg", path=env["PATH"])
    if ffmpeg:
        cmd += ["--ffmpeg-location", str(Path(ffmpeg).parent)]
    cmd += [
        "--cookies-from-browser", "chrome",
        "-f", "bv*+ba/b",
        "--merge-output-format", "mp4",
        "--embed-thumbnail",
        "--embed-metadata",
        "-P", target_dir,
        url,
    ]

    log(f"RUN: {' '.join(cmd)}")

    # Capture output so it never leaks onto stdout (which is the
    # native messaging channel and must carry only framed JSON).
    result = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    if result.returncode == 0:
        return {
            "success": True,
            "message": "Finished",
            "savedTo": target_dir,
            "fellBack": fell_back,
        }

    err = result.stderr.decode("utf-8", "replace")
    log("ERROR:\n" + err)
    return {"success": False, "error": err[-500:] or "Download failed"}


def main():
    try:
        log(f"=== invoked: python={sys.version.split()[0]} exe={sys.executable} ===")
        msg = read_message()
        log(f"received message: {msg}")
        if msg is None:
            return

        if msg.get("action") == "pickFolder":
            send_message(pick_folder())
            return

        url = (msg.get("url") or "").strip()
        if not url:
            send_message({"success": False, "error": "Missing URL"})
            return
        send_message(download(url, msg.get("dir")))
    except Exception as e:  # noqa: BLE001
        log("EXCEPTION:\n" + traceback.format_exc())
        send_message({"success": False, "error": str(e)})


if __name__ == "__main__":
    main()
