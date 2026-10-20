#!/usr/bin/env bash
# Clones OCA/l10n-paraguay into vendor/ pinned to the commit the addon is tested against.
set -euo pipefail

OCA_REPO="${OCA_REPO:-https://github.com/OCA/l10n-paraguay.git}"
OCA_COMMIT="${OCA_COMMIT:-61d565a}"
TARGET="$(cd "$(dirname "$0")/.." && pwd)/vendor/l10n-paraguay"

if [ ! -d "$TARGET/.git" ]; then
    mkdir -p "$(dirname "$TARGET")"
    git clone --no-checkout "$OCA_REPO" "$TARGET"
fi
git -C "$TARGET" fetch --quiet origin 18.0 || true
git -C "$TARGET" checkout --quiet --detach "$OCA_COMMIT"
echo "OCA/l10n-paraguay en $(git -C "$TARGET" rev-parse --short HEAD)"
