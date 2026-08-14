#!/bin/sh
# Make the seller's Chrome reachable at the address the code expects, then run the daemon.
#
# The endpoint has to be loopback on this side too: Chrome refuses a /json request whose Host
# header is a DNS name, and the WebSocket URL it answers with is loopback-shaped and dialled
# verbatim by Playwright. So socat listens on 127.0.0.1 in here and forwards to the host. A Linux
# host shares the host's network namespace instead and sets SELLY_CDP_FORWARD=0.

set -eu

# Both fail silently if unset — UTC moves the seller's quiet hours, having neither credential
# dies at the first pass — so refuse to start. Checked here because compose interpolation runs
# on every command, including a build that needs none of them.
missing=0
if [ -z "${TZ:-}" ]; then
	echo "error: TZ is unset" >&2
	missing=1
fi
if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && [ -z "${ANTHROPIC_API_KEY:-}" ]; then
	echo "error: neither CLAUDE_CODE_OAUTH_TOKEN nor ANTHROPIC_API_KEY is set" >&2
	missing=1
fi
[ "$missing" = 0 ] || exit 1

# Both set is usually an accident: compose reads the host shell's environment before .env, so an
# ANTHROPIC_API_KEY exported in the seller's shell silently outbids the token they put in .env.
# The claude CLI prefers the API key, which moves billing from their Claude subscription to
# per-token Console billing — warn rather than leave that to the next invoice.
if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && [ -n "${ANTHROPIC_API_KEY:-}" ]; then
	echo "selly-agent: both CLAUDE_CODE_OAUTH_TOKEN and ANTHROPIC_API_KEY are set;" \
		"the claude CLI will use ANTHROPIC_API_KEY (per-token Console billing)" >&2
fi

port="${SELLY_CDP_PORT:-9222}"
# Docker Desktop's name for the host. Podman calls it host.containers.internal.
host="${SELLY_CDP_HOST:-host.docker.internal}"

if [ "${SELLY_CDP_FORWARD:-1}" = "1" ]; then
	if ! getent hosts "$host" >/dev/null 2>&1; then
		echo "selly-agent: cannot resolve $host, so the browser will be unreachable" >&2
		echo "selly-agent: on a Linux host, add -f compose.yaml -f compose.linux.yaml" >&2
	fi
	socat "TCP-LISTEN:${port},bind=127.0.0.1,fork,reuseaddr" "TCP:${host}:${port}" &
fi

# Where the seller drops photos. World-writable because on a Linux host this process is root, and
# a root-owned directory inside the bind mount would leave them unable to put anything in it.
mkdir -p /data/inbox
chmod 0777 /data/inbox

exec "$@"
