const status = document.getElementById("status");
const button = document.getElementById("downloadButton");

button.addEventListener("click", async () => {
    try {
        status.innerText = "Getting current page...";

        const [tab] = await chrome.tabs.query({
            active: true,
            currentWindow: true
        });

        status.innerText = "Downloading...";

        const response = await fetch("http://127.0.0.1:8765/download", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                url: tab.url
            })
        });

        const result = await response.json();

        if (result.success) {
            status.innerText = "✅ Finished";
        } else {
            status.innerText = "❌ Download Failed";
        }

    } catch (e) {
        console.error(e);
        status.innerText = "❌ " + e.message;
    }
});