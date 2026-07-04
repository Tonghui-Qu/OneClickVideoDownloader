// Background service worker. It owns the native-messaging connection so a
// download keeps running even after the popup is closed (closing the popup used
// to disconnect the port and kill yt-dlp mid-download).

const HOST = "com.oneclick.downloader";

// The single in-flight/last download's state, kept here (not in the popup) so it
// survives the popup being closed and can be restored when it reopens.
let state = null;
let port = null;
let keepAliveTimer = null;

function parsePercent(str) {
    const m = /([\d.]+)\s*%/.exec(str || "");
    return m ? parseFloat(m[1]) : NaN;
}

// While a download runs, the port streams frequent progress messages which keep
// the service worker alive. During silent stretches (e.g. the final ffmpeg
// merge) no messages flow, so we ping an extension API periodically to reset the
// worker's idle timer and prevent Chrome from terminating it (which would drop
// the port and abort the download).
function startKeepAlive() {
    if (keepAliveTimer) return;
    keepAliveTimer = setInterval(() => {
        chrome.runtime.getPlatformInfo(() => void chrome.runtime.lastError);
    }, 20000);
}

function stopKeepAlive() {
    if (keepAliveTimer) {
        clearInterval(keepAliveTimer);
        keepAliveTimer = null;
    }
}

function broadcast() {
    // The popup may be closed (no receiver); ignore the resulting error.
    chrome.runtime.sendMessage({ type: "state", state }).catch(() => {});
}

function startDownload({ url, dir, title }) {
    if (state && state.active) return; // one download at a time

    state = {
        active: true,
        url,
        title: title || "",
        percent: NaN,
        statusText: "Starting…",
        done: false,
        success: false,
        error: "",
        fellBack: false,
    };
    broadcast();
    startKeepAlive();

    port = chrome.runtime.connectNative(HOST);

    port.onMessage.addListener((msg) => {
        if (!msg) return;
        if (msg.type === "progress") {
            state.percent = parsePercent(msg.percent);
            const bits = [msg.percent, msg.speed, msg.eta ? "ETA " + msg.eta : ""]
                .filter((s) => s && s !== "NA" && s !== "N/A");
            state.statusText = "Downloading " + bits.join(" · ");
        } else if (msg.type === "status") {
            state.percent = NaN;
            state.statusText = msg.stage || "Processing…";
        } else if (msg.type === "done") {
            state.active = false;
            state.done = true;
            state.success = !!msg.success;
            state.fellBack = !!msg.fellBack;
            state.error = msg.error || "";
            try { port.disconnect(); } catch (e) { /* already gone */ }
            port = null;
            stopKeepAlive();
        }
        broadcast();
    });

    port.onDisconnect.addListener(() => {
        port = null;
        stopKeepAlive();
        // If we disconnected before a "done" frame, the host crashed or Chrome
        // couldn't launch it — report it as a failure.
        if (state && state.active) {
            state.active = false;
            state.done = true;
            state.success = false;
            const err = chrome.runtime.lastError;
            state.error = (err && err.message) ? err.message : "Connection closed";
            broadcast();
        }
    });

    port.postMessage({ url, dir, title });
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (!msg || !msg.cmd) return; // ignore "state" broadcasts / unrelated msgs
    if (msg.cmd === "start") {
        startDownload(msg);
        sendResponse({ ok: true });
    } else if (msg.cmd === "getState") {
        sendResponse({ state });
    } else if (msg.cmd === "clearFinished") {
        // Popup acknowledges a finished result so a later reopen starts clean.
        if (state && !state.active) state = null;
        sendResponse({ ok: true });
    }
    return true; // keep the channel open for the async sendResponse
});
