# Server deployment

The Bridge is a small Python 3.11+ service that reads RomM through a
least-privilege Client API Token and publishes a credential-free catalogue.
It does not need database access and must not receive the RomM authentication
secret or metadata-provider credentials.

## 1. Install files

The supplied systemd units assume these locations:

```text
/opt/romm-esde-bridge/               application checkout
/etc/romm-esde-bridge/bridge.env     private configuration (0600)
/etc/romm-esde-bridge/server-token   Client API Token (0600)
/var/lib/romm-esde-bridge/           generated catalogue
/srv/romm/                           host-visible RomM data root
```

Create a dedicated service account and directories, then copy the checkout:

```bash
sudo useradd --system --home /var/lib/romm-esde-bridge --shell /usr/sbin/nologin romm-esde-bridge
sudo install -d -o romm-esde-bridge -g romm-esde-bridge /var/lib/romm-esde-bridge
sudo install -d -m 0755 /opt/romm-esde-bridge /etc/romm-esde-bridge
sudo cp -a . /opt/romm-esde-bridge/
sudo install -m 0600 bridge.env.example /etc/romm-esde-bridge/bridge.env
```

Edit `bridge.env` for the RomM URL, public Bridge URL, platform filter and the
actual host paths. You may instead edit the service units if your preferred
layout differs.

## 2. Create the server token

In RomM, create a Client API Token for the Bridge and save its raw value to
`/etc/romm-esde-bridge/server-token`. The intended scope set is:

```text
me.read roms.read roms.user.read platforms.read assets.read
collections.read firmware.read devices.read
```

The file must be readable only by the service account:

```bash
sudo chown romm-esde-bridge:romm-esde-bridge /etc/romm-esde-bridge/server-token
sudo chmod 0600 /etc/romm-esde-bridge/server-token
```

Never place this token in Git, a URL, an installer bundle or `catalog.json`.

## Optional browser screenshot translation

The PC-98 browser player can pause the game, send its 640x400 screenshot to a
vision model and display Simplified Chinese translations over the game screen.
The feature is disabled until all translation settings are configured. Put the
provider key in a separate mode-0600 file and set these private environment
values in `bridge.env`:

```text
BRIDGE_TRANSLATION_BASE_URL=https://your-proxy.example/v1
BRIDGE_TRANSLATION_API_KEY_FILE=/etc/romm-esde-bridge/translation-api-key
BRIDGE_TRANSLATION_MODEL=gemini-3-flash
BRIDGE_TRANSLATION_API_STYLE=openai
```

`openai` sends an OpenAI-compatible multimodal request. Set
`BRIDGE_TRANSLATION_API_STYLE=gemini` when the proxy exposes native Gemini
`generateContent`. The provider key is used only by the Bridge and is never
sent to the browser or RomM.

## 3. Start services

```bash
sudo install -m 0644 romm-esde-bridge.service romm-esde-bridge-refresh.service \
  romm-esde-bridge-refresh.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now romm-esde-bridge-refresh.timer romm-esde-bridge.service
```

Verify both endpoints:

```bash
curl --fail http://127.0.0.1:8090/health.json
curl --fail http://127.0.0.1:8090/catalog.json
```

Expose port 8090 only to trusted clients or place it behind your own authenticated
reverse proxy. ROM downloads still require each client's RomM token.

## Optional Windows SSH helper

`bootstrap/enable-ssh.ps1` never contains a repository-owned key. To publish a
preconfigured helper for your own trusted network, point
`BOOTSTRAP_SSH_PUBLIC_KEY_FILE` at an administrator-controlled public key. If no
file is configured, users must explicitly pass `-PublicKey` to the helper.
