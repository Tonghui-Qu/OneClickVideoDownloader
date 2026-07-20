// Background service worker. It owns the native-messaging connections so
// downloads keep running even after the popup is closed, several can run at the
// same time (each gets its own native host process), and each can be
// paused/resumed (pausing drops the port, which stops yt-dlp but keeps the
// partial .part file; resuming reconnects and yt-dlp continues it).

const HOST = "com.oneclick.downloader";

// id -> download state; id -> its native Port. Kept here (not in the popup) so
// they survive the popup being closed and can be restored when it reopens.
const downloads = new Map();
const ports = new Map();
let nextId = 1;
let keepAliveTimer = null;

// --- Media sniffing --------------------------------------------------------
// Per-tab captured media URLs (streaming manifests + progressive files),
// populated passively from the moment a page loads. This is what lets us find
// videos whose page URL carries no id (e.g. dashboards, feed modals): the
// player still fetches an .m3u8/.mpd/.mp4 over the network, and we grab that.
// Because the listener runs from page load, autoplay/preload requests are
// already captured by the time the popup opens — usually no need to hit play.
// Kept in memory; if the service worker was asleep and lost them, the page's
// ongoing playback requests repopulate the list.
const sniffed = new Map(); // tabId -> Map(url -> {url, kind, ts})
const MAX_PER_TAB = 40;

// URLs whose extension already reveals them as media.
const MEDIA_URL_RE = /\.(m3u8|mpd|mp4|m4v|mov|webm)(\?|#|$)/i;
// Media identified by response Content-Type (signed/opaque CDN URLs, API
// endpoints) that carry no useful extension.
const MEDIA_CT_RE = /^(application\/(vnd\.apple\.mpegurl|x-mpegurl|dash\+xml)|video\/)/i;

function classifyByUrl(url) {
    const m = MEDIA_URL_RE.exec(url);
    if (!m) return null;
    const ext = m[1].toLowerCase();
    return (ext === "m3u8" || ext === "mpd") ? "manifest" : "file";
}

// Facebook/fbcdn serves DASH stories/reels as separate audio + video tracks,
// each fetched as a range slice of a full per-representation .mp4 (…&bytestart=
// X&byteend=Y). A slice on its own is a broken, unplayable file, so drop the
// range: the same URL without it returns the whole track, and repeated slices
// collapse to one entry. The host later pairs the video + audio tracks and muxes
// them into a single playable mp4.
function normalizeMediaUrl(url) {
    try {
        const u = new URL(url);
        if (/(^|\.)fbcdn\.net$/i.test(u.hostname) &&
            (u.searchParams.has("bytestart") || u.searchParams.has("byteend"))) {
            u.searchParams.delete("bytestart");
            u.searchParams.delete("byteend");
            return u.toString();
        }
    } catch (e) { /* leave non-URLs untouched */ }
    return url;
}

function recordMedia(tabId, url, kind) {
    if (tabId < 0 || !url) return;
    if (url.startsWith("blob:") || url.startsWith("data:")) return;
    url = normalizeMediaUrl(url);
    let m = sniffed.get(tabId);
    if (!m) { m = new Map(); sniffed.set(tabId, m); }
    if (m.has(url)) return;
    if (m.size >= MAX_PER_TAB) m.delete(m.keys().next().value); // drop oldest
    m.set(url, { url, kind, ts: Date.now() });
}

// Request types we bother inspecting. Video streams surface as `media` (native
// <video>), `xmlhttprequest` (JS players: hls.js/dash.js/Shaka fetch manifests
// and segments), or occasionally `other` (fetch from a page Worker, etc.); the
// last is the safety net so unusual players aren't missed. `main_frame` is kept
// only to clear a tab's captures on navigation. Everything else (image, script,
// stylesheet, font, ping, beacon, …) is never media, so skipping it avoids
// waking/running the worker for the bulk of a page's requests.
const SNIFF_TYPES = ["main_frame", "media", "xmlhttprequest", "other"];
// onHeadersReceived doesn't need main_frame (it only classifies by Content-Type).
const CT_TYPES = ["media", "xmlhttprequest", "other"];

chrome.webRequest.onBeforeRequest.addListener(
    (details) => {
        // A new top-level navigation means a new page: forget the old captures.
        if (details.type === "main_frame") { sniffed.delete(details.tabId); return; }
        const kind = classifyByUrl(details.url);
        if (kind) recordMedia(details.tabId, details.url, kind);
    },
    { urls: ["<all_urls>"], types: SNIFF_TYPES }
);

chrome.webRequest.onHeadersReceived.addListener(
    (details) => {
        const h = (details.responseHeaders || []).find(
            (x) => x.name.toLowerCase() === "content-type");
        const ct = (h && h.value) || "";
        if (MEDIA_CT_RE.test(ct)) {
            recordMedia(details.tabId, details.url,
                /mpegurl|dash/i.test(ct) ? "manifest" : "file");
        }
    },
    { urls: ["<all_urls>"], types: CT_TYPES },
    ["responseHeaders"]
);

chrome.tabs.onRemoved.addListener((tabId) => sniffed.delete(tabId));

// Returns captured candidates for a tab. Manifests come first (they expose the
// full quality ladder, so yt-dlp can pick the highest rendition). Within each
// group the newest are first: CDN media URLs (e.g. Douyin) carry short-lived,
// often single-use tokens, so the most recently seen one is the most likely to
// still be valid — probing stale ones first just stalls until they time out.
function getCandidates(tabId) {
    const m = sniffed.get(tabId);
    if (!m) return [];
    return Array.from(m.values()).sort((a, b) => {
        const byKind = (a.kind === "manifest" ? 0 : 1) - (b.kind === "manifest" ? 0 : 1);
        return byKind !== 0 ? byKind : b.ts - a.ts;
    });
}

function parsePercent(str) {
    const m = /([\d.]+)\s*%/.exec(str || "");
    return m ? parseFloat(m[1]) : NaN;
}

function anyActive() {
    for (const d of downloads.values()) if (d.active) return true;
    return false;
}

// While a download is actively running, frequent progress messages keep the
// service worker alive. During silent stretches (e.g. the final ffmpeg merge)
// no messages flow, so we ping an extension API periodically to reset the
// worker's idle timer and stop Chrome terminating it (which would drop ports).
function startKeepAlive() {
    if (keepAliveTimer) return;
    keepAliveTimer = setInterval(() => {
        chrome.runtime.getPlatformInfo(() => void chrome.runtime.lastError);
    }, 20000);
}

function stopKeepAliveIfIdle() {
    if (keepAliveTimer && !anyActive()) {
        clearInterval(keepAliveTimer);
        keepAliveTimer = null;
    }
}

function listState() {
    // Oldest first, so new downloads append at the end of the list.
    return Array.from(downloads.values()).sort((a, b) => a.startedAt - b.startedAt);
}

function broadcast() {
    // The popup may be closed (no receiver); ignore the resulting error.
    chrome.runtime.sendMessage({ type: "state", downloads: listState() }).catch(() => {});
}

// Opens a native-messaging port for a download and wires up its handlers.
// Used both to start a new download and to resume a paused one.
function connect(id) {
    const s = downloads.get(id);
    if (!s) return;

    const port = chrome.runtime.connectNative(HOST);
    ports.set(id, port);

    port.onMessage.addListener((msg) => {
        if (!msg) return;
        const d = downloads.get(id);
        if (!d) return;
        if (msg.type === "progress") {
            d.percent = parsePercent(msg.percent);
            const bits = [msg.percent, msg.speed, msg.eta ? "ETA " + msg.eta : ""]
                .filter((x) => x && x !== "NA" && x !== "N/A");
            d.statusText = "Downloading " + bits.join(" · ");
        } else if (msg.type === "status") {
            d.percent = NaN;
            d.statusText = msg.stage || "Processing…";
        } else if (msg.type === "meta") {
            // Output-file stem, kept so a later cancel can delete the partials
            // even if the download was paused (no live host to clean up itself).
            d.stem = msg.stem;
        } else if (msg.type === "done") {
            d.active = false;
            d.paused = false;
            d.done = true;
            d.success = !!msg.success;
            d.fellBack = !!msg.fellBack;
            d.path = msg.path || "";
            d.error = msg.error || "";
            try { port.disconnect(); } catch (e) { /* already gone */ }
            ports.delete(id);
            stopKeepAliveIfIdle();
        }
        broadcast();
    });

    port.onDisconnect.addListener(() => {
        ports.delete(id);
        const d = downloads.get(id);
        // Only treat this as a failure if we were still actively downloading.
        // A pause sets active=false *before* disconnecting, so it lands here as
        // a no-op (the paused state is preserved).
        if (d && d.active) {
            d.active = false;
            d.done = true;
            d.success = false;
            const err = chrome.runtime.lastError;
            d.error = (err && err.message) ? err.message : "Connection closed";
        }
        stopKeepAliveIfIdle();
        broadcast();
    });

    port.postMessage({ url: s.url, dir: s.dir, title: s.title,
                       referer: s.referer, audioUrl: s.audioUrl });
}

function startDownload({ url, dir, title, referer, thumb, audioUrl }) {
    // Don't start a second copy of a URL that's already downloading or paused.
    for (const d of downloads.values()) {
        if ((d.active || d.paused) && d.url === url) return d.id;
    }

    const id = nextId++;
    downloads.set(id, {
        id,
        url,
        // Optional companion audio track (Facebook DASH): the host downloads it
        // alongside the video URL and muxes them into one file.
        audioUrl: audioUrl || "",
        referer: referer || "",
        dir: dir || "",
        title: title || "",
        // Small (~few KB) thumbnail shown beside the row. Kept small on purpose:
        // the whole download list is re-broadcast to the popup several times a
        // second, so a big data URI here would be wasteful.
        thumb: thumb || "",
        path: "",
        percent: NaN,
        statusText: "Starting…",
        active: true,
        paused: false,
        done: false,
        success: false,
        error: "",
        fellBack: false,
        startedAt: Date.now(),
    });
    broadcast();
    startKeepAlive();
    connect(id);
    return id;
}

function pauseDownload(id) {
    const d = downloads.get(id);
    if (!d || !d.active) return;
    // Mark paused first so onDisconnect doesn't record it as a failure.
    d.active = false;
    d.paused = true;
    d.statusText = "Paused";
    const port = ports.get(id);
    if (port) {
        try { port.disconnect(); } catch (e) { /* already gone */ }
        ports.delete(id);
    }
    stopKeepAliveIfIdle();
    broadcast();
}

function resumeDownload(id) {
    const d = downloads.get(id);
    if (!d || !d.paused) return;
    d.paused = false;
    d.active = true;
    d.done = false;
    d.success = false;
    d.error = "";
    d.statusText = "Resuming…";
    broadcast();
    startKeepAlive();
    connect(id);
}

// Cancels (if running) and removes a download from the list entirely, deleting
// any partial files it created (unlike pause, which keeps them for resume).
function removeDownload(id) {
    const d = downloads.get(id);
    const port = ports.get(id);
    if (port) {
        // Active: ask the host to stop yt-dlp AND delete the partials it made.
        // The message is buffered in the pipe, so it's read before the EOF from
        // disconnect — the host reliably sees the cleanup request.
        try { port.postMessage({ action: "cancel", cleanup: true }); } catch (e) { /* gone */ }
        try { port.disconnect(); } catch (e) { /* already gone */ }
        ports.delete(id);
    } else if (d && d.stem) {
        // Paused: no live host, so spawn a one-shot one to remove the leftovers.
        try {
            chrome.runtime.sendNativeMessage(
                HOST, { action: "cleanup", stem: d.stem },
                () => void chrome.runtime.lastError);
        } catch (e) { /* ignore */ }
    }
    downloads.delete(id);
    stopKeepAliveIfIdle();
    broadcast();
}

function clearFinished() {
    for (const [id, d] of downloads) {
        if (!d.active && !d.paused) downloads.delete(id);
    }
    broadcast();
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (!msg || !msg.cmd) return; // ignore "state" broadcasts / unrelated msgs
    if (msg.cmd === "start") {
        sendResponse({ ok: true, id: startDownload(msg) });
    } else if (msg.cmd === "getCandidates") {
        sendResponse({ candidates: getCandidates(msg.tabId) });
    } else if (msg.cmd === "getState") {
        sendResponse({ downloads: listState() });
    } else if (msg.cmd === "pause") {
        pauseDownload(msg.id);
        sendResponse({ ok: true });
    } else if (msg.cmd === "resume") {
        resumeDownload(msg.id);
        sendResponse({ ok: true });
    } else if (msg.cmd === "remove") {
        removeDownload(msg.id);
        sendResponse({ ok: true });
    } else if (msg.cmd === "clearFinished") {
        clearFinished();
        sendResponse({ ok: true });
    }
    return true; // keep the channel open for the async sendResponse
});
