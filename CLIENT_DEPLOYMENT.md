# RomM ES-DE 客户端部署

## 一条命令

SteamOS 或桌面 Linux 在终端运行：

```bash
curl -fsSL http://romm-server.local:8090/bootstrap/install.sh | bash
```

Windows x64 在 PowerShell 运行：

```powershell
irm http://romm-server.local:8090/bootstrap/install.ps1 | iex
```

Windows 安装器默认使用 PowerShell 当前所在目录的盘符作为统一数据盘；例如先执行 `E:`，再运行安装命令，EmuDeck、ES-DE、RomM 客户端和缓存都会默认放在 E 盘。交互时仍可改选其他盘，也可通过 `ROMM_ESDE_INSTALL_DRIVE` 环境变量预设。

安装器的决策流程：

1. 检测 SteamOS/Linux、Steam Deck、EmuDeck、RetroArch Flatpak 和 NP2Kai。
2. 如果检测到 EmuDeck，询问确认后仅安装 RomM ES-DE 集成。
3. 如果没有 EmuDeck，询问确认并运行 EmuDeck 官方 Linux 安装器；其 Easy/Custom 和存储位置仍在官方 GUI 中选择。
4. 在 RomM 发起标准设备配对。浏览器中登录哪个 RomM 用户，这台设备就绑定哪个用户；Token 不放在命令或安装包中。
5. 安装客户端、ES-DE 自定义系统、按需 ROM 缓存、当前平台固件、收藏/隐藏、游玩记录、即时存档和截图同步。
6. 安装 systemd 用户级同步、媒体和文件监听服务，执行首次同步和诊断。

重复运行同一命令用于更新或修复。检测到已有绑定时，安装器会询问是否保留原账号、设备身份、缓存和同步数据库。

客户端采用按需会话同步，不常驻轮询：启动 ES-DE 前同步目录与封面，运行中
监视 `gamelist.xml` 并即时回传收藏/隐藏，退出后再回传一次。Windows 请从
桌面或开始菜单的 `RomM ES-DE` 快捷方式启动；Steam Deck 的 EmuDeck ES-DE
启动脚本已自动接入同一会话包装器。

## 用户与共享模型

- ROM、平台元数据和 RomM 已刮削媒体由所有获准用户共享。
- 收藏、隐藏、游玩记录、即时存档和截图按 RomM 用户隔离。
- 同一个用户的多台设备获得不同设备身份，但共享该用户的状态。
- 新用户需要先能登录 RomM。若需写回收藏、隐藏和存档，该用户必须获准 `roms.user.write`、`assets.write` 和 `devices.write`；只有读取权限时客户端会降级为只读并给出提示。

## 当前跨平台状态

| 客户端系统 | ES-DE | 本桥接一键部署 | 主要剩余适配 |
|---|---:|---:|---|
| SteamOS / Steam Deck | 支持 | 首发支持 | 已完成 |
| 桌面 Linux + EmuDeck | 支持 | 首发支持 | 不同发行版的 EmuDeck 依赖仍由官方安装器处理 |
| Windows x64 | 支持 | v5 测试版 | PowerShell 安装、嵌入式 Python、原生 RetroArch、Task Scheduler、Windows 文件锁和路径均已实现，正在实机验收 |
| macOS | 支持 | 尚未 | 签名应用包、LaunchAgent、RetroArch/ES-DE 路径和文件锁 |
| Android | 支持 | 尚未 | 原生伴侣应用、SAF/content URI、前台下载服务；不能照搬 systemd/Flatpak 客户端 |
| iOS/iPadOS | 非本项目目标 | 尚未 | 后台任务和共享文件访问限制较大 |

ES-DE 本身是跨平台前端，但桥接客户端还包含系统服务、文件锁、启动命令和目录协议，所以不能仅凭 ES-DE 可运行就宣称整套客户端已经跨平台。共享协议已经平台中立：RomM HTTP API、设备配对、Bridge JSON 清单和按需下载不需要改变；新增平台主要实现本地适配层。

## 发布物

服务器每次刷新 Bridge 清单时，同时生成：

- `/bootstrap/install.sh`
- `/bootstrap/romm-esde-linux.tar.gz`
- `/bootstrap/romm-esde-linux.tar.gz.sha256`
- `/bootstrap/release.json`
- `/bootstrap/install.ps1`
- `/bootstrap/romm-esde-windows.zip`
- `/bootstrap/romm-esde-windows.zip.sha256`

安装包不含任何用户或服务器管理 Token。安装时通过 RomM 十分钟有效的设备码流程换取当前用户、当前设备专属 Token。

## 官方上游

- EmuDeck Linux 安装说明：https://emudeck.github.io/how-to-install-emudeck/linux/
- EmuDeck 新版安装手册：https://manual.emudeck.com/install-guide/1_index/
- ES-DE 项目与支持平台：https://gitlab.com/es-de/emulationstation-de
