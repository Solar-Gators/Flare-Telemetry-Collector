#!/usr/bin/env bash
# Re-vendor the CAN message catalog from the firmware repo.
# Run from anywhere; resolves paths relative to this script.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$here/.." && pwd)"
src="$repo_root/../Flare-Firmware/docs/can_messages.toml"
dst="$here/can_messages.toml"

if [[ ! -f "$src" ]]; then
    echo "error: firmware catalog not found at $src" >&2
    echo "       clone Flare-Firmware next to this repo, or edit \$src in sync.sh" >&2
    exit 1
fi

cp "$src" "$dst"
echo "synced $dst from $src"
echo "now run: (cd $repo_root/collector && ../.venv/bin/python -m pytest tests/test_upload_map.py)"
