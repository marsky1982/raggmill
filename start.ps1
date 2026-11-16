param(
    [ValidateSet('ragas','demo')][string]$Mode = 'ragas',
    [int]$BackendPort = 0,
    [int]$FrontendPort = 0,
    [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
$launcherArgs = @((Join-Path $PSScriptRoot 'run.py'), 'start', '--mode', $Mode,
    '--backend-port', [string]$BackendPort, '--frontend-port', [string]$FrontendPort)
if ($NoBrowser) { $launcherArgs += '--no-browser' }
& (Join-Path $PSScriptRoot 'backend/.venv/Scripts/python.exe') @launcherArgs
exit $LASTEXITCODE
