param(
    [string]$PublicKey = '@@SSH_PUBLIC_KEY@@'
)

$ErrorActionPreference = 'Stop'
$BridgeUrl = if ($env:ROMM_ESDE_BRIDGE_URL) { $env:ROMM_ESDE_BRIDGE_URL.TrimEnd('/') } else { '@@BRIDGE_URL@@' }
if ([string]::IsNullOrWhiteSpace($PublicKey) -or $PublicKey -eq '@@SSH_PUBLIC_KEY@@') {
    throw '未配置 SSH 公钥。请通过 -PublicKey 传入部署者自己的公钥。'
}
if ($PublicKey -notmatch '^ssh-(ed25519|rsa)\s+[A-Za-z0-9+/=]+(?:\s+.*)?$') {
    throw 'SSH 公钥格式无效，仅接受 ssh-ed25519 或 ssh-rsa 公钥。'
}

$Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$Principal = New-Object Security.Principal.WindowsPrincipal($Identity)
if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw '请在管理员 PowerShell 中运行此命令'
}

if (-not (Get-Service sshd -ErrorAction SilentlyContinue)) {
    $Msi = Join-Path $env:TEMP 'OpenSSH-Win64-v10.0.0.0.msi'
    Invoke-WebRequest "$BridgeUrl/bootstrap/OpenSSH-Win64-v10.0.0.0.msi" -OutFile $Msi
    $Expected = 'ddec9c53864280759cf9f74791cefd387100e3946aa849a1c138a4ed1b96b7d9'
    if ((Get-FileHash $Msi -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Expected) {
        throw 'OpenSSH MSI 校验失败'
    }
    $Process = Start-Process msiexec.exe -ArgumentList @('/i', ('"' + $Msi + '"'), '/qn', '/norestart') -Wait -PassThru
    Remove-Item $Msi -Force -ErrorAction SilentlyContinue
    if ($Process.ExitCode -ne 0) { throw "OpenSSH MSI 安装失败，退出码 $($Process.ExitCode)" }
}

Start-Service sshd
Set-Service sshd -StartupType Automatic
if (-not (Get-NetFirewallRule -Name 'RomM-OpenSSH' -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -Name 'RomM-OpenSSH' -DisplayName 'RomM OpenSSH Server' -Direction Inbound -Protocol TCP -LocalPort 22 -Action Allow | Out-Null
}

$Auth = Join-Path $env:ProgramData 'ssh\administrators_authorized_keys'
$ExistingKeys = @()
if (Test-Path $Auth) {
    $ExistingKeys = @(Get-Content $Auth | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}
if ($ExistingKeys -notcontains $PublicKey.Trim()) {
    $ExistingKeys += $PublicKey.Trim()
}
[IO.File]::WriteAllText($Auth, (($ExistingKeys -join "`r`n") + "`r`n"), [Text.UTF8Encoding]::new($false))
icacls.exe $Auth /inheritance:r | Out-Null
icacls.exe $Auth /grant:r '*S-1-5-32-544:F' | Out-Null
icacls.exe $Auth /grant '*S-1-5-18:F' | Out-Null
Write-Host "SSH_READY $env:USERNAME@$env:COMPUTERNAME"
