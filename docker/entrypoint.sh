#!/bin/sh
# Make the seller's Chrome reachable at the address the code expects, then run the daemon.
#
# The endpoint has to be loopback on this side too: Chrome refuses a /json request whose Host
# header is a DNS name, and the WebSocket URL it answers with is loopback-shaped and dialled
# verbatim by Playwright. So socat listens on 127.0.0.1 in here and forwards to the host. A Linux
# host shares the host's network namespace instead and sets SELLY_CDP_FORWARD=0.

set -eu

# Both fail silently if unset — UTC moves the seller's quiet hours, no token dies at the first
# pass — so refuse to start. Checked here because compose interpolation runs on every command,
# including a build that needs neither.
missing=0
if [ -z "${TZ:-}" ]; then
	echo "error: TZ is unset" >&2
	missing=1
fi
if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
	echo "error: CLAUDE_CODE_OAUTH_TOKEN is unset" >&2
	missing=1
fi
[ "$missing" = 0 ] || exit 1

port="${SELLY_CDP_PORT:-9222}"
# Docker Desktop's name for the host. Podman calls it host.containers.internal.
host="${SELLY_CDP_HOST:-host.docker.internal}"

if [ "${SELLY_CDP_FORWARD:-1}" = "1" ]; then
	if ! getent hosts "$host" >/dev/null 2>&1; then
		echo "warn: cannot resolve $host, so the browser will be unreachable" >&2
		echo "warn: on a Linux host, add -f compose.yaml -f compose.linux.yaml" >&2
	fi
	socat "TCP-LISTEN:${port},bind=127.0.0.1,fork,reuseaddr" "TCP:${host}:${port}" &
fi

# Where the seller drops photos. World-writable because on a Linux host this process is root, and
# a root-owned directory inside the bind mount would leave them unable to put anything in it.
mkdir -p /data/inbox
chmod 0777 /data/inbox

exec "$@"
