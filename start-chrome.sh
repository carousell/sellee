#!/bin/sh
# Start the Chrome the agent drives — on this computer, with a profile of its own, so your
# everyday tabs and logins are untouched. Leave it running while the agent is working; close it
# and the agent says so and waits.
#
# The switches are the ones a host install uses. The debugging port stays on loopback: it is
# browser control with no authentication.
#
# The port is pinned here, unlike a Chrome the daemon starts for itself — that one is given
# --remote-debugging-port=0 and announces its choice inside its own profile directory. Nothing
# started from here can do that: the daemon is on the other side of a container boundary or at
# least reads a different config, so the port has to be a number both sides already know. Set
# SELLEE_CDP_PORT on both if the default is taken, and set chrome_cdp_port to match.

set -eu

port="${SELLEE_CDP_PORT:-9222}"
profile="${SELLEE_CHROME_PROFILE:-$HOME/.sellee/chrome-profile}"

die() {
	echo "$1" >&2
	exit 1
}

# Two Chromes on one profile: the second either hangs or opens it read-only.
if command -v curl >/dev/null 2>&1 &&
	curl -fsS --max-time 2 "http://127.0.0.1:$port/json/version" >/dev/null 2>&1; then
	echo "The agent's Chrome is already running on port $port — nothing to do."
	exit 0
fi

case "$(uname -s)" in
Darwin)
	default="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
	;;
*)
	default=""
	for candidate in google-chrome google-chrome-stable chromium chromium-browser; do
		found="$(command -v "$candidate" 2>/dev/null || true)"
		[ -n "$found" ] && default="$found" && break
	done
	;;
esac

chrome="${SELLEE_CHROME_BIN:-$default}"
[ -n "$chrome" ] && [ -x "$chrome" ] ||
	die "Could not find Google Chrome. Install it, or set SELLEE_CHROME_BIN to its path."

mkdir -p "$profile"

echo "Starting the agent's Chrome (profile: $profile, debugging port: $port)."
echo "Leave this window open while the agent is working."

"$chrome" \
	--remote-debugging-port="$port" \
	--user-data-dir="$profile" \
	--disable-backgrounding-occluded-windows \
	--no-first-run \
	--no-default-browser-check \
	--restore-last-session \
	--hide-crash-restore-bubble \
	--window-position=80,80 \
	--window-size=1200,900 \
	>/dev/null 2>&1 &

echo "Started. Sign in to your marketplaces in that window if you have not already."
