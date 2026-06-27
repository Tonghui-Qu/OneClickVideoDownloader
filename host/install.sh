#!/usr/bin/env bash
#
# Registers the OneClick Downloader native messaging host with Chrome.
#
# Usage:
#   ./install.sh <EXTENSION_ID>
#
# Get <EXTENSION_ID> from chrome://extensions after loading the unpacked
# extension (it's the long string of letters under the extension name).

set -euo pipefail

HOST_NAME="com.oneclick.downloader"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ $# -lt 1 ]; then
  echo "Error: missing extension ID."
  echo "Usage: ./install.sh <EXTENSION_ID>"
  echo "Find the ID at chrome://extensions (Developer mode -> under the extension name)."
  exit 1
fi

EXTENSION_ID="$1"

# macOS protects ~/Documents (and ~/Desktop, ~/Downloads) via TCC, so Chrome
# cannot launch a native host located there. Deploy the executable scripts to
# ~/Library/Application Support, which Chrome can access without restriction.
DEPLOY_DIR="$HOME/Library/Application Support/OneClickDownloader"
HOST_PATH="$DEPLOY_DIR/run_host.sh"

mkdir -p "$DEPLOY_DIR"
cp "$SCRIPT_DIR/run_host.sh" "$SCRIPT_DIR/host.py" "$DEPLOY_DIR/"
chmod +x "$DEPLOY_DIR/run_host.sh" "$DEPLOY_DIR/host.py"

# Chrome's native messaging host directory on macOS.
TARGET_DIR="$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts"
TARGET_FILE="$TARGET_DIR/$HOST_NAME.json"

mkdir -p "$TARGET_DIR"

cat > "$TARGET_FILE" <<EOF
{
  "name": "$HOST_NAME",
  "description": "OneClick Video Downloader native host",
  "path": "$HOST_PATH",
  "type": "stdio",
  "allowed_origins": [
    "chrome-extension://$EXTENSION_ID/"
  ]
}
EOF

echo "Installed native messaging host:"
echo "  manifest : $TARGET_FILE"
echo "  deployed : $DEPLOY_DIR (copied out of ~/Documents to avoid macOS TCC)"
echo "  host     : $HOST_PATH"
echo "  extension: $EXTENSION_ID"
echo
echo "Done. Click the toolbar button (no extension reload needed)."
echo "Re-run this script after editing host.py to redeploy the copy."
