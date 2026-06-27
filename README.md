# OneClick Video Downloader

A Chrome extension that downloads the video on the current tab (YouTube, Instagram, TikTok, and [anything `yt-dlp` supports](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md)) with a single click. Videos are saved to your `~/Downloads` folder in the highest available quality.

It works by pairing a small Chrome extension with a local **native messaging host** (a Python script that runs `yt-dlp`). Chrome launches the helper automatically on demand — there is **no server to start manually**.

```
┌──────────────────┐   native messaging        ┌──────────────────────┐
│ Chrome extension │  (stdin/stdout, on demand) │ run_host.sh → host.py │
│  (popup button)  │ ─────────────────────────▶ │  runs yt-dlp          │
└──────────────────┘   { url } / { success }     └──────────────────────┘
                                                            │
                                                            ▼
                                                     ~/Downloads/*.mp4
```

> **Platform:** This guide targets **macOS + Google Chrome**. The host scripts and the installer (`host/install.sh`) are written for macOS.

---

## 1. Prerequisites

Install the command-line tools the downloader relies on. The easiest way is [Homebrew](https://brew.sh):

```bash
brew install yt-dlp ffmpeg deno
```

| Tool | Why it's needed |
|------|-----------------|
| `yt-dlp` | Does the actual downloading/extraction |
| `ffmpeg` | Merges video + audio and embeds thumbnail/metadata |
| `deno` | Solves YouTube's JavaScript challenges (required for YouTube) |

Python 3 is also required, but macOS already includes it (the host uses Homebrew's Python if present, otherwise the system one).

---

## 2. Install the Chrome extension

1. Open `chrome://extensions` in Google Chrome.
2. Turn on **Developer mode** (top-right toggle).
3. Click **Load unpacked**.
4. Select the **`extension/`** subfolder of this project (the folder that directly contains `manifest.json`), e.g.:

   ```
   /path/to/OneClickVideoDownloader/extension
   ```

   > Tip: in the macOS file dialog press **Cmd+Shift+G** and paste the full path to the `extension` folder. Do **not** select the project root — `manifest.json` lives inside `extension/`.

5. The extension **OneClick Video Downloader** now appears in the list. **Copy its ID** (the long string of letters shown under the name) — you need it in the next step.

---

## 3. Register the native host

This step tells Chrome how to launch the downloader helper, and deploys the helper to a location Chrome is allowed to run from.

```bash
cd /path/to/OneClickVideoDownloader/host
./install.sh <YOUR_EXTENSION_ID>
```

Replace `<YOUR_EXTENSION_ID>` with the ID you copied in step 2.

What this does:

- Copies `host.py` and `run_host.sh` to `~/Library/Application Support/OneClickDownloader/`
  (macOS blocks Chrome from launching scripts inside `~/Documents`, `~/Desktop`, etc., so the helper is deployed here).
- Writes the native messaging manifest to
  `~/Library/Application Support/Google/Chrome/NativeMessagingHosts/com.oneclick.downloader.json`,
  locked to your extension ID.

---

## 4. Use it

1. Go to a video page (YouTube, Instagram, TikTok, …).
2. Click the **OneClick Video Downloader** icon in the toolbar.
3. Click **⬇ Download Current Video**.
4. The status shows **Downloading…** → **✅ Finished**, and the `.mp4` appears in `~/Downloads`.

That's it — you never have to start a server or run anything manually again.

---

## Updating / re-installing

- **Editing `host.py` or `run_host.sh`:** Chrome runs the *deployed copy*, so re-run the installer to redeploy:

  ```bash
  cd /path/to/OneClickVideoDownloader/host
  ./install.sh <YOUR_EXTENSION_ID>
  ```

- **Editing `extension/` files** (`popup.js`, `manifest.json`, …): click the **refresh icon** on the extension card in `chrome://extensions`.

- **Extension ID changed?** If you remove and re-add the extension (or load it from a different path), its ID may change. Re-run `./install.sh <NEW_ID>` so the host matches.

- **Keep yt-dlp current** (sites change often):

  ```bash
  brew upgrade yt-dlp
  ```

---

## Troubleshooting

The host writes logs on every run — check these first:

| Log file | What it tells you |
|----------|-------------------|
| `/tmp/oneclick-wrapper.log` | Whether Chrome actually launched the helper |
| `~/oneclick-downloader.log` | The exact `yt-dlp` command and its full error output |

Common issues:

- **"Native host has exited."**
  Chrome launched the helper but it failed (or couldn't start). Check `/tmp/oneclick-wrapper.log`:
  - No new line after you click → Chrome isn't launching the helper. Make sure you ran `./install.sh <ID>` with the **current** extension ID, and that you're on standard Google Chrome.
  - A `WRAPPER started` line appears → the helper ran; check `~/oneclick-downloader.log` for the real `yt-dlp` error.

- **"Specified native messaging host not found." / "…is forbidden."**
  The manifest isn't installed or the extension ID doesn't match. Re-run `./install.sh <YOUR_EXTENSION_ID>`.

- **"⚠ Open a video page first"**
  You clicked while on a `chrome://` page or a New Tab. Switch to an actual `http(s)` video tab.

- **Instagram fails with "Instagram API is not granting access" / HTTP 400**
  Instagram frequently blocks `yt-dlp`. Make sure you're **logged into Instagram in Chrome** (the host reads your Chrome cookies via `--cookies-from-browser chrome`) and run `brew upgrade yt-dlp`. Some private/restricted posts may never be downloadable.

- **YouTube only downloads low quality or errors on formats**
  Ensure `deno` is installed (`brew install deno`) — it's required to unlock YouTube's high-resolution streams.

---

## Project structure

```
OneClickVideoDownloader/
├── extension/              # The Chrome extension (load this folder)
│   ├── manifest.json       # MV3 manifest (nativeMessaging permission)
│   ├── popup.html          # Toolbar popup UI
│   └── popup.js            # Sends current tab URL to the native host
└── host/                   # The local download helper
    ├── run_host.sh         # Bash launcher Chrome execs (picks a real python3)
    ├── host.py             # Native messaging host; runs yt-dlp
    ├── install.sh          # Deploys the helper + registers it with Chrome
    └── manifest.json       # Native-host manifest template (install.sh generates the real one)
```

---

## How downloads are configured

The host downloads the best video + best audio and merges them to MP4, using your Chrome login cookies:

```
yt-dlp --cookies-from-browser chrome -f "bv*+ba/b" \
       --merge-output-format mp4 --embed-thumbnail --embed-metadata \
       -P ~/Downloads <url>
```

You can tweak this in `host/host.py` (then re-run `install.sh` to redeploy).
