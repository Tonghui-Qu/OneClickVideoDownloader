const HOST = "com.oneclick.downloader";

const status = document.getElementById("status");
const downloadButton = document.getElementById("downloadButton");
const addFolderButton = document.getElementById("addFolder");
const foldersEl = document.getElementById("folders");
const previewEl = document.getElementById("preview");
const thumbEl = document.getElementById("thumb");
const titleEl = document.getElementById("title");
const metaEl = document.getElementById("meta");
const downloadsSection = document.getElementById("downloadsSection");
const downloadsEl = document.getElementById("downloads");
const clearFinishedButton = document.getElementById("clearFinished");

// The download button doubles as the detection indicator: it shows the normal
// "Download Current Video" label when a video is ready and "No video found"
// (disabled) otherwise, so no separate status line is needed for those states.
const DEFAULT_BTN_LABEL = downloadButton.textContent;
const NO_VIDEO_BTN_LABEL = "No video found";

function setButtonState(label, enabled) {
    downloadButton.textContent = label;
    downloadButton.disabled = !enabled;
}

function showPreview(title, thumb, meta) {
    previewEl.classList.remove("hidden", "checking");
    // Inline style is used instead of a class because the #thumb ID selector
    // outranks a .hidden class rule, so toggling a class wouldn't hide it.
    if (thumb) {
        thumbEl.src = thumb;
        thumbEl.style.display = "block";
    } else {
        thumbEl.removeAttribute("src");
        thumbEl.style.display = "none";
    }
    titleEl.textContent = title || "";
    metaEl.textContent = meta || "";
    metaEl.style.display = meta ? "block" : "none";
}

function hidePreview() {
    previewEl.classList.add("hidden");
    previewEl.classList.remove("checking");
    // Restore inner markup destroyed by the "checking" text state.
    previewEl.innerHTML = "";
    previewEl.appendChild(thumbEl);
    previewEl.appendChild(titleEl);
    previewEl.appendChild(metaEl);
}

// Set when the user clicks Download so the next state render scrolls the newly
// added row into view (the popup is often taller than Chrome's ~600px cap, so
// the downloads list would otherwise sit below the fold, needing a manual scroll).
let scrollToNewDownload = false;

// What a download click should actually fetch. For sites yt-dlp knows, this is
// the page URL (best quality via the site's extractor). When that finds nothing,
// it becomes a sniffed media URL plus the page as its referer.
let currentSource = null;

// Instagram post/reel pages (and similar) identify a specific item via the URL
// (e.g. ?img_index=N). Sniff fallback would pick media from other carousel
// slides, so callers skip it when this returns true.
function isCarouselPageUrl(url) {
    try {
        const u = new URL(url);
        const host = u.hostname.replace(/^www\./, "");
        if (host === "instagram.com" || host.endsWith(".instagram.com")) {
            return /^\/(p|reel|tv)\//i.test(u.pathname);
        }
    } catch (e) { /* ignore */ }
    return false;
}

// Ask the host to probe the media URLs the background sniffed on this page, and
// return the best (highest-resolution) one. Used only when the page URL itself
// isn't a recognizable video.
async function trySniffedCandidates(tab, pageUrl) {
    if (!tab || tab.id == null) return null;
    let resp;
    try {
        resp = await chrome.runtime.sendMessage({ cmd: "getCandidates", tabId: tab.id });
    } catch (e) {
        return null;
    }
    const candidates = (resp && resp.candidates) || [];
    if (!candidates.length) return null;

    setStatus("Checking detected media…");
    try {
        const best = await chrome.runtime.sendNativeMessage(HOST, {
            action: "sniffProbe",
            url: pageUrl,
            title: tab.title || "",
            candidates: candidates.map((c) => c.url),
        });
        if (best && best.ok) return best; // { title, thumb, meta, url }
    } catch (e) { /* fall through to "no video" */ }
    return null;
}

// Runs the host probe over a native-messaging *port* so it can deliver two
// messages: `probeMeta` (title + resolution — resolves this promise so the
// button can enable) and, a beat later, `probeThumb` (the preview image, handed
// to onThumb). Resolves with the meta message (or null), rejects on port error.
function runProbePort(url, title, onThumb) {
    return new Promise((resolve, reject) => {
        let port;
        try {
            port = chrome.runtime.connectNative(HOST);
        } catch (e) {
            reject(e);
            return;
        }
        let meta = null;
        let settled = false;
        port.onMessage.addListener((msg) => {
            if (!msg) return;
            if (msg.type === "probeMeta") {
                meta = msg;
                if (!settled) { settled = true; resolve(msg); }
            } else if (msg.type === "probeThumb") {
                if (onThumb) onThumb(msg.thumb);
                try { port.disconnect(); } catch (e) { /* already gone */ }
            }
        });
        port.onDisconnect.addListener(() => {
            const err = chrome.runtime.lastError;
            if (!settled) {
                settled = true;
                if (err) reject(new Error(err.message));
                else resolve(meta);
            }
        });
        port.postMessage({ action: "probe", url, title });
    });
}

async function probeCurrentTab() {
    // No downloadable video until we confirm one exists.
    downloadButton.disabled = true;
    currentSource = null;

    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    const url = tab && tab.url ? tab.url : "";

    if (!/^https?:\/\//i.test(url)) {
        hidePreview();
        setButtonState(NO_VIDEO_BTN_LABEL, false);
        setStatus("");
        return;
    }

    // Optimistic: show the tab's own title immediately so the popup feels
    // responsive, then refine with the probe result (thumbnail arrives after).
    // tab.title is already in the language the user selected on the site.
    const tabTitle = tab.title || "";
    showPreview(tabTitle, null, "");
    setStatus("Checking this page…");

    let meta = null;
    try {
        meta = await runProbePort(url, tabTitle, (thumb) => {
            // Thumbnail lands after meta; only show it once a video is confirmed.
            if (thumb && !downloadButton.disabled) {
                thumbEl.src = thumb;
                thumbEl.style.display = "block";
            }
            // Stash it so the download row can carry a small thumbnail.
            if (thumb && currentSource) currentSource.thumb = thumb;
        });
    } catch (e) {
        hidePreview();
        setStatus("❌ " + e.message);
        return;
    }

    if (meta && meta.ok) {
        // Prefer the probe's title, fall back to the tab title already shown.
        showPreview(meta.title || tabTitle, null, meta.meta);
        currentSource = { url, referer: "", title: meta.title || tabTitle, thumb: null };
        setButtonState(DEFAULT_BTN_LABEL, true);
        setStatus("");
        return;
    }

    // Instagram /p/ and /reel/ URLs are the content itself (incl. carousels via
    // ?img_index=N). If the current slide isn't a video, don't fall back to
    // sniffed media from other slides — that would show the wrong item.
    if (isCarouselPageUrl(url)) {
        hidePreview();
        setButtonState(NO_VIDEO_BTN_LABEL, false);
        setStatus("");
        return;
    }

    // The page URL isn't a recognizable video — fall back to sniffed media.
    const sniffed = await trySniffedCandidates(tab, url);
    hidePreview();
    if (sniffed) {
        showPreview(sniffed.title, sniffed.thumb, sniffed.meta);
        currentSource = {
            url: sniffed.url, referer: url,
            title: sniffed.title || tabTitle, thumb: sniffed.thumb || null,
        };
        setButtonState(DEFAULT_BTN_LABEL, true);
        setStatus("");
    } else {
        setButtonState(NO_VIDEO_BTN_LABEL, false);
        setStatus("");
    }
}

// Downscales a (possibly large) thumbnail data URI to a small square JPEG data
// URI (~a few KB) for the download rows. Returns null on any failure.
function shrinkThumb(dataUri, size = 80) {
    return new Promise((resolve) => {
        if (!dataUri) { resolve(null); return; }
        const img = new Image();
        img.onload = () => {
            try {
                const canvas = document.createElement("canvas");
                canvas.width = size;
                canvas.height = size;
                const ctx = canvas.getContext("2d");
                // Cover-crop to a centered square so the icon isn't letterboxed.
                const scale = Math.max(size / img.width, size / img.height);
                const w = img.width * scale;
                const h = img.height * scale;
                ctx.drawImage(img, (size - w) / 2, (size - h) / 2, w, h);
                resolve(canvas.toDataURL("image/jpeg", 0.7));
            } catch (e) {
                resolve(null);
            }
        };
        img.onerror = () => resolve(null);
        img.src = dataUri;
    });
}

// path "" is the special default entry → host saves to ~/Downloads.
const DEFAULT_FOLDERS = [{ label: "Downloads (default)", path: "" }];

let folders = DEFAULT_FOLDERS.slice();
let selected = 0;

function setStatus(text) {
    status.innerText = text;
}

async function loadState() {
    const data = await chrome.storage.local.get(["folders", "selected"]);
    if (Array.isArray(data.folders) && data.folders.length) {
        folders = data.folders;
    }
    selected = Number.isInteger(data.selected) ? data.selected : 0;
    if (selected < 0 || selected >= folders.length) selected = 0;
}

function saveState() {
    return chrome.storage.local.set({ folders, selected });
}

function render() {
    foldersEl.innerHTML = "";

    if (!folders.length) {
        folders = DEFAULT_FOLDERS.slice();
        selected = 0;
    }

    folders.forEach((folder, index) => {
        const row = document.createElement("div");
        row.className = "folder";

        const radio = document.createElement("input");
        radio.type = "radio";
        radio.name = "folder";
        radio.checked = index === selected;
        radio.addEventListener("change", () => {
            selected = index;
            saveState();
        });

        const info = document.createElement("div");
        info.className = "folder-info";
        const label = document.createElement("div");
        label.className = "folder-label";
        label.textContent = folder.label || "Folder";
        const path = document.createElement("div");
        path.className = "folder-path";
        path.textContent = folder.path || "~/Downloads";
        path.title = folder.path || "~/Downloads";
        info.appendChild(label);
        info.appendChild(path);

        info.addEventListener("click", () => {
            selected = index;
            saveState();
            render();
        });

        row.appendChild(radio);
        row.appendChild(info);

        // The default entry is not removable.
        if (folder.path !== "") {
            const remove = document.createElement("button");
            remove.className = "remove-btn";
            remove.textContent = "✕";
            remove.title = "Remove";
            remove.addEventListener("click", (e) => {
                e.stopPropagation();
                folders.splice(index, 1);
                if (!folders.length) folders = DEFAULT_FOLDERS.slice();
                if (selected >= folders.length) selected = folders.length - 1;
                saveState();
                render();
            });
            row.appendChild(remove);
        }

        foldersEl.appendChild(row);
    });
}

function labelFromPath(p) {
    const parts = p.split("/").filter(Boolean);
    return parts.length ? parts[parts.length - 1] : p;
}

addFolderButton.addEventListener("click", async () => {
    setStatus("Opening folder picker…");
    try {
        const result = await chrome.runtime.sendNativeMessage(HOST, { action: "pickFolder" });
        if (result && result.success && result.path) {
            const path = result.path;
            const existing = folders.findIndex((f) => f.path === path);
            if (existing >= 0) {
                selected = existing;
            } else {
                folders.push({ label: labelFromPath(path), path });
                selected = folders.length - 1;
            }
            await saveState();
            render();
            setStatus("Added: " + path);
        } else if (result && result.canceled) {
            setStatus("Ready");
        } else {
            setStatus("❌ " + ((result && result.error) || "Could not pick folder"));
        }
    } catch (e) {
        console.error(e);
        setStatus("❌ " + e.message);
    }
});

// Rendered rows kept across updates and reused, keyed by download id. Reusing
// the DOM (instead of rebuilding it every state broadcast) is what keeps the
// "Starting…" indeterminate bar animating smoothly: recreating the element each
// time — which happens ~4x/sec when another download reports progress — would
// restart its CSS animation and make it visibly flicker.
const dlRows = new Map();

// Builds one download row and wires its buttons once. The button handlers read
// entry.dl so they always act on the latest state without being re-bound.
function createRow(id) {
    const row = document.createElement("div");
    row.className = "dl";

    // Small thumbnail beside the row (hidden until a thumb is available).
    const thumb = document.createElement("img");
    thumb.className = "dl-thumb";
    thumb.alt = "";
    thumb.style.display = "none";
    row.appendChild(thumb);

    const main = document.createElement("div");
    main.className = "dl-main";

    const title = document.createElement("div");
    title.className = "dl-title";
    // Click the filename to reveal the finished file in Finder (folder opens
    // with the file selected). Only acts when the download finished and we know
    // its path; the class toggled in updateRow signals when it's clickable.
    title.addEventListener("click", () => {
        const dl = entry.dl;
        if (!dl || !dl.done || !dl.success || !dl.path) return;
        chrome.runtime.sendNativeMessage(
            HOST, { action: "reveal", path: dl.path },
            () => void chrome.runtime.lastError);
    });

    const bar = document.createElement("div");
    bar.className = "dl-bar";
    const fill = document.createElement("div");
    fill.className = "dl-fill";
    bar.appendChild(fill);

    const stat = document.createElement("div");
    stat.className = "dl-status";

    main.appendChild(title);
    main.appendChild(bar);
    main.appendChild(stat);
    row.appendChild(main);

    const entry = { row, thumb, title, bar, fill, stat, dl: null };

    // Pause (while active) or resume (while paused).
    const toggle = document.createElement("button");
    toggle.className = "dl-btn";
    toggle.addEventListener("click", () => {
        const dl = entry.dl;
        if (!dl) return;
        chrome.runtime.sendMessage({
            cmd: dl.active ? "pause" : "resume", id: dl.id,
        }).catch(() => {});
    });

    // Remove (cancels if running/paused, otherwise just dismisses).
    const close = document.createElement("button");
    close.className = "dl-x";
    close.textContent = "✕";
    close.addEventListener("click", () => {
        chrome.runtime.sendMessage({ cmd: "remove", id }).catch(() => {});
    });
    row.appendChild(close);

    entry.toggle = toggle;
    entry.close = close;
    return entry;
}

// Updates an existing row in place. Uses classList.toggle (not a wholesale
// className reset) so a bar that stays "indeterminate" keeps the same running
// animation across updates.
function updateRow(entry, dl) {
    entry.dl = dl;
    const { thumb, title, bar, fill, stat, toggle, close, row } = entry;

    // Prefer the real saved filename once known (path → stem → title → url).
    const fromPath = dl.path ? dl.path.split("/").pop() : "";
    const fromStem = dl.stem ? dl.stem.split("/").pop() : "";
    const name = fromPath || fromStem || dl.title || dl.url || "Video";
    title.textContent = name;
    title.title = name;

    const pct = (typeof dl.percent === "number" && !isNaN(dl.percent))
        ? Math.max(0, Math.min(100, dl.percent)) : null;

    const isActive = !!dl.active;
    const isPaused = !!dl.paused;
    const isDone = !isActive && !isPaused;
    const isOk = isDone && !!dl.success;
    const isErr = isDone && !dl.success;
    const indeterminate = isActive && pct === null;

    // Small thumbnail, shown as soon as we have one.
    if (dl.thumb) {
        if (thumb.src !== dl.thumb) thumb.src = dl.thumb;
        thumb.style.display = "block";
    } else {
        thumb.style.display = "none";
    }

    // The filename becomes a clickable "reveal in Finder" link once finished.
    const canReveal = isOk && !!dl.path;
    title.classList.toggle("link", canReveal);
    title.title = canReveal ? "Show in Finder" : name;

    bar.classList.toggle("indeterminate", indeterminate);
    bar.classList.toggle("paused", isPaused);
    bar.classList.toggle("ok", isOk);
    bar.classList.toggle("err", isErr);

    // For indeterminate / finished / errored bars the width comes from CSS, so
    // clear any inline width that would otherwise override it.
    fill.style.width = (indeterminate || isOk || isErr)
        ? "" : (pct !== null ? pct : 0) + "%";

    stat.classList.toggle("ok", isOk);
    stat.classList.toggle("err", isErr);
    if (isActive) {
        stat.textContent = dl.statusText || "Downloading…";
    } else if (isPaused) {
        stat.textContent = "Paused" + (pct !== null ? ` · ${pct}%` : "");
    } else if (isOk) {
        stat.textContent = dl.fellBack
            ? "✅ Finished — folder missing, saved to ~/Downloads"
            : "✅ Finished";
    } else {
        stat.textContent = "❌ " + (dl.error || "Download failed");
    }

    if (isActive || isPaused) {
        toggle.textContent = isActive ? "⏸" : "▶";
        toggle.title = isActive ? "Pause" : "Resume";
        if (!toggle.isConnected) row.insertBefore(toggle, close);
    } else if (toggle.isConnected) {
        toggle.remove();
    }
    close.title = (isActive || isPaused) ? "Cancel" : "Dismiss";
}

// Renders the list of downloads (owned by the background worker). Several can
// run at once; each gets its own row with independent progress. Rows are reused
// across calls (see dlRows) so in-progress animations don't restart.
function renderDownloads(list) {
    list = Array.isArray(list) ? list : [];
    downloadsSection.classList.toggle("hidden", list.length === 0);
    clearFinishedButton.classList.toggle("hidden", !list.some((d) => d.done));

    const seen = new Set();
    // The list is stably ordered (oldest first), so existing rows keep their
    // position and only brand-new rows need appending at the end.
    list.forEach((dl) => {
        seen.add(dl.id);
        let entry = dlRows.get(dl.id);
        if (!entry) {
            entry = createRow(dl.id);
            dlRows.set(dl.id, entry);
            downloadsEl.appendChild(entry.row);
        }
        updateRow(entry, dl);
    });

    for (const [id, entry] of dlRows) {
        if (!seen.has(id)) {
            entry.row.remove();
            dlRows.delete(id);
        }
    }

    // Just clicked Download: bring the freshly added (bottom-most) row into view
    // so the user sees its progress without scrolling. Wait a frame so the
    // just-unhidden section has its final layout before scrolling.
    if (scrollToNewDownload && list.length) {
        scrollToNewDownload = false;
        requestAnimationFrame(() => {
            downloadsSection.scrollIntoView({ behavior: "smooth", block: "end" });
            // The list has its own max-height/scroll; pin it to the newest row too.
            downloadsEl.scrollTop = downloadsEl.scrollHeight;
        });
    }
}

// Live updates from the background worker while the popup is open.
chrome.runtime.onMessage.addListener((msg) => {
    if (msg && msg.type === "state") renderDownloads(msg.downloads);
});

clearFinishedButton.addEventListener("click", () => {
    chrome.runtime.sendMessage({ cmd: "clearFinished" }).catch(() => {});
});

downloadButton.addEventListener("click", async () => {
    try {
        const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
        const pageUrl = tab && tab.url ? tab.url : "";

        // Prefer the source resolved during probing (may be a sniffed media URL);
        // otherwise fall back to the page URL.
        const src = currentSource
            || (/^https?:\/\//i.test(pageUrl) ? { url: pageUrl, referer: "" } : null);
        if (!src) {
            setStatus("⚠ Open a video page first (YouTube/Instagram/TikTok)");
            return;
        }

        const dir = (folders[selected] && folders[selected].path) || "";

        // Prefer the video title resolved during probing over the raw tab title
        // (which for e.g. Instagram is just "Instagram").
        const title = (src.title && src.title.trim()) || tab.title || "";
        // A tiny thumbnail for the row (downscaled so it's cheap to re-broadcast).
        const thumb = await shrinkThumb(src.thumb, 80);

        // Hand the download to the background worker so it survives this popup
        // being closed and runs alongside any others. Progress arrives via the
        // "state" broadcast above.
        // Scroll the new row into view when the resulting state broadcast lands.
        scrollToNewDownload = true;
        await chrome.runtime.sendMessage({
            cmd: "start", url: src.url, referer: src.referer || "",
            dir, title, thumb,
        });
        setStatus("Added to downloads ↓");
    } catch (e) {
        console.error(e);
        setStatus("❌ " + e.message);
    }
});

(async function init() {
    await loadState();
    render();

    // Restore any in-flight / finished downloads so reopening the popup shows
    // their live progress and results.
    try {
        const resp = await chrome.runtime.sendMessage({ cmd: "getState" });
        renderDownloads(resp && resp.downloads);
    } catch (e) { /* worker not ready yet */ }

    probeCurrentTab();
})();
