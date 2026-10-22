#!/usr/bin/env bash
# Resolve the newest claude / codex / pi CLIs and herdr release, print them as KEY=VALUE build args.
#   scripts/resolve-versions.sh [amd64|arm64]          (default amd64)
# herdr's sha256 comes from the digest GitHub publishes for the release asset; without one this fails, it never
# falls back to hashing an unverified download. Set GH_TOKEN / GITHUB_TOKEN to avoid the anonymous API rate limit.
set -euo pipefail
case "${1:-amd64}" in
  amd64) asset=herdr-linux-x86_64 ;;
  arm64) asset=herdr-linux-aarch64 ;;
  *) echo "unknown arch $1" >&2; exit 2 ;;
esac

npm_latest() { npm view "$1" version; }
CLAUDE_VERSION=$(npm_latest @anthropic-ai/claude-code)
CODEX_VERSION=$(npm_latest @openai/codex)
PI_VERSION=$(npm_latest @earendil-works/pi-coding-agent)

token=${GH_TOKEN:-${GITHUB_TOKEN:-}}
auth=()
[ -n "$token" ] && auth=(-H "Authorization: Bearer $token")
release=$(curl -fsSL ${auth[@]+"${auth[@]}"} -H 'Accept: application/vnd.github+json' \
  https://api.github.com/repos/herdrdev/herdr/releases/latest)
HERDR_TAG=$(jq -r '.tag_name' <<<"$release")
digest=$(jq -r --arg a "$asset" '.assets[] | select(.name == $a) | .digest // empty' <<<"$release")
case "$digest" in
  sha256:[0-9a-f]*) HERDR_SHA256=${digest#sha256:} ;;
  *) echo "herdr $HERDR_TAG: no published sha256 digest for $asset" >&2; exit 1 ;;
esac
[ ${#HERDR_SHA256} -eq 64 ] || { echo "herdr digest malformed" >&2; exit 1; }

for v in CLAUDE_VERSION CODEX_VERSION PI_VERSION HERDR_TAG; do
  [[ ${!v} =~ ^v?[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.]+)?$ ]] || { echo "$v looks wrong: ${!v}" >&2; exit 1; }
done
printf 'CLAUDE_VERSION=%s\nCODEX_VERSION=%s\nPI_VERSION=%s\nHERDR_VERSION=%s\nHERDR_SHA256=%s\n' \
  "$CLAUDE_VERSION" "$CODEX_VERSION" "$PI_VERSION" "${HERDR_TAG#v}" "$HERDR_SHA256"
