# The container install profile. See docs/docker.md.
#
# Node is the base rather than an add-on: two of the three programs this runs are Node ones (the
# `claude` CLI and the Playwright MCP server). Debian, not Alpine — the interpreter uv provisions
# is a glibc build, and no musl triple exists for it.
FROM node:26-trixie-slim

ARG CLAUDE_CODE_VERSION=2.1.220

RUN apt-get update \
	&& apt-get install -y --no-install-recommends \
		ca-certificates \
		curl \
		procps \
		socat \
		tini \
		tzdata \
	&& rm -rf /var/lib/apt/lists/*

RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"

WORKDIR /opt/sellee
COPY . /opt/sellee

# The same front door a person runs on their own machine. XDG_DATA_HOME is overridden for this
# step alone: at runtime it points into the bind mount, which does not exist yet, and uv's
# interpreter store has to land in the image.
RUN XDG_DATA_HOME=/opt/sellee-runtime ./setup --bootstrap-only

# Warm the npx cache with the spec the code asks for, read out of the code so a version bump
# cannot leave a stale image quietly downloading on the first browser action.
RUN SPEC="$(.venv/bin/python -c 'import sys; sys.path.insert(0, "src"); \
	from sellee.browser import client; print(client.PINNED_MCP_SPEC)')" \
	&& npx --yes "$SPEC" --version

RUN printf '#!/bin/sh\nexec /opt/sellee/.venv/bin/python /opt/sellee/bin/sellee "$@"\n' \
	> /usr/local/bin/sellee \
	&& chmod 755 /usr/local/bin/sellee

ENV XDG_DATA_HOME=/data/share \
	XDG_STATE_HOME=/data/state \
	XDG_CONFIG_HOME=/data/config \
	XDG_CACHE_HOME=/data/cache

ENV SELLEE_DEPLOYMENT=container

# A published port arrives on the bridge address, not this container's loopback, so loopback-only
# would answer nothing. Set it back to 127.0.0.1 wherever the container shares a real host's
# network namespace — compose.linux.yaml does.
ENV SELLEE_BIND_HOST=0.0.0.0

# tini, because the daemon reaps only the children it spawns and installs no SIGCHLD handler — as
# PID 1 it would collect an orphaned `node` per pass.
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/sellee/docker/entrypoint.sh"]
CMD ["/opt/sellee/.venv/bin/python", "/opt/sellee/bin/sellee", "daemon", "run"]
