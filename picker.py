"""Isolated native dialogs, keeping Tk's UI thread separate from the web server."""
import json
import sys
import tkinter as tk
from tkinter import filedialog

root = tk.Tk()
root.withdraw()
root.attributes("-topmost", True)
try:
    kind = sys.argv[1]
    if kind == "files":
        result = list(filedialog.askopenfilenames(title="批量选择视频", filetypes=[("视频", "*.mp4 *.MP4 *.mov *.MOV *.m4v *.M4V"), ("所有文件", "*.*")]))
    else:
        result = filedialog.askdirectory(title="选择输出文件夹" if kind == "output" else "选择视频文件夹", mustexist=True)
    print(json.dumps(result, ensure_ascii=True))
finally:
    root.destroy()
