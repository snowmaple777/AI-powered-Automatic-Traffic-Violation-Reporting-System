"""Verify actual weights, rejecting missing files and Git LFS pointers."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    manifest = ROOT / 'models/weights_manifest.json'
    if not manifest.is_file():
        raise SystemExit('Use the partner release ZIP containing weights_manifest.json.')
    failed = []
    for item in json.loads(manifest.read_text(encoding='utf-8')):
        p = ROOT / item['path']
        digest = hashlib.sha256()
        if p.is_file():
            with p.open('rb') as source:
                for chunk in iter(lambda:source.read(1024*1024),b''):
                    digest.update(chunk)
        if not p.is_file() or p.stat().st_size != item['bytes'] or digest.hexdigest()!=item['sha256']:
            failed.append(item['path'])
        else:
            print('OK:',item['path'])
    if failed:
        raise SystemExit('Invalid/missing models: '+', '.join(failed))
    print('All model weights verified.')


if __name__ == '__main__':
    main()
