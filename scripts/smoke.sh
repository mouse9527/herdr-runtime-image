#!/usr/bin/env bash
# Smoke test for a built image: scripts/smoke.sh <image>
# Runs it the way the cluster will: read-only root, /tmp as a fresh local dir, home on a persistent volume (set
# SMOKE_HOME_DIR to an NFS mount to test the real storage; otherwise a docker volume), no capabilities, non-root.
# Exercises every herdr subcommand the bridge depends on with a real lead (pi with an offline dummy model), three
# concurrent tasks in one topic, and a restart.
set -euo pipefail
IMG=${1:?usage: smoke.sh <image>}
HERE=$(cd "$(dirname "$0")" && pwd)
NAME=herdr-smoke-$$
KEYS=herdr-smoke-keys-$$
HOMEVOL=herdr-smoke-home-$$
NET=herdr-smoke-net-$$
WORK=$(mktemp -d)
cleanup() {
  status=$?
  if [ $status -ne 0 ]; then echo "--- container log"; docker logs "$NAME" 2>&1 | tail -60 || true; fi
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker volume rm "$KEYS" "$HOMEVOL" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  rm -rf "$WORK"
  exit $status
}
trap cleanup EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }
ok() { echo "ok - $*"; }
ex() { docker exec "$NAME" "$@"; }
B=http://127.0.0.1:18080
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
api() { curl -fsS -H 'x-mcp-router-sub: smoke-user' -H 'content-type: application/json' "$@"; }
wait_for() {  # wait_for <seconds> <description> <command...>
  local n=$1 what=$2; shift 2
  for _ in $(seq 1 "$n"); do "$@" >/dev/null 2>&1 && return 0; sleep 1; done
  fail "timed out: $what"
}

if [ -n "${SMOKE_HOME_DIR:-}" ]; then HOME_MOUNT="$SMOKE_HOME_DIR"; else HOME_MOUNT=$HOMEVOL; docker volume create "$HOMEVOL" >/dev/null
  docker run --rm --user 0 --entrypoint chown -v "$HOMEVOL:/h" "$IMG" 10001:10001 /h; fi

# authorized_keys arrives root-owned and read-only, like a Kubernetes Secret volume.
ssh-keygen -q -t ed25519 -N '' -C smoke -f "$WORK/id"
docker volume create "$KEYS" >/dev/null
docker run --rm -i --user 0 --entrypoint sh -v "$KEYS:/k" "$IMG" \
  -c 'cat > /k/authorized_keys && chmod 0644 /k/authorized_keys && chmod 0755 /k' < "$WORK/id.pub"

docker network create "$NET" >/dev/null
docker run -d --name "$NAME" --network "$NET" --network-alias runtime --read-only --tmpfs /tmp:mode=1777 -v "$HOME_MOUNT:/home/agent" \
  --cap-drop ALL --security-opt no-new-privileges \
  -e ALLOWED_SUBS=smoke-user -e LEAD_DEFAULT_PROFILE=pi -e LEAD_MODEL_PI=smoke/smoke -e POLL_SECONDS=1 \
  -v "$KEYS:/etc/herdr-runtime/ssh:ro" -p 127.0.0.1:18080:8080 -p 127.0.0.1:12222:2222 "$IMG" >/dev/null
wait_for 60 "bridge /healthz" curl -fsS "$B/healthz"
echo "--- $(ex cat /opt/herdr-runtime/VERSIONS | tr '\n' ';')"

# 1. Non-root everywhere, read-only root.
[ "$(ex id -u)" = 10001 ] || fail "id -u is not 10001"
uids=$(ex ps -eo uid= | tr -d ' ' | sort -u | tr '\n' ' ')
[ "$uids" = "10001 " ] || fail "processes not all uid 10001: $uids"
ok "all processes run as uid 10001"
ex sh -c 'touch /opt/herdr-runtime/x 2>/dev/null || touch /usr/local/bin/x 2>/dev/null' && fail "root filesystem is writable"
ok "root filesystem is read-only"

# 2. herdr headless server; sockets and locks off the (NFS) home volume.
ex herdr --session assistant api snapshot | jq -e '.result.type == "session_snapshot"' >/dev/null || fail "api snapshot"
ex herdr session list --json | jq -e '.sessions[] | select(.name == "assistant" and .running)' >/dev/null \
  || fail "session list does not show assistant running"
ex test -S /tmp/herdr-runtime/herdr/sessions/assistant/herdr.sock || fail "herdr socket not on local /tmp"
[ -z "$(ex find /home/agent -xdev -type s)" ] || fail "unix socket on the home volume: $(ex find /home/agent -xdev -type s)"
ok "herdr server answers api snapshot; its sockets live in /tmp, none on the home volume"
# `herdr machine add` refuses a server without detached_server_daemon, which herdr derives from getsid(0) == getpid().
ex herdr --session assistant status server --json | jq -e '.capabilities.detached_server_daemon == true and .compatible' >/dev/null \
  || fail "herdr server is not a detached daemon (session leader): $(ex herdr --session assistant status server --json)"
ok "herdr server is a session leader (detached_server_daemon: true)"

# 3. Bridge health and identity.
[ "$(code $B/healthz)" = 200 ] && [ "$(code $B/readyz)" = 200 ] || fail "/healthz or /readyz"
[ "$(code $B/tasks)" = 403 ] || fail "no sub must be 403"
[ "$(code -H 'x-mcp-router-sub: someone-else' $B/tasks)" = 403 ] || fail "unknown sub must be 403"
[ "$(code -H 'x-mcp-router-sub: smoke-user' $B/tasks)" = 200 ] || fail "allowed sub must be 200"
[ "$(code -H 'x-mcp-router-sub: smoke-user' $B/topics)" = 200 ] || fail "allowed sub /topics must be 200"
ok "bridge: healthz/readyz 200, no sub 403, other sub 403, allowed sub 200"

# 4. A real lead in herdr: three concurrent tasks in one topic (one chat), each finishing on its own report.
docker exec -i "$NAME" sh -c 'mkdir -p ~/.pi/agent && cat > ~/.pi/agent/models.json' <<'JSON'
{"providers": {"smoke": {"baseUrl": "http://127.0.0.1:9/v1", "api": "openai-completions", "apiKey": "offline",
  "models": [{"id": "smoke"}]}}}
JSON
ids=()
for n in 1 2 3; do
  id=deleg_$(printf '%032x' "$n")
  ids+=("$id")
  api -d "{\"id\":\"$id\",\"generation\":1,\"request_key\":\"k$n\",\"task\":\"smoke task $n\",\"parent_chat_id\":\"chat-smoke\",\"topic\":\"smoke topic\"}" \
    "$B/tasks" | jq -e '.status == "started" and .topic.name == "smoke topic"' >/dev/null || fail "POST /tasks $n"
done
state() { api "$B/tasks/$1/events" | jq -r .state; }
prompted() { ex python3 -c "import sqlite3,sys; c=sqlite3.connect('/home/agent/.local/state/herdr-bridge/ledger.sqlite3'); sys.exit(0 if c.execute('select count(*) from tasks where prompted=1').fetchone()[0]==3 else 1)"; }
wait_for 120 "all three tasks prompted to the lead" prompted
for id in "${ids[@]}"; do [ "$(state "$id")" = running ] || fail "$id not running: $(api "$B/tasks/$id/events")"; done
topics=$(api "$B/topics")
echo "$topics" | jq -e '(.topics | length) == 1 and .topics[0].active_tasks == 3' >/dev/null || fail "one topic expected: $topics"
lead=$(echo "$topics" | jq -r '.topics[0].lead')
ex herdr --session assistant agent get "$lead" | jq -e '.result.agent | .agent == "pi" and (.terminal_id | length > 0) and (.state_change_seq | type == "number") and (.agent_status | type == "string")' >/dev/null \
  || fail "agent get JSON fields"
[ "$(ex herdr --session assistant agent list | jq "[.result.agents[] | select(.name == \"$lead\")] | length")" = 1 ] || fail "lead not in agent list"
tdir() { ex find /home/agent/workspaces/assistant/topics -maxdepth 3 -type d -name "$1"; }
for id in "${ids[@]}"; do ex test -f "$(tdir "$id")/TASK.md" || fail "TASK.md missing for $id"; done
ok "one topic lead ($lead) started via herdr agent start, all three task briefs prompted; agent get/list fields present"

ex sh -c "printf 'STATUS: done\nsmoke result 2\n' > '$(tdir "${ids[1]}")/REPORT.md'"
ex sh -c "printf 'STATUS: blocked（需要用户决定：A 还是 B）\n' > '$(tdir "${ids[2]}")/REPORT.md'"
wait_for 30 "task 2 completed" sh -c "[ \"\$(curl -s -H 'x-mcp-router-sub: smoke-user' $B/tasks/${ids[1]}/events | jq -r .state)\" = completed ]"
wait_for 30 "task 3 blocked" sh -c "[ \"\$(curl -s -H 'x-mcp-router-sub: smoke-user' $B/tasks/${ids[2]}/events | jq -r .state)\" = blocked ]"
[ "$(state "${ids[0]}")" = running ] || fail "task 1 must still be running"
q=$(api "$B/tasks/${ids[2]}/events" | jq -c .question)
api -d "{\"question_id\":$(echo "$q" | jq .id),\"version\":$(echo "$q" | jq .version),\"answer\":\"B\",\"request_key\":\"r1\"}" \
  "$B/tasks/${ids[2]}/reply" | jq -e '.status == "answered"' >/dev/null || fail "reply"
wait_for 20 "answer file" ex test -f "$(tdir "${ids[2]}")/answers/q1.md"
[ "$(state "${ids[2]}")" = running ] || fail "task 3 should be running after the answer"
ok "tasks finish independently (completed / blocked -> answered -> running / running) while the lead stays the same"

# Read-only file access on the real filesystem (home on NFS in CI).
ex sh -c "d='$(tdir "${ids[1]}")'; mkdir -p \"\$d/out\" && echo artifact > \"\$d/out/a.txt\" && ln -s /etc/passwd \"\$d/out/link\""
api -d '{}' "$B/tasks/${ids[1]}/read" | jq -e '.path == "REPORT.md" and .content == "STATUS: done\nsmoke result 2\n" and .truncated == false' >/dev/null \
  || fail "read REPORT.md"
api -d '{"path":"out/a.txt"}' "$B/tasks/${ids[1]}/read" | jq -e '.content == "artifact\n"' >/dev/null || fail "read artifact"
api "$B/tasks/${ids[1]}/files" | jq -e '[.files[] | select(.path == "out/a.txt" or (.path == "out/link" and .type == "symlink"))] | length == 2' >/dev/null \
  || fail "list task files"
[ "$(code -H 'x-mcp-router-sub: smoke-user' -d '{"path":"out/link"}' "$B/tasks/${ids[1]}/read")" = 400 ] || fail "symlink read must be 400"
[ "$(code -H 'x-mcp-router-sub: smoke-user' -d '{"path":"../../TOPIC.md"}' "$B/tasks/${ids[1]}/read")" = 400 ] || fail "traversal must be 400"
[ "$(code -H 'x-mcp-router-sub: other-user' -d '{}' "$B/tasks/${ids[1]}/read")" = 403 ] || fail "other sub must be 403"
ok "task files: REPORT.md by default, artifact, listing; symlink / traversal / other sub refused"

# 5. sshd: key login lands in the same herdr session; passwords and forwarding are refused.
SSH=(ssh -F /dev/null -o IdentitiesOnly=yes -i "$WORK/id" -p 12222 -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
     -o LogLevel=ERROR agent@127.0.0.1)
out=$("${SSH[@]}" 'id -u; herdr --session assistant agent list') || fail "ssh key login"
echo "$out" | head -1 | grep -qx 10001 || fail "ssh user is not uid 10001"
echo "$out" | tail -1 | jq -e ".result.agents[] | select(.name == \"$lead\")" >/dev/null || fail "ssh does not see the lead"
if ssh -F /dev/null -p 12222 -o BatchMode=yes -o PubkeyAuthentication=no -o PreferredAuthentications=password,keyboard-interactive \
     -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null agent@127.0.0.1 true 2>/dev/null; then
  fail "login without a key succeeded"
fi
if "${SSH[@]}" -o ExitOnForwardFailure=yes -R 127.0.0.1:19999:127.0.0.1:8080 true 2>/dev/null; then
  fail "remote port forwarding was allowed"
fi
ok "sshd: key login (uid 10001, sees the lead); password login and forwarding refused"

# 5b. `herdr machine add` from a separate client container (same image, its own empty home), through an ssh-config alias.
docker run --rm -i --network "$NET" --read-only --tmpfs /tmp:mode=1777 --tmpfs /home/agent:uid=10001,gid=10001,mode=0700 \
  -e KEY="$(base64 < "$WORK/id")" -e HERDR_CONFIG_PATH=/home/agent/.config/herdr/config.toml --entrypoint bash "$IMG" -s \
  > "$WORK/machine.log" 2>&1 <<'EOS' || { cat "$WORK/machine.log"; fail "herdr machine add"; }
set -euo pipefail
mkdir -p ~/.ssh ~/.config/herdr && chmod 700 ~/.ssh
echo "$KEY" | base64 -d > ~/.ssh/id && chmod 600 ~/.ssh/id
printf 'Host smoke-runtime\n  HostName runtime\n  Port 2222\n  User agent\n  IdentityFile ~/.ssh/id\n  IdentitiesOnly yes\n  StrictHostKeyChecking accept-new\n' > ~/.ssh/config
herdr machine add ssh://smoke-runtime --label smoke-runtime --remote-session assistant
herdr --machine smoke-runtime agent list | jq -e '.result.agents | length >= 1'
EOS
ok "herdr machine add from another container saves the machine; --machine agent list sees the lead"

# 6. Installed versions match the image labels and run.
for c in herdr claude codex pi; do
  want=$(docker image inspect "$IMG" --format "{{index .Config.Labels \"io.github.mouse9527.herdr-runtime.$c\"}}")
  [ -n "$want" ] || fail "label for $c missing"
  ex "$c" --version 2>&1 | tee /dev/stderr | grep -qF "$want" || fail "$c --version does not report $want"
done
ex gh --version | head -1 | grep -q 'gh version' || fail "gh"
ok "herdr, claude, codex, pi report the labelled versions; gh runs"

# 7. Volume layout and ledger storage.
ex test -f /home/agent/workspaces/assistant/README.md || fail "ws README missing"
# skills: projected from the public mouse9527/skills (network) by the supervisor's `skills` child
for i in $(seq 90); do ex test -f /home/agent/.agents/skills/herdr-workers/SKILL.md && break; sleep 1; done
for d in .agents .claude; do
  for s in workspace herdr-workers; do
    ex test -f "/home/agent/$d/skills/$s/SKILL.md" || fail "$s skill link missing in ~/$d/skills"
  done
done
ex test ! -e /home/agent/.codex/skills/workspace || fail "old baked skill link still in ~/.codex/skills"
ex python3 -c "import sqlite3,sys; sys.exit(sqlite3.connect('/home/agent/.local/state/herdr-bridge/ledger.sqlite3').execute('pragma journal_mode').fetchone()[0] != 'delete')" \
  || fail "ledger journal_mode is not delete"
ex test ! -e /home/agent/.local/state/herdr-bridge/ledger.sqlite3-wal || fail "ledger has a WAL file"
hostkey=$(ex ssh-keygen -lf /home/agent/.local/state/herdr-runtime/ssh/ssh_host_ed25519_key.pub)
ok "workspace, skills, host key on the volume; ledger uses a rollback journal"

# 8. Restart (local /tmp is wiped, the volume stays): state survives, the lost execution is interrupted.
docker stop -t 30 "$NAME" >/dev/null
# docker logs can lag right after stop; wait for the lines instead of reading once
wait_for 10 "herdr state saved on stop" sh -c "docker logs '$NAME' 2>&1 | grep -q 'supervisor: saved herdr state'"
wait_for 10 "supervisor stopped cleanly" sh -c "docker logs '$NAME' 2>&1 | grep -q 'supervisor: stopped'"
docker logs "$NAME" 2>&1 | grep -E 'supervisor: (herdr server stop|saved herdr state|stopped)' | sed 's/^/  /'
docker start "$NAME" >/dev/null
wait_for 60 "bridge back" curl -fsS "$B/readyz"
[ "$(ex ssh-keygen -lf /home/agent/.local/state/herdr-runtime/ssh/ssh_host_ed25519_key.pub)" = "$hostkey" ] || fail "host key changed"
ex herdr --session assistant workspace list | jq -e '.result.workspaces[] | select(.label == "assistant")' >/dev/null \
  || fail "herdr layout not restored"
[ "$(state "${ids[1]}")" = completed ] || fail "completed task lost"
[ "$(state "${ids[0]}")" = interrupted ] || fail "in-flight task should be interrupted after restart"
[ -z "$(ex find /home/agent -xdev -type s)" ] || fail "socket on the home volume after restart"
ok "after restart: same host key, herdr layout restored, ledger kept, in-flight task interrupted (not re-run)"

# 9. No credentials in the image: positive control first, then the whole filesystem (no volume mounted).
mkdir "$WORK/control"
python3 -c "print('token=' + 'ghp_' + 'Q' * 36)" > "$WORK/control/planted"
if python3 "$HERE/scan_secrets.py" "$WORK/control" >/dev/null; then fail "scanner missed a planted token"; fi
docker run --rm -i --user 0 --entrypoint python3 "$IMG" - / < "$HERE/scan_secrets.py" || fail "credential-like data in image"
ok "no token / private key shaped strings in the image (scanner caught the planted control)"
echo "smoke: all checks passed"
