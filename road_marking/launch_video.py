import argparse
import json
import subprocess
import sys
from pathlib import Path


def ensure_environment(root):
    marker = root / 'environment_ready.json'
    try:
        ready = json.loads(marker.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        ready = {}
    if not isinstance(ready, dict) or ready.get('status') != 'ready':
        print('Setup has not completed. Run install_environment.cmd and check install_logs if it fails.')
        return False
    if Path(ready.get('root', '')).resolve() == root:
        return True
    print('Folder name/location changed. Checking the existing environment; no reinstall.', flush=True)
    try:
        result = subprocess.call([sys.executable, str(root / 'check_environment.py')], cwd=str(root))
    except OSError as error:
        print('Environment check could not start:', error)
        return False
    if result != 0:
        print('The environment check failed. See the actual error above; keep the existing .venv for diagnosis.')
        return False
    ready.update(root=str(root), python=sys.executable)
    try:
        marker.write_text(json.dumps(ready, ensure_ascii=False, indent=2), encoding='utf-8')
    except OSError as error:
        print('Check passed, but the new path could not be saved:', error)
    print('Environment check passed. Continuing.')
    return True


def main():
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser()
    parser.add_argument('video', nargs='?')
    args = parser.parse_args()
    if not ensure_environment(root):
        return 2
    video = args.video
    if not video:
        try:
            import tkinter as tk
            from tkinter.filedialog import askopenfilename
            window = tk.Tk()
            window.withdraw()
            video = askopenfilename(title='Select a video', initialdir=str(root / 'inputs'),
                filetypes=[('Videos', '*.mp4 *.avi *.mov *.mkv'), ('All files', '*.*')])
            window.destroy()
            if not video:
                return 0
        except ImportError:
            video = input('Video file path: ').strip().strip('"')
    path = Path(video).resolve()
    if not path.is_file():
        print('Video does not exist:', path)
        return 2
    return subprocess.call([sys.executable, str(root / 'video_inference_b2_rcs_diagnostic.py'), str(path)], cwd=str(root))


if __name__ == '__main__':
    sys.exit(main())
