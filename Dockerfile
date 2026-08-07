# The container install profile. See docs/docker.md.
#
# Node is the base rather than an add-on: two of the three programs this runs are Node ones (the
# `claude` CLI and the Playwright MCP server). Debian, not Alpine — the interpreter uv provisions
# is a glibc build, and no musl triple exists for it.
FROM node:26-trixie-slim

ARG CLAUDE_CODE_VERSION=2.1.220

RUN apt-get update \
	# imagemagick keeps its recommends: the delegate package they pull in is what gives it HEIC.
	&& apt-get install -y imagemagick \
	&& apt-get install -y --no-install-recommends \
		ca-certificates \
		curl \
		procps \
		socat \
		tini \
		tzdata \
	&& rm -rf /var/lib/apt/lists/*

RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"

WORKDIR /opt/selly-agent
COPY . /opt/selly-agent

# The same front door a person runs on their own machine. XDG_DATA_HOME is overridden for this
# step alone: at runtime it points into the bind mount, which does not exist yet, and uv's
# interpreter store has to land in the image.
RUN XDG_DATA_HOME=/opt/selly-runtime ./setup --bootstrap-only

# Warm the npx cache with the spec the code asks for, read out of the code so a version bump
# cannot leave a stale image quietly downloading on the first browser action.
RUN SPEC="$(.venv/bin/python -c 'import sys; sys.path.insert(0, "src"); \
	from selly_agent.browser import client; print(client.PINNED_MCP_SPEC)')" \
	&& npx --yes "$SPEC" --version

RUN printf '#!/bin/sh\nexec /opt/selly-agent/.venv/bin/python /opt/selly-agent/bin/selly-agent "$@"\n' \
	> /usr/local/bin/selly-agent \
	&& chmod 755 /usr/local/bin/selly-agent

ENV XDG_DATA_HOME=/data/share \
	XDG_STATE_HOME=/data/state \
	XDG_CONFIG_HOME=/data/config \
	XDG_CACHE_HOME=/data/cache

ENV SELLY_DEPLOYMENT=container

# A published port arrives on the bridge address, not this container's loopback, so loopback-only
# would answer nothing. Set it back to 127.0.0.1 wherever the container shares a real host's
# network namespace — compose.linux.yaml does.
ENV SELLY_BIND_HOST=0.0.0.0

# tini, because the daemon reaps only the children it spawns and installs no SIGCHLD handler — as
# PID 1 it would collect an orphaned `node` per pass.
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/selly-agent/docker/entrypoint.sh"]
CMD ["/opt/selly-agent/.venv/bin/python", "/opt/selly-agent/bin/selly-agent", "daemon", "run"]
