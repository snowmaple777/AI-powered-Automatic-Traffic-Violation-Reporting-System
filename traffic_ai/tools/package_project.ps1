param([Parameter(Mandatory=$true)][string]$Destination)
$ErrorActionPreference = 'Stop'
$sourceRoot = Split-Path $PSScriptRoot -Parent
$targetRoot = [IO.Path]::GetFullPath($Destination)
if (Test-Path -LiteralPath $targetRoot) { throw 'Destination must be a new directory.' }
New-Item -ItemType Directory -Path $targetRoot | Out-Null
$files = & rg --files --hidden $sourceRoot -g '!.venv/**' -g '!outputs/**'
# Restrict to source/config/docs/tests; never package personal profiles or media.
foreach ($file in $files) {
    $relative = [IO.Path]::GetRelativePath($sourceRoot, $file).Replace('\','/')
    if ($relative -match '(^|/)(\.git|\.venv|__pycache__|outputs|input|cases|scratch|\.tempmediaStorage|\.agents|\.codex)(/|$)') { continue }
    if ($relative -match '(^|/)(reporter_profile\.json|\.env.*)$') { continue }
    if ($relative -match '\.(pyc|pt|pth|onnx|mp4|avi|jpg|png|log|zip)$') { continue }
    if ($relative -match '/' -and $relative -notmatch '^(configs|docs|models|model_library|reporting|road_compensation|tests|third_party|tools)/') { continue }
    $target = Join-Path $targetRoot $relative
    New-Item -ItemType Directory -Force -Path (Split-Path $target -Parent) | Out-Null
    Copy-Item -LiteralPath $file -Destination $target
}
Write-Output $targetRoot
