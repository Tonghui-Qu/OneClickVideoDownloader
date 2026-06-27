const status = document.getElementById("status");
const button = document.getElementById("downloadButton");

button.addEventListener("click", async () => {
    try {
        status.innerText = "Getting current page...";

        const [tab] = await chrome.tabs.query({
            active: true,
            currentWindow: true
        });

        const url = tab && tab.url ? tab.url : "";

        if (!/^https?:\/\//i.test(url)) {
            status.innerText = "⚠ Open a video page first (YouTube/Instagram/TikTok)";
            return;
        }

        status.innerText = "Downloading...";

        const result = await chrome.runtime.sendNativeMessage(
            "com.oneclick.downloader",
            { url: url }
        );

        if (result && result.success) {
            status.innerText = "✅ Finished";
        } else {
            status.innerText = "❌ " + ((result && result.error) || "Download Failed");
        }

    } catch (e) {
        console.error(e);
        status.innerText = "❌ " + e.message;
    }
});