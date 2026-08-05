#!/usr/bin/env bash
# Regenerate all diagrams under docs/. Entry point: `make diagrams`.
#
# Per-diagram handling lives here. Currently only pikchr diagrams exist:
# each is rendered to PNG (the committed artifact — font-stable across
# devices, unlike SVG whose text rendering varies with the viewer's fonts).
# The intermediate SVG is piped straight into the converter, never written
# to disk.
set -euo pipefail

cd "$(dirname "$0")"

# rsvg-convert (librsvg) is required for SVG -> PNG; not auto-installed.
if ! command -v rsvg-convert >/dev/null 2>&1; then
	echo "error: rsvg-convert not found in PATH — install librsvg and re-run" >&2
	exit 1
fi

# pikchr is a single-file C program; build and install it if missing.
if ! command -v pikchr >/dev/null 2>&1; then
	echo "pikchr not found in PATH; building from source..." >&2
	tmp=$(mktemp -d)
	trap 'rm -rf "$tmp"' EXIT
	curl -fsSL \
		"https://pikchr.org/home/raw/157276b22395ca1423bce1532e07d56fc3597cc813bf5cd46294c32181bbe1dc?at=pikchr.c" \
		> "$tmp/pikchr.c"
	gcc -O2 -DPIKCHR_SHELL -o "$tmp/pikchr" "$tmp/pikchr.c" -lm
	mv "$tmp/pikchr" /usr/local/bin/pikchr
	echo "installed pikchr to /usr/local/bin/pikchr" >&2
fi

# render_pikchr <basename> <png-width-px>
#
# PNG width is per-diagram deliberately — diagrams differ in size and detail,
# so a global scale isn't worth generalizing yet. rsvg-convert renders the
# vector directly at the target width (height follows the aspect ratio).
render_pikchr() {
	local name=$1 width=$2
	pikchr --svg-only "$name.pikchr" \
		| rsvg-convert --width "$width" --background-color white -o "$name.png"
	echo "rendered $name.png (${width}px wide)"
}

render_pikchr architecture-master 800
