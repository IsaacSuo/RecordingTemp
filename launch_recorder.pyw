"""Double-click entry point. Keep startup errors visible without a console."""
import os
from pathlib import Path
import sys
import traceback

root = Path(__file__).resolve().parent
os.chdir(root)


def find_ffmpeg():
    """FFMPEG_DIR, an ffmpeg\\bin folder next to the program, the system PATH, then this lab PC's old location."""
    import shutil
    candidates = [os.environ.get('FFMPEG_DIR'), root / 'ffmpeg' / 'bin', root.parent / 'ffmpeg' / 'bin']
    for folder in filter(None, candidates):
        if (Path(folder) / 'ffmpeg.exe').is_file():
            return Path(folder)
    if shutil.which('ffmpeg'):
        return None
    legacy = Path(r'D:\Program Files (x86)\ffmpeg\bin')
    return legacy if (legacy / 'ffmpeg.exe').is_file() else None


ffmpeg_bin = find_ffmpeg()
if ffmpeg_bin:
    os.environ['PATH'] = str(ffmpeg_bin) + os.pathsep + os.environ.get('PATH', '')
try:
    from multi_camera_app import main
    main()
except Exception:
    text = traceback.format_exc()
    (root / 'startup-error.log').write_text(text, encoding='utf-8')
    import tkinter as tk
    from tkinter import messagebox
    window = tk.Tk()
    window.withdraw()
    messagebox.showerror('多相机录像台启动失败', text)
    window.destroy()
