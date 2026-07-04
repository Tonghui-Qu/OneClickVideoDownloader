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
import signal
import struct
import subprocess
import sys
import tempfile
import threading
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
    # Weibo plays a single video in a modal on top of a profile/feed page, keeping
    # the profile URL (e.g. /u/<uid>?...&layerid=<mid>). Handed that URL, yt-dlp's
    # WeiboUser extractor would enumerate and download the *whole* profile's videos
    # one after another. The layerid is the status mid, so rewrite it to the
    # single-video URL m.weibo.cn/status/<mid> that yt-dlp treats as one video.
    if host.endswith("weibo.com") or host.endswith("weibo.cn"):
        qs = urllib.parse.parse_qs(parts.query)
        layer = (qs.get("layerid") or [None])[0]
        if layer and layer.isdigit():
            return f"https://m.weibo.cn/status/{layer}"
    return url


def _start_control_reader(canceled, cleanup, proc_holder):
    """Watches stdin in the background during a download and stops yt-dlp when
    asked. Two flavors of stop:

    - Pause (Chrome closes the port -> EOF, or an explicit "pause"/"stop"): the
      partial `.part` file is kept so a later run can resume it with --continue.
    - Cancel (an explicit {"action":"cancel","cleanup":true} message sent just
      before the port is dropped): also flag `cleanup` so the leftover partial
      files get deleted once yt-dlp has exited."""
    def stop_proc():
        proc = proc_holder.get("proc")
        if proc and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass

    def reader():
        while True:
            try:
                msg = read_message()
            except Exception:
                msg = None
            if msg is None:
                canceled.set()
                stop_proc()
                return
            if isinstance(msg, dict) and msg.get("action") in ("cancel", "pause", "stop"):
                if msg.get("action") == "cancel" and msg.get("cleanup"):
                    cleanup.set()
                canceled.set()
                stop_proc()
                return
    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    return thread


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

# Desktop UA for ffmpeg/ffprobe fetches: the media was requested by desktop
# Chrome, and some CDNs (e.g. Cloudflare-fronted ones) reject a mobile UA.
DESKTOP_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/125.0.0.0 Safari/537.36")


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


def _remote_size(url, referer=None, timeout=15):
    """Byte size of a remote file via a 1-byte Range request (reads the total
    from Content-Range, falling back to Content-Length). Returns None on failure.
    Used when a site's metadata blob omits the size (e.g. some Douyin videos)."""
    try:
        headers = {"User-Agent": DESKTOP_UA, "Range": "bytes=0-0"}
        if referer:
            headers["Referer"] = referer
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            cr = resp.headers.get("Content-Range")  # e.g. "bytes 0-0/1234567"
            if cr and "/" in cr:
                total = cr.rsplit("/", 1)[1].strip()
                if total.isdigit():
                    return int(total)
            cl = resp.headers.get("Content-Length")
            if cl and cl.isdigit():
                return int(cl)
    except Exception as e:
        log(f"_remote_size failed: {e}")
    return None


def _ffprobe_info(url, referer=None, timeout=20):
    """Reads width/height/fps from a remote video with ffprobe (only the
    container header is needed, so it's quick for clips with a front-loaded
    moov). Returns a dict with any of {width, height, fps}. Used to fill in
    resolution/fps a site's metadata blob or yt-dlp couldn't provide."""
    env = build_env()
    ffprobe = shutil.which("ffprobe", path=env["PATH"])
    if not ffprobe:
        return {}
    cmd = [ffprobe, "-v", "quiet", "-user_agent", DESKTOP_UA]
    if referer:
        cmd += ["-headers", f"Referer: {referer}\r\n"]
    # nokey output preserves the show_entries order: width, height, r_frame_rate.
    cmd += ["-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate",
            "-of", "default=nw=1:nk=1", url]
    try:
        out = subprocess.run(cmd, env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=timeout)
        vals = out.stdout.decode("utf-8", "replace").strip().splitlines()
        info = {}
        if len(vals) >= 1 and vals[0].strip().isdigit():
            info["width"] = int(vals[0].strip())
        if len(vals) >= 2 and vals[1].strip().isdigit():
            info["height"] = int(vals[1].strip())
        if len(vals) >= 3:
            r = vals[2].strip()  # e.g. "30/1"
            try:
                if "/" in r:
                    num, den = r.split("/", 1)
                    den = float(den)
                    if den:
                        info["fps"] = float(num) / den
                else:
                    info["fps"] = float(r)
            except ValueError:
                pass
        return info
    except Exception as e:
        log(f"_ffprobe_info failed: {e}")
        return {}


def _frame_thumb_data_uri(url, referer=None, timeout=30):
    """Extracts a single frame from a video with ffmpeg and returns it as a
    base64 JPEG data URI, for previewing sniffed streams that carry no cover
    image of their own. Returns None on failure."""
    env = build_env()
    ffmpeg = shutil.which("ffmpeg", path=env["PATH"])
    if not ffmpeg:
        return None
    tmp = tempfile.mkdtemp(prefix="ocvd-frame-")
    out = os.path.join(tmp, "frame.jpg")
    try:
        cmd = [ffmpeg, "-v", "quiet", "-user_agent", DESKTOP_UA]
        if referer:
            cmd += ["-headers", f"Referer: {referer}\r\n"]
        cmd += ["-ss", "0.5", "-i", url, "-frames:v", "1",
                "-vf", "scale='min(640,iw)':-2", "-q:v", "4", "-y", out]
        subprocess.run(cmd, env=env, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=timeout)
        if os.path.exists(out) and os.path.getsize(out) > 0:
            data = base64.b64encode(Path(out).read_bytes()).decode("ascii")
            return "data:image/jpeg;base64," + data
    except Exception as e:
        log(f"_frame_thumb failed: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
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

        # The blob already carries dimensions/fps/size, so we can show the same
        # resolution line the yt-dlp path shows — no extra probe needed. fps and
        # byte size live in the per-quality bit_rate[] entries, not play_addr.
        w = video.get("width")
        h = video.get("height")
        res = f"{w}x{h}" if w and h else ""
        bit_rates = video.get("bit_rate") or []
        first_br = bit_rates[0] if bit_rates else {}
        fps = first_br.get("FPS") or video.get("fps")
        data_size = (video.get("play_addr", {}) or {}).get("data_size") \
            or (first_br.get("play_addr", {}) or {}).get("data_size")
        # This share-page blob often omits size/fps: fetch them from the file
        # itself (cheap: a Range request for size, ffprobe header read for fps).
        if not data_size:
            data_size = _remote_size(media_url, referer="https://www.douyin.com/")
        if not fps:
            fps = _ffprobe_info(media_url, referer="https://www.douyin.com/").get("fps")
        log(f"resolve_douyin meta: res={res} fps={fps} size={data_size} "
            f"br_count={len(bit_rates)}")
        meta = _fmt_meta(res, fps, data_size)

        return {
            "url": media_url,
            "title": (item.get("desc") or "").strip(),
            "referer": "https://www.douyin.com/",
            "thumb_url": thumb_url,
            "meta": meta,
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


def _strip_hashtags(text):
    """Removes social hashtags from a title so they don't clutter filenames.
    Handles both the trailing/inline '#tag' form and the wrapped '#话题#' form
    (Douyin/Weibo). Leaves the rest of the caption intact."""
    if not text:
        return text
    text = re.sub(r'#[^#\s]+#', " ", text)   # wrapped:  #话题#
    text = re.sub(r'#\S+', " ", text)         # inline/trailing:  #jiojio
    return re.sub(r'\s+', " ", text).strip()


def _safe_filename(name):
    """Strips characters that are illegal in filenames; keeps it readable."""
    name = re.sub(r'[\\/:*?"<>|\n\r\t]+', " ", name).strip()
    return (name or "video")[:150]


def _human_size(n):
    """Bytes -> human string using decimal units (MB/GB). Returns None for
    missing/zero/unparseable values."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            # GB/TB keep one decimal; smaller units are shown as whole numbers.
            return f"{n:.1f}{unit}" if unit in ("GB", "TB") else f"{int(round(n))}{unit}"
        n /= 1000


def _fmt_meta(resolution, fps, filesize=None, filesize_approx=None):
    """Formats the '1920×1080 · 60fps · 478.4MB' line shown under the title.
    Missing or unknown ('NA') parts are simply dropped."""
    parts = []
    if resolution and "x" in resolution.lower():
        w, _, h = resolution.lower().partition("x")
        try:
            parts.append(f"{int(w)}×{int(h)}")
        except ValueError:
            pass
    try:
        f = float(fps)
        if f > 0:
            parts.append(f"{int(round(f))}fps")
    except (TypeError, ValueError):
        pass
    size = _human_size(filesize) or _human_size(filesize_approx)
    if size:
        parts.append(size)
    return " · ".join(parts)


# Marker prefixes for the metadata / direct-URL print lines, so we can pick them
# out of yt-dlp's stdout regardless of what the title contains.
META_MARKER = "OCVDMETA|"
URL_MARKER = "OCVDURL|"


def _estimate_size(tbr, duration):
    """Estimates a byte size from total bitrate (kbps) × duration (s), for
    streaming formats (HLS/DASH) whose manifests carry no exact size. Returns an
    int or None when either input is missing/unparseable."""
    try:
        kbps = float(tbr)
        secs = float(duration)
    except (TypeError, ValueError):
        return None
    if kbps <= 0 or secs <= 0:
        return None
    return int(kbps * 1000 / 8 * secs)


def _res_height(res):
    """Pulls the pixel height out of a 'WIDTHxHEIGHT' resolution string, for
    ranking candidates by quality. Returns 0 when unknown."""
    if res and "x" in res.lower():
        try:
            return int(res.lower().split("x")[1])
        except (ValueError, IndexError):
            return 0
    return 0


def _probe_ytdlp(ytdlp, env, url, referer=None, force_generic=False, timeout=90,
                 frame_referer=None):
    """Runs one yt-dlp metadata probe on a URL (page, manifest, or direct file).
    Returns {ok, title, meta, thumb, height} on success, else {ok: False, error}.
    `height` is the pixel height of the selected stream, used to rank quality.
    `frame_referer` is sent when falling back to ffmpeg frame extraction (some
    CDNs, e.g. Weibo, 403 a frame grab that lacks the page Referer)."""
    tmp = tempfile.mkdtemp(prefix="ocvd-probe-")
    try:
        cmd = [ytdlp]
        ffmpeg = shutil.which("ffmpeg", path=env["PATH"])
        if ffmpeg:
            cmd += ["--ffmpeg-location", str(Path(ffmpeg).parent)]
        if force_generic:
            cmd += ["--force-generic-extractor"]
        if referer:
            cmd += ["--add-header", f"Referer: {referer}"]
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
            "--print", META_MARKER + "%(resolution)s|%(fps)s|%(filesize)s|"
            "%(filesize_approx)s|%(tbr)s|%(duration)s",
            "--print", URL_MARKER + "%(url)s",
            url,
        ]
        try:
            result = subprocess.run(cmd, env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "Timed out while checking"}

        title = ""
        res = fps = fsize = fapprox = tbr = dur = ""
        media_url = ""
        if result.stdout:
            lines = result.stdout.decode("utf-8", "replace").strip().splitlines()
            non_meta = []
            for ln in lines:
                if ln.startswith(META_MARKER):
                    bits = ln[len(META_MARKER):].split("|")
                    res = bits[0] if len(bits) > 0 else ""
                    fps = bits[1] if len(bits) > 1 else ""
                    fsize = bits[2] if len(bits) > 2 else ""
                    fapprox = bits[3] if len(bits) > 3 else ""
                    tbr = bits[4] if len(bits) > 4 else ""
                    dur = bits[5] if len(bits) > 5 else ""
                elif ln.startswith(URL_MARKER):
                    media_url = ln[len(URL_MARKER):].strip()
                else:
                    non_meta.append(ln)
            if non_meta:
                title = non_meta[0]

        have_url = media_url and media_url not in ("NA", "None")
        # On the page-probe path (frame_referer set), some extractors omit
        # resolution/fps/size (e.g. Twitch, xvideos). Fill the gaps ourselves.
        # (Skipped for sniff candidates so we don't probe every one — sniff_probe
        # enriches only its chosen best.)
        if frame_referer:
            ref = referer or frame_referer
            need_res = not (res and "x" in res.lower())
            need_fps = not fps or fps in ("NA", "none", "None", "")
            if have_url and (need_res or need_fps):
                info = _ffprobe_info(media_url, referer=ref)
                if need_res and info.get("width") and info.get("height"):
                    res = f'{info["width"]}x{info["height"]}'
                if need_fps and info.get("fps"):
                    fps = str(info["fps"])
            if _human_size(fsize) is None and _human_size(fapprox) is None:
                # A direct file has an exact size; a manifest (HLS/DASH) doesn't,
                # so estimate it from the total bitrate × duration instead.
                if have_url and not _is_manifest_url(media_url):
                    rs = _remote_size(media_url, referer=ref)
                    if rs:
                        fsize = str(rs)
                if _human_size(fsize) is None:
                    est = _estimate_size(tbr, dur)
                    if est:
                        fsize = str(est)

        meta = _fmt_meta(res, fps, fsize, fapprox)
        height = _res_height(res)

        thumb = None
        jpgs = sorted(Path(tmp).glob("*.jpg"))
        if jpgs:
            data = base64.b64encode(jpgs[0].read_bytes()).decode("ascii")
            thumb = "data:image/jpeg;base64," + data

        if result.returncode == 0:
            # Some extractors (e.g. Weibo) return no thumbnail; grab a frame from
            # the stream itself so the preview still shows an image.
            if not thumb and media_url and media_url not in ("NA", "None"):
                thumb = _frame_thumb_data_uri(
                    media_url, referer=referer or frame_referer)
            return {"ok": True, "title": title, "meta": meta,
                    "thumb": thumb, "height": height}
        err = result.stderr.decode("utf-8", "replace")
        return {"ok": False, "error": err or "No video found"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def probe(url, browser_title=None):
    """Checks whether a URL has a downloadable video, without downloading it.
    Returns the title, a thumbnail (as a base64 data URI, so the popup can show
    it regardless of CORS / login requirements), and a resolution/fps line."""
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
        return {"ok": True, "title": title, "thumb": thumb,
                "meta": special.get("meta", "")}

    env = build_env()
    res = _probe_ytdlp(ytdlp, env, url, force_generic=False, frame_referer=url)
    if not res.get("ok") and wants_generic_fallback(res.get("error", "")):
        log("probe: retrying with --force-generic-extractor")
        generic = _probe_ytdlp(ytdlp, env, url, force_generic=True, frame_referer=url)
        if generic.get("ok"):
            return generic
    if not res.get("ok"):
        res["error"] = (res.get("error") or "No video found")[-300:]
    return res


def _is_manifest_url(u):
    return bool(re.search(r'\.(m3u8|mpd)(\?|#|$)', u or "", re.I))


def sniff_probe(page_url, candidates, browser_title=None):
    """Fallback used when the page URL itself isn't a recognizable video: probe
    the media URLs the browser sniffed on that page and return the best one.

    Manifests are tried first because they carry the whole quality ladder, so
    yt-dlp can select the highest rendition from them — this is what lets the
    sniffing path still reach maximum quality, not just whatever segment played.
    Among successful probes we keep the highest resolution."""
    ytdlp = find_ytdlp()
    if not ytdlp:
        return {"ok": False, "error": "yt-dlp not found"}
    env = build_env()

    # Manifests first, then progressive files; cap the work so probing stays
    # snappy. The extension already orders these freshest-first, so a valid CDN
    # link is usually hit immediately; a short per-candidate timeout keeps a batch
    # of expired links from stalling the whole check.
    ordered = sorted(candidates or [], key=lambda u: 0 if _is_manifest_url(u) else 1)
    best = None
    for cand in ordered[:5]:
        r = _probe_ytdlp(ytdlp, env, cand, referer=page_url, timeout=20)
        log(f"sniff_probe candidate ok={r.get('ok')} h={r.get('height')} {cand[:120]}")
        if r.get("ok"):
            r["url"] = cand
            if best is None or r.get("height", 0) > best.get("height", 0):
                best = r
    if best:
        # The page's tab title is more meaningful than a CDN filename.
        if browser_title:
            best["title"] = browser_title
        # Raw progressive CDN URLs give yt-dlp no thumbnail or dimensions, so
        # derive them straight from the file: a frame for the preview image, and
        # ffprobe + a size request for the resolution/fps/size line.
        info = _ffprobe_info(best["url"], referer=page_url)
        res = f'{info["width"]}x{info["height"]}' \
            if info.get("width") and info.get("height") else ""
        size = _remote_size(best["url"], referer=page_url)
        enriched = _fmt_meta(res, info.get("fps"), size)
        if enriched:
            best["meta"] = enriched
        if not best.get("thumb"):
            best["thumb"] = _frame_thumb_data_uri(best["url"], referer=page_url)
        return best
    return {"ok": False, "error": "No downloadable media detected"}


# yt-dlp is told to emit progress lines in this parseable form via
# --progress-template. Fields: percent | speed | eta | total size.
PROGRESS_MARKER = "__OCVD__"
PROGRESS_RE = re.compile(re.escape(PROGRESS_MARKER) + r"(.*?)\|(.*?)\|(.*?)\|(.*)")

# Post-download stages (merge/embed) that have no percentage of their own.
STAGE_PREFIXES = ("[Merger]", "[EmbedThumbnail]", "[Metadata]", "[VideoConvertor]", "[ExtractAudio]")

# Lines yt-dlp prints when it decides on an output file. We record these so a
# canceled download can delete exactly what it created (the streams, the merged
# file, the sidecar thumbnail, and any leftover .part/.ytdl fragments).
DEST_RE = re.compile(r'\[download\]\s+Destination:\s*(.+?)\s*$')
MERGE_RE = re.compile(r'Merging formats into\s+"(.+?)"\s*$')

# Suffixes of transient files that are safe to sweep away for a stem we own.
TEMP_SUFFIXES = (".part", ".ytdl", ".temp")
THUMB_SUFFIXES = (".webp", ".jpg", ".jpeg", ".png")


def _try_unlink(path, removed):
    try:
        if os.path.isfile(path):
            os.remove(path)
            removed.append(path)
    except Exception as e:  # noqa: BLE001
        log(f"cleanup: could not remove {path}: {e}")


def _stem_of(path):
    """Reduces an output path to its title stem: strips the extension and
    yt-dlp's per-format id (e.g. '/dir/Title.f251.webm' -> '/dir/Title')."""
    p = Path(path)
    name = re.sub(r'\.part$', '', p.name)
    name = re.sub(r'\.[^.]+$', '', name)   # extension
    name = re.sub(r'\.f\d+$', '', name)    # yt-dlp format id
    return str(p.parent / name)


def cleanup_partials(paths=None, stems=None):
    """Deletes partial-download artifacts. `paths` are exact files we recorded
    (streams / merged output); `stems` drive a scoped sweep for leftover
    .part/.ytdl/fragment files and sidecar thumbnails. Scoping to our own stems
    means we never touch unrelated files in the folder."""
    import glob as globmod
    removed = []
    for p in (paths or []):
        for cand in (p, p + ".part", p + ".ytdl"):
            _try_unlink(cand, removed)
        for frag in globmod.glob(globmod.escape(p) + ".part-Frag*"):
            _try_unlink(frag, removed)
    for stem in (stems or []):
        if not stem:
            continue
        base = globmod.escape(stem)
        for suf in TEMP_SUFFIXES + THUMB_SUFFIXES:
            for f in globmod.glob(base + "*" + suf):
                _try_unlink(f, removed)
        for f in globmod.glob(base + "*.part-Frag*"):
            _try_unlink(f, removed)
    return removed


def download(url, requested_dir=None, browser_title=None, req_referer=None,
             canceled=None, cleanup=None, proc_holder=None):
    """Runs yt-dlp and streams progress frames, then a final 'done' frame.

    `canceled` (threading.Event) lets the control reader stop us mid-download;
    `cleanup` (threading.Event) additionally means the user canceled and wants
    the partial files deleted (vs. a pause, which keeps them for resume);
    `proc_holder` is a dict the reader uses to reach the live subprocess."""
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
    # Referer: our own resolver knows the right one; otherwise use the page URL
    # the popup passed for a sniffed media/manifest download (CDNs often require it).
    referer = special.get("referer") if special else req_referer
    out_tmpl = "%(title)s.%(ext)s"
    if special:
        # Name the file after the best available title (see special_title).
        name = _strip_hashtags(special_title(special, browser_title))
        if name:
            out_tmpl = _safe_filename(name) + ".%(ext)s"
    elif req_referer and browser_title:
        # Sniffed download of a raw media/manifest URL: yt-dlp's title would be a
        # meaningless CDN filename, so name the file after the page title.
        out_tmpl = _safe_filename(_strip_hashtags(browser_title)) + ".%(ext)s"

    # Files/stems yt-dlp tells us it's creating, so a cancel can delete exactly
    # those (and their .part/.ytdl/thumbnail sidecars).
    created = set()
    stems = set()
    meta_sent = [False]

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
            # Only ever the single current video: never enumerate a channel/user/
            # feed page into a batch of downloads (e.g. a Weibo profile URL).
            "--no-playlist",
            # Keep the partial .part file and resume it on a later run — this is
            # what makes pause/resume work. (--continue is yt-dlp's default, but
            # we set it explicitly so intent is clear.)
            "--continue",
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
        # start_new_session gives the child its own process group so the control
        # reader can kill it (and any ffmpeg children) cleanly on pause/cancel.
        proc = subprocess.Popen(
            cmd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, start_new_session=True,
        )
        if proc_holder is not None:
            proc_holder["proc"] = proc

        tail = []
        last_sent = 0.0
        for line in proc.stdout:
            if canceled is not None and canceled.is_set():
                break
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

            # Record output files so a cancel can remove them. Also relay the
            # stem to the extension once, so it can clean up a *paused* download
            # later (when no host process is running to do it itself).
            md = DEST_RE.search(line) or MERGE_RE.search(line)
            if md:
                fp = md.group(1).strip()
                created.add(fp)
                stem = _stem_of(fp)
                stems.add(stem)
                if not meta_sent[0]:
                    meta_sent[0] = True
                    send_message({"type": "meta", "stem": stem})

            if line.startswith(STAGE_PREFIXES):
                send_message({"type": "status", "stage": "Processing…"})

        proc.wait()
        return proc.returncode, tail

    def _finish_canceled():
        """yt-dlp was stopped by the user. On a cancel we delete the leftovers;
        on a pause we keep the .part file so a later run can resume it."""
        if cleanup is not None and cleanup.is_set():
            removed = cleanup_partials(paths=created, stems=stems)
            log(f"download: canceled, removed {len(removed)} partial file(s): {removed}")
        else:
            log("download: paused, partial file kept for resume")

    returncode, tail = run_attempt(force_generic=False)

    # Stopped by the user (pause/cancel) or by Chrome closing the port: don't
    # report a bogus failure — the popup already knows the new state, and the
    # port is usually gone anyway.
    if canceled is not None and canceled.is_set():
        _finish_canceled()
        return

    # Some sites are blocked by / missing a dedicated extractor. Retry once with
    # the generic extractor, which can grab a plain embedded mp4/m3u8.
    if returncode != 0 and wants_generic_fallback("\n".join(tail)):
        log("download: retrying with --force-generic-extractor")
        send_message({"type": "status", "stage": "Retrying…"})
        returncode, tail = run_attempt(force_generic=True)
        if canceled is not None and canceled.is_set():
            _finish_canceled()
            return

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

        # Fallback probe over the media URLs the browser sniffed on the page.
        if msg.get("action") == "sniffProbe":
            send_message(sniff_probe((msg.get("url") or "").strip(),
                                     msg.get("candidates") or [], msg.get("title")))
            return

        # Delete leftovers for a canceled download that has no running host
        # process (e.g. it was paused first). Scoped to the recorded stem.
        if msg.get("action") == "cleanup":
            removed = cleanup_partials(stems=[msg.get("stem")])
            log(f"cleanup action removed {len(removed)} file(s): {removed}")
            send_message({"type": "cleaned", "removed": removed})
            return

        url = (msg.get("url") or "").strip()
        if not url:
            send_message({"type": "done", "success": False, "error": "Missing URL"})
            return
        # Watch stdin so a paused/canceled download actually stops yt-dlp.
        # `cleanup` distinguishes a cancel (delete partials) from a pause (keep).
        canceled = threading.Event()
        cleanup = threading.Event()
        proc_holder = {}
        _start_control_reader(canceled, cleanup, proc_holder)
        download(url, msg.get("dir"), msg.get("title"),
                 req_referer=msg.get("referer"),
                 canceled=canceled, cleanup=cleanup, proc_holder=proc_holder)
    except Exception as e:  # noqa: BLE001
        log("EXCEPTION:\n" + traceback.format_exc())
        send_message({"type": "done", "success": False, "error": str(e)})


if __name__ == "__main__":
    try:
        main()
    finally:
        # Hard-exit instead of returning into normal interpreter shutdown. The
        # control-reader daemon thread is usually blocked in a stdin read and
        # holds the stdin buffer's lock; on Python 3.14 the finalizer's attempt
        # to close that buffer then aborts the process ("_enter_buffered_busy",
        # SIGABRT) — which surfaced as a "Python quit unexpectedly" dialog after
        # a successful download. os._exit skips finalization entirely. All our
        # messages are already flushed by send_message; flush once more for
        # safety (stdout isn't the locked stream, so this can't deadlock).
        try:
            sys.stdout.buffer.flush()
        except Exception:
            pass
        os._exit(0)
