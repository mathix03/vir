$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$pythonWorks = $false
if (Test-Path -LiteralPath $pythonPath) {
    try {
        & $pythonPath -c 'import sys' 2>$null
        $pythonWorks = $LASTEXITCODE -eq 0
    } catch {
        $pythonWorks = $false
    }
}
if (-not $pythonWorks) {
    & python -m venv (Join-Path $PSScriptRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Python 3 est requis pour creer les supports automatiques.' }
}
& $pythonPath -c 'import pycdlib; import passlib; import paramiko; import cryptography; from pyfatfs.PyFatFS import PyFatFS; from dissect.hypervisor.disk.vdi import VDI; from dissect.volume.disk.disk import Disk; from dissect.ntfs import NTFS'
if ($LASTEXITCODE -ne 0) {
    & $pythonPath -m pip install --force-reinstall -r (Join-Path $PSScriptRoot 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Installation des dependances impossible.' }
}
& (Join-Path $PSScriptRoot 'server.ps1')
