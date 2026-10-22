# herdr-runtime-image

Container image for a single-user [herdr](https://github.com/herdrdev/herdr) agent runtime. One Pod per user runs:

- a headless herdr server (`herdr --session assistant server`);
- the `claude`, `codex` and `pi` coding-agent CLIs, plus `gh`, git, ripgrep, Python 3 (with venv) and Node LTS;
- a **task bridge** that turns delegated tasks into herdr "lead" agents and reports their progress over a small REST API;
- `sshd` on port 2222 (non-root, public key only) so the owner can attach with `herdr machine add`.

There is no compiler toolchain, Docker, kubectl or helm in the image, and no credential of any kind. Building and testing
code is left to each repository's CI; agents in the Pod edit code, push branches and open pull requests.

## Contents and pinning

| Component | Version | How it is pinned |
|---|---|---|
| Base image | `debian:bookworm-slim` | index digest in `Dockerfile` |
| Debian packages | bookworm | apt (signed repository) |
| Node.js | 24.22.1 (LTS) | sha256 of the official tarball |
| gh | 2.97.0 | sha256 of the release tarball |
| herdr | 0.9.5 | sha256 of the release binary |
| claude / codex / pi | see `cli/package.json` | `npm ci` against `cli/package-lock.json` (sha512 integrity) |
| bridge, supervisor, `task-ask` | this repository | Python standard library only |

Self-update is off everywhere: herdr reads a read-only config (`HERDR_CONFIG_PATH`, version and manifest checks off,
`resume_agents_on_restore = false`), `DISABLE_AUTOUPDATER=1` (claude), `PI_SKIP_VERSION_CHECK=1` (pi),
`check_for_update_on_startup = false` in `~/.codex/config.toml` (codex), `GH_NO_UPDATE_NOTIFIER=1` (gh). The CLIs live
under the read-only `/opt/herdr-runtime`, so an update could not be written anyway.

## Layout

```
/opt/herdr-runtime/            read-only, from this repository (rootfs/)
  bin/herdr-runtime-supervisor  process supervisor (tini -> supervisor -> herdr / bridge / sshd)
  bin/task-ask                  lets a lead ask the human a question through the bridge
  bridge/herdr_bridge.py        task bridge
  etc/                          herdr config, sshd config template
  skills/                       `workspace` and `herdr-workers` agent skills
  templates/                    workspace skeleton, lead task brief, global agent guide
  cli/                          pinned npm CLIs
/home/agent                    the user's volume (PVC); UID/GID 10001
  workspaces/assistant/         workspace: README.md, repos/, tasks/<task-id>/{TASK.md,REPORT.md,answers/}
  .claude/ .codex/ .pi/         CLI logins and settings; skills and the global guide are linked from /opt
  .config/herdr/                herdr session state (session `assistant`)
  .local/state/herdr-bridge/    bridge ledger (SQLite)
  .local/state/herdr-runtime/ssh/  sshd host key, generated on first start
```

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
| `MAX_ACTIVE_TASKS` | `1` | concurrent tasks per user |
| `POLL_SECONDS` | `5` | how often the bridge looks at herdr and the reports |
| `/etc/herdr-runtime/ssh/authorized_keys` | — | mount read-only (e.g. a Secret); without it nobody can log in over SSH |
| `GH_TOKEN` | — | GitHub identity for `gh`/git inside the Pod (inject from a Secret; not baked in) |
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

Before the first real task, attach once (SSH or `herdr machine`) and walk each CLI through its first-run screens:
theme, login, the yolo-mode confirmation and directory trust. herdr reports a CLI as ready while it still shows these
screens; the bridge then sees no work start and asks for a takeover instead of typing into them.

## Task bridge

REST API on `:8080` (the client is an Open WebUI event plugin):

| Request | Result |
|---|---|
| `POST /tasks` `{id, generation, request_key, task}` | `{status: started}` or `{status: existing}`; 409 if the same id comes with a different request |
| `GET /tasks/{id}/events` | `{state, question, events}`; state ∈ running / blocked / completed / failed / interrupted |
| `POST /tasks/{id}/reply` `{question_id, version, answer, request_key}` | receipt check first (a retried request returns `already_answered`), then 409 for a stale question |
| `GET /tasks` | the caller's tasks |

**Identity.** The only identity is the `x-mcp-router-sub` header, which must be in `ALLOWED_SUBS`. It is trustworthy
only because the router in front verified a signed token and overwrote the header, and NetworkPolicy lets nothing else
reach port 8080. Never expose 8080 any other way.

**Execution.** For a new task the bridge creates `tasks/<id>/TASK.md` (from `templates/lead-task.md`, with the user's
text marked as task content, not extra authority), opens a herdr tab, starts `lead-<id>` with the profile's fixed
flags — claude `--dangerously-skip-permissions`, codex `--dangerously-bypass-approvals-and-sandbox`, pi (no flag
needed) — plus `--model`, prompts it with the brief and checks that it actually started working.

**Progress.** The lead writes `tasks/<id>/REPORT.md`; the bridge reads only the first line:

| Observation | Event |
|---|---|
| `STATUS: done` | `completed` (the report is waiting for the user's review; it is not an approval) |
| `STATUS: blocked（…）` | `needs_input` with the text in brackets |
| lead runs `task-ask "…"` | `needs_input`; the command returns at once |
| herdr shows the lead blocked in its CLI (permission / login dialog), or the lead never started | `needs_input` asking for a manual takeover; the bridge never presses keys |
| lead gone from herdr without `done` | `failed` |
| bridge restarted and the lead's herdr terminal is not the one recorded | `interrupted`; open questions revoked; **never re-run automatically** |

Answers are written to `tasks/<id>/answers/<question_id>.md` and the lead is prompted to read them.
`task-ask --wait` blocks until answered (for scripts; agent tool calls usually time out long before a human answers).

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
docker build -t herdr-runtime:dev .
scripts/smoke.sh herdr-runtime:dev                   # same checks as CI
```

GitHub Actions (`.github/workflows/image.yml`): tests → build linux/amd64 → smoke test → push to
`ghcr.io/mouse9527/herdr-runtime-image` (`sha-<commit>`, `latest` on main, `v*` tags) on push events only. Pull
requests build and smoke-test without pushing. Deploy by digest.

## Upgrading a component

1. **CLI:** change the version in `cli/package.json`, run `npm install --package-lock-only --ignore-scripts` in `cli/`,
   commit both files.
2. **herdr / gh / Node:** change the version and both sha256 values in the `Dockerfile` (from the project's published
   checksums or release asset digests, not from a hash you computed over an unverified download).
3. **Base image:** replace the digest.
4. Open a PR; the smoke test must pass. After merge, deploy the new digest and re-check the CLIs inside the Pod (login
   state is on the volume and normally survives).

## License

MIT
