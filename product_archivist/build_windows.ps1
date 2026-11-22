$ErrorActionPreference = 'Stop'
$appDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $appDir
$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { throw 'Repository .venv missing; create it and install product_archivist/requirements.txt first.' }
& $python -m pip install -r (Join-Path $appDir 'requirements.txt')
& $python -m PyInstaller --noconfirm --clean --onefile --windowed --paths $appDir --name 'XudaaiProductArchivist' --distpath (Join-Path $appDir 'dist') --workpath (Join-Path $appDir 'build') (Join-Path $appDir 'main.py')
Write-Host "Build complete: $(Join-Path $appDir 'dist\XudaaiProductArchivist.exe')"
