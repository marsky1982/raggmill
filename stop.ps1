$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'backend/.venv/Scripts/python.exe') (Join-Path $PSScriptRoot 'run.py') stop
exit $LASTEXITCODE
