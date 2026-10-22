# herdr-runtime-image

Container image for a single-user [herdr](https://github.com/herdrdev/herdr) agent runtime. One Pod per user runs:

- a headless herdr server (`herdr --session assistant server`);
- the `claude`, `codex` and `pi` coding-agent CLIs, plus `gh`, git, ripgrep, Python 3 (with venv) and Node LTS;
- a **task bridge** that hands delegated tasks to long-lived per-topic herdr "lead" agents and reports each task's
  progress over a small REST API;
- `sshd` on port 2222 (non-root, public key only) so the owner can attach with `herdr machine add`.

There is no compiler toolchain, Docker, kubectl or helm in the image, and no credential of any kind. Building and testing
code is left to each repository's CI; agents in the Pod edit code, push branches and open pull requests.

## Contents and versions

| Component | Version | How it is chosen and checked |
|---|---|---|
| Base image | `debian:bookworm-slim` | fixed index digest in `Dockerfile` |
| Debian packages | bookworm | apt (signed repository) |
| Node.js | 24.22.1 (LTS) | fixed; sha256 of the official tarball |
| gh | 2.97.0 | fixed; sha256 of the release tarball |
| herdr | **newest release at build time** | sha256 = the digest GitHub publishes for the release asset; no digest → build fails |
| claude / codex / pi | **newest on npm at build time** | exact versions passed as build args; npm verifies registry integrity |
| bridge, supervisor, `task-ask` | this repository | Python standard library only |

`scripts/resolve-versions.sh` picks the herdr / CLI versions; CI rebuilds every Monday (and on every merge or manual
run). The image records what it actually contains in `/opt/herdr-runtime/VERSIONS` and in labels
`io.github.mouse9527.herdr-runtime.{herdr,claude,codex,pi}`.

Tags: `latest` (newest build that passed the smoke test), `YYYY-MM-DD` (build date, UTC), `sha-<commit>`. A build
whose smoke test fails pushes nothing, so `latest` stays where it was. **Roll back** by deploying the previous dated
tag (or its digest).

Self-update is off everywhere: herdr reads a read-only config (`HERDR_CONFIG_PATH`, version and manifest checks off,
`resume_agents_on_restore = false`), `DISABLE_AUTOUPDATER=1` (claude), `PI_SKIP_VERSION_CHECK=1` (pi),
`check_for_update_on_startup = false` in `~/.codex/config.toml` (codex), `GH_NO_UPDATE_NOTIFIER=1` (gh). The CLIs live
under the read-only `/opt/herdr-runtime`, so an update could not be written anyway; Debian's `fd` is included so pi
does not download one at first start.

Clients that attach with `herdr machine add` must run a herdr version compatible with the Pod's; update them
(`herdr update`) when the image moves to a new herdr release.

## Layout

```
/opt/herdr-runtime/            read-only, from this repository (rootfs/)
  bin/herdr-runtime-supervisor  process supervisor (tini -> supervisor -> herdr / bridge / sshd)
  bin/task-ask                  lets a lead ask the human a question through the bridge
  bridge/herdr_bridge.py        task bridge
  etc/                          herdr config, sshd config template
  skills/                       `workspace` and `herdr-workers` agent skills
  templates/                    workspace skeleton, lead task brief, global agent guide
  cli/                          the npm CLIs
  VERSIONS                      what this image contains
/home/agent                    the user's volume (NFS PV in the cluster); UID/GID 10001
  workspaces/assistant/         workspace: README.md, repos/, topics/<topic>/{TOPIC.md,NOTES.md,tasks/<task-id>/}
  .claude/ .claude.json .codex/ .pi/   CLI logins and settings; skills and the global guide are linked from /opt
  .config/gh/                   gh login (hosts.yml), written by `gh auth login --with-token` inside the Pod
  .config/herdr/sessions  ->    symlink to /tmp/herdr-runtime/herdr/sessions (sockets stay on local disk)
  .local/state/herdr-runtime/herdr-session/  herdr layout state (session.json, snapshots), synced from /tmp
  .local/state/herdr-bridge/    bridge ledger (SQLite, rollback journal)
  .local/state/herdr-runtime/ssh/  sshd host key, generated on first start
/tmp/herdr-runtime             local emptyDir: herdr sockets and lock, sshd config and pid
```

**Home on NFS.** Unix sockets, lock files and SQLite's WAL are not safe on NFS. herdr always creates its sockets in its
session directory, so `~/.config/herdr/sessions` is a symlink into `/tmp`; herdr's layout state is restored from the
volume before the server starts, saved every minute and after a clean stop (a hard kill can lose up to a minute of
layout changes). The bridge ledger uses SQLite's rollback journal (`journal_mode=DELETE`, `synchronous=FULL`) with the
bridge as the only writer, which relies on NFS v4 byte-range locks. Run one Pod per volume.

On every start the supervisor creates missing directories, copies missing workspace files (never overwriting
existing ones), links the skills, installs herdr's agent-state hooks for claude/codex/pi, and generates the SSH host
key if there is none.

## Running

Required:

| Setting | Value |
|---|---|
| user | runs as `10001:10001` (set `runAsUser`/`runAsGroup`/`fsGroup` to match) |
| `/home/agent` | persistent volume |
| `/tmp` | writable (emptyDir); root filesystem can be read-only |
| `ALLOWED_SUBS` | comma-separated user ids the bridge accepts (the bridge refuses to start without it) |

Optional:

| Setting | Default | Meaning |
|---|---|---|
| `LEAD_MODEL_CLAUDE` / `LEAD_MODEL_CODEX` / `LEAD_MODEL_PI` | empty | `--model` for leads of that profile; **a profile without a model is refused**, CLI defaults are never used |
| `LEAD_DEFAULT_PROFILE` | `claude` | profile when the task text has no `@profile <name>` first line |
| `TOPIC_IDLE_DAYS` | `7` | a topic with no task activity this long has its lead write NOTES.md and is closed |
| `POLL_SECONDS` | `5` | how often the bridge looks at herdr and the reports |
| `/etc/herdr-runtime/ssh/authorized_keys` | — | mount read-only (e.g. a Secret); without it nobody can log in over SSH |
| `GH_TOKEN` | — | optional; normally the owner runs `gh auth login --with-token` in the Pod once and the login stays on the volume |
| `BRIDGE_DISABLED=1`, `SSH_DISABLED=1` | — | turn a component off |

Ports: `8080` bridge (expose only to the identity-verifying router), `2222` sshd (expose only to a controlled admin
path, e.g. `kubectl port-forward`). Probes: readiness `GET /readyz` (herdr API + ledger), liveness `GET /healthz`.
Give the Pod a termination grace period of about 60 s.

Example (local):

```bash
docker run --rm --read-only --tmpfs /tmp -v herdr-home:/home/agent \
  --cap-drop ALL --security-opt no-new-privileges \
  -e ALLOWED_SUBS=me -e LEAD_MODEL_CLAUDE=opus \
  -v "$PWD/keys:/etc/herdr-runtime/ssh:ro" -p 8080:8080 -p 2222:2222 \
  ghcr.io/mouse9527/herdr-runtime-image:latest
herdr machine add ssh://agent@127.0.0.1:2222 --label runtime --remote-session assistant
```

Before the first real task, attach once (SSH or `herdr machine`), log in to `gh`, and walk each CLI through its
first-run screens: theme, login, the yolo-mode confirmation and directory trust. herdr reports a CLI as ready while it still shows these
screens; the bridge then sees no work start and asks for a takeover instead of typing into them.

## Task bridge

REST API on `:8080` (the client is an Open WebUI event plugin; requests without `topic`/`parent_chat_id` still work):

| Request | Result |
|---|---|
| `POST /tasks` `{id, generation, request_key, task, topic?, parent_chat_id?}` | `{status: started \| existing, topic}`; 409 if the same id comes with a different request |
| `GET /tasks/{id}/events` | `{state, question, events, topic}`; state ∈ running / blocked / completed / failed / interrupted |
| `POST /tasks/{id}/reply` `{question_id, version, answer, request_key}` | receipt check first (a retried request returns `already_answered`), then 409 for a stale question |
| `GET /tasks`, `GET /topics` | the caller's tasks / topics |
| `POST /topics/{id}/close` | ask the topic's lead to write NOTES.md, then close it (409 while tasks are in flight) |

**Identity.** The only identity is the `x-mcp-router-sub` header, which must be in `ALLOWED_SUBS`. It is trustworthy
only because the router in front verified a signed token and overwrote the header, and NetworkPolicy lets nothing else
reach port 8080. Never expose 8080 any other way.

**Topics.** A topic is one long-lived lead in its own herdr tab plus `topics/<topic_id>/`. A task joins the topic named
in the request (by id or name), else the topic last used from the same chat, else a new topic. The first task of a
topic starts its lead with the profile's fixed flags — claude `--dangerously-skip-permissions`, codex
`--dangerously-bypass-approvals-and-sandbox`, pi (no flag needed) — plus `--model`, and checks that it actually started
working. Every later task is prompted to the same lead even while it is busy (herdr queues the prompt): **no queue and
no limits** in the bridge; the lead schedules its own work and workers. Context compaction is left to the CLIs.

**Progress.** Each task's state comes only from its own `REPORT.md` first line, never from the lead's herdr state:

| Observation | Event |
|---|---|
| `STATUS: done` | `completed` (the report is waiting for the user's review; it is not an approval) |
| `STATUS: blocked（…）` | `needs_input` with the text in brackets |
| lead runs `task-ask --task <id> "…"` | `needs_input`; the command returns at once |
| the lead is blocked in its CLI (permission / login dialog), or a new lead never started | `needs_input` asking for a manual takeover; the bridge never presses keys |
| the lead is gone from herdr | `failed` for its tasks without `done`; the next task starts a new lead |
| bridge restarted and the lead's herdr terminal is not the one recorded | `interrupted`; open questions revoked; **never re-run automatically** |

Answers are written to the task's `answers/<question_id>.md` and the lead is prompted to read them. A topic idle for
`TOPIC_IDLE_DAYS`, or closed explicitly, gets a prompt to write `NOTES.md`; once written (or after 30 minutes) its pane
is closed. The next task in that topic starts a new lead, which reads `NOTES.md` first.

## Security boundary

- Runs as UID 10001 with no capabilities needed; no setuid use, works with `no-new-privileges` and a read-only root.
- sshd runs as the same user: key authentication only, no passwords/PAM, no agent/TCP/stream-local/X11 forwarding, no
  user environment files. Host key lives on the volume; `authorized_keys` comes from a read-only mount the agent cannot
  edit.
- **Every agent in the Pod runs without approval prompts.** Their write boundary is the Pod itself: network policy,
  no ServiceAccount token, non-root, the scope of the injected GitHub token, branch protection on the repositories,
  and review of the results. The image contains no credentials; logins happen inside the Pod and stay on its volume.
- The bridge shares the agents' UID. An agent can read or change the ledger, so the bridge is a convenience layer for
  one user, not an authorization gate. Do not give it credentials or approvals that must be protected from the agents.
- CI scans the repository and the whole image filesystem for credential-shaped strings and fails on any finding.

## Build, test, release

```bash
pip install -r requirements-dev.txt && pytest -q     # bridge and supervisor unit tests
docker build $(scripts/resolve-versions.sh amd64 | sed 's/^/--build-arg /') -t herdr-runtime:dev .
scripts/smoke.sh herdr-runtime:dev                   # same checks as CI (SMOKE_HOME_DIR=<nfs mount> for NFS)
```

GitHub Actions (`.github/workflows/image.yml`): tests → resolve versions → build linux/amd64 → smoke test with home on
an NFS v4.1 mount → push to `ghcr.io/mouse9527/herdr-runtime-image` on main (push, weekly schedule, manual run). Pull
requests build and smoke-test without pushing.

## Upgrading a component

1. **herdr and the CLIs:** nothing to do; the weekly build takes the newest. Run the workflow by hand
   (`workflow_dispatch`) to pick up a release sooner.
2. **gh / Node:** change the version and both sha256 values in the `Dockerfile` (from the project's published
   checksums, not from a hash you computed over an unverified download).
3. **Base image:** replace the digest.
4. Open a PR; the smoke test must pass. After a new image is pushed, the Pod uses it only after a restart (with
   `imagePullPolicy: Always` for `latest`, or after switching to the new digest). Login state is on the volume.

## License

MIT
