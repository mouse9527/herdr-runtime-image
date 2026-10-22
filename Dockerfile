# herdr runtime: headless herdr + claude / codex / pi CLIs + gh + task bridge, for one user per Pod.
# Every download is pinned and checked: base image by digest, tarballs/binaries by sha256, npm packages by the
# sha512 integrity in cli/package-lock.json. CI builds linux/amd64; the arm64 sums exist only for local builds.
FROM debian:bookworm-slim@sha256:7ff6ba31527b9e336162fa061746c366c4b2032f4d2cff2078bea43f78efe3f8

ARG DEBIAN_FRONTEND=noninteractive
# openssh-server generates host keys at install time; drop them, the runtime keeps its own key on the volume.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bash ca-certificates curl git jq less openssh-client openssh-server procps python3 python3-venv ripgrep \
      tini xz-utils \
 && rm -rf /var/lib/apt/lists/* /var/cache/apt/* /var/log/apt /var/log/dpkg.log \
 && rm -f /etc/ssh/ssh_host_*

ARG NODE_VERSION=24.22.1
ARG NODE_SHA256_AMD64=6c172a9ab6718f7b801fb5a7bd486af2ee5e32758086a04b84fcce2aa77095e6
ARG NODE_SHA256_ARM64=dbabf7a1cbf947e6059ffad2c27b80041db97dea1e553b62afdee7f13fca336e
ARG GH_VERSION=2.97.0
ARG GH_SHA256_AMD64=a2c9b8497e1f85b1ad0dfcb78b5a622e098801b8e461e459e88e1ee12f018112
ARG GH_SHA256_ARM64=73ea440ecad9c9e284429997ee6f93577bc6f7bc6fba357ef62c53ad8fb641a5
ARG HERDR_VERSION=0.9.5
ARG HERDR_SHA256_AMD64=14d9e8bc8b4a9e0118d77c32266f76d461f09b40342eb23752960bd89f59929d
ARG HERDR_SHA256_ARM64=17d7536f2da50859523680ef279dffcac8e4a235f4e46fe8da6c2ec44141897c
RUN set -eu; \
    case "$(dpkg --print-architecture)" in \
      amd64) node=x64; gh=amd64; herdr=x86_64; \
             node_sha=$NODE_SHA256_AMD64; gh_sha=$GH_SHA256_AMD64; herdr_sha=$HERDR_SHA256_AMD64 ;; \
      arm64) node=arm64; gh=arm64; herdr=aarch64; \
             node_sha=$NODE_SHA256_ARM64; gh_sha=$GH_SHA256_ARM64; herdr_sha=$HERDR_SHA256_ARM64 ;; \
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
    curl -fsSLo /usr/local/bin/herdr "https://github.com/herdrdev/herdr/releases/download/v${HERDR_VERSION}/herdr-linux-${herdr}"; \
    echo "${herdr_sha}  /usr/local/bin/herdr" | sha256sum -c -; \
    chmod 0755 /usr/local/bin/herdr /usr/local/bin/gh; \
    rm -f /tmp/node.tar.xz /tmp/gh.tar.gz

# Coding-agent CLIs, exact versions from the lockfile (npm ci verifies each package's sha512 integrity).
COPY cli/package.json cli/package-lock.json /opt/herdr-runtime/cli/
RUN cd /opt/herdr-runtime/cli \
 && npm ci --omit=dev --no-audit --no-fund \
 && for b in claude codex pi; do ln -s /opt/herdr-runtime/cli/node_modules/.bin/$b /usr/local/bin/$b; done \
 && rm -rf /root/.npm /root/.cache /tmp/*

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

LABEL org.opencontainers.image.source="https://github.com/mouse9527/herdr-runtime-image" \
      org.opencontainers.image.description="Single-user herdr agent runtime: herdr, claude/codex/pi CLIs, gh, task bridge" \
      org.opencontainers.image.licenses="MIT"

USER 10001:10001
WORKDIR /home/agent
EXPOSE 8080 2222
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/opt/herdr-runtime/bin/herdr-runtime-supervisor"]
