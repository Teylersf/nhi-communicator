[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
    $buildPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $buildPython)) {
        & python.exe -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the build environment.' }
    }
    & $buildPython -m pip install -r build-requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Build dependencies could not be installed.' }
    & $buildPython scripts\prepare_native_tools.py
    if ($LASTEXITCODE -ne 0) { throw 'Native dependencies could not be prepared.' }
    & (Join-Path $PSScriptRoot 'scripts\Build-HackRFLibrary.ps1')
    & (Join-Path $PSScriptRoot 'dashboard\Build-RadioControl.ps1')
    & (Join-Path $PSScriptRoot 'dashboard\Build-RadioControl.ps1') -TestFixture
    & $buildPython -m unittest discover -s dashboard -p 'test_*.py'
    if ($LASTEXITCODE -ne 0) { throw 'Offline checks failed.' }
    & $buildPython -m PyInstaller --noconfirm nhi-communicator.spec
    if ($LASTEXITCODE -ne 0) { throw 'Standalone build failed.' }
    & $buildPython scripts\make_release.py
    if ($LASTEXITCODE -ne 0) { throw 'Release packaging failed.' }
} finally {
    Pop-Location
}
