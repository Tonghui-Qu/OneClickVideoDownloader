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

    port.postMessage({ url: s.url, dir: s.dir, title: s.title });
}

function startDownload({ url, dir, title }) {
    // Don't start a second copy of a URL that's already downloading or paused.
    for (const d of downloads.values()) {
        if ((d.active || d.paused) && d.url === url) return d.id;
    }

    const id = nextId++;
    downloads.set(id, {
        id,
        url,
        dir: dir || "",
        title: title || "",
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
