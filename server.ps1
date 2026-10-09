param([int]$port = 8080)
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'automation-api.ps1')

function Send-HttpResponse {
    param(
        [System.Net.HttpListenerResponse]$res,
        $content = "",
        [string]$contentType = "text/plain",
        [int]$statusCode = 200
    )
    try {
        $res.StatusCode = $statusCode
        $res.ContentType = $contentType
        $bytes = if ($content -is [byte[]]) { $content } else { [System.Text.Encoding]::UTF8.GetBytes([string]$content) }
        $res.ContentLength64 = $bytes.Length
        $res.OutputStream.Write($bytes, 0, $bytes.Length)
    } catch {
        # Client may have aborted the connection
    } finally {
        try { $res.Close() } catch {}
    }
}

Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Text;
using System.Diagnostics;

public class VBoxFocusHelper {
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc enumProc, IntPtr lParam);
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);
    [DllImport("user32.dll")] public static extern void SwitchToThisWindow(IntPtr hWnd, bool fAltTab);
    [DllImport("user32.dll")] public static extern bool AllowSetForegroundWindow(int dwProcessId);
    [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
    [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);

    public struct RECT { public int Left, Top, Right, Bottom; }
    public static readonly IntPtr HWND_TOPMOST = new IntPtr(-1);
    public static readonly IntPtr HWND_NOTOPMOST = new IntPtr(-2);
    public const uint SWP_NOMOVE = 0x0002;
    public const uint SWP_NOSIZE = 0x0001;
    public const uint SWP_SHOWWINDOW = 0x0040;
    public const int SW_RESTORE = 9;
    public const int ASFW_ANY = -1;

    public static IntPtr FindVMWindow(string vmName) {
        IntPtr exactMatch = IntPtr.Zero;
        IntPtr genericMatch = IntPtr.Zero;

        EnumWindows((hWnd, lParam) => {
            if (!IsWindowVisible(hWnd)) return true;
            uint pid;
            GetWindowThreadProcessId(hWnd, out pid);
            Process p = null;
            try { p = Process.GetProcessById((int)pid); } catch {}
            if (p != null && (p.ProcessName.IndexOf("VirtualBox", StringComparison.OrdinalIgnoreCase) >= 0 || p.ProcessName.IndexOf("VBox", StringComparison.OrdinalIgnoreCase) >= 0 || p.ProcessName.IndexOf("vmware", StringComparison.OrdinalIgnoreCase) >= 0)) {
                RECT rc;
                GetWindowRect(hWnd, out rc);
                int w = rc.Right - rc.Left;
                int h = rc.Bottom - rc.Top;
                if (w > 200 && h > 150) {
                    StringBuilder sbTitle = new StringBuilder(512);
                    GetWindowText(hWnd, sbTitle, 512);
                    string title = sbTitle.ToString();

                    if (!string.IsNullOrEmpty(vmName) && title.IndexOf(vmName, StringComparison.OrdinalIgnoreCase) >= 0) {
                        exactMatch = hWnd;
                        return false;
                    }
                    if (genericMatch == IntPtr.Zero && (title.IndexOf("VirtualBox", StringComparison.OrdinalIgnoreCase) >= 0 || title.IndexOf("VMware", StringComparison.OrdinalIgnoreCase) >= 0)) {
                        genericMatch = hWnd;
                    }
                }
            }
            return true;
        }, IntPtr.Zero);

        return (exactMatch != IntPtr.Zero) ? exactMatch : genericMatch;
    }

    public static bool Bring(string vmName) {
        IntPtr target = FindVMWindow(vmName);
        if (target == IntPtr.Zero) return false;

        AllowSetForegroundWindow(ASFW_ANY);

        IntPtr fgHwnd = GetForegroundWindow();
        uint fgPid;
        uint fgThread = GetWindowThreadProcessId(fgHwnd, out fgPid);
        uint curThread = GetCurrentThreadId();

        if (fgThread != 0 && fgThread != curThread) {
            AttachThreadInput(curThread, fgThread, true);
        }

        ShowWindow(target, SW_RESTORE);
        BringWindowToTop(target);
        SetWindowPos(target, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW);
        SetWindowPos(target, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW);
        SwitchToThisWindow(target, true);
        SetForegroundWindow(target);

        if (fgThread != 0 && fgThread != curThread) {
            AttachThreadInput(curThread, fgThread, false);
        }

        return true;
    }
}
"@

$listener = New-Object System.Net.HttpListener
$listener.Prefixes.Add("http://localhost:$port/")
$listener.Prefixes.Add("http://127.0.0.1:$port/")

$publicDir = Join-Path $PSScriptRoot "public"
$deployDir = Join-Path $PSScriptRoot "deployments"
$isosDir = Join-Path $PSScriptRoot "isos"

if (-not (Test-Path $deployDir)) { New-Item -ItemType Directory -Path $deployDir | Out-Null }
if (-not (Test-Path $isosDir)) { New-Item -ItemType Directory -Path $isosDir | Out-Null }

$vboxSvc = "C:\Program Files\Oracle\VirtualBox\VBoxSVC.exe"
if (Test-Path $vboxSvc) {
    if (-not (Get-Process VBoxSVC -ErrorAction SilentlyContinue)) {
        Start-Process -FilePath $vboxSvc -ArgumentList "--auto-shutdown" -WindowStyle Hidden
    }
}

try {
    $listener.Start()
    Write-Host "========================================================" -ForegroundColor Cyan
    Write-Host "  VM APPLIANCE MANAGER - NATIVE VIRTUALBOX EDITION    " -ForegroundColor Cyan
    Write-Host "========================================================" -ForegroundColor Cyan
    Write-Host "Serveur demarre sur http://localhost:$port/" -ForegroundColor Green
    Write-Host "Dossier des ISOs : $isosDir" -ForegroundColor Yellow
    Write-Host "Appuyez sur CTRL+C pour arreter le serveur."
    Write-Host "========================================================" -ForegroundColor Cyan
} catch {
    Write-Error "Impossible de demarrer le serveur sur le port $port. Il est peut-etre deja utilise."
    exit
}

try {
    while ($listener.IsListening) {
        $context = $listener.GetContext()
        try {
            $request = $context.Request
            $response = $context.Response
        
        $path = $request.Url.LocalPath
        $method = $request.HttpMethod

        # CORS
        if ($method -eq "OPTIONS") {
            $response.Headers.Add("Access-Control-Allow-Origin", "*")
            $response.Headers.Add("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            $response.Headers.Add("Access-Control-Allow-Headers", "Content-Type")
            Send-HttpResponse -res $response -content "" -statusCode 200
            continue
        }

        # API: System Info (Adaptation au matériel)
        if ($method -eq "GET" -and $path -eq "/api/system-info") {
            $cpu = (Get-CimInstance Win32_ComputerSystem).NumberOfLogicalProcessors
            $ramBytes = (Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory
            $ramGB = [math]::Floor($ramBytes / 1GB)
            
            $driveLetter = (Split-Path $PSScriptRoot -Qualifier)
            $diskBytes = (Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$driveLetter'").FreeSpace
            $diskGB = [math]::Floor($diskBytes / 1GB)

            $sysInfo = @{ cpu = $cpu; ramGB = $ramGB; diskGB = $diskGB }
            Send-HttpResponse -res $response -content ($sysInfo | ConvertTo-Json) -contentType "application/json"
            continue
        }

        # API: List ISOs
        if ($method -eq "GET" -and $path -eq "/api/isos") {
            $files = @(Get-ChildItem -Path $isosDir -File | Where-Object { $_.Extension -in '.iso', '.vmdk', '.img' -and $_.Length -ge 1048576 } | Select-Object -ExpandProperty Name)
            $resJson = ConvertTo-Json -InputObject $files -Compress
            Send-HttpResponse -res $response -content $resJson -contentType "application/json"
            continue
        }

        # API: Upload ISO from local PC
        if ($method -eq "POST" -and $path -eq "/api/upload-iso") {
            $filename = $request.QueryString["filename"]
            if ([string]::IsNullOrWhiteSpace($filename)) {
                Send-HttpResponse -res $response -content "{ `"error`": `"Parametre filename manquant`" }" -contentType "application/json" -statusCode 400
                continue
            }

            # Security: Extract filename only to avoid directory traversal
            $safeName = [System.IO.Path]::GetFileName($filename)
            $destPath = Join-Path $isosDir $safeName
            $tmpPath = "$destPath.uploading"

            Write-Host "Reception d'un fichier ISO local : $safeName" -ForegroundColor Green
            try {
                if (Test-Path $tmpPath) { Remove-Item -Path $tmpPath -Force -ErrorAction SilentlyContinue }
                $fileStream = [System.IO.File]::Create($tmpPath)
                $request.InputStream.CopyTo($fileStream, 1048576)
                $fileStream.Close()

                if (Test-Path $destPath) { Remove-Item -Path $destPath -Force -ErrorAction SilentlyContinue }
                Rename-Item -Path $tmpPath -NewName $safeName -Force

                # Nettoyage des anciens fichiers de progression ou d'erreur
                $progFile = Join-Path $isosDir "$safeName.progress"
                $errFile = Join-Path $isosDir "$safeName.error"
                $cancelFile = Join-Path $isosDir "$safeName.cancel"
                if (Test-Path $progFile) { Remove-Item -Path $progFile -Force -ErrorAction SilentlyContinue }
                if (Test-Path $errFile) { Remove-Item -Path $errFile -Force -ErrorAction SilentlyContinue }
                if (Test-Path $cancelFile) { Remove-Item -Path $cancelFile -Force -ErrorAction SilentlyContinue }

                Write-Host "Fichier ISO stocke avec succes : $safeName" -ForegroundColor Green
                $resObj = @{ success = $true; filename = $safeName }
                $resJson = ConvertTo-Json -InputObject $resObj -Compress
                Send-HttpResponse -res $response -content $resJson -contentType "application/json"
            } catch {
                if ($null -ne $fileStream) { $fileStream.Close() }
                if (Test-Path $tmpPath) { Remove-Item -Path $tmpPath -Force -ErrorAction SilentlyContinue }
                Write-Host "Erreur lors du transfert ISO : $($_.Exception.Message)" -ForegroundColor Red
                Send-HttpResponse -res $response -content "{ `"error`": `"$($_.Exception.Message)`" }" -contentType "application/json" -statusCode 500
            }
            continue
        }
        
        # API: Get Download Progress
        if ($method -eq "GET" -and $path -eq "/api/download-progress") {
            $filename = $request.QueryString["filename"]
            if ($filename -notmatch '^[a-zA-Z0-9][a-zA-Z0-9._-]*$') {
                Send-HttpResponse -res $response -content 'Nom invalide' -statusCode 400
                continue
            }
            $progFile = Join-Path $isosDir "$filename.progress"
            $pct = "0"
            if (Test-Path $progFile) { 
                $val = Get-Content -Path $progFile -Raw -ErrorAction SilentlyContinue
                if ($null -ne $val -and $val.Trim() -ne "") { $pct = $val }
            }
            if ($request.QueryString['detail'] -eq 'error') {
                $errorPath = Join-Path $isosDir "$filename.error"
                $pct = if (Test-Path -LiteralPath $errorPath) { [System.IO.File]::ReadAllText($errorPath) } else { 'Consultez le journal aria2 pour ce fichier.' }
            }
            Send-HttpResponse -res $response -content $pct -contentType "text/plain"
            continue
        }

        # API: Cancel Download
        if ($method -eq "POST" -and $path -eq "/api/cancel-download") {
            $reader = New-Object System.IO.StreamReader($request.InputStream)
            $reqData = $reader.ReadToEnd() | ConvertFrom-Json
            $reader.Close()
            
            $cancelFile = Join-Path $isosDir "$($reqData.filename).cancel"
            Set-Content -Path $cancelFile -Value "1"
            
            Send-HttpResponse -res $response -content "OK" -statusCode 200
            continue
        }

        # API: Download files with aria2
        if ($method -eq "POST" -and $path -eq "/api/download-iso") {
            $reader = New-Object System.IO.StreamReader($request.InputStream)
            $reqData = $reader.ReadToEnd() | ConvertFrom-Json
            $reader.Close()
            
            $url = $reqData.url
            $filename = [string]$reqData.filename
            $uri = $null
            $checksum = [string]$reqData.sha256
            $archiveChecksum = [string]$reqData.sha1
            $archive = [string]$reqData.archive
            # Use audited catalogue metadata even when an API caller omits it.
            $auditPath = Join-Path $PSScriptRoot 'public\iso-link-status.json'
            if (Test-Path -LiteralPath $auditPath) {
                $audit = [System.IO.File]::ReadAllText($auditPath) | ConvertFrom-Json
                $source = $audit.entries | Where-Object { $_.filename -eq $filename -and $_.url -eq $url } | Select-Object -First 1
                if ($source) {
                    if ($source.status -eq 'unavailable') {
                        Send-HttpResponse -res $response -content 'Cette source est indisponible. Utilisez la page officielle ou Charger.' -statusCode 422
                        continue
                    }
                    $checksum = [string]$source.sha256
                    $archiveChecksum = [string]$source.sha1
                    $archive = [string]$source.archive
                }
            }
            if (($checksum -and $checksum -notmatch '^[a-fA-F0-9]{64}$') -or ($archiveChecksum -and $archiveChecksum -notmatch '^[a-fA-F0-9]{40}$') -or ($archive -and $archive -ne 'zip')) {
                Send-HttpResponse -res $response -content 'Verification ou archive invalide' -statusCode 400
                continue
            }
            if ($filename -notmatch '^[a-zA-Z0-9][a-zA-Z0-9._-]*$' -or
                -not [Uri]::TryCreate($url, [UriKind]::Absolute, [ref]$uri) -or
                $uri.Scheme -notin @('http', 'https')) {
                Send-HttpResponse -res $response -content 'URL ou nom invalide' -statusCode 400
                continue
            }
            # Priorité à l'exécutable local (bin\aria2c.exe), puis au PATH système
            $localAria2Path = Join-Path $PSScriptRoot 'bin\aria2c.exe'
            $aria2 = if (Test-Path -LiteralPath $localAria2Path) {
                [pscustomobject]@{ Source = $localAria2Path }
            } else {
                Get-Command aria2c.exe -ErrorAction SilentlyContinue
            }
            if (-not $aria2) {
                Send-HttpResponse -res $response -content 'aria2 introuvable. Relancez start.ps1 pour le télécharger automatiquement.' -statusCode 503
                continue
            }
            $destPath = Join-Path $isosDir $reqData.filename
            if ((Test-Path -LiteralPath "$destPath.downloading") -or (Test-Path -LiteralPath $destPath)) {
                Send-HttpResponse -res $response -content 'Fichier present ou telechargement en cours' -statusCode 409
                continue
            }
            Set-Content -LiteralPath "$destPath.downloading" -Value 'aria2'
            Remove-Item -LiteralPath "$destPath.cancel" -Force -ErrorAction SilentlyContinue
            Set-Content -LiteralPath "$destPath.progress" -Value '0' -NoNewline
            $worker = Join-Path $PSScriptRoot 'automation\download.py'
            $python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
            
            Write-Host "Telechargement lance en arriere-plan : $url" -ForegroundColor Cyan
            
            try {
                # Start Python directly: no extra PowerShell worker startup.
                $downloadOptions = @{ sha256 = $checksum; sha1 = $archiveChecksum; archive = $archive } | ConvertTo-Json -Compress
                $workerArgs = @($worker, $uri.AbsoluteUri, $destPath, $aria2.Source, $downloadOptions) | ForEach-Object {
                    '"' + ($_ -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
                }
                Start-Process -FilePath $python -ArgumentList $workerArgs -WindowStyle Hidden -ErrorAction Stop | Out-Null
            } catch {
                Set-Content -LiteralPath "$destPath.progress" -Value 'ERROR' -NoNewline
                Remove-Item -LiteralPath "$destPath.downloading" -Force -ErrorAction SilentlyContinue
                Send-HttpResponse -res $response -content 'Impossible de lancer aria2' -statusCode 500
                continue
            }
            
            Send-HttpResponse -res $response -content "OK" -statusCode 200
            continue
        }

        # API: structured status; legacy plain logs remain available.
        if ($method -eq 'GET' -and $path -eq '/api/status') {
            $jobId = $request.QueryString['jobId']
            if ($jobId -notmatch '^vbox-[0-9]{8}-[0-9]{6}(?:-[a-f0-9]{8})?$') {
                Send-HttpResponse -res $response -content '{"error":"Job invalide"}' -statusCode 400 -contentType 'application/json'
                continue
            }
            $jobDir = Join-Path $deployDir $jobId
            $logFile = Join-Path $jobDir 'log.txt'
            $logContent = if (Test-Path -LiteralPath $logFile) { [System.IO.File]::ReadAllText($logFile, [System.Text.Encoding]::UTF8) } else { '' }
            if ($request.QueryString['format'] -eq 'json') {
                $statusPath = Join-Path $jobDir 'status.json'
                if (-not (Test-Path -LiteralPath $statusPath)) {
                    Send-HttpResponse -res $response -content '{"error":"Deploiement inconnu"}' -statusCode 404 -contentType 'application/json'
                    continue
                }
                $status = Get-Content -LiteralPath $statusPath -Raw -Encoding UTF8 | ConvertFrom-Json
                if ($status.workerPid -and $status.state -notin @('ready','failed','needs_attention','manual','cloned')) {
                    if (-not (Get-Process -Id $status.workerPid -ErrorAction SilentlyContinue)) {
                        $status.state = 'needs_attention'
                        $status.message = 'Le processus de suivi est arrete. La fin de la configuration ne peut pas etre confirmee.'
                    }
                }
                if (-not $status.workerPid -and $status.updatedAt -and $status.state -notin @('ready','failed','needs_attention','manual','cloned')) {
                    $lastUpdate = [DateTimeOffset]::Parse($status.updatedAt)
                    if ([DateTimeOffset]::UtcNow.Subtract($lastUpdate).TotalMinutes -gt 10) {
                        $status.state = 'needs_attention'
                        $status.message = 'Ancien suivi interrompu. Le resultat de l installation n a pas ete confirme.'
                    }
                }
                $status | Add-Member -NotePropertyName log -NotePropertyValue $logContent -Force
                Send-HttpResponse -res $response -content ($status | ConvertTo-Json -Depth 5) -contentType 'application/json; charset=utf-8'
            } else {
                Send-HttpResponse -res $response -content $logContent -contentType 'text/plain; charset=utf-8'
            }
            continue
        }

        # API: Focus VM Window (Bring to Foreground / Launch on Desktop)
        if ($method -eq "POST" -and $path -eq "/api/focus-vm") {
            $reader = New-Object System.IO.StreamReader($request.InputStream)
            $reqData = $reader.ReadToEnd() | ConvertFrom-Json
            $reader.Close()
            
            $vmName = $reqData.name
            $shortcutPath = ""
            if (-not [string]::IsNullOrWhiteSpace($vmName)) {
                try {
                    $desktopDir = [Environment]::GetFolderPath('Desktop')
                    $shortcutPath = Join-Path $desktopDir "$vmName.lnk"
                    if (-not (Test-Path $shortcutPath)) {
                        $wsh = New-Object -ComObject WScript.Shell
                        $sc = $wsh.CreateShortcut($shortcutPath)
                        $sc.TargetPath = "C:\Program Files\Oracle\VirtualBox\VirtualBoxVM.exe"
                        $sc.Arguments = "--comment `"$vmName`" --startvm `"$vmName`""
                        $sc.IconLocation = "C:\Program Files\Oracle\VirtualBox\VirtualBoxVM.exe,0"
                        $sc.WorkingDirectory = "C:\Program Files\Oracle\VirtualBox"
                        $sc.Save()
                    }
                    # Executer via explorer pour que le processus s'ouvre sur le bureau interactif utilisateur
                    Start-Process "explorer.exe" -ArgumentList "`"$shortcutPath`"" -ErrorAction SilentlyContinue
                } catch {}
            }

            $focused = [VBoxFocusHelper]::Bring($vmName)
            if (-not $focused) {
                $focused = [VBoxFocusHelper]::Bring("VirtualBox")
            }
            $cleanShortcut = if ($shortcutPath) { $shortcutPath.Replace('\', '\\') } else { '' }
            $resJson = "{ `"status`": `"ok`", `"focused`": $(if ($focused) { 'true' } else { 'false' }), `"shortcut`": `"$cleanShortcut`" }"
            Send-HttpResponse -res $response -content $resJson -contentType "application/json"
            continue
        }

        # API: VM Live Screenshot
        if ($method -eq "GET" -and $path -eq "/api/vm-screenshot") {
            $vmName = $request.QueryString["name"]
            if (-not [string]::IsNullOrWhiteSpace($vmName)) {
                $safeName = [System.IO.Path]::GetFileNameWithoutExtension($vmName)
                $tmpScreen = Join-Path $env:TEMP "vbox_preview_$safeName.png"
                $vboxPath = "C:\Program Files\Oracle\VirtualBox\VBoxManage.exe"
                if (Test-Path $vboxPath) {
                    & $vboxPath controlvm $vmName screenshotpng $tmpScreen 2>&1 | Out-Null
                    if (Test-Path $tmpScreen) {
                        $bytes = [System.IO.File]::ReadAllBytes($tmpScreen)
                        try {
                            $response.Headers.Add("Cache-Control", "no-cache, no-store, must-revalidate")
                            $response.Headers.Add("Access-Control-Allow-Origin", "*")
                        } catch {}
                        Send-HttpResponse -res $response -content $bytes -contentType "image/png"
                        continue
                    }
                }
            }
            Send-HttpResponse -res $response -statusCode 404
            continue
        }

        # API: List Running VMs with details
        if ($method -eq "GET" -and $path -eq "/api/running-vms") {
            $vboxPath = "C:\Program Files\Oracle\VirtualBox\VBoxManage.exe"
            $vmsList = @()
            if (Test-Path $vboxPath) {
                $runningRaw = & $vboxPath list runningvms 2>&1
                foreach ($line in ($runningRaw -split "`r?`n")) {
                    if ($line -match '^"(.+?)"\s+\{(.+?)\}') {
                        $vmName = $matches[1]
                        $vmUuid = $matches[2]
                        $info = & $vboxPath showvminfo $vmUuid --machinereadable 2>&1
                        $os = "Inconnu"
                        $cpus = 1
                        $ram = 1024
                        $state = "running"
                        $stateTime = ""
                        foreach ($infoLine in ($info -split "`r?`n")) {
                            if ($infoLine -match '^ostype="(.+?)"') { $os = $matches[1] }
                            if ($infoLine -match '^cpus=(\d+)') { $cpus = [int]$matches[1] }
                            if ($infoLine -match '^memory=(\d+)') { $ram = [int]$matches[1] }
                            if ($infoLine -match '^VMState="(.+?)"') { $state = $matches[1] }
                            if ($infoLine -match '^VMStateChangeTime="(.+?)"') { $stateTime = $matches[1] }
                        }
                        $metaFile = Join-Path $deployDir "$vmName.meta.json"
                        $vmKbd = "ch-fr"
                        $vmLang = "fr_CH.UTF-8"
                        if (Test-Path $metaFile) {
                            try {
                                $metaData = Get-Content $metaFile -Raw | ConvertFrom-Json
                                if ($metaData.keyboard) { $vmKbd = $metaData.keyboard }
                                if ($metaData.lang) { $vmLang = $metaData.lang }
                            } catch {}
                        }
                        $vmsList += @{
                            name = $vmName
                            uuid = $vmUuid
                            osType = $os
                            cpus = $cpus
                            ramMB = $ram
                            state = $state
                            stateTime = $stateTime
                            keyboard = $vmKbd
                            lang = $vmLang
                        }
                    }
                }
            }
            $json = ConvertTo-Json -InputObject $vmsList -Compress
            Send-HttpResponse -res $response -content $json -contentType "application/json"
            continue
        }

        # API: Stop Running VM
        if ($method -eq "POST" -and $path -eq "/api/stop-vm") {
            $reader = New-Object System.IO.StreamReader($request.InputStream)
            $reqData = $reader.ReadToEnd() | ConvertFrom-Json
            $reader.Close()
            $vboxPath = "C:\Program Files\Oracle\VirtualBox\VBoxManage.exe"
            $vmName = $reqData.name
            $force = [bool]$reqData.force
            if (-not [string]::IsNullOrWhiteSpace($vmName) -and (Test-Path $vboxPath)) {
                if ($force) {
                    & $vboxPath controlvm $vmName poweroff 2>&1 | Out-Null
                } else {
                    & $vboxPath controlvm $vmName acpipowerbutton 2>&1 | Out-Null
                }
            }
            Send-HttpResponse -res $response -content "{ `"status`": `"ok`" }" -contentType "application/json"
            continue
        }

        # API: Reset / Restart VM
        if ($method -eq "POST" -and $path -eq "/api/reset-vm") {
            $reader = New-Object System.IO.StreamReader($request.InputStream)
            $reqData = $reader.ReadToEnd() | ConvertFrom-Json
            $reader.Close()
            $vboxPath = "C:\Program Files\Oracle\VirtualBox\VBoxManage.exe"
            $vmName = $reqData.name
            if (-not [string]::IsNullOrWhiteSpace($vmName) -and (Test-Path $vboxPath)) {
                & $vboxPath controlvm $vmName reset 2>&1 | Out-Null
            }
            Send-HttpResponse -res $response -content "{ `"status`": `"ok`" }" -contentType "application/json"
            continue
        }

        # API: VM Live Keyboard Input (Direct Scancodes / Text / Actions)
        if ($method -eq "POST" -and $path -eq "/api/vm-keyboard") {
            $reader = New-Object System.IO.StreamReader($request.InputStream)
            $reqData = $reader.ReadToEnd() | ConvertFrom-Json
            $reader.Close()
            
            $vmName = $reqData.name
            $scancodes = $reqData.scancodes
            $action = $reqData.action
            $text = $reqData.text
            $enter = [bool]$reqData.enter
            $layout = if ($reqData.layout) {
                "$($reqData.layout)".ToLower()
            } else {
                $metaFile = Join-Path $deployDir "$vmName.meta.json"
                if (Test-Path $metaFile) {
                    try { (Get-Content $metaFile -Raw | ConvertFrom-Json).keyboard } catch { "ch-fr" }
                } else { "ch-fr" }
            }
            $vboxPath = "C:\Program Files\Oracle\VirtualBox\VBoxManage.exe"
            
            if (-not [string]::IsNullOrWhiteSpace($vmName) -and (Test-Path $vboxPath)) {
                $bytesToSend = @()
                
                if ($action) {
                    switch ($action.ToLower()) {
                        "enter"        { $bytesToSend = @("1c", "9c") }
                        "backspace"    { $bytesToSend = @("0e", "8e") }
                        "tab"          { $bytesToSend = @("0f", "8f") }
                        "escape"       { $bytesToSend = @("01", "81") }
                        "space"        { $bytesToSend = @("39", "b9") }
                        "up"           { $bytesToSend = @("e0", "48", "e0", "c8") }
                        "down"         { $bytesToSend = @("e0", "50", "e0", "d0") }
                        "left"         { $bytesToSend = @("e0", "4b", "e0", "cb") }
                        "right"        { $bytesToSend = @("e0", "4d", "e0", "cd") }
                        "ctrl_c"       { $bytesToSend = @("1d", "2e", "ae", "9d") }
                        "ctrl_d"       { $bytesToSend = @("1d", "20", "a0", "9d") }
                        "ctrl_l"       { $bytesToSend = @("1d", "26", "a6", "9d") }
                        "ctrl_alt_del" { $bytesToSend = @("1d", "38", "e0", "53", "e0", "d3", "b8", "9d") }
                    }
                } elseif ($scancodes) {
                    if ($scancodes -is [System.Array]) {
                        $bytesToSend = @($scancodes | ForEach-Object { "$_".Trim() } | Where-Object { $_ -ne "" })
                    } elseif ($scancodes -is [string]) {
                        $bytesToSend = @(($scancodes -split '\s+').Trim() | Where-Object { $_ -ne "" })
                    }
                } elseif ($text) {
                    # Tables de conversion texte -> scancodes PS/2 matériels selon la disposition
                    $qwertzMap = @{
                        'a' = @('1e','9e'); 'b' = @('30','b0'); 'c' = @('2e','ae'); 'd' = @('20','a0')
                        'e' = @('12','92'); 'f' = @('21','a1'); 'g' = @('22','a2'); 'h' = @('23','a3')
                        'i' = @('17','97'); 'j' = @('24','a4'); 'k' = @('25','a5'); 'l' = @('26','a6')
                        'm' = @('32','b2'); 'n' = @('31','b1'); 'o' = @('18','98'); 'p' = @('19','99')
                        'q' = @('10','90'); 'r' = @('13','93'); 's' = @('1f','9f'); 't' = @('14','94')
                        'u' = @('16','96'); 'v' = @('2f','af'); 'w' = @('11','91'); 'x' = @('2d','ad')
                        'y' = @('15','95'); 'z' = @('2c','ac'); ' ' = @('39','b9'); '-' = @('35','b5')
                        '_' = @('2a','35','b5','aa'); '/' = @('2a','08','88','aa'); '.' = @('34','b4')
                        ':' = @('2a','34','b4','aa'); ';' = @('2a','33','b3','aa'); ',' = @('33','b3')
                        '=' = @('2a','0b','8b','aa'); '+' = @('2a','02','82','aa'); '*' = @('2a','04','84','aa')
                        '1' = @('02','82'); '2' = @('03','83'); '3' = @('04','84'); '4' = @('05','85')
                        '5' = @('06','86'); '6' = @('07','87'); '7' = @('08','88'); '8' = @('09','89')
                        '9' = @('0a','8a'); '0' = @('0b','8b')
                    }
                    $qwertyMap = @{
                        'a' = @('1e','9e'); 'b' = @('30','b0'); 'c' = @('2e','ae'); 'd' = @('20','a0')
                        'e' = @('12','92'); 'f' = @('21','a1'); 'g' = @('22','a2'); 'h' = @('23','a3')
                        'i' = @('17','97'); 'j' = @('24','a4'); 'k' = @('25','a5'); 'l' = @('26','a6')
                        'm' = @('32','b2'); 'n' = @('31','b1'); 'o' = @('18','98'); 'p' = @('19','99')
                        'q' = @('10','90'); 'r' = @('13','93'); 's' = @('1f','9f'); 't' = @('14','94')
                        'u' = @('16','96'); 'v' = @('2f','af'); 'w' = @('11','91'); 'x' = @('2d','ad')
                        'y' = @('15','95'); 'z' = @('2c','ac'); ' ' = @('39','b9'); '-' = @('0c','8c')
                        '_' = @('2a','0c','8c','aa'); '/' = @('35','b5'); '.' = @('34','b4')
                        ':' = @('2a','27','a7','aa'); ';' = @('27','a7'); ',' = @('33','b3')
                        '=' = @('0d','8d'); '+' = @('2a','0d','8d','aa'); '*' = @('2a','09','89','aa')
                        '1' = @('02','82'); '2' = @('03','83'); '3' = @('04','84'); '4' = @('05','85')
                        '5' = @('06','86'); '6' = @('07','87'); '7' = @('08','88'); '8' = @('09','89')
                        '9' = @('0a','8a'); '0' = @('0b','8b')
                    }
                    $azertyMap = @{
                        'a' = @('10','90'); 'b' = @('30','b0'); 'c' = @('2e','ae'); 'd' = @('20','a0')
                        'e' = @('12','92'); 'f' = @('21','a1'); 'g' = @('22','a2'); 'h' = @('23','a3')
                        'i' = @('17','97'); 'j' = @('24','a4'); 'k' = @('25','a5'); 'l' = @('26','a6')
                        'm' = @('27','a7'); 'n' = @('31','b1'); 'o' = @('18','98'); 'p' = @('19','99')
                        'q' = @('1e','9e'); 'r' = @('13','93'); 's' = @('1f','9f'); 't' = @('14','94')
                        'u' = @('16','96'); 'v' = @('2f','af'); 'w' = @('2c','ac'); 'x' = @('2d','ad')
                        'y' = @('15','95'); 'z' = @('11','91'); ' ' = @('39','b9'); '-' = @('07','87')
                        '_' = @('09','89'); '/' = @('2a','35','b5','aa'); '.' = @('2a','34','b4','aa')
                        ':' = @('35','b5'); ';' = @('34','b4'); ',' = @('32','b2'); '=' = @('0d','8d')
                        '+' = @('2a','0d','8d','aa'); '*' = @('2b','ab'); '\' = @('e0','38','08','88','e0','b8')
                        '1' = @('2a','02','82','aa'); '2' = @('2a','03','83','aa'); '3' = @('2a','04','84','aa')
                        '4' = @('2a','05','85','aa'); '5' = @('2a','06','86','aa'); '6' = @('2a','07','87','aa')
                        '7' = @('2a','08','88','aa'); '8' = @('2a','09','89','aa'); '9' = @('2a','0a','8a','aa')
                        '0' = @('2a','0b','8b','aa')
                    }

                    $charMap = if ($layout -match "ch|qwertz") {
                        $qwertzMap
                    } elseif ($layout -match "us|gb|ca|qwerty") {
                        $qwertyMap
                    } else {
                        $azertyMap
                    }

                    foreach ($char in $text.ToCharArray()) {
                        $c = "$char"
                        $isUpper = [char]::IsUpper($char)
                        $lowerC = $c.ToLower()
                        if ($charMap.ContainsKey($lowerC)) {
                            $codes = $charMap[$lowerC]
                            if ($isUpper) {
                                $bytesToSend += "2a"
                                $bytesToSend += $codes
                                $bytesToSend += "aa"
                            } else {
                                $bytesToSend += $codes
                            }
                        }
                    }
                    if ($enter) {
                        $bytesToSend += @("1c", "9c")
                    }
                }
                
                if ($bytesToSend.Count -gt 0) {
                    for ($i = 0; $i -lt $bytesToSend.Count; $i += 24) {
                        $maxIdx = [Math]::Min($i + 23, $bytesToSend.Count - 1)
                        $chunk = $bytesToSend[$i..$maxIdx]
                        & $vboxPath controlvm $vmName keyboardputscancode $chunk 2>&1 | Out-Null
                    }
                    Send-HttpResponse -res $response -content "{ `"status`": `"ok`", `"sent`": $($bytesToSend.Count) }" -contentType "application/json"
                    continue
                }
            }
            Send-HttpResponse -res $response -content "{ `"status`": `"ignored`" }" -contentType "application/json"
            continue
        }

        # API: automation capabilities, template registry and deployment preflight
        if ($method -eq "GET" -and $path -eq "/api/templates") {
            $result = Invoke-AutomationEngine -Action 'templates'
            Send-HttpResponse -res $response -content $result -contentType 'application/json; charset=utf-8'
            continue
        }
        if ($method -eq "POST" -and $path -in @('/api/preflight', '/api/templates', '/api/provision')) {
            # Only the local application's origin may submit provisioning requests.
            $origin = $request.Headers['Origin']
            if ($origin -and $origin -ne "http://localhost:$port" -and $origin -ne "http://127.0.0.1:$port") {
                Send-HttpResponse -res $response -content '{"error":"Origine non autorisee."}' -statusCode 403 -contentType 'application/json'
                continue
            }
            $json = Read-AutomationJson -Request $request
            $actionName = if ($path -eq '/api/templates') { 'register' } else { 'check' }
            $result = Invoke-AutomationEngine -Action $actionName -Json $json
            $checked = $result | ConvertFrom-Json
            if ($checked.ok -eq $false) {
                Send-HttpResponse -res $response -content $result -statusCode 400 -contentType 'application/json; charset=utf-8'
                continue
            }
            if ($path -ne '/api/provision') {
                Send-HttpResponse -res $response -content $result -contentType 'application/json; charset=utf-8'
                continue
            }
            $jobId = 'vbox-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0,8)
            $jobDir = Join-Path $deployDir $jobId
            New-Item -ItemType Directory -Path $jobDir | Out-Null
            @{state='preparing'; progress=0; message='Demarrage de l automatisation'; jobId=$jobId} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $jobDir 'status.json') -Encoding UTF8
            $launched = Invoke-AutomationEngine -Action 'launch' -Json $json -Directory $jobDir
            $launchResult = $launched | ConvertFrom-Json
            $launchStatus = if ($launchResult.ok -eq $false) { 400 } else { 200 }
            Send-HttpResponse -res $response -content $launched -statusCode $launchStatus -contentType 'application/json'
            continue
        }

        if ($method -eq 'POST' -and $path -in @('/api/auto-setup-omarchy','/api/macro-macos')) {
            Send-HttpResponse -res $response -content '{"error":"Les anciennes macros clavier ont ete remplacees par le parcours automatique de creation."}' -statusCode 410 -contentType 'application/json'
            continue
        }

        # Serve static files
        if ($path -eq "/") { $path = "/index.html" }
        $filePath = Join-Path $publicDir $path
        
        if (Test-Path $filePath -PathType Leaf) {
            $ext = [System.IO.Path]::GetExtension($filePath)
            $cType = switch ($ext) {
                ".html" { "text/html; charset=utf-8" }
                ".css"  { "text/css; charset=utf-8" }
                ".js"   { "application/javascript; charset=utf-8" }
                default { "application/octet-stream" }
            }
            $bytes = [System.IO.File]::ReadAllBytes($filePath)
            Send-HttpResponse -res $response -content $bytes -contentType $cType
        } else {
            Send-HttpResponse -res $response -content "Not Found" -statusCode 404
        }
        } catch {
            Write-Host "Erreur requete: $($_.Exception.Message)" -ForegroundColor Red
            if ($null -ne $context -and $null -ne $context.Response) {
                try { Send-HttpResponse -res $context.Response -content "Server Error" -statusCode 500 } catch {}
            }
        }
    }
} catch {
    Write-Host "Erreur globale serveur: $($_.Exception.Message)" -ForegroundColor Red
} finally {
    $listener.Stop()
}
