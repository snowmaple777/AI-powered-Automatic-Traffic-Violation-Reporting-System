"""Prepare a source-only inference repository. Never publishes or edits sources."""
import argparse
import ast
import hashlib
import importlib.metadata
import json
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--out', type=Path, required=True)
    a = parser.parse_args()
    out = a.out.resolve()
    archive = out.with_suffix('.zip')
    if out.exists() or archive.exists():
        raise FileExistsError('Choose a new output directory and ZIP name')
    if not out.is_relative_to(ROOT / 'work_dirs'):
        raise ValueError('Preparation output must be inside project work_dirs')
    files = set()
    for folder in ['mmseg', 'requirements']:
        for p in (ROOT / folder).rglob('*'):
            if p.is_file() and not any(x in p.parts for x in ['__pycache__', '.mim']) and p.suffix not in ['.pyc', '.pyo']:
                files.add(p.relative_to(ROOT))
    for name in ['setup.py', 'setup.cfg', 'MANIFEST.in', 'LICENSE', 'CITATION.cff',
                 'requirements.txt', 'configs/road_marking_infer.yaml', 'configs/overlay_palette.yaml',
                 'tools/road_marking_core.py', 'tools/road_marking_infer.py',
                 'tools/road_marking_overlay.py', 'tools/verify_infer_pipeline.py',
                 'tests/test_road_marking_inference_module.py',
                 'road_marking_inference/README.md', 'road_marking_inference/overlay_palette_card.png',
                 'docs/road_marking_github_README.md',
                 'tools/prepare_road_marking_github.py']:
        files.add(Path(name))

    def config(p):
        p = p.resolve()
        relative = p.relative_to(ROOT)
        if relative in files:
            return
        files.add(relative)
        for node in ast.parse(p.read_text(encoding='utf-8-sig')).body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '_base_' for t in node.targets):
                bases = ast.literal_eval(node.value)
                for base in ([bases] if isinstance(bases, str) else bases):
                    config(p.parent / base)

    config(ROOT / 'configs/rlmd/segformer_b2_rlmd_expanded_weather_relearn_768.py')
    forbidden = {'.pth', '.pt', '.ckpt', '.mp4', '.avi', '.jpg', '.jpeg', '.npz', '.npy', '.zip'}
    if any(p.suffix.lower() in forbidden for p in files):
        raise ValueError('Unexpected model/data/video artifact in source allowlist')
    out.mkdir(parents=True)
    for relative in sorted(files):
        target = out / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)

    def put(relative, text):
        target = out / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')

    # Only display/executable location changes; model/config/core bytes stay exact.
    settings = out / 'configs/road_marking_infer.yaml'
    lines = settings.read_text(encoding='utf-8').splitlines()
    settings.write_text('\n'.join("  ffmpeg: ffmpeg  # Install FFmpeg and add its bin directory to PATH" if l.startswith('  ffmpeg:') else l for l in lines)+'\n', encoding='utf-8')
    put('.gitignore', '''# Private artifacts and generated results
data/
work_dirs/
checkpoints/
weights/
outputs/
staging*/
*.pth
*.pt
*.ckpt
*.onnx
*.mp4
*.avi
*.mov
*.mkv
*.zip
*.7z
*.tar
*.tar.gz
*.csv
.env
.env.*
*.pem
*.key
__pycache__/
*.py[cod]
*.egg-info/
.venv/
venv/
.pytest_cache/
.mypy_cache/
build/
dist/
.idea/
.vscode/
''')
    put('.gitattributes', '* -text\n# Preserve audited source/config bytes and SHA-based identities.\n*.png binary\n*.gz binary\n')
    packages = ['torch', 'torchvision', 'mmcv', 'mmengine', 'numpy', 'Pillow',
                'opencv-python', 'PyYAML', 'matplotlib', 'scipy', 'ftfy', 'regex', 'prettytable', 'packaging']
    versions = {n: importlib.metadata.version(n) for n in packages}
    put('requirements-inference.txt', '# Tested Python 3.10 environment; install torch/mmcv separately as documented.\n' +
        '\n'.join(n+'=='+v for n,v in versions.items() if n not in ['torch','torchvision','mmcv'])+'\n')
    put('environment-tested.json', json.dumps(dict(python='3.10', cuda='12.1', packages=versions,
        note='Observed working environment, not a promise of wheel availability on other systems.'), indent=2)+'\n')
    put('THIRD_PARTY_NOTICES.md', '''# Attribution and scope

This source distribution contains MMSegmentation 1.2.2 from OpenMMLab, with local
road-marking changes (including slide inference, loss and dataset code).
The original Apache-2.0 LICENSE and CITATION.cff are retained; upstream copyright
headers are preserved. This is a custom inference distribution, not an official
OpenMMLab release. External dependencies retain their own licenses.

No checkpoint, dataset or input video is distributed. Their redistribution rights
are not established by this source release. Do not infer dataset/model licenses
from the framework license. The tokenizer vocabulary bundled by MMSegmentation
is preserved as an upstream runtime resource.
''')
    put('README.md', '''# Road marking inference — best56000

台灣道路標線 25 類 SegFormer B2 的可重現影片推論模組。本版只支援經 SHA256
驗證的 best_mIoU_iter_56000.pth，不提供訓練入口，也不包含權重、資料集或影片。

## 固定方案

- FP32，純 slide 1024/683；原始 test pipeline 保留。
- EMA alpha=0.7，GPU fp16 機率儲存。
- 存在門檻 0.05%，15 幀嚴格過半投票，前 14 幀留空。
- 獨立 RGB 疊色 YAML，不變更模型 metainfo 或驗證指紋。
- 驗證失敗、缺少證明或環境指紋不同時拒絕推論，沒有 skip/force。

完整說明、已知弱點與 24 色色卡：[使用手冊](road_marking_inference/README.md)。
此手冊內 D 槽路徑為開發機範例；新電腦請使用以下指令與自己的實際路徑。

## 環境安裝

已測環境為 Windows、Python 3.10、CUDA-enabled PyTorch 2.1.2+cu121、
MMCV 2.1.0、MMEngine 0.10.7。需要 NVIDIA GPU；不自動降為 CPU 或改 precision。
FFmpeg 須支援 libx264，並將 bin 目錄加入 PATH。其他系統未完成驗證。

以下是對應已測版本的安裝指令；MMCV wheel 的可用性取決於 OS/Python/Torch。
若找不到匹配 wheel，請停止並回報，不要讓 pip 靜默編譯或改裝 mmcv-lite。

```powershell
py -3.10 -m venv .venv
$py = '.\\.venv\\Scripts\\python.exe'
& $py -m pip install torch==2.1.2 torchvision==0.16.2 --index-url https://download.pytorch.org/whl/cu121
& $py -m pip install --only-binary=:all: mmcv==2.1.0 -f https://download.openmmlab.com/mmcv/dist/cu121/torch2.1/index.html
& $py -m pip install -r requirements-inference.txt
& $py -m pip install --no-deps -e .
if ($LASTEXITCODE -ne 0) { throw '安裝失敗，請檢查完整錯誤' }
```

## 私有資產（另外提供）

1. 原始 best56000（含 config metadata）放在
   `work_dirs/segformer_b2_rlmd_expanded_weather_relearn_768/best_mIoU_iter_56000.pth`。
   SHA256 必須為 `75ccd13c8efe31a0c1a76f22ddcd3867ebdd606287af3c39ae43b6c589d96b36`。
   不可換成 stripped 權重或其他模型。
2. 固定 374 張 val：`data/rlmd_kaggle_upload/images/val/`、
   `annotations/val/`、`splits/val.txt`。必須是同一批原始影像與 mask，不能任意建立替代集。
   val.txt SHA256 為 `618aeb35daaf724a919e3ff9e446cc1482d94fb9ffa21675d7e28e14a82ca2de`。
3. 輸入影片由使用者提供。修改 YAML 的路徑欄位或命令列 input 即可。
   不需要另載 ImageNet 權重；模型從已核准的完整 checkpoint strict=True 載入。

## 驗證與執行影片（PowerShell）

先切換到本目錄，將 `$video` 改成實際影片路徑。第一次使用新電腦或環境變更時，
必須重新跑兩組完整 val；舊電腦驗證報告不隨程式發佈，不能拿來跳過驗證。

```powershell
$py = '.\\.venv\\Scripts\\python.exe'
$env:PYTHONIOENCODING = 'utf-8'
$env:MPLBACKEND = 'Agg'
& $py -u tools/verify_infer_pipeline.py --settings configs/road_marking_infer.yaml
if ($LASTEXITCODE -ne 0) { throw '驗證未通過，停止推論' }

$video = 'D:\\videos\\your_video.mp4'
$out = 'work_dirs\\video_' + (Get-Date -Format 'yyyyMMdd_HHmmss_fff')
& $py -u tools/road_marking_infer.py --settings configs/road_marking_infer.yaml `
  --input $video --out $out --overlay
if ($LASTEXITCODE -ne 0) { throw '推論失敗' }
Invoke-Item -LiteralPath $out
```

輸出 `overlay.mp4`、`pixel_counts.csv`、`presence_timeline.csv`、`events.csv`、
run identity 與完成狀態。輸出目錄必須是新目錄。影格資料夾需加 `--fps` 真實 FPS。
去掉 `--overlay` 可只輸出 CSV／事件，不需要 FFmpeg。

## 測量與限制

完整 val mIoU：512/341 **58.65745**，1024/683 **59.93841**。
線類閃爍 **14.3% → 9.3%** 來自先前 slide512 的 alpha=0.7 比較；
slide1024 相應為約 **14.15% → 9.72%**。影片沒有 GT，不能把較少閃爍當作準確率提升。
alpha=0.7 約 1–3 幀延遲是視覺估計；15 幀投票另有延遲。
right arrow、虛白線與夜間區域仍有弱點，詳細限制與捨棄方案見使用手冊。

## 本機檢查與準備上傳

```powershell
& $py tests/test_road_marking_inference_module.py -v
& $py release/check_source.py
```

上傳位置尚未指定。本目錄沒有設定 remote，也沒有執行 git push。
只將這個獨立目錄作為未來 repository 根目錄，**不要上傳原始實驗專案整包**。
`.gitignore` 排除權重、資料集、影片、CSV、環境與執行輸出；token 不要寫進程式或 README。
`.gitattributes` 保留已稽核來源原始 bytes，避免換行轉換使驗證指紋改變。
程式／資料授權說明見 LICENSE 與 THIRD_PARTY_NOTICES.md。
''')
    # Keep the reviewed, self-contained PowerShell instructions when rebuilding.
    put('README.md', (ROOT/'docs/road_marking_github_README.md').read_text(encoding='utf-8'))
    put('release/check_source.py', '''"""Read-only release integrity and leakage check; no CUDA required."""
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
''')
    manifest = dict(scope='source-only best56000 inference; no remote/push/training',
        copied_source_files=len(files), config_chain=sorted(str(p).replace('\\','/') for p in files if str(p).startswith('configs')),
        source_unchanged=all(sha(ROOT/p)==sha(out/p) for p in files if str(p).replace('\\','/')!='configs/road_marking_infer.yaml'),
        files=[dict(path=p.relative_to(out).as_posix(),size=p.stat().st_size,sha256=sha(p))
               for p in sorted(out.rglob('*')) if p.is_file()])
    put('release/source_manifest.json', json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for p in sorted(out.rglob('*')):
            if p.is_file():z.write(p,p.relative_to(out))
    with zipfile.ZipFile(archive) as z:
        if z.testzip():raise ValueError('ZIP CRC failed')
    print(json.dumps(dict(directory=str(out),zip=str(archive),files=len(manifest['files'])+1,
        source_bytes=sum(p.stat().st_size for p in out.rglob('*') if p.is_file()),
        zip_bytes=archive.stat().st_size,unchanged=manifest['source_unchanged']),ensure_ascii=False))


if __name__=='__main__':main()
