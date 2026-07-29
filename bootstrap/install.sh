#!/usr/bin/env bash
set -euo pipefail

bridge_url="${ROMM_ESDE_BRIDGE_URL:-@@BRIDGE_URL@@}"
tmp_dir="$(mktemp -d)"
cleanup() { rm -rf -- "$tmp_dir"; }
trap cleanup EXIT

curl -fsSL "$bridge_url/bootstrap/romm-esde-linux.tar.gz" -o "$tmp_dir/client.tar.gz"
curl -fsSL "$bridge_url/bootstrap/romm-esde-linux.tar.gz.sha256" -o "$tmp_dir/client.tar.gz.sha256"
expected="$(awk '{print $1}' "$tmp_dir/client.tar.gz.sha256")"
actual="$(sha256sum "$tmp_dir/client.tar.gz" | awk '{print $1}')"
if [[ "$actual" != "$expected" ]]; then
    printf '安装包校验失败。\n' >&2
    exit 1
fi
tar -xzf "$tmp_dir/client.tar.gz" -C "$tmp_dir"
python3 "$tmp_dir/romm-esde-linux/installer.py" "$@"
