#Requires -Version 5.1
<#
.SYNOPSIS
    Script de lancement du VM Configurator
.PARAMETER Port
    Port HTTP du serveur (defaut : 8080)
.PARAMETER NoBrowser
    Ne pas ouvrir le navigateur automatiquement
#>
param(
    [int]$Port = 8080,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Write-Step { param($msg) Write-Host "  >> $msg" -ForegroundColor Cyan }
function Write-OK   { param($msg) Write-Host "  OK $msg" -ForegroundColor Green }
function Write-Warn { param($msg) Write-Host "  !! $msg" -ForegroundColor Yellow }
function Write-Fail { param($msg) Write-Host "  XX $msg" -ForegroundColor Red }

function Test-PortFree {
    param([int]$p)
    $tcp = New-Object System.Net.Sockets.TcpClient
    try { $tcp.Connect('127.0.0.1', $p); $tcp.Close(); return $false }
    catch { return $true }
}

Clear-Host
Write-Host ""
Write-Host "  ======================================================" -ForegroundColor Cyan
Write-Host "       VM CONFIGURATOR  --  Demarrage" -ForegroundColor Cyan
Write-Host "  ======================================================" -ForegroundColor Cyan
Write-Host ""

# -- 1. Python --
Write-Step "Verification de Python..."
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Fail "Python 3 introuvable. Installez-le depuis https://python.org"
    Read-Host "Appuyez sur Entree pour quitter"
    exit 1
}
$pyVer = & python --version 2>&1
Write-OK "Python detecte : $pyVer"

# -- 2. Virtualenv --
$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$venvWorks  = $false

if (Test-Path -LiteralPath $venvPython) {
    try {
        & $venvPython -c 'import pycdlib, passlib, paramiko, cryptography' 2>$null
        $venvWorks = ($LASTEXITCODE -eq 0)
    } catch { $venvWorks = $false }
}

if (-not $venvWorks) {
    Write-Step "Creation / mise a jour du virtualenv Python..."
    & python -m venv (Join-Path $PSScriptRoot '.venv')
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "Impossible de creer le virtualenv."
        Read-Host "Appuyez sur Entree pour quitter"
        exit 1
    }
    Write-Step "Installation des dependances (requirements.txt)..."
    & $venvPython -m pip install --quiet --force-reinstall -r (Join-Path $PSScriptRoot 'requirements.txt')
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "L'installation des dependances a echoue."
        Read-Host "Appuyez sur Entree pour quitter"
        exit 1
    }
    Write-OK "Dependances installees."
} else {
    Write-OK "Environnement Python pret."
}

# -- 3. VirtualBox --
Write-Step "Verification de VirtualBox..."
$vboxManage = 'C:\Program Files\Oracle\VirtualBox\VBoxManage.exe'
if (Test-Path $vboxManage) {
    $vboxVer = (& $vboxManage --version 2>&1) -replace "`r?`n", ''
    Write-OK "VirtualBox $vboxVer detecte."
} else {
    Write-Warn "VirtualBox introuvable. Certaines fonctions VM seront desactivees."
}

# -- 4. aria2 local (bin\aria2c.exe) --
Write-Step "Verification de aria2 (local)..."
$binDir     = Join-Path $PSScriptRoot 'bin'
$localAria2 = Join-Path $binDir 'aria2c.exe'

if (-not (Test-Path -LiteralPath $localAria2)) {
    Write-Step "aria2 absent dans bin\ -- telechargement depuis GitHub..."
    if (-not (Test-Path -LiteralPath $binDir)) { New-Item -ItemType Directory -Path $binDir | Out-Null }

    try {
        $release = Invoke-RestMethod 'https://api.github.com/repos/aria2/aria2/releases/latest'
        $asset   = $release.assets | Where-Object { $_.name -like '*win-64bit*.zip' } | Select-Object -First 1
        if (-not $asset) { throw "Asset aria2 win-64bit introuvable dans la release GitHub." }

        $zipPath = Join-Path $binDir 'aria2-tmp.zip'
        Write-Step "Telechargement de $($asset.name)..."
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zipPath -UseBasicParsing

        Write-Step "Extraction de aria2c.exe..."
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $zip   = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
        $entry = $zip.Entries | Where-Object { $_.Name -eq 'aria2c.exe' } | Select-Object -First 1
        if (-not $entry) { $zip.Dispose(); throw "aria2c.exe introuvable dans l'archive." }
        [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $localAria2, $true)
        $zip.Dispose()
        Remove-Item $zipPath -Force -ErrorAction SilentlyContinue

        Write-OK "aria2 $($release.tag_name) installe dans bin\aria2c.exe"
    } catch {
        Write-Warn "Impossible de telecharger aria2 : $_"
        Write-Warn "Les telechargements ISO seront desactives."
    }
} else {
    $ver = (& $localAria2 --version 2>&1 | Select-Object -First 1) -replace 'aria2 version ', ''
    Write-OK "aria2 $ver (local bin\aria2c.exe)"
}

# -- 5. Port disponible ? --
Write-Step "Verification du port $Port..."
if (-not (Test-PortFree $Port)) {
    Write-Fail "Le port $Port est deja utilise. Essayez : .\start.ps1 -Port 8081"
    Read-Host "Appuyez sur Entree pour quitter"
    exit 1
}
Write-OK "Port $Port disponible."

# -- 6. Ouverture du navigateur --
if (-not $NoBrowser) {
    $url = "http://localhost:$Port/"
    Write-Step "Le navigateur s'ouvrira sur $url dans 2 secondes..."
    Start-Job -ScriptBlock {
        param($u) Start-Sleep 2; Start-Process $u
    } -ArgumentList $url | Out-Null
}

# -- 7. Lancement du serveur --
Write-Host ""
Write-Host "  ======================================================" -ForegroundColor Green
Write-Host "   Serveur en cours de demarrage sur le port $Port..." -ForegroundColor Green
Write-Host "   Appuyez sur CTRL+C pour arreter." -ForegroundColor Green
Write-Host "  ======================================================" -ForegroundColor Green
Write-Host ""

& (Join-Path $PSScriptRoot 'server.ps1') -port $Port
