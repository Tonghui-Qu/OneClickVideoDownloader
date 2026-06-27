from pathlib import Path
from flask import Flask, request, jsonify
import subprocess

app = Flask(__name__)

DOWNLOAD_DIR = str(Path.home() / "Downloads")


@app.post("/download")
def download():

    data = request.get_json()

    url = data.get("url", "").strip()

    if not url:
        return jsonify({"success": False, "error": "Missing URL"}), 400

    cmd = [
        "yt-dlp",
        "--remote-components", "ejs:github",
        "--cookies-from-browser", "chrome",
        "-f", "bv*+ba/b",
        "--merge-output-format", "mp4",
        "--embed-thumbnail",
        "--embed-metadata",
        "-P", DOWNLOAD_DIR,
        url
    ]

    print("===== START DOWNLOAD =====")

    result = subprocess.run(cmd)
    
    print("===== DOWNLOAD FINISHED =====")

    if result.returncode == 0:

        return jsonify({

            "success": True,

            "message": "Finished"

        })

    return jsonify({

        "success": False,

        "message": "Download failed"

    }), 500


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8765)