#!/usr/bin/env bash
# Smoke test for a built image: scripts/smoke.sh <image>
# Runs it the way the cluster will (read-only root, home on a separate volume, no capabilities, non-root) and checks
# every process, the herdr server, bridge identity rules, sshd, the pinned tool versions and the image filesystem.
set -euo pipefail
IMG=${1:?usage: smoke.sh <image>}
HERE=$(cd "$(dirname "$0")" && pwd)
NAME=herdr-smoke-$$
KEYS=herdr-smoke-keys-$$
WORK=$(mktemp -d)
cleanup() {
  status=$?
  if [ $status -ne 0 ]; then echo "--- container log"; docker logs "$NAME" 2>&1 | tail -50 || true; fi
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker volume rm "$KEYS" >/dev/null 2>&1 || true
  rm -rf "$WORK"
  exit $status
}
trap cleanup EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }
ok() { echo "ok - $*"; }
ex() { docker exec "$NAME" "$@"; }

# authorized_keys arrives root-owned and read-only, like a Kubernetes Secret volume.
ssh-keygen -q -t ed25519 -N '' -C smoke -f "$WORK/id"
docker volume create "$KEYS" >/dev/null
docker run --rm -i --user 0 --entrypoint sh -v "$KEYS:/k" "$IMG" \
  -c 'cat > /k/authorized_keys && chmod 0644 /k/authorized_keys && chmod 0755 /k' < "$WORK/id.pub"

docker run -d --name "$NAME" --read-only --tmpfs /tmp --tmpfs /home/agent:uid=10001,gid=10001,mode=0700 \
  --cap-drop ALL --security-opt no-new-privileges \
  -e ALLOWED_SUBS=smoke-user -v "$KEYS:/etc/herdr-runtime/ssh:ro" \
  -p 127.0.0.1:18080:8080 -p 127.0.0.1:12222:2222 "$IMG" >/dev/null

for _ in $(seq 1 60); do curl -fsS -o /dev/null http://127.0.0.1:18080/healthz 2>/dev/null && break; sleep 1; done
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

# 1. Non-root everywhere.
[ "$(ex id -u)" = 10001 ] || fail "id -u is not 10001"
uids=$(ex ps -eo uid= | tr -d ' ' | sort -u | tr '\n' ' ')
[ "$uids" = "10001 " ] || fail "processes not all uid 10001: $uids"
ok "all processes run as uid 10001"
ex sh -c 'touch /opt/herdr-runtime/x 2>/dev/null || touch /usr/local/bin/x 2>/dev/null' && fail "root filesystem is writable"
ok "root filesystem is read-only"

# 2. herdr headless server and API snapshot.
ex herdr --session assistant api snapshot | jq -e '.result.type == "session_snapshot"' >/dev/null || fail "api snapshot"
ok "herdr --session assistant server answers api snapshot"

# 3. Bridge health and identity.
[ "$(code http://127.0.0.1:18080/healthz)" = 200 ] || fail "/healthz"
[ "$(code http://127.0.0.1:18080/readyz)" = 200 ] || fail "/readyz"
[ "$(code http://127.0.0.1:18080/tasks)" = 403 ] || fail "no sub must be 403"
[ "$(code -H 'x-mcp-router-sub: someone-else' http://127.0.0.1:18080/tasks)" = 403 ] || fail "unknown sub must be 403"
[ "$(code -H 'x-mcp-router-sub: smoke-user' http://127.0.0.1:18080/tasks)" = 200 ] || fail "allowed sub must be 200"
[ "$(code -H 'x-mcp-router-sub: smoke-user' http://127.0.0.1:18080/tasks/deleg_00000000000000000000000000000000/events)" = 404 ] \
  || fail "unknown task must be 404"
ok "bridge: healthz/readyz 200, no sub 403, other sub 403, allowed sub 200"

# 4. sshd: key login as the agent user lands in the same herdr session; passwords and forwarding are refused.
SSH=(ssh -i "$WORK/id" -p 12222 -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
     -o LogLevel=ERROR agent@127.0.0.1)
out=$("${SSH[@]}" 'id -u; herdr --version; herdr session list --json') || fail "ssh key login"
echo "$out" | head -1 | grep -qx 10001 || fail "ssh user is not uid 10001"
echo "$out" | tail -1 | jq -e '.sessions[] | select(.name == "assistant" and .running)' >/dev/null \
  || fail "ssh session does not see the running assistant session"
if ssh -p 12222 -o BatchMode=yes -o PubkeyAuthentication=no -o PreferredAuthentications=password,keyboard-interactive \
     -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null agent@127.0.0.1 true 2>/dev/null; then
  fail "login without a key succeeded"
fi
if "${SSH[@]}" -o ExitOnForwardFailure=yes -R 127.0.0.1:19999:127.0.0.1:8080 true 2>/dev/null; then
  fail "remote port forwarding was allowed"
fi
ok "sshd: key login (uid 10001, sees session assistant); password login and forwarding refused"

# 5. Pinned tool versions.
want_claude=$(jq -r '.dependencies["@anthropic-ai/claude-code"]' "$HERE/../cli/package.json")
want_codex=$(jq -r '.dependencies["@openai/codex"]' "$HERE/../cli/package.json")
want_pi=$(jq -r '.dependencies["@earendil-works/pi-coding-agent"]' "$HERE/../cli/package.json")
ex claude --version | tee /dev/stderr | grep -qF "$want_claude" || fail "claude version"
ex codex --version | tee /dev/stderr | grep -qF "$want_codex" || fail "codex version"
ex pi --version 2>&1 | tee /dev/stderr | grep -qF "$want_pi" || fail "pi version"
ex gh --version | head -1 | tee /dev/stderr | grep -q 'gh version' || fail "gh"
ex herdr --version | tee /dev/stderr | grep -q '^herdr ' || fail "herdr"
ok "claude $want_claude, codex $want_codex, pi $want_pi, gh, herdr report their versions"

# 6. Volume layout prepared on first start.
ex test -f /home/agent/workspaces/assistant/README.md || fail "ws README missing"
ex test -f /home/agent/.claude/skills/workspace/SKILL.md || fail "workspace skill link missing"
ex test -f /home/agent/.claude/skills/herdr-workers/SKILL.md || fail "herdr-workers skill link missing"
ex test -f /home/agent/.local/state/herdr-runtime/ssh/ssh_host_ed25519_key || fail "host key not on the volume"
ok "workspace, skills and ssh host key are on the home volume"

# 7. No credentials in the image: positive control first, then the whole filesystem (excluding the live volume).
mkdir "$WORK/control"
python3 -c "print('token=' + 'ghp_' + 'Q' * 36)" > "$WORK/control/planted"
if python3 "$HERE/scan_secrets.py" "$WORK/control" >/dev/null; then fail "scanner missed a planted token"; fi
docker run --rm -i --user 0 --entrypoint python3 "$IMG" - / < "$HERE/scan_secrets.py" || fail "credential-like data in image"
ok "no token / private key shaped strings in the image (scanner caught the planted control)"

# 8. Graceful stop.
docker stop -t 30 "$NAME" >/dev/null
docker logs "$NAME" 2>&1 | grep -q 'supervisor: stopped' || fail "supervisor did not stop cleanly"
ok "SIGTERM stops bridge, herdr and sshd cleanly"
echo "smoke: all checks passed"
