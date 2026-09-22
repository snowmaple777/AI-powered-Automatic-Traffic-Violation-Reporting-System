"""Build a partner ZIP containing source and all locally available model weights."""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SKIP = {'.git', '.venv', '__pycache__', 'outputs', 'input', 'cases', 'scratch',
        '.tempmediaStorage', '.agents', '.codex', '.idea', '.vscode'}


def main():
    destination = ROOT / 'outputs/traffic_ai-partner-20260922.zip'
    destination.parent.mkdir(exist_ok=True)
    weights = []
    paths = []
    for folder, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP]
        for name in files:
            p = Path(folder) / name
            if name == 'reporter_profile.json' or name.startswith('.env'):
                continue
            if p.suffix.lower() in {'.pyc', '.mp4', '.avi', '.jpg', '.png', '.log', '.zip'}:
                continue
            paths.append(p)
            if p.suffix.lower() in {'.pt', '.pth', '.onnx'}:
                weights.append({'path':p.relative_to(ROOT).as_posix(), 'bytes':p.stat().st_size,
                                'sha256':hashlib.file_digest(p.open('rb'),'sha256').hexdigest()
                                if hasattr(hashlib,'file_digest') else hashlib.sha256(p.read_bytes()).hexdigest()})
    for config in ('default', 'full', 'onnx_vehicle'):
        p = ROOT / 'configs' / (config+'.json')
        for model in json.loads(p.read_text(encoding='utf-8'))['models'].values():
            weight = model.get('params',{}).get('weights')
            if weight and not (p.parent/weight).is_file():
                raise FileNotFoundError(weight)
    packages = {}
    for name in ('numpy','opencv-python','torch','torchvision','ultralytics','lap','mmengine',
                 'mmcv-lite','filelock','onnxruntime','onnx','rapidocr-onnxruntime'):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    with zipfile.ZipFile(destination,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
        for p in sorted(paths):
            archive.write(p, 'traffic_ai/'+p.relative_to(ROOT).as_posix())
        archive.writestr('traffic_ai/models/weights_manifest.json',json.dumps(weights,indent=2))
        archive.writestr('traffic_ai/reference_environment.json',json.dumps({
            'python':platform.python_version(),'platform':platform.platform(),'packages':packages},indent=2))
    with zipfile.ZipFile(destination) as archive:
        assert archive.testzip() is None
    print(json.dumps({'archive':str(destination),'bytes':destination.stat().st_size,
                      'weights':len(weights),'sha256':hashlib.sha256(destination.read_bytes()).hexdigest()}))


if __name__ == '__main__':
    main()
