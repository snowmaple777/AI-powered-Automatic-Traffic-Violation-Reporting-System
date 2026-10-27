param(
    [Parameter(Mandatory=$true)][string]$Video,
    [string]$Output,
    [switch]$NoOverlay,
    [int]$MaxFrames = 0
)
$ErrorActionPreference = 'Stop'
# Resolve user-relative paths BEFORE changing directory.
try {
    $inputVideo = (Resolve-Path -LiteralPath $Video -ErrorAction Stop).Path
    if ($Output) { $Output = [IO.Path]::GetFullPath($Output) }
    Set-Location -LiteralPath $PSScriptRoot
    $env:PYTHONIOENCODING = 'utf-8'
    $env:MPLBACKEND = 'Agg'
    $python = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
    $settings = Join-Path $PSScriptRoot 'work_dirs/portable_settings.yaml'
    if (-not (Test-Path -LiteralPath $python) -or -not (Test-Path -LiteralPath $settings)) {
        throw 'Run install.cmd successfully first. It installs the environment, model, validation data and FFmpeg.'
    }
    if (-not $Output) { $Output = Join-Path $PSScriptRoot ('work_dirs/video_' + (Get-Date -Format 'yyyyMMdd_HHmmss_fff')) }
    $arguments = @('-u','tools/road_marking_infer.py','--settings',$settings,'--input',$inputVideo,'--out',$Output)
    if (-not $NoOverlay) { $arguments += '--overlay' }
    if ($MaxFrames -lt 0) { throw 'MaxFrames must be nonnegative' }
    if ($MaxFrames -gt 0) { $arguments += @('--max-frames', [string]$MaxFrames) }
    & $python @arguments
    if ($LASTEXITCODE -ne 0) { throw "Inference failed (exit $LASTEXITCODE). See full error above." }
    Write-Output "OUTPUT: $Output"
} catch {
    Write-Error $_
    exit 1
}
