[CmdletBinding()]
param([switch]$ShieldedTest, [switch]$Demo)

$ErrorActionPreference = 'Stop'
$appExecutable = Join-Path $PSScriptRoot 'NHI Communicator.exe'
$appArguments = @()
if (-not (Test-Path -LiteralPath $appExecutable -PathType Leaf)) {
    $appExecutable = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $appExecutable -PathType Leaf)) {
        $appExecutable = (Get-Command python.exe -ErrorAction Stop).Source
    }
    $appArguments += ('"' + (Join-Path $PSScriptRoot 'launch.py') + '"')
}
if ($ShieldedTest) { $appArguments += '--shielded-test' }
if ($Demo) { $appArguments += '--demo' }
$appState = Join-Path $env:LOCALAPPDATA 'NHICommunicator'
New-Item -ItemType Directory -Path $appState -Force | Out-Null
$appStart = @{FilePath=$appExecutable; WorkingDirectory=$PSScriptRoot;
    WindowStyle='Hidden'; PassThru=$true;
    RedirectStandardOutput=(Join-Path $appState 'launcher.stdout.log');
    RedirectStandardError=(Join-Path $appState 'launcher.stderr.log')}
if ($appArguments.Count) { $appStart.ArgumentList = $appArguments }
$appProcess = Start-Process @appStart
Start-Sleep -Milliseconds 750
if ($appProcess.HasExited -and $appProcess.ExitCode -ne 0) {
    $appFailure = Get-Content -Raw -LiteralPath (Join-Path $appState 'launcher.stderr.log')
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show($appFailure, 'NHI Communicator could not start') | Out-Null
}
