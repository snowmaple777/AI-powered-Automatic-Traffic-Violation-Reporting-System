param()
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONIOENCODING = 'utf-8'
$env:MPLBACKEND = 'Agg'

function Invoke-Checked {
    param([string]$Executable, [string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed (exit $LASTEXITCODE): $Executable $($Arguments -join ' ')" }
}

try {
    # Source ZIPs can be extracted into two repeated, long repository names.
    # Check before creating venv: pip itself may then exceed Windows MAX_PATH.
    $pathSettings = Get-ItemProperty -LiteralPath 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name LongPathsEnabled -ErrorAction SilentlyContinue
    $pipImportPath = Join-Path $PSScriptRoot '.venv\Lib\site-packages\pip\_vendor\urllib3\packages\ssl_match_hostname\_implementation.py'
    if ($pathSettings.LongPathsEnabled -ne 1 -and $pipImportPath.Length -ge 260) {
        throw "Installation path exceeds Windows MAX_PATH ($($pipImportPath.Length) characters for pip). Copy the project to a short NEW folder such as D:\rm_infer, EXCLUDING .venv, then run install.cmd there. Do not copy or reuse the old .venv. No files or Windows settings were changed."
    }
    if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
        if (-not (Get-Command py -ErrorAction SilentlyContinue)) { throw 'Install 64-bit Python 3.10 with Python Launcher first.' }
        Invoke-Checked 'py' @('-3.10','-m','venv','.venv')
    }
    $python = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
    Invoke-Checked $python @('-c','import sys; sys.exit(sys.version_info[:2] != (3,10))')
    & $python -m pip --version
    if ($LASTEXITCODE -ne 0) {
        throw 'The local .venv pip is incomplete or unusable. Use a short project path and create a fresh .venv; do not copy an existing environment.'
    }
    Invoke-Checked $python @('-m','pip','install','setuptools==68.2.2','wheel==0.41.3')
    Invoke-Checked $python @('-m','pip','install','-r','requirements-inference.txt')
    Invoke-Checked $python @('-m','pip','install','torch==2.1.2','torchvision==0.16.2','--index-url','https://download.pytorch.org/whl/cu121')
    # Wheels only; never fall back to mmcv-lite, a source build or another ABI.
    Invoke-Checked $python @('-m','pip','install','mmcv==2.1.0','--no-deps','--only-binary=:all:','--no-index','-f','https://download.openmmlab.com/mmcv/dist/cu121/torch2.1/index.html')
    Invoke-Checked $python @('-m','pip','install','--no-deps','--no-build-isolation','-e','.')
    Invoke-Checked $python @('tools/setup_portable.py')
    Write-Output 'ENVIRONMENT READY. No inference/validation was run. Run verify.cmd yourself once, then use run_video.cmd.'
} catch {
    Write-Error $_
    exit 1
}
