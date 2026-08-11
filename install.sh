#!/bin/sh
# The curl bootstrap: fetch a verified release and hand off to its own installer.
#
# This is intentionally the smallest useful thing. It can vouch for exactly one property — that
# the archive it downloaded is the one whose checksum was published — and everything after that
# is the release's own ./setup, which is versioned, reviewable in the repo, and prints where it
# will write before it writes anything. Logic inlined here instead would be logic served from a
# URL with no version and no review, which is worse, not better.
#
# POSIX sh: it runs before anything is installed, on whatever /bin/sh the machine has.

set -eu

REPO_URL="https://github.com/carousell/selly-agent"
DEFAULT_BASE_URL="$REPO_URL/releases/latest/download"
BASE_URL="${SELLY_INSTALL_BASE_URL:-}"

say() {
	echo "$1"
}

die() {
	echo "$1" >&2
	exit 1
}

# --- not yet ---------------------------------------------------------------------------------
# Release hosting is not public yet, so the honest answer is that this path does not work rather
# than a 404 halfway through. Setting a base URL is how the end-to-end test exercises the real
# code path. REMOVE THIS BLOCK at cutover, when releases are published.
if [ -z "$BASE_URL" ]; then
	echo "SELLY: installing with this script isn't supported yet." >&2
	echo "SELLY:   Clone the repo and run ./setup instead:" >&2
	echo "SELLY:     git clone $REPO_URL && cd selly-agent && ./setup" >&2
	exit 1
fi

# --dev points the install at the tree it was run from, and this one is a temp directory that is
# deleted the moment setup returns — the install would be dead on arrival, with no error.
for arg in "$@"; do
	[ "$arg" = "--dev" ] && die "--dev needs a checkout; clone the repo and run ./setup --dev there."
done

# --- what is about to happen ---------------------------------------------------------------

say "Here's what this does, before it does any of it:"
say "  1. Download $BASE_URL/SHA256SUMS"
say "  2. Download the selly-agent archive it names, and check it against that checksum"
say "  3. Unpack it into a temporary directory, deleted when this finishes"
say "  4. Run the unpacked ./setup, which fetches the Python it runs on and then lists"
say "     everywhere it writes before writing"
say ""

# --- prerequisites ---------------------------------------------------------------------------

case "$(uname -s)" in
Darwin | Linux) ;;
*) die "selly-agent runs on macOS and Linux today (this is $(uname -s))." ;;
esac

# No python3 here: the release's own ./setup provisions the interpreter it needs, so the machine
# having one — or having a usable one — is not a precondition for installing.
for tool in curl tar; do
	command -v "$tool" >/dev/null 2>&1 || die "$tool is required and isn't on your PATH."
done

# macOS ships `shasum`, GNU coreutils ships `sha256sum`, and a machine rarely has both. Either
# verifies the archive, so requiring a particular one would refuse a machine that can.
if command -v shasum >/dev/null 2>&1; then
	sha256_check() { shasum -a 256 -c "$1"; }
elif command -v sha256sum >/dev/null 2>&1; then
	sha256_check() { sha256sum -c "$1"; }
else
	die "shasum or sha256sum is required and neither is on your PATH."
fi

# --- fetch, checksums first -------------------------------------------------------------------

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT INT TERM
cd "$work"

say "Fetching the checksum file…"
curl -fsSL "$BASE_URL/SHA256SUMS" -o SHA256SUMS ||
	die "couldn't download $BASE_URL/SHA256SUMS"

# The archive's name is read out of the checksum file, so there is no second source to disagree
# with it, no API call, and nothing to parse JSON with.
archives="$(awk '$2 ~ /^\*?selly-agent-.*\.tar\.gz$/ { sub(/^\*/, "", $2); print $2 }' SHA256SUMS)"
[ -n "$archives" ] || die "SHA256SUMS doesn't name a selly-agent archive."
if [ "$(echo "$archives" | wc -l)" -gt 1 ]; then
	# A release directory holds exactly one. More than one means we would be guessing which,
	# and guessing which code to run is not something this should do.
	die "SHA256SUMS names more than one archive; can't tell which release to install."
fi
archive="$archives"

say "Downloading $archive"
curl -fsSL "$BASE_URL/$archive" -o "$archive" || die "couldn't download $BASE_URL/$archive"

say "Checking it against the published checksum…"
grep " \*\{0,1\}$archive\$" SHA256SUMS >expected.sums
sha256_check expected.sums >/dev/null 2>&1 ||
	die "$archive does not match its published checksum — refusing to run it."

# --- unpack and hand off -----------------------------------------------------------------------

say "Unpacking…"
tar -xzf "$archive"
tree="$(find . -maxdepth 1 -type d -name 'selly-agent-*' | head -n 1)"
[ -n "$tree" ] && [ -x "$tree/setup" ] || die "the archive doesn't contain a runnable ./setup."

say "Handing over to the installer."
say ""
cd "$tree"

# `curl … | sh` leaves stdin holding this script, so the wizard's prompts would read the rest of
# it instead of reaching a person. Reattach the terminal when there is one; with no terminal,
# setup's own non-interactive rules take over.
#
# The test is whether /dev/tty can be *opened*, not whether it exists: it is present as a device
# node even in contexts with no controlling terminal (CI, an agent session, a launchd job), where
# opening it fails and would take the install down on its very last step.
if (exec </dev/tty) 2>/dev/null; then
	./setup "$@" </dev/tty
else
	./setup "$@"
fi
