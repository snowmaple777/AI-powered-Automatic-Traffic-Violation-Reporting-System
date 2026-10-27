"""Read-only release integrity and leakage check; no CUDA required."""
import hashlib,json
from pathlib import Path
root=Path(__file__).resolve().parents[1]
manifest=json.loads((root/'release/source_manifest.json').read_text(encoding='utf-8'))
for row in manifest['files']:
    p=root/row['path']
    if hashlib.sha256(p.read_bytes()).hexdigest()!=row['sha256']:
        raise ValueError('Missing/changed release file: '+row['path'])
# Source manifest excludes generated outputs and runtime assets. Runtime assets
# are checked into the branch and verified separately by setup.
for row in manifest['files']:
    if Path(row['path']).suffix.lower() in {'.pth','.pt','.ckpt','.mp4','.avi','.zip'}:
        raise ValueError('Binary asset must be a Release asset, not tracked source: '+row['path'])
assert (root/'mmseg/utils/bpe_simple_vocab_16e6.txt.gz').is_file()
print('PASS: source hashes, vocabulary and tracked-source artifact check')
