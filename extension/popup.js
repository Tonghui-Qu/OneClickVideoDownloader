const HOST = "com.oneclick.downloader";

const status = document.getElementById("status");
const downloadButton = document.getElementById("downloadButton");
const addFolderButton = document.getElementById("addFolder");
const foldersEl = document.getElementById("folders");
const progressEl = document.getElementById("progress");
const progressBar = document.getElementById("progressBar");
const previewEl = document.getElementById("preview");
const thumbEl = document.getElementById("thumb");
const titleEl = document.getElementById("title");

function showPreview(title, thumb) {
    previewEl.classList.remove("hidden", "checking");
    if (thumb) {
        thumbEl.src = thumb;
        thumbEl.classList.remove("hidden");
    } else {
        thumbEl.classList.add("hidden");
    }
    titleEl.textContent = title || "";
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
        const result = await chrome.runtime.sendNativeMessage(HOST, { action: "probe", url });
        hidePreview();
        if (result && result.ok) {
            showPreview(result.title, result.thumb);
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

function showProgress(percent) {
    progressEl.classList.remove("hidden");
    if (typeof percent === "number" && !isNaN(percent)) {
        progressEl.classList.remove("indeterminate");
        progressBar.style.width = Math.max(0, Math.min(100, percent)) + "%";
    } else {
        // Unknown size / post-processing: show an animated indeterminate bar.
        progressEl.classList.add("indeterminate");
    }
}

function hideProgress() {
    progressEl.classList.add("hidden");
    progressEl.classList.remove("indeterminate");
    progressBar.style.width = "0%";
}

function parsePercent(str) {
    const m = /([\d.]+)\s*%/.exec(str || "");
    return m ? parseFloat(m[1]) : NaN;
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

downloadButton.addEventListener("click", async () => {
    try {
        setStatus("Getting current page…");

        const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
        const url = tab && tab.url ? tab.url : "";

        if (!/^https?:\/\//i.test(url)) {
            setStatus("⚠ Open a video page first (YouTube/Instagram/TikTok)");
            return;
        }

        const dir = (folders[selected] && folders[selected].path) || "";

        // A persistent connection lets the host stream progress updates.
        setStatus("Starting…");
        showProgress(NaN);
        downloadButton.disabled = true;

        let done = false;
        const port = chrome.runtime.connectNative(HOST);

        port.onMessage.addListener((msg) => {
            if (!msg) return;
            if (msg.type === "progress") {
                showProgress(parsePercent(msg.percent));
                const bits = [msg.percent, msg.speed, msg.eta ? "ETA " + msg.eta : ""]
                    .filter((s) => s && s !== "NA" && s !== "N/A");
                setStatus("Downloading " + bits.join(" · "));
            } else if (msg.type === "status") {
                showProgress(NaN);
                setStatus(msg.stage || "Processing…");
            } else if (msg.type === "done") {
                done = true;
                hideProgress();
                downloadButton.disabled = false;
                if (msg.success) {
                    setStatus(msg.fellBack
                        ? "✅ Finished — folder missing, saved to ~/Downloads"
                        : "✅ Finished");
                } else {
                    setStatus("❌ " + (msg.error || "Download Failed"));
                }
                port.disconnect();
            }
        });

        port.onDisconnect.addListener(() => {
            if (done) return;
            hideProgress();
            downloadButton.disabled = false;
            const err = chrome.runtime.lastError;
            setStatus("❌ " + (err && err.message ? err.message : "Connection closed"));
        });

        port.postMessage({ url, dir });
    } catch (e) {
        console.error(e);
        hideProgress();
        downloadButton.disabled = false;
        setStatus("❌ " + e.message);
    }
});

(async function init() {
    await loadState();
    render();
    probeCurrentTab();
})();
