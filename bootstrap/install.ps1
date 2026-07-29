$ErrorActionPreference = 'Stop'
$BridgeUrl = if ($env:ROMM_ESDE_BRIDGE_URL) { $env:ROMM_ESDE_BRIDGE_URL.TrimEnd('/') } else { '@@BRIDGE_URL@@' }
$Stage = Join-Path $env:TEMP ("romm-esde-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $Stage | Out-Null
try {
    $Zip = Join-Path $Stage 'client.zip'
    $HashFile = Join-Path $Stage 'client.zip.sha256'
    Invoke-WebRequest "$BridgeUrl/bootstrap/romm-esde-windows.zip" -OutFile $Zip
    Invoke-WebRequest "$BridgeUrl/bootstrap/romm-esde-windows.zip.sha256" -OutFile $HashFile
    $Expected = ((Get-Content $HashFile -Raw).Trim() -split '\s+')[0].ToLowerInvariant()
    $Actual = (Get-FileHash $Zip -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($Actual -ne $Expected) { throw 'Windows 客户端安装包校验失败' }
    Expand-Archive $Zip -DestinationPath $Stage
    & (Join-Path $Stage 'romm-esde-windows\installer.ps1')
} finally {
    Remove-Item -LiteralPath $Stage -Recurse -Force -ErrorAction SilentlyContinue
}
