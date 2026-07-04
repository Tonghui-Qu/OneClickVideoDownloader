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
        // tab.title is already in the language the user selected on the site,
        // so the host can use it for the (localized) preview and filename.
        const result = await chrome.runtime.sendNativeMessage(HOST, { action: "probe", url, title: tab.title || "" });
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

// Reflects a download's state (owned by the background worker) onto the UI.
// Called both for live broadcasts and to restore the view when the popup opens.
function applyDownloadState(dl) {
    if (!dl) return;
    if (dl.active) {
        downloadButton.disabled = true;
        showProgress(typeof dl.percent === "number" && !isNaN(dl.percent) ? dl.percent : NaN);
        setStatus(dl.statusText || "Downloading…");
    } else if (dl.done) {
        hideProgress();
        downloadButton.disabled = false;
        if (dl.success) {
            setStatus(dl.fellBack
                ? "✅ Finished — folder missing, saved to ~/Downloads"
                : "✅ Finished");
        } else {
            setStatus("❌ " + (dl.error || "Download Failed"));
        }
    }
}

// Live updates from the background worker while the popup is open.
chrome.runtime.onMessage.addListener((msg) => {
    if (msg && msg.type === "state") applyDownloadState(msg.state);
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

        // Hand the download to the background worker so it survives this popup
        // being closed. Progress arrives via the "state" broadcast above.
        setStatus("Starting…");
        showProgress(NaN);
        downloadButton.disabled = true;

        await chrome.runtime.sendMessage({
            cmd: "start", url, dir, title: tab.title || "",
        });
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

    // If a download is already running (popup was reopened mid-download), show
    // its live progress instead of probing the current tab.
    let dl = null;
    try {
        const resp = await chrome.runtime.sendMessage({ cmd: "getState" });
        dl = resp && resp.state;
    } catch (e) { /* worker not ready; fall through to probe */ }

    if (dl && dl.active) {
        applyDownloadState(dl);
        return;
    }
    if (dl && dl.done) {
        // Show the last result, then clear it so it isn't shown again later.
        applyDownloadState(dl);
        chrome.runtime.sendMessage({ cmd: "clearFinished" }).catch(() => {});
    }
    probeCurrentTab();
})();
