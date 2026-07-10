#!/usr/bin/env bash
# Download the MCP Toolbox binary for macOS arm64 into ./bin/
# Idempotent: skips download if the binary already exists at the expected version.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="${TOOLBOX_VERSION:-1.3.0}"

mkdir -p "${SCRIPT_DIR}/bin"
BIN="${SCRIPT_DIR}/bin/toolbox"

# OS/arch detection. SHA-256 pins are per-platform for the default VERSION
# (1.3.0) — recompute all of them when bumping TOOLBOX_VERSION. There is no
# published linux/arm64 build at 1.3.0.
OS="$(uname -s)"
ARCH="$(uname -m)"
case "${OS}/${ARCH}" in
  Darwin/arm64)  PLAT="darwin/arm64"
                 SHA256="b16ea9f864b0b9c711dff0b08a663e6dee5969b41033fe6d05412dc04e85cfb8" ;;
  Darwin/x86_64) PLAT="darwin/amd64"
                 SHA256="94d6fd02a4bbc67ad9dcf69d5f36af5a584735d2fb2ebb0023e91cb701e7a98a" ;;
  Linux/x86_64)  PLAT="linux/amd64"
                 SHA256="08e00671737ff4fd6c7af25a1a0c5da43b3657c4a435fd0a381757876d694b45" ;;
  *)
    echo "✗ Unsupported platform: ${OS}/${ARCH}" >&2
    exit 1
    ;;
esac
if [[ "${VERSION}" != "1.3.0" ]]; then
  SHA256=""  # pins above only apply to 1.3.0
fi

if [[ -x "${BIN}" ]]; then
  CURRENT="$("${BIN}" --version 2>/dev/null | head -1 || echo '?')"
  if [[ "${CURRENT}" == *"${VERSION}"* ]]; then
    echo "✓ toolbox ${VERSION} already installed at ${BIN}"
    exit 0
  fi
  echo "→ replacing existing toolbox (${CURRENT}) with v${VERSION}"
fi

URL="https://storage.googleapis.com/mcp-toolbox-for-databases/v${VERSION}/${PLAT}/toolbox"
echo "→ Downloading toolbox v${VERSION} for ${PLAT}..."
curl -fL --progress-bar -o "${BIN}" "${URL}"
if [[ -n "${SHA256}" ]]; then
  echo "${SHA256}  ${BIN}" | shasum -a 256 -c - || {
    echo "✗ Checksum mismatch — removing ${BIN}" >&2
    rm -f "${BIN}"
    exit 1
  }
else
  echo "⚠ No pinned checksum for toolbox v${VERSION} on ${PLAT} — verify manually:"
  shasum -a 256 "${BIN}"
fi
chmod +x "${BIN}"

echo "✓ Installed at ${BIN}"
"${BIN}" --version
