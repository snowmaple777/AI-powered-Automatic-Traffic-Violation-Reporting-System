"""Select a video or accept drag-and-drop; run the default hybrid pipeline."""
import subprocess
import sys
from pathlib import Path

def main():
    root = Path(__file__).resolve().parent
    videos = [v for v in sys.argv[1:] if v]
    if not videos:
        import tkinter as tk
        from tkinter.filedialog import askopenfilenames
        window = tk.Tk()
        window.withdraw()
        videos = list(askopenfilenames(title='Select videos', initialdir=str(root / 'inputs'),
                    filetypes=[('Videos', '*.mp4 *.avi *.mov *.mkv'), ('All files', '*.*')]))
        window.destroy()
    if not videos:
        return 0
    return subprocess.call([sys.executable, str(root / 'infer_video.py'), *videos], cwd=root)

if __name__ == '__main__':
    raise SystemExit(main())
