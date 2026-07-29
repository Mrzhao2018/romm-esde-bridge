$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
$Defaults = Get-Content (Join-Path $Root 'defaults.json') -Raw | ConvertFrom-Json
$ServerUrl = if ($env:ROMM_ESDE_SERVER_URL) { $env:ROMM_ESDE_SERVER_URL.TrimEnd('/') } else { $Defaults.server_url.TrimEnd('/') }
$BridgeUrl = if ($env:ROMM_ESDE_BRIDGE_URL) { $env:ROMM_ESDE_BRIDGE_URL.TrimEnd('/') } else { $Defaults.bridge_url.TrimEnd('/') }
$Version = [string]$Defaults.client_version
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Read-YesNo([string]$Prompt, [bool]$Default = $true) {
    $Suffix = if ($Default) { ' [Y/n]' } else { ' [y/N]' }
    $Answer = (Read-Host ($Prompt + $Suffix)).Trim().ToLowerInvariant()
    if (-not $Answer) { return $Default }
    return @('y', 'yes', '是', '好') -contains $Answer
}

function Write-Utf8([string]$Path, [string]$Value) {
    $Parent = Split-Path -Parent $Path
    if ($Parent) { New-Item -ItemType Directory -Path $Parent -Force | Out-Null }
    [IO.File]::WriteAllText($Path, $Value, $Utf8NoBom)
}

function To-TomlString([string]$Value) {
    return ($Value | ConvertTo-Json -Compress)
}

function Get-EmulationCandidates {
    $Found = @()
    foreach ($Drive in Get-PSDrive -PSProvider FileSystem) {
        $Candidate = Join-Path $Drive.Root 'Emulation'
        if ((Test-Path (Join-Path $Candidate 'roms')) -and (Test-Path (Join-Path $Candidate 'bios'))) {
            $Found += (Get-Item $Candidate).FullName
        }
    }
    return @($Found | Select-Object -Unique)
}

function Find-FirstFile([string[]]$Roots, [string]$Name) {
    foreach ($SearchRoot in $Roots) {
        if (-not $SearchRoot -or -not (Test-Path $SearchRoot)) { continue }
        $Direct = Join-Path $SearchRoot $Name
        if (Test-Path $Direct) { return (Get-Item $Direct).FullName }
        $Found = Get-ChildItem $SearchRoot -Filter $Name -File -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($Found) { return $Found.FullName }
    }
    return $null
}

function Ensure-DirectoryJunction([string]$Link, [string]$Target) {
    New-Item -ItemType Directory -Path $Target -Force | Out-Null
    if (Test-Path $Link) {
        $Item = Get-Item -LiteralPath $Link -Force
        if (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            Write-Host "保留现有目录联接：$Link -> $($Item.Target)"
            return
        }
        Write-Host "正在把 $Link 迁移到 $Target ……"
        & robocopy.exe $Link $Target /E /MOVE /R:2 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
        if ($LASTEXITCODE -ge 8) { throw "迁移失败：$Link -> $Target（robocopy $LASTEXITCODE）" }
        Remove-Item -LiteralPath $Link -Recurse -Force -ErrorAction SilentlyContinue
    }
    New-Item -ItemType Directory -Path (Split-Path -Parent $Link) -Force | Out-Null
    New-Item -ItemType Junction -Path $Link -Target $Target | Out-Null
    Write-Host "已建立目录联接：$Link -> $Target"
}

function Start-EmuDeckInstall {
    Write-Host "`n将运行 EmuDeck 官方 Windows 启动器。Easy/Custom、存储位置和 ES-DE 选择仍在官方界面确认。"
    Ensure-EmuDeckBackend
    $Cmd = Join-Path $env:TEMP 'EmuDeck.cmd'
    Invoke-WebRequest 'https://www.emudeck.com/EmuDeck.cmd' -OutFile $Cmd
    Start-Process $env:ComSpec -ArgumentList @('/d', '/c', ('"' + $Cmd + '"')) -Wait
    Remove-Item $Cmd -Force -ErrorAction SilentlyContinue
    [void](Read-Host '完成 EmuDeck 配置后按 Enter 继续')
}

function Ensure-EmuDeckBackend {
    $EmuDeckData = Join-Path $env:APPDATA 'EmuDeck'
    $Backend = Join-Path $EmuDeckData 'backend'
    if ((Test-Path (Join-Path $Backend '.git')) -and (Test-Path (Join-Path $Backend 'functions'))) {
        Push-Location $Backend
        try {
            & git.exe config core.autocrlf false
            & git.exe config core.filemode false
        } finally { Pop-Location }
        return
    }
    New-Item -ItemType Directory -Path $EmuDeckData -Force | Out-Null
    if (Test-Path $Backend) {
        Move-Item $Backend (Join-Path $EmuDeckData ("backend.failed-" + (Get-Date -Format 'yyyyMMdd-HHmmss')))
    }
    Write-Host 'Windows 直连 GitHub 不稳定，正在通过 RomM 局域网服务器安装 EmuDeck 后端……'
    $Archive = Join-Path $EmuDeckData 'emudeck-we-main.tar.gz'
    Invoke-WebRequest "$BridgeUrl/bootstrap/emudeck-we-main.tar.gz" -OutFile $Archive
    $Expected = 'f5ab37b39df7497b656adc8aedc045416468d57369bc1205431d2659e6da04dd'
    if ((Get-FileHash $Archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Expected) {
        throw 'EmuDeck 后端校验失败'
    }
    & tar.exe -xzf $Archive -C $EmuDeckData
    if ($LASTEXITCODE -ne 0) { throw 'EmuDeck 后端解压失败' }
    Remove-Item $Archive -Force
    Push-Location $Backend
    try {
        & git.exe config core.autocrlf false
        & git.exe config core.filemode false
    } finally { Pop-Location }
}

function Initialize-Pairing([string]$DeviceName, [string]$InstallRoot) {
    $IdentityPath = Join-Path $InstallRoot 'client-instance-id'
    if (Test-Path $IdentityPath) {
        $Identity = (Get-Content $IdentityPath -Raw).Trim()
    } else {
        $Identity = [guid]::NewGuid().ToString()
        Write-Utf8 $IdentityPath ($Identity + "`n")
    }
    $Scopes = @(
        'me.read', 'roms.read', 'roms.user.read', 'roms.user.write',
        'platforms.read', 'assets.read', 'assets.write', 'devices.read',
        'devices.write', 'firmware.read', 'collections.read', 'collections.write'
    )
    $Payload = @{
        client_device_identifier = $Identity
        name = $DeviceName
        client = 'romm-esde'
        platform = 'Windows'
        client_version = $Version
        requested_scopes = $Scopes
    } | ConvertTo-Json
    $Init = Invoke-RestMethod -Method Post -Uri "$ServerUrl/api/auth/device/init" -ContentType 'application/json' -Body $Payload
    $VerifyUrl = $ServerUrl + $Init.verification_path_complete
    Write-Host "`n请在 RomM 中登录要绑定的账号并批准设备："
    Write-Host "  配对码：$($Init.user_code)"
    Write-Host "  $VerifyUrl`n"
    Start-Process $VerifyUrl
    $Interval = [Math]::Max(2, [int]$Init.interval)
    $Deadline = (Get-Date).AddSeconds([int]$Init.expires_in)
    while ((Get-Date) -lt $Deadline) {
        Start-Sleep -Seconds $Interval
        try {
            $Body = @{ device_code = $Init.device_code } | ConvertTo-Json
            return Invoke-RestMethod -Method Post -Uri "$ServerUrl/api/auth/device/token" -ContentType 'application/json' -Body $Body
        } catch {
            $Detail = $null
            try { $Detail = ($_.ErrorDetails.Message | ConvertFrom-Json).detail } catch {}
            if ($Detail -eq 'authorization_pending') { continue }
            if ($Detail -eq 'slow_down') { $Interval += 2; continue }
            throw "RomM 配对失败：$Detail"
        }
    }
    throw 'RomM 配对码已过期，请重新运行安装命令'
}

function Install-PythonRuntime([string]$InstallRoot) {
    $Runtime = Join-Path $InstallRoot 'runtime'
    $Python = Join-Path $Runtime 'python.exe'
    if (Test-Path $Python) { return $Python }
    New-Item -ItemType Directory -Path $Runtime -Force | Out-Null
    $Zip = Join-Path $env:TEMP 'python-3.13.12-embed-amd64.zip'
    Write-Host '正在通过 RomM 局域网服务器安装 Python 运行环境……'
    Invoke-WebRequest "$BridgeUrl/bootstrap/python-3.13.12-embed-amd64.zip" -OutFile $Zip
    $Expected = '76f238f606250c87c6beac75dccd35ee99070a13490555936abb6cb64ecce3d0'
    if ((Get-FileHash $Zip -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Expected) {
        throw 'Python 官方运行时校验失败'
    }
    Expand-Archive $Zip -DestinationPath $Runtime -Force
    Remove-Item $Zip -Force
    return $Python
}

function Register-RommTask([string]$Name, [string]$PythonW, [string]$Client, [string]$Config, [string]$Command, [int]$Minutes) {
    $Arguments = ('"{0}" --config "{1}" {2}' -f $Client, $Config, $Command)
    $Action = New-ScheduledTaskAction -Execute $PythonW -Argument $Arguments
    # This Windows 11 build rejects a repetition pattern whose Duration is
    # omitted (0x80041318), even though Task Scheduler accepts registration.
    $Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
        -RepetitionInterval (New-TimeSpan -Minutes $Minutes) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
    $Trigger.Repetition.StopAtDurationEnd = $false
    $Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $Name -Action $Action -Trigger $Trigger -Settings $Settings -Description 'Managed by RomM ES-DE bridge' -Force | Out-Null
}

if (-not [Environment]::Is64BitOperatingSystem) { throw '当前 Windows 客户端仅支持 x64 Windows' }
Write-Host "RomM ES-DE Windows 客户端 $Version · $env:COMPUTERNAME · $env:USERNAME"

$Candidates = @(Get-EmulationCandidates)
$PreferredDrive = $null
$CurrentLocation = Get-Location
if ($CurrentLocation.Provider.Name -eq 'FileSystem' -and $CurrentLocation.Drive) {
    $PreferredDrive = $CurrentLocation.Drive.Name + ':'
}
if ($env:ROMM_ESDE_INSTALL_DRIVE) {
    $PreferredDrive = $env:ROMM_ESDE_INSTALL_DRIVE.Trim().TrimEnd('\').TrimEnd(':') + ':'
}
if (-not $PreferredDrive -and $Candidates.Count) { $PreferredDrive = Split-Path -Qualifier $Candidates[0] }
if (-not $PreferredDrive) {
    foreach ($Drive in Get-PSDrive -PSProvider FileSystem | Where-Object { $_.Name -ne 'C' } | Sort-Object Free -Descending) {
        if (Test-Path (Join-Path $Drive.Root 'retroarch\retroarch.exe')) { $PreferredDrive = $Drive.Name + ':'; break }
    }
}
if (-not $PreferredDrive) {
    $DataDrive = Get-PSDrive -PSProvider FileSystem | Where-Object { $_.Name -ne 'C' } | Sort-Object Free -Descending | Select-Object -First 1
    $PreferredDrive = if ($DataDrive) { $DataDrive.Name + ':' } else { 'C:' }
}
$EnteredDrive = Read-Host "统一安装盘（默认使用 PowerShell 当前所在盘；EmuDeck、ES-DE、RomM 和缓存）[$PreferredDrive]"
$InstallDrive = if ($EnteredDrive) { $EnteredDrive.Trim().TrimEnd('\').TrimEnd(':') + ':' } else { $PreferredDrive }
if (-not (Test-Path ($InstallDrive + '\'))) { throw "磁盘不存在：$InstallDrive" }
$DefaultInstallRoot = Join-Path ($InstallDrive + '\') 'RomM-ESDE'
$EnteredInstallRoot = Read-Host "RomM 客户端、运行时、缓存和媒体目录 [$DefaultInstallRoot]"
$InstallRoot = if ($EnteredInstallRoot) { $EnteredInstallRoot.Trim('"') } else { $DefaultInstallRoot }
if ((Split-Path -Qualifier $InstallRoot) -eq 'C:') {
    if (-not (Read-YesNo '你选择了 C 盘，确定继续吗？' $false)) { throw '已取消，请重新运行并选择其他盘' }
}
New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null

$EmuDeckRoot = Join-Path $env:APPDATA 'EmuDeck'
$EsdeRoot = Join-Path $env:USERPROFILE 'ES-DE'
$EmuDeckPhysical = Join-Path ($InstallDrive + '\') 'EmuDeck'
$EsdePhysical = Join-Path ($InstallDrive + '\') 'ES-DE'
if ($InstallDrive -ne 'C:' -and (Read-YesNo "把 EmuDeck 和 ES-DE 的标准用户目录也迁移到 $InstallDrive 盘吗？")) {
    Ensure-DirectoryJunction $EmuDeckRoot $EmuDeckPhysical
    Ensure-DirectoryJunction $EsdeRoot $EsdePhysical
}

$HasEmuDeck = (Test-Path (Join-Path $EmuDeckRoot 'Emulators')) -or $Candidates.Count -gt 0
if ($HasEmuDeck) {
    if (-not (Read-YesNo '检测到 EmuDeck，跳过 EmuDeck 并只安装 RomM 集成，继续吗？')) { exit 1 }
} elseif (Read-YesNo '没有检测到 EmuDeck。现在运行 EmuDeck 官方安装流程吗？') {
    Start-EmuDeckInstall
    $Candidates = @(Get-EmulationCandidates)
} else {
    throw '已取消；当前客户端依赖 EmuDeck 提供 ES-DE 和 RetroArch'
}

$DefaultEmulation = if ($Candidates.Count) { $Candidates[0] } else { Join-Path ($InstallDrive + '\') 'Emulation' }
$Entered = Read-Host "EmuDeck 的 Emulation 目录 [$DefaultEmulation]"
$EmulationRoot = if ($Entered) { $Entered.Trim('"') } else { $DefaultEmulation }
if (-not (Test-Path (Join-Path $EmulationRoot 'roms'))) {
    throw "未找到 $EmulationRoot\roms，请先完成 EmuDeck 配置"
}

$InstallDriveRoot = (Split-Path -Qualifier $EmulationRoot) + '\'
$SearchRoots = @(
    $EmuDeckRoot,
    (Join-Path $env:APPDATA 'EmuDeck\Emulators'),
    $EmulationRoot,
    (Join-Path $InstallDriveRoot 'retroarch'),
    (Join-Path $InstallDriveRoot 'RetroArch-Win64')
)
$RetroArch = Find-FirstFile $SearchRoots 'retroarch.exe'
if (-not $RetroArch) { throw '没有找到 EmuDeck 的 retroarch.exe，请在 EmuDeck Manage Emulators 中安装 RetroArch 后重试' }
$RetroArchRoot = Split-Path -Parent $RetroArch
$Core = Join-Path $RetroArchRoot 'cores\np2kai_libretro.dll'
if (-not (Test-Path $Core)) {
    Write-Host 'NP2Kai 核心不存在，正在从 Libretro 官方 buildbot 安装……'
    $CoreZip = Join-Path $env:TEMP 'np2kai_libretro.dll.zip'
    Invoke-WebRequest 'https://buildbot.libretro.com/nightly/windows/x86_64/latest/np2kai_libretro.dll.zip' -OutFile $CoreZip
    New-Item -ItemType Directory -Path (Split-Path -Parent $Core) -Force | Out-Null
    Expand-Archive $CoreZip -DestinationPath (Split-Path -Parent $Core) -Force
    Remove-Item $CoreZip -Force
}

# EmuDeck's Windows build ships ES-DE in portable mode.  In that mode ES-DE
# ignores %USERPROFILE%\ES-DE and reads the ES-DE directory beside ES-DE.exe.
$EsdePortableParent = Join-Path $EmuDeckRoot 'EmulationStation-DE'
$EsdeExe = Join-Path $EsdePortableParent 'ES-DE.exe'
if (-not (Test-Path $EsdeExe)) { throw "没有找到 ES-DE：$EsdeExe" }
$EsdeDataRoot = if (Test-Path (Join-Path $EsdePortableParent 'portable.txt')) {
    Join-Path $EsdePortableParent 'ES-DE'
} else {
    $EsdeRoot
}
Write-Host "ES-DE 数据目录：$EsdeDataRoot"
$EsdeMediaRoot = Join-Path $EsdeDataRoot 'downloaded_media'
$EsdeSettingsPath = Join-Path $EsdeDataRoot 'settings\es_settings.xml'
if (Test-Path $EsdeSettingsPath) {
    try {
        $EsdeSettings = [xml](Get-Content $EsdeSettingsPath -Raw)
        $MediaNode = $EsdeSettings.SelectSingleNode('//string[@name="MediaDirectory"]')
        if ($MediaNode -and $MediaNode.value) {
            $ConfiguredMedia = [Environment]::ExpandEnvironmentVariables([string]$MediaNode.value)
            if ($ConfiguredMedia.StartsWith('~')) {
                $ConfiguredMedia = Join-Path $env:USERPROFILE $ConfiguredMedia.Substring(1).TrimStart('\', '/')
            }
            $EsdeMediaRoot = $ConfiguredMedia
        }
    } catch {
        Write-Warning "无法解析 ES-DE 媒体目录设置，将使用默认目录：$EsdeMediaRoot"
    }
}
Write-Host "ES-DE 媒体目录：$EsdeMediaRoot"

$DeviceDefault = "Windows · $env:COMPUTERNAME"
$DeviceName = Read-Host "设备名称 [$DeviceDefault]"
if (-not $DeviceName) { $DeviceName = $DeviceDefault }
$ConfigRoot = Join-Path $InstallRoot 'config'
$ConfigPath = Join-Path $ConfigRoot 'config.toml'
$TokenPath = Join-Path $ConfigRoot 'token'
$Pair = $null
if ((Test-Path $TokenPath) -and (Test-Path $ConfigPath) -and (Read-YesNo '发现现有 RomM 绑定，保留原账号和设备身份吗？')) {
    $Token = (Get-Content $TokenPath -Raw).Trim()
    $OldConfig = Get-Content $ConfigPath
    $OldDevice = $OldConfig | Where-Object { $_ -match '^paired_device_id\s*=' } | Select-Object -First 1
    $DeviceId = if ($OldDevice) { (($OldDevice -split '=', 2)[1]).Trim().Trim('"') } else { '' }
    $GrantedScopes = @()
} else {
    $Pair = Initialize-Pairing $DeviceName $InstallRoot
    $Token = $Pair.access_token
    $DeviceId = $Pair.device_id
    $GrantedScopes = @($Pair.scopes)
}

$Python = Install-PythonRuntime $InstallRoot
$PythonW = Join-Path (Split-Path -Parent $Python) 'pythonw.exe'
$ClientPath = Join-Path $InstallRoot 'client.py'
New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
Copy-Item (Join-Path $Root 'deck_client.py') $ClientPath -Force
Write-Utf8 $TokenPath ($Token + "`n")
$CurrentUserSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
& icacls.exe $TokenPath /inheritance:r /grant:r ("*{0}:(F)" -f $CurrentUserSid) '*S-1-5-18:(F)' '*S-1-5-32-544:(F)' | Out-Null
if ($LASTEXITCODE -ne 0) { throw '无法收紧 RomM 令牌文件的 Windows ACL' }

$EsdeRoot = Join-Path $env:USERPROFILE 'ES-DE'
$DataDir = Join-Path $InstallRoot 'data'
$MediaDir = Join-Path $EsdeMediaRoot 'romm-pc98'
$ThumbnailDir = Join-Path $InstallRoot 'media\thumbnails'
$Launcher = Join-Path $InstallRoot 'romm-esde-launch.cmd'
$StateDir = Join-Path $EmulationRoot 'saves\retroarch\states'
$RaConfig = Join-Path $RetroArchRoot 'config\Neko Project II kai'
$Config = @(
    'server_url = ' + (To-TomlString $ServerUrl)
    'bridge_url = ' + (To-TomlString $BridgeUrl)
    'token_file = ' + (To-TomlString $TokenPath)
    'platform_slug = "pc-9800-series"'
    'data_dir = ' + (To-TomlString $DataDir)
    'stub_dir = ' + (To-TomlString (Join-Path $EmulationRoot 'roms\romm-pc98'))
    'gamelist_path = ' + (To-TomlString (Join-Path $EsdeDataRoot 'gamelists\romm-pc98\gamelist.xml'))
    'media_dir = ' + (To-TomlString $MediaDir)
    'thumbnail_dir = ' + (To-TomlString $ThumbnailDir)
    'cache_dir = ' + (To-TomlString (Join-Path $InstallRoot 'cache'))
    'systems_xml = ' + (To-TomlString (Join-Path $EsdeDataRoot 'custom_systems\es_systems.xml'))
    'retroarch_core = ' + (To-TomlString $Core)
    'state_dir = ' + (To-TomlString $StateDir)
    'retroarch_autoconfig = ' + (To-TomlString (Join-Path $RetroArchRoot 'autoconfig\romm-esde-unused.cfg'))
    'np2kai_options = ' + (To-TomlString (Join-Path $RaConfig 'Neko Project II kai.opt'))
    'np2kai_override = ' + (To-TomlString (Join-Path $RaConfig 'Neko Project II kai.cfg'))
    'firmware_dir = ' + (To-TomlString (Join-Path $EmulationRoot 'bios'))
    'runtime_platform = "Windows"'
    'steam_deck_tuning = false'
    'launcher_command = ' + (To-TomlString $Launcher)
    'device_name = ' + (To-TomlString $DeviceName)
    'paired_device_id = ' + (To-TomlString $DeviceId)
    'retroarch_command = [' + (To-TomlString $RetroArch) + ']'
    'cache_max_bytes = 53687091200'
    'min_free_percent = 20.0'
    'user_flags_ttl_seconds = 900'
) -join "`n"
Write-Utf8 $ConfigPath ($Config + "`n")
New-Item -ItemType Directory -Path $StateDir -Force | Out-Null

$LaunchBody = "@echo off`r`n`"$Python`" `"$ClientPath`" --config `"$ConfigPath`" launch %*`r`n"
Write-Utf8 $Launcher $LaunchBody
$ClientCmd = Join-Path $InstallRoot 'romm-esde-client.cmd'
Write-Utf8 $ClientCmd ("@echo off`r`n`"$Python`" `"$ClientPath`" --config `"$ConfigPath`" %*`r`n")

foreach ($TaskName in @('RomM ES-DE Catalog Sync', 'RomM ES-DE User Sync', 'RomM ES-DE Media Sync')) {
    & schtasks.exe /Delete /TN $TaskName /F 2>$null | Out-Null
}
$SessionLauncher = Join-Path $InstallRoot 'romm-esde-esde.cmd'
$SessionBody = "@echo off`r`n`"$Python`" `"$ClientPath`" --config `"$ConfigPath`" session -- `"$EsdeExe`" %*`r`n"
Write-Utf8 $SessionLauncher $SessionBody
$Shell = New-Object -ComObject WScript.Shell
foreach ($ShortcutPath in @(
    (Join-Path ([Environment]::GetFolderPath('Desktop')) 'RomM ES-DE.lnk'),
    (Join-Path ([Environment]::GetFolderPath('Programs')) 'RomM ES-DE.lnk')
)) {
    $Shortcut = $Shell.CreateShortcut($ShortcutPath)
    $Shortcut.TargetPath = $SessionLauncher
    $Shortcut.WorkingDirectory = $EsdePortableParent
    $Shortcut.IconLocation = "$EsdeExe,0"
    $Shortcut.Save()
}

& $Python $ClientPath --config $ConfigPath sync
& $Python $ClientPath --config $ConfigPath firmware
& $Python $ClientPath --config $ConfigPath repair-system
& $Python $ClientPath --config $ConfigPath doctor

if ($Pair) {
    $RequiredWrite = @('roms.user.write', 'collections.write', 'assets.write', 'devices.write')
    $Missing = @($RequiredWrite | Where-Object { $GrantedScopes -notcontains $_ })
    if ($Missing.Count) { Write-Warning ('缺少写权限，收藏/隐藏或存档同步会只读：' + ($Missing -join ', ')) }
}
Write-Host "`n安装完成。配置：$ConfigPath"
Write-Host "打开 ES-DE 后选择 ‘RomM · PC-98’；游戏内容会按需下载到本机缓存。"
