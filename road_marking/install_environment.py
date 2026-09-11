"""Create and verify this package's own environment; no external venv paths."""
import argparse
import json
import os
import struct
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / '.venv'
READY = ROOT / 'environment_ready.json'


def install():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 10) or struct.calcsize('P') != 8:
        print('STOP: this package requires 64-bit Python 3.10.')
        return 2
    if args.check_only:
        print('PASS: Python 3.10 x64:', sys.executable)
        return 0
    logs = ROOT / 'install_logs'
    logs.mkdir(exist_ok=True)
    path = logs / ('install_' + datetime.now().strftime('%Y%m%d_%H%M%S') + '.log')
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    with path.open('w', encoding='utf-8') as log:
        def say(message):
            print(message, flush=True)
            log.write(message + '\n')
            log.flush()

        def run(label, command):
            say('\nSTEP: ' + label)
            process = subprocess.Popen(command, cwd=str(ROOT), env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       encoding='utf-8', errors='replace')
            for line in process.stdout:
                say(line.rstrip())
            result = process.wait()
            if result:
                raise RuntimeError('{} failed (exit {}).'.format(label, result))

        try:
            # Invalidate the readiness marker without removing or replacing an environment.
            READY.write_text(json.dumps({'status': 'installing'}), encoding='utf-8')
            python = VENV / 'Scripts/python.exe'
            if not VENV.exists():
                run('Create local .venv', [sys.executable, '-m', 'venv', str(VENV)])
            elif not python.is_file():
                raise RuntimeError('Existing .venv is incomplete. Rename it to .venv_old, then retry. Nothing was deleted.')
            run('Verify local Python', [str(python), '-c',
                "import sys,struct; assert sys.version_info[:2]==(3,10) and struct.calcsize('P')==8; print(sys.executable)"])
            pip = [str(python), '-m', 'pip']
            run('Install packaging tools', pip + ['install', '--upgrade', 'pip', 'wheel', 'setuptools==68.2.2'])
            run('Install NumPy', pip + ['install', 'numpy==1.26.4'])
            run('Install PyTorch (large download)', pip + ['install', 'torch==2.1.2', 'torchvision==0.16.2',
                '--index-url', 'https://download.pytorch.org/whl/cu121'])
            run('Install MMCV wheel', pip + ['install', 'mmcv==2.1.0', '--only-binary=mmcv',
                '-f', 'https://download.openmmlab.com/mmcv/dist/cu121/torch2.1/index.html'])
            run('Install inference dependencies', pip + ['install', '-r', 'requirements.txt'])
            run('Install local MMSegmentation', pip + ['install', '-e', '.', '--no-deps', '--no-build-isolation'])
            run('Check model and environment', [str(python), 'check_environment.py'])
            run('Check one actual model forward pass', [str(python), 'smoke_inference.py'])
            READY.write_text(json.dumps({'status': 'ready', 'root': str(ROOT),
                'python': str(python), 'verified_at': datetime.now().isoformat()}, indent=2), encoding='utf-8')
            say('\nINSTALLATION COMPLETE. Now use run_video.cmd.')
            return 0
        except (OSError, RuntimeError) as error:
            say('\nINSTALLATION STOPPED: ' + str(error))
            say('Do not run the inference command yet. Send this log: ' + str(path))
            return 1


if __name__ == '__main__':
    sys.exit(install())
