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

function showChecking() {
    previewEl.classList.remove("hidden");
    previewEl.classList.add("checking");
    previewEl.textContent = "Checking this page…";
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

async function probeCurrentTab() {
    // No downloadable video until we confirm one exists.
    downloadButton.disabled = true;

    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    const url = tab && tab.url ? tab.url : "";

    if (!/^https?:\/\//i.test(url)) {
        hidePreview();
        setStatus("No video found");
        return;
    }

    showChecking();
    setStatus("");
    try {
        // tab.title is already in the language the user selected on the site,
        // so the host can use it for the (localized) preview and filename.
        const result = await chrome.runtime.sendNativeMessage(HOST, { action: "probe", url, title: tab.title || "" });
        hidePreview();
        if (result && result.ok) {
            showPreview(result.title, result.thumb, result.meta);
            setStatus("Ready to download");
            downloadButton.disabled = false;
        } else {
            setStatus("No video found");
        }
    } catch (e) {
        hidePreview();
        setStatus("❌ " + e.message);
    }
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

// Renders the list of downloads (owned by the background worker). Several can
// run at once; each gets its own row with independent progress.
function renderDownloads(list) {
    list = Array.isArray(list) ? list : [];
    downloadsSection.classList.toggle("hidden", list.length === 0);
    clearFinishedButton.classList.toggle("hidden", !list.some((d) => d.done));

    downloadsEl.innerHTML = "";
    list.forEach((dl) => {
        const row = document.createElement("div");
        row.className = "dl";

        const main = document.createElement("div");
        main.className = "dl-main";

        const title = document.createElement("div");
        title.className = "dl-title";
        title.textContent = dl.title || dl.url || "Video";
        title.title = dl.title || dl.url || "";

        const bar = document.createElement("div");
        bar.className = "dl-bar";
        const fill = document.createElement("div");
        fill.className = "dl-fill";

        const stat = document.createElement("div");
        stat.className = "dl-status";

        const pct = (typeof dl.percent === "number" && !isNaN(dl.percent))
            ? Math.max(0, Math.min(100, dl.percent)) : null;

        if (dl.active) {
            if (pct !== null) fill.style.width = pct + "%";
            else bar.classList.add("indeterminate");
            stat.textContent = dl.statusText || "Downloading…";
        } else if (dl.paused) {
            bar.classList.add("paused");
            if (pct !== null) fill.style.width = pct + "%";
            stat.textContent = "Paused" + (pct !== null ? ` · ${pct}%` : "");
        } else if (dl.success) {
            bar.classList.add("ok");
            stat.classList.add("ok");
            stat.textContent = dl.fellBack
                ? "✅ Finished — folder missing, saved to ~/Downloads"
                : "✅ Finished";
        } else {
            bar.classList.add("err");
            stat.classList.add("err");
            stat.textContent = "❌ " + (dl.error || "Download failed");
        }

        bar.appendChild(fill);
        main.appendChild(title);
        main.appendChild(bar);
        main.appendChild(stat);

        row.appendChild(main);

        // Pause (while active) or resume (while paused).
        if (dl.active || dl.paused) {
            const toggle = document.createElement("button");
            toggle.className = "dl-btn";
            toggle.textContent = dl.active ? "⏸" : "▶";
            toggle.title = dl.active ? "Pause" : "Resume";
            toggle.addEventListener("click", () => {
                chrome.runtime.sendMessage({
                    cmd: dl.active ? "pause" : "resume", id: dl.id,
                }).catch(() => {});
            });
            row.appendChild(toggle);
        }

        // Remove (cancels if running/paused, otherwise just dismisses).
        const close = document.createElement("button");
        close.className = "dl-x";
        close.textContent = "✕";
        close.title = (dl.active || dl.paused) ? "Cancel" : "Dismiss";
        close.addEventListener("click", () => {
            chrome.runtime.sendMessage({ cmd: "remove", id: dl.id }).catch(() => {});
        });
        row.appendChild(close);

        downloadsEl.appendChild(row);
    });
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
        const url = tab && tab.url ? tab.url : "";

        if (!/^https?:\/\//i.test(url)) {
            setStatus("⚠ Open a video page first (YouTube/Instagram/TikTok)");
            return;
        }

        const dir = (folders[selected] && folders[selected].path) || "";

        // Hand the download to the background worker so it survives this popup
        // being closed and runs alongside any others. Progress arrives via the
        // "state" broadcast above.
        await chrome.runtime.sendMessage({
            cmd: "start", url, dir, title: tab.title || "",
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
