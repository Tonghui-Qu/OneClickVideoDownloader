#!/usr/bin/python3
"""Native messaging host for OneClick Downloader.

Chrome launches this script on demand and exchanges messages over
stdin/stdout using the native messaging protocol:
a 4-byte little-endian length header followed by a UTF-8 JSON body.
"""

import base64
import datetime
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
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


def probe(url):
    """Checks whether a URL has a downloadable video, without downloading it.
    Returns the title and a thumbnail (as a base64 data URI, so the popup can
    show it regardless of CORS / login requirements)."""
    ytdlp = find_ytdlp()
    if not ytdlp:
        return {"ok": False, "error": "yt-dlp not found"}

    env = build_env()
    tmp = tempfile.mkdtemp(prefix="ocvd-probe-")
    try:
        cmd = [ytdlp]
        ffmpeg = shutil.which("ffmpeg", path=env["PATH"])
        if ffmpeg:
            cmd += ["--ffmpeg-location", str(Path(ffmpeg).parent)]
        cmd += [
            "--no-warnings",
            "--skip-download",
            "--no-simulate",  # --print alone implies simulate, which skips --write-thumbnail
            "--no-playlist",
            "--playlist-items", "1",
            "--write-thumbnail",
            "--convert-thumbnails", "jpg",
            "--cookies-from-browser", "chrome",
            "-P", tmp,
            "-o", "%(id)s.%(ext)s",
            "--print", "%(title)s",
            url,
        ]
        try:
            result = subprocess.run(cmd, env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=90)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "Timed out while checking"}

        title = ""
        if result.stdout:
            lines = result.stdout.decode("utf-8", "replace").strip().splitlines()
            if lines:
                title = lines[0]

        jpgs = sorted(Path(tmp).glob("*.jpg"))
        if result.returncode == 0 and jpgs:
            data = base64.b64encode(jpgs[0].read_bytes()).decode("ascii")
            return {"ok": True, "title": title,
                    "thumb": "data:image/jpeg;base64," + data}

        # Extraction succeeded but no thumbnail, or extraction failed.
        if result.returncode == 0:
            return {"ok": True, "title": title, "thumb": None}
        err = result.stderr.decode("utf-8", "replace")
        return {"ok": False, "error": err[-300:] or "No video found"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# yt-dlp is told to emit progress lines in this parseable form via
# --progress-template. Fields: percent | speed | eta | total size.
PROGRESS_MARKER = "__OCVD__"
PROGRESS_RE = re.compile(re.escape(PROGRESS_MARKER) + r"(.*?)\|(.*?)\|(.*?)\|(.*)")

# Post-download stages (merge/embed) that have no percentage of their own.
STAGE_PREFIXES = ("[Merger]", "[EmbedThumbnail]", "[Metadata]", "[VideoConvertor]", "[ExtractAudio]")


def download(url, requested_dir=None):
    """Runs yt-dlp and streams progress frames, then a final 'done' frame."""
    ytdlp = find_ytdlp()
    if not ytdlp:
        send_message({"type": "done", "success": False,
                      "error": "yt-dlp not found. Run: brew install yt-dlp"})
        return

    target_dir, fell_back = resolve_dir(requested_dir)
    env = build_env()

    cmd = [ytdlp]
    ffmpeg = shutil.which("ffmpeg", path=env["PATH"])
    if ffmpeg:
        cmd += ["--ffmpeg-location", str(Path(ffmpeg).parent)]
    cmd += [
        "--newline",
        "--progress-template",
        (PROGRESS_MARKER + "%(progress._percent_str)s|%(progress._speed_str)s"
         "|%(progress._eta_str)s|%(progress._total_bytes_str)s"),
        "--cookies-from-browser", "chrome",
        # Prefer the highest-resolution stream, but avoid the AV1 codec: some
        # sites (notably Bilibili) serve AV1 from a broken CDN node that stalls
        # mid-download ("0 bytes read, N more expected"). H.265/H.264 at the
        # same resolution download reliably. AV1 is kept only as a last resort.
        "-f", "bv*[vcodec!*=av01]+ba/b[vcodec!*=av01]/bv*+ba/b",
        "--merge-output-format", "mp4",
        "--embed-thumbnail",
        "--embed-metadata",
        "-P", target_dir,
        url,
    ]

    log(f"RUN: {' '.join(cmd)}")

    # Stream output line by line so we can forward progress live. stderr is
    # merged into stdout; we keep the last lines around for error reporting.
    proc = subprocess.Popen(
        cmd, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )

    tail = []
    last_sent = 0.0
    for line in proc.stdout:
        line = line.rstrip("\n")
        if PROGRESS_MARKER in line:
            m = PROGRESS_RE.search(line)
            if m:
                now = time.time()
                # Throttle to ~4 updates/sec to avoid flooding the channel.
                if now - last_sent >= 0.25:
                    last_sent = now
                    send_message({
                        "type": "progress",
                        "percent": m.group(1).strip(),
                        "speed": m.group(2).strip(),
                        "eta": m.group(3).strip(),
                        "total": m.group(4).strip(),
                    })
            continue

        tail.append(line)
        if len(tail) > 50:
            tail.pop(0)
        if line.startswith(STAGE_PREFIXES):
            send_message({"type": "status", "stage": "Processing…"})

    proc.wait()

    if proc.returncode == 0:
        send_message({"type": "done", "success": True,
                      "savedTo": target_dir, "fellBack": fell_back})
    else:
        err = "\n".join(tail)
        log("ERROR:\n" + err)
        send_message({"type": "done", "success": False,
                      "error": err[-500:] or "Download failed"})


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

        if msg.get("action") == "probe":
            send_message(probe((msg.get("url") or "").strip()))
            return

        url = (msg.get("url") or "").strip()
        if not url:
            send_message({"type": "done", "success": False, "error": "Missing URL"})
            return
        download(url, msg.get("dir"))
    except Exception as e:  # noqa: BLE001
        log("EXCEPTION:\n" + traceback.format_exc())
        send_message({"type": "done", "success": False, "error": str(e)})


if __name__ == "__main__":
    main()
