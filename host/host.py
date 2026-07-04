#!/usr/bin/python3
"""Native messaging host for OneClick Downloader.

Chrome launches this script on demand and exchanges messages over
stdin/stdout using the native messaging protocol:
a 4-byte little-endian length header followed by a UTF-8 JSON body.
"""

import base64
import datetime
import html as html_mod
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
import urllib.parse
import urllib.request
from pathlib import Path

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0.0.0 Safari/537.36")

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


def normalize_url(url):
    """Rewrites known SPA/feed URLs into the canonical form yt-dlp understands.

    Douyin's web player shows a video inside a modal on top of a feed page
    (e.g. /jingxuan, /discover, /user/..., /follow), keeping the feed URL and
    stashing the real video id in a ?modal_id=... query param. yt-dlp's Douyin
    extractor only matches /video/<id>, so the raw feed URL is "Unsupported".
    We pull the id out and hand yt-dlp the /video/<id> URL it expects."""
    try:
        parts = urllib.parse.urlparse(url)
    except Exception:
        return url
    host = parts.netloc.lower()
    if host.endswith("douyin.com"):
        # Already a canonical video/note URL -> leave it alone.
        if re.match(r"^/(?:video|note)/\d+", parts.path):
            return url
        qs = urllib.parse.parse_qs(parts.query)
        modal = (qs.get("modal_id") or [None])[0]
        if modal and modal.isdigit():
            return f"https://www.douyin.com/video/{modal}"
    return url


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


# When the dedicated extractor refuses or doesn't exist, these markers in the
# error output tell us it's worth retrying with the generic extractor, which
# scrapes the page for a plain <video>/mp4/m3u8 source. yt-dlp deliberately
# blocks some sites (e.g. it labels them "[Piracy]"); forcing the generic
# extractor bypasses that URL-matched block for pages that embed a real file.
GENERIC_FALLBACK_MARKERS = (
    "This website is no longer supported",
    "Unsupported URL",
    "Piracy",
    "primarily used for piracy",
)


def wants_generic_fallback(err_text):
    return any(m in err_text for m in GENERIC_FALLBACK_MARKERS)


# --- Site-specific resolvers -------------------------------------------------
# Some sites hide the real media URL behind obfuscated JavaScript and serve it
# from a rotating CDN domain, so neither the dedicated nor the generic yt-dlp
# extractor finds it. For those we scrape the page ourselves and hand yt-dlp the
# resolved direct URL (which it downloads normally, with progress).

def is_91porn(url):
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return False
    return "91porn" in host


def resolve_91porn(url):
    """Returns {"url": direct_mp4, "title": str, "referer": page_url} or None.

    The page embeds the real <source> inside strencode2("%xx%xx...") which is
    just URL-encoded HTML; the visible ccm.* link in the markup is a dead
    decoy inside an HTML comment."""
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": BROWSER_UA, "Referer": url})
        page = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
    except Exception as e:
        log(f"resolve_91porn fetch failed: {e}")
        return None

    m = re.search(r'strencode2\("([^"]+)"\)', page)
    if not m:
        return None
    decoded = urllib.parse.unquote(m.group(1))
    srcs = re.findall(r'src=[\'"]([^\'"]+\.mp4[^\'"]*)[\'"]', decoded)
    if not srcs:
        return None

    title = ""
    tm = re.search(r'<title>(.*?)</title>', page, re.S)
    if tm:
        title = _clean_91_title(tm.group(1))

    # Thumbnail: prefer the social-share image, then a <video poster=...>.
    thumb_url = None
    om = (re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', page, re.I)
          or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', page, re.I))
    if om:
        thumb_url = html_mod.unescape(om.group(1))
    if not thumb_url:
        pm = re.search(r'poster=[\'"]([^\'"]+)[\'"]', decoded + page)
        if pm:
            thumb_url = html_mod.unescape(pm.group(1))

    return {"url": srcs[0], "title": title, "referer": url,
            "thumb_url": thumb_url, "prefer_browser_title": True}


def _clean_91_title(raw):
    title = _unescape_all(raw).strip()
    return re.sub(r'\s*-\s*(?:\d+\s*)?91porn.*$', '', title, flags=re.I).strip()


def _fetch_thumb_data_uri(thumb_url, referer=None):
    """Downloads an image and returns it as a base64 data URI, so the popup can
    show it without hitting CORS / hotlink protection. Returns None on failure."""
    try:
        headers = {"User-Agent": BROWSER_UA}
        if referer:
            headers["Referer"] = referer
        req = urllib.request.Request(thumb_url, headers=headers)
        resp = urllib.request.urlopen(req, timeout=20)
        data = resp.read(5_000_000)
        ctype = resp.headers.get("Content-Type", "").split(";")[0].strip()
        if not ctype.startswith("image/"):
            ext = Path(urllib.parse.urlparse(thumb_url).path).suffix.lower()
            ctype = {".png": "image/png", ".webp": "image/webp",
                     ".gif": "image/gif"}.get(ext, "image/jpeg")
        return "data:%s;base64,%s" % (ctype, base64.b64encode(data).decode("ascii"))
    except Exception as e:
        log(f"thumb fetch failed: {e}")
        return None


MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) "
             "Version/15.0 Mobile/15E148 Safari/604.1")


def is_douyin(url):
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return False
    return host.endswith("douyin.com")


def _douyin_aweme_id(url):
    """Extracts the numeric aweme (video) id from a Douyin URL, following the
    v.douyin.com short-link redirect when necessary."""
    parts = urllib.parse.urlparse(url)
    m = re.search(r"/(?:video|note)/(\d+)", parts.path)
    if m:
        return m.group(1)
    modal = urllib.parse.parse_qs(parts.query).get("modal_id", [None])[0]
    if modal and modal.isdigit():
        return modal
    # Short links (v.douyin.com/XXXX) redirect to the canonical page.
    if parts.netloc.lower().startswith("v.douyin.com"):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": MOBILE_UA})
            final = urllib.request.urlopen(req, timeout=30).geturl()
            m = re.search(r"/(?:video|note)/(\d+)", final)
            if m:
                return m.group(1)
            m = re.search(r"[?&]modal_id=(\d+)", final)
            if m:
                return m.group(1)
        except Exception as e:
            log(f"douyin short-link resolve failed: {e}")
    return None


def resolve_douyin(url):
    """Returns {"url": direct_mp4, "title": str, "referer": ...} or None.

    yt-dlp's Douyin extractor is broken (it needs a JS-signed `a_bogus` param
    that browser cookies alone can't satisfy). Instead we hit the public share
    page, which embeds the full video metadata in a `window._ROUTER_DATA` JSON
    blob — no signature required — and pull the direct play_addr URL from it."""
    aweme_id = _douyin_aweme_id(url)
    if not aweme_id:
        return None

    share_url = f"https://www.iesdouyin.com/share/video/{aweme_id}/"
    try:
        req = urllib.request.Request(
            share_url, headers={"User-Agent": MOBILE_UA,
                                "Referer": "https://www.douyin.com/"})
        page = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
    except Exception as e:
        log(f"resolve_douyin fetch failed: {e}")
        return None

    m = re.search(r'window\._ROUTER_DATA\s*=\s*(\{.*?\})</script>', page, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
        info = data["loaderData"]["video_(id)/page"].get("videoInfoRes", {})
        items = info.get("item_list") or []
        if not items:
            log(f"resolve_douyin: empty item_list (filter={info.get('filter_list')})")
            return None
        item = items[0]
        video = item.get("video", {})
        urls = (video.get("play_addr", {}) or {}).get("url_list") or []
        if not urls:
            return None
        # Swap the watermarked endpoint for the clean one (smaller, no logo).
        media_url = urls[0].replace("/playwm/", "/play/")

        thumb_url = None
        cover = (video.get("cover") or video.get("origin_cover") or {}).get("url_list") or []
        # Prefer a plain jpeg cover (some entries lead with .webp).
        for c in cover:
            if ".jpeg" in c or ".jpg" in c:
                thumb_url = c
                break
        if not thumb_url and cover:
            thumb_url = cover[0]

        return {
            "url": media_url,
            "title": (item.get("desc") or "").strip(),
            "referer": "https://www.douyin.com/",
            "thumb_url": thumb_url,
        }
    except Exception as e:
        log(f"resolve_douyin parse failed: {e}")
        return None


def resolve_special(url):
    """Dispatches to a site-specific resolver. Returns a dict or None."""
    if is_91porn(url):
        return resolve_91porn(url)
    if is_douyin(url):
        return resolve_douyin(url)
    return None


def special_title(special, browser_title):
    """Picks the best title for a self-resolved site. Some sites (91porn) show a
    localized title in the tab that beats our cookieless fetch; others (Douyin)
    put the uploader — not the video caption — in the tab, so the resolver's own
    title wins there."""
    if special.get("prefer_browser_title") and browser_title:
        return _clean_91_title(browser_title)
    return special.get("title") or (browser_title or "")


def _unescape_all(text):
    """HTML-unescapes repeatedly to handle double-encoded entities
    (e.g. '&amp;quot;' -> '&quot;' -> '\"')."""
    for _ in range(3):
        new = html_mod.unescape(text)
        if new == text:
            break
        text = new
    return text


def _safe_filename(name):
    """Strips characters that are illegal in filenames; keeps it readable."""
    name = re.sub(r'[\\/:*?"<>|\n\r\t]+', " ", name).strip()
    return (name or "video")[:150]


def probe(url, browser_title=None):
    """Checks whether a URL has a downloadable video, without downloading it.
    Returns the title and a thumbnail (as a base64 data URI, so the popup can
    show it regardless of CORS / login requirements)."""
    ytdlp = find_ytdlp()
    if not ytdlp:
        return {"ok": False, "error": "yt-dlp not found"}

    url = normalize_url(url)

    # Sites we resolve ourselves: if we can find the real media URL, the video
    # is downloadable even though yt-dlp's own extractors can't see it.
    special = resolve_special(url)
    if special:
        title = special_title(special, browser_title)
        thumb = None
        if special.get("thumb_url"):
            thumb = _fetch_thumb_data_uri(special["thumb_url"], referer=url)
        return {"ok": True, "title": title, "thumb": thumb}

    env = build_env()

    def attempt(force_generic):
        tmp = tempfile.mkdtemp(prefix="ocvd-probe-")
        try:
            cmd = [ytdlp]
            ffmpeg = shutil.which("ffmpeg", path=env["PATH"])
            if ffmpeg:
                cmd += ["--ffmpeg-location", str(Path(ffmpeg).parent)]
            if force_generic:
                cmd += ["--force-generic-extractor"]
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
            return {"ok": False, "error": err or "No video found"}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    res = attempt(force_generic=False)
    if not res.get("ok") and wants_generic_fallback(res.get("error", "")):
        log("probe: retrying with --force-generic-extractor")
        generic = attempt(force_generic=True)
        if generic.get("ok"):
            return generic
    if not res.get("ok"):
        res["error"] = (res.get("error") or "No video found")[-300:]
    return res


# yt-dlp is told to emit progress lines in this parseable form via
# --progress-template. Fields: percent | speed | eta | total size.
PROGRESS_MARKER = "__OCVD__"
PROGRESS_RE = re.compile(re.escape(PROGRESS_MARKER) + r"(.*?)\|(.*?)\|(.*?)\|(.*)")

# Post-download stages (merge/embed) that have no percentage of their own.
STAGE_PREFIXES = ("[Merger]", "[EmbedThumbnail]", "[Metadata]", "[VideoConvertor]", "[ExtractAudio]")


def download(url, requested_dir=None, browser_title=None):
    """Runs yt-dlp and streams progress frames, then a final 'done' frame."""
    ytdlp = find_ytdlp()
    if not ytdlp:
        send_message({"type": "done", "success": False,
                      "error": "yt-dlp not found. Run: brew install yt-dlp"})
        return

    url = normalize_url(url)

    target_dir, fell_back = resolve_dir(requested_dir)
    env = build_env()

    # For sites we resolve ourselves, hand yt-dlp the direct media URL (plus the
    # page as Referer) instead of the original page URL.
    special = resolve_special(url)
    dl_url = special["url"] if special else url
    referer = special.get("referer") if special else None
    out_tmpl = "%(title)s.%(ext)s"
    if special:
        # Name the file after the best available title (see special_title).
        name = special_title(special, browser_title)
        if name:
            out_tmpl = _safe_filename(name) + ".%(ext)s"

    def run_attempt(force_generic):
        """Runs one yt-dlp download, streaming progress. Returns (returncode,
        tail_lines)."""
        cmd = [ytdlp]
        ffmpeg = shutil.which("ffmpeg", path=env["PATH"])
        if ffmpeg:
            cmd += ["--ffmpeg-location", str(Path(ffmpeg).parent)]
        if force_generic:
            cmd += ["--force-generic-extractor"]
        if referer:
            cmd += ["--add-header", f"Referer: {referer}"]
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
            "-o", out_tmpl,
            dl_url,
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
        return proc.returncode, tail

    returncode, tail = run_attempt(force_generic=False)

    # Some sites are blocked by / missing a dedicated extractor. Retry once with
    # the generic extractor, which can grab a plain embedded mp4/m3u8.
    if returncode != 0 and wants_generic_fallback("\n".join(tail)):
        log("download: retrying with --force-generic-extractor")
        send_message({"type": "status", "stage": "Retrying…"})
        returncode, tail = run_attempt(force_generic=True)

    if returncode == 0:
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
            send_message(probe((msg.get("url") or "").strip(), msg.get("title")))
            return

        url = (msg.get("url") or "").strip()
        if not url:
            send_message({"type": "done", "success": False, "error": "Missing URL"})
            return
        download(url, msg.get("dir"), msg.get("title"))
    except Exception as e:  # noqa: BLE001
        log("EXCEPTION:\n" + traceback.format_exc())
        send_message({"type": "done", "success": False, "error": str(e)})


if __name__ == "__main__":
    main()
