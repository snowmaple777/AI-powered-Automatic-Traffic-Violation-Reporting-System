"""Read-only release integrity and leakage check; no CUDA required."""
import hashlib,json
from pathlib import Path
root=Path(__file__).resolve().parents[1]
manifest=json.loads((root/'release/source_manifest.json').read_text(encoding='utf-8'))
for row in manifest['files']:
    p=root/row['path']
    if hashlib.sha256(p.read_bytes()).hexdigest()!=row['sha256']:
        raise ValueError('Missing/changed release file: '+row['path'])
for p in root.rglob('*'):
    rel=p.relative_to(root)
    if any(x in rel.parts for x in ['.git','.venv','__pycache__']):continue
    if p.is_file() and p.suffix.lower() in {'.pth','.pt','.ckpt','.mp4','.avi','.zip'}:
        raise ValueError('Private/generated artifact found; exclude before upload: '+str(rel))
assert (root/'mmseg/utils/bpe_simple_vocab_16e6.txt.gz').is_file()
print('PASS: release hashes, vocabulary and source-only artifact check')
