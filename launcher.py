"""Start/reopen the local workspace, without leaving a terminal window open."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / ".runtime"


def running():
    try:
        info = json.loads((RUNTIME / "server.json").read_text(encoding="utf-8"))
        if not info["url"].startswith("http://127.0.0.1:"):
            return None
        with urllib.request.urlopen(info["url"] + "/api/ping", timeout=1) as response:
            data = json.load(response)
        if data.get("product") == "Action4 Studio":
            return info["url"]
    except (OSError, ValueError, KeyError):
        return None


def main():
    RUNTIME.mkdir(exist_ok=True)
    url = running()
    if url:
        webbrowser.open(url)
        return
    with (RUNTIME / "server.log").open("a", encoding="utf-8") as log:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        child = subprocess.Popen([sys.executable, str(ROOT / "server.py"), "--no-browser"], cwd=ROOT,
                                 stdout=log, stderr=log, creationflags=flags,
                                 env=dict(os.environ, PYTHONUTF8="1"))
    for _ in range(100):
        url = running()
        if url:
            webbrowser.open(url)
            return
        if child.poll() is not None:
            break
        time.sleep(.2)
    print("启动未完成，请查看：" + str(RUNTIME / "server.log"))
    input("按回车关闭…")


if __name__ == "__main__":
    main()
