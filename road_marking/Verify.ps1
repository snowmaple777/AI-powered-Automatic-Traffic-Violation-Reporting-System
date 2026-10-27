param()
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONIOENCODING = 'utf-8'
$env:MPLBACKEND = 'Agg'
try {
    $python = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $python) -or -not (Test-Path -LiteralPath 'work_dirs/portable_settings.yaml')) {
        throw 'Run install.cmd first.'
    }
    & $python -u tools/verify_infer_pipeline.py --settings work_dirs/portable_settings.yaml
    if ($LASTEXITCODE -ne 0) { throw "Verification failed (exit $LASTEXITCODE). See full error above." }
    Write-Output 'VERIFICATION PASSED. You can now run run_video.cmd with your video path.'
} catch {
    Write-Error $_
    exit 1
}
