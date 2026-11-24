# =============================================================================
#  下載並檢查 Traffic AI（master 分支）需要的模型權重
#
#  用法：雙擊「下載模型.cmd」，或在專案資料夾的 PowerShell 執行
#        powershell -ExecutionPolicy Bypass -File .\download_models.ps1
#
#  每個檔案的來源都「釘」在固定的 commit 上，下載後一定比對 SHA-256，
#  跟「模型清單.txt」/ models\perception_manifest.json 一致才算成功。
#  已經存在且正確的檔案會直接跳過，所以重複執行也沒關係。
# =============================================================================
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
$Repo = "snowmaple777/AI-powered-Automatic-Traffic-Violation-Reporting-System"

# 來源 commit（用 commit 而不是分支名稱，之後分支再更新也不會拿錯檔案）
$MainCommit  = "9a1f58cac97442eb7b68114e100c1a2f8101d3a2"   # main
$LightCommit = "ff6d731e95a4c6b2ee8af20c9dc42555cd46ec1d"   # feature/traffic-light-model
$RoadCommit  = "e4b4c1a1a9f2e0fe4cbb8814327ecf44bbe02fce"   # feature/road-marking（Git LFS）

# Kind：git = 從本機 git 物件取出；lfs = 從 GitHub LFS 網址下載；head = 目前分支裡的檔案（修正換行）
$Models = @(
    @{ Name = "車輛模型 YOLO26s";      Target = "models/vehicle/yolo26s.onnx";
       Kind = "git"; Ref = $MainCommit; Path = "perception/checkpoints/yolo26s.onnx";
       Sha = "f2b02a290c0a654b70d91128a35291d8466f66dc2f095ca6a7085a4fdc2db055" },
    @{ Name = "車牌定位";              Target = "models/plate/license-plate-finetune-v1s.onnx";
       Kind = "git"; Ref = $MainCommit; Path = "perception/checkpoints/license-plate-finetune-v1s.onnx";
       Sha = "fa0aa53e802444b1dc99904060d65f4412bb58591e5b4eb9fef054f9df5cbece" },
    @{ Name = "車距估測 Depth Anything"; Target = "models/depth/depth_anything_v2_metric_vkitti_vits.onnx";
       Kind = "git"; Ref = $MainCommit; Path = "perception/checkpoints/depth_anything_v2_metric_vkitti_vits.onnx";
       Sha = "528467b574d0198657f28d8c5d0dc85ad3ce63313015c4145697b1cdf1b0fe11" },
    @{ Name = "交通燈 YOLO26s";        Target = "models/traffic_light/traffic_light_best.pt";
       Kind = "git"; Ref = $LightCommit; Path = "traffic-light-model/models/traffic_light_best.pt";
       Sha = "3ff0b2e2a039ed8302a63ee5c12437389cb32760329a57cd8b21537c789d62cb" },
    @{ Name = "道路標線 SegFormer B2";  Target = "models/road_marking/segformer_b2_rlmd_best56000.pth";
       Kind = "lfs"; Ref = $RoadCommit; Path = "road_marking/work_dirs/segformer_b2_rlmd_expanded_weather_relearn_768/best_mIoU_iter_56000.pth";
       Sha = "75ccd13c8efe31a0c1a76f22ddcd3867ebdd606287af3c39ae43b6c589d96b36" },
    @{ Name = "OCR 字典";              Target = "models/ocr/ppocrv6_dict.txt";
       Kind = "head"; Path = "models/ocr/ppocrv6_dict.txt";
       Sha = "b5f2bfe2bdd9448429e3e82b51c789775d9b42f2403d082b00662eb77e401c5d" },
    @{ Name = "OCR 設定 inference.yml"; Target = "models/ocr/PP-OCRv6_taiwan_infer/inference.yml";
       Kind = "head"; Path = "models/ocr/PP-OCRv6_taiwan_infer/inference.yml";
       Sha = "965b3350c145a30e07b3febb4b63fe0acf11c5e98ae8b69f715b163fb7a85c13" },
    @{ Name = "OCR 模型 inference.json"; Target = "models/ocr/PP-OCRv6_taiwan_infer/inference.json";
       Kind = "head"; Path = "models/ocr/PP-OCRv6_taiwan_infer/inference.json";
       Sha = "98ada7422100554f89cff5dc1a4062ef295c3ccbf39cfd91c839670fdd22a1c2" },
    @{ Name = "OCR 權重 pdiparams";     Target = "models/ocr/PP-OCRv6_taiwan_infer/inference.pdiparams";
       Kind = "head"; Path = "models/ocr/PP-OCRv6_taiwan_infer/inference.pdiparams";
       Sha = "a2403aadb52bad4f623ff9d88c0c8703d14d0077d065ab92d623e39f848b9341" }
)

function Get-Sha256($file) {
    if (-not (Test-Path -LiteralPath $file)) { return "" }
    return (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLower()
}

# 執行 git 並回傳結束代碼（不用 PowerShell 的 2>$null，避免 Windows PowerShell 把 git 的訊息當成錯誤）
function Invoke-Git([string]$argLine, [switch]$Show) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = "git"; $psi.Arguments = $argLine; $psi.WorkingDirectory = $Root
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = -not $Show; $psi.RedirectStandardError = -not $Show
    $p = [System.Diagnostics.Process]::Start($psi)
    if (-not $Show) { $null = $p.StandardOutput.ReadToEndAsync(); $null = $p.StandardError.ReadToEndAsync() }
    $p.WaitForExit()
    return $p.ExitCode
}

# 用 git cat-file 把 git 裡的檔案原封不動（不轉換換行）寫成檔案
function Save-GitBlob($spec, $outFile) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = "git"
    $psi.Arguments = "cat-file blob `"$spec`""
    $psi.WorkingDirectory = $Root
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $p = [System.Diagnostics.Process]::Start($psi)
    $errTask = $p.StandardError.ReadToEndAsync()
    $fs = [System.IO.File]::Create($outFile)
    try { $p.StandardOutput.BaseStream.CopyTo($fs) } finally { $fs.Close() }
    $p.WaitForExit()
    if ($p.ExitCode -ne 0) { throw "git 找不到 $spec：$($errTask.Result.Trim())" }
}

function Save-Url($url, $outFile) {
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($curl) {
        & $curl.Source -L --fail --retry 3 --progress-bar -o $outFile $url
        if ($LASTEXITCODE -ne 0) { throw "下載失敗（curl 代碼 $LASTEXITCODE）：$url" }
    } else {
        Invoke-WebRequest -Uri $url -OutFile $outFile -UseBasicParsing
    }
}

Write-Host ""
Write-Host "=== Traffic AI 模型下載 ===" -ForegroundColor Cyan
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw "找不到 git，請先安裝 Git for Windows" }

# 1. 讓 git 之後不要再改 models 底下檔案的換行（只寫在本機 .git\info\attributes，不影響別人）
$attr = Join-Path $Root ".git/info/attributes"
if (Test-Path (Join-Path $Root ".git") -PathType Container) {
    New-Item -ItemType Directory -Force -Path (Split-Path $attr) | Out-Null
    $line = "models/** -text"
    if (-not ((Test-Path $attr) -and (Select-String -LiteralPath $attr -SimpleMatch $line -Quiet))) {
        Add-Content -LiteralPath $attr -Value $line -Encoding ASCII
        Write-Host "已設定：models 底下的檔案不做換行轉換（.git\info\attributes）"
    }
}

# 2. 確認本機有這些 commit；沒有的話先 git fetch
$needFetch = $false
foreach ($c in @($MainCommit, $LightCommit)) {
    if ((Invoke-Git "cat-file -e $c^{commit}") -ne 0) { $needFetch = $true }
}
if ($needFetch) {
    Write-Host "本機缺少部分 commit，執行 git fetch origin ..."
    if ((Invoke-Git "fetch origin" -Show) -ne 0) { throw "git fetch 失敗，請確認網路" }
}

# 3. 逐一處理
$results = @()
foreach ($m in $Models) {
    $target = Join-Path $Root $m.Target
    $status = ""
    Write-Host ""
    Write-Host ("[{0}] {1}" -f $m.Name, $m.Target) -ForegroundColor Yellow
    if ((Get-Sha256 $target) -eq $m.Sha) {
        Write-Host "  已存在且 SHA-256 正確，跳過" -ForegroundColor Green
        $results += [pscustomobject]@{ 模型 = $m.Name; 結果 = "已存在 OK" }
        continue
    }
    New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null
    $tmp = "$target.download"
    try {
        switch ($m.Kind) {
            "git"  { Write-Host "  從 git 取出 $($m.Ref.Substring(0,7)):$($m.Path)"; Save-GitBlob "$($m.Ref):$($m.Path)" $tmp }
            "head" { Write-Host "  重新從 git 取出原始檔（修正 Windows 換行）"; Save-GitBlob "HEAD:$($m.Path)" $tmp }
            "lfs"  {
                $url = "https://media.githubusercontent.com/media/$Repo/$($m.Ref)/$($m.Path)"
                Write-Host "  從 GitHub LFS 下載（約 100 MB）"; Save-Url $url $tmp
            }
        }
        $got = Get-Sha256 $tmp
        if ($got -ne $m.Sha) { throw "SHA-256 不符：拿到 $got" }
        Move-Item -LiteralPath $tmp -Destination $target -Force
        Write-Host "  完成，SHA-256 正確" -ForegroundColor Green
        $results += [pscustomobject]@{ 模型 = $m.Name; 結果 = "已下載 OK" }
    } catch {
        if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force }
        Write-Host "  失敗：$($_.Exception.Message)" -ForegroundColor Red
        $results += [pscustomobject]@{ 模型 = $m.Name; 結果 = "失敗" }
    }
}

Write-Host ""
Write-Host "=== 結果 ===" -ForegroundColor Cyan
foreach ($r in $results) {
    $color = "Green"; if ($r.結果 -eq "失敗") { $color = "Red" }
    Write-Host ("  {0,-8}  {1}" -f $r.結果, $r.模型) -ForegroundColor $color
}
Write-Host "（models\ocr\PP-OCRv6_rec_small.onnx 是選用的純 ONNX 替代，git 裡沒有這個檔，預設設定用不到）"
if ($results | Where-Object { $_.結果 -eq "失敗" }) { exit 1 }
