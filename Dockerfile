# herdr runtime: headless herdr + claude / codex / pi CLIs + gh + task bridge, for one user per Pod.
# Fixed: base image (digest), Node and gh (sha256). Newest at build time (CI resolves them weekly, see
# scripts/resolve-versions.sh): herdr (sha256 from the release's published asset digest) and the claude / codex / pi
# CLIs (exact versions from npm, whose registry integrity npm verifies). VERSIONS records what was installed.
# CI builds linux/amd64; the arm64 sums exist only for local builds.
FROM debian:bookworm-slim@sha256:7ff6ba31527b9e336162fa061746c366c4b2032f4d2cff2078bea43f78efe3f8

ARG DEBIAN_FRONTEND=noninteractive
# openssh-server generates host keys at install time; drop them, the runtime keeps its own key on the volume.
# pi downloads `fd` at first start when it is missing; ship Debian's instead (binary is called fdfind there).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bash ca-certificates curl fd-find git jq less openssh-client openssh-server procps python3 python3-venv \
      ripgrep tini xz-utils \
 && ln -s /usr/bin/fdfind /usr/local/bin/fd \
 && rm -rf /var/lib/apt/lists/* /var/cache/apt/* /var/log/apt /var/log/dpkg.log \
 && rm -f /etc/ssh/ssh_host_*

ARG NODE_VERSION=24.22.1
ARG NODE_SHA256_AMD64=6c172a9ab6718f7b801fb5a7bd486af2ee5e32758086a04b84fcce2aa77095e6
ARG NODE_SHA256_ARM64=dbabf7a1cbf947e6059ffad2c27b80041db97dea1e553b62afdee7f13fca336e
ARG GH_VERSION=2.97.0
ARG GH_SHA256_AMD64=a2c9b8497e1f85b1ad0dfcb78b5a622e098801b8e461e459e88e1ee12f018112
ARG GH_SHA256_ARM64=73ea440ecad9c9e284429997ee6f93577bc6f7bc6fba357ef62c53ad8fb641a5
RUN set -eu; \
    case "$(dpkg --print-architecture)" in \
      amd64) node=x64; gh=amd64; node_sha=$NODE_SHA256_AMD64; gh_sha=$GH_SHA256_AMD64 ;; \
      arm64) node=arm64; gh=arm64; node_sha=$NODE_SHA256_ARM64; gh_sha=$GH_SHA256_ARM64 ;; \
      *) echo "unsupported architecture" >&2; exit 1 ;; \
    esac; \
    cd /tmp; \
    curl -fsSLo node.tar.xz "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${node}.tar.xz"; \
    echo "${node_sha}  node.tar.xz" | sha256sum -c -; \
    mkdir -p /usr/local/lib/nodejs; \
    tar -xJf node.tar.xz -C /usr/local/lib/nodejs --strip-components=1 --no-same-owner; \
    for b in node npm npx; do ln -s /usr/local/lib/nodejs/bin/$b /usr/local/bin/$b; done; \
    curl -fsSLo gh.tar.gz "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${gh}.tar.gz"; \
    echo "${gh_sha}  gh.tar.gz" | sha256sum -c -; \
    tar -xzf gh.tar.gz --strip-components=2 --no-same-owner -C /usr/local/bin "gh_${GH_VERSION}_linux_${gh}/bin/gh"; \
    chmod 0755 /usr/local/bin/gh; \
    rm -f /tmp/node.tar.xz /tmp/gh.tar.gz

# herdr: version and the sha256 of this architecture's asset come from scripts/resolve-versions.sh. No defaults:
# a build without them fails instead of silently picking something unverified.
ARG HERDR_VERSION
ARG HERDR_SHA256
RUN set -eu; \
    test -n "$HERDR_VERSION" && test -n "$HERDR_SHA256" || { echo "HERDR_VERSION/HERDR_SHA256 required" >&2; exit 1; }; \
    case "$(dpkg --print-architecture)" in amd64) a=x86_64 ;; arm64) a=aarch64 ;; *) exit 1 ;; esac; \
    curl -fsSLo /usr/local/bin/herdr "https://github.com/herdrdev/herdr/releases/download/v${HERDR_VERSION}/herdr-linux-${a}"; \
    echo "${HERDR_SHA256}  /usr/local/bin/herdr" | sha256sum -c -; \
    chmod 0755 /usr/local/bin/herdr

# Coding-agent CLIs at the exact versions resolved for this build.
ARG CLAUDE_VERSION
ARG CODEX_VERSION
ARG PI_VERSION
RUN set -eu; \
    for v in "$CLAUDE_VERSION" "$CODEX_VERSION" "$PI_VERSION"; do test -n "$v" || { echo "CLI versions required" >&2; exit 1; }; done; \
    mkdir -p /opt/herdr-runtime/cli; cd /opt/herdr-runtime/cli; \
    echo '{"name":"herdr-runtime-cli","private":true}' > package.json; \
    npm install --omit=dev --no-audit --no-fund --save-exact \
      "@anthropic-ai/claude-code@${CLAUDE_VERSION}" "@openai/codex@${CODEX_VERSION}" \
      "@earendil-works/pi-coding-agent@${PI_VERSION}"; \
    for b in claude codex pi; do ln -s /opt/herdr-runtime/cli/node_modules/.bin/$b /usr/local/bin/$b; done; \
    rm -rf /root/.npm /root/.cache /tmp/*

RUN groupadd -g 10001 agent \
 && useradd -u 10001 -g 10001 -d /home/agent -M -s /bin/bash agent \
 && usermod -p '*' agent \
 && install -d -o 10001 -g 10001 -m 0700 /home/agent \
 && install -d -m 0755 /etc/herdr-runtime/ssh

COPY rootfs/ /
RUN chmod 0755 /opt/herdr-runtime/bin/* /opt/herdr-runtime/bridge/herdr_bridge.py \
 && ln -s /opt/herdr-runtime/bin/task-ask /usr/local/bin/task-ask

ENV HOME=/home/agent \
    USER=agent \
    LANG=C.UTF-8 \
    PATH=/opt/herdr-runtime/bin:/usr/local/bin:/usr/bin:/bin \
    HERDR_SESSION=assistant \
    HERDR_CONFIG_PATH=/opt/herdr-runtime/etc/herdr/config.toml \
    HERDR_RUNTIME_DIR=/tmp/herdr-runtime \
    DISABLE_AUTOUPDATER=1 \
    PI_SKIP_VERSION_CHECK=1 \
    GH_NO_UPDATE_NOTIFIER=1 \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    PYTHONDONTWRITEBYTECODE=1

# What is actually installed (read back from the binaries, not from the build args). The CLIs write state into
# $HOME when run, so they get a throwaway one: /home/agent must stay empty in the image.
RUN set -eu; export HOME=/tmp/versions-home; { \
      echo "herdr: $(herdr --version)"; \
      echo "claude: $(claude --version)"; \
      echo "codex: $(codex --version)"; \
      echo "pi: $(pi --version 2>&1)"; \
      echo "gh: $(gh --version | head -1)"; \
      echo "node: $(node --version)"; \
      echo "debian: $(cat /etc/debian_version)"; \
    } > /opt/herdr-runtime/VERSIONS; cat /opt/herdr-runtime/VERSIONS; \
    grep -qF "herdr ${HERDR_VERSION}" /opt/herdr-runtime/VERSIONS; \
    grep -qF "claude: ${CLAUDE_VERSION}" /opt/herdr-runtime/VERSIONS; \
    grep -qF "${CODEX_VERSION}" /opt/herdr-runtime/VERSIONS; \
    grep -qF "pi: ${PI_VERSION}" /opt/herdr-runtime/VERSIONS; \
    rm -rf /tmp/versions-home; \
    test -z "$(ls -A /home/agent)"

LABEL io.github.mouse9527.herdr-runtime.herdr="${HERDR_VERSION}" \
      io.github.mouse9527.herdr-runtime.claude="${CLAUDE_VERSION}" \
      io.github.mouse9527.herdr-runtime.codex="${CODEX_VERSION}" \
      io.github.mouse9527.herdr-runtime.pi="${PI_VERSION}" \
      org.opencontainers.image.source="https://github.com/mouse9527/herdr-runtime-image" \
      org.opencontainers.image.description="Single-user herdr agent runtime: herdr, claude/codex/pi CLIs, gh, task bridge" \
      org.opencontainers.image.licenses="MIT"

USER 10001:10001
WORKDIR /home/agent
EXPOSE 8080 2222
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/opt/herdr-runtime/bin/herdr-runtime-supervisor"]
