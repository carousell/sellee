#!/usr/bin/env bash
# Regenerate all diagrams under docs/. Entry point: `make diagrams`.
#
# Per-diagram handling lives here. Currently only d2 diagrams exist: each is
# rendered to PNG (the committed artifact — font-stable across devices, unlike
# SVG whose text rendering varies with the viewer's fonts). d2 rasterizes
# natively, so no intermediate SVG or external converter is involved.
set -euo pipefail

cd "$(dirname "$0")"

# d2 with the TALA layout engine is required; not auto-installed. The diagrams
# rely on TALA's fixed positioning (top/left), which the bundled dagre/elk
# engines ignore. See https://d2lang.com/tour/install.
if ! command -v d2 >/dev/null 2>&1; then
	echo "error: d2 not found in PATH — install d2 and re-run" >&2
	exit 1
fi
if ! d2 layout 2>/dev/null | grep -q '^tala '; then
	echo "error: d2 has no TALA layout engine — install a TALA-enabled d2 and re-run" >&2
	exit 1
fi

# render_d2 <basename>
#
# d2 renders PNGs at 2x the diagram's nominal pixel size, which suits
# high-DPI displays; --pad trims d2's default 100px margin.
render_d2() {
	local name=$1
	d2 --layout=tala --pad 20 "$name.d2" "$name.png"
	optimize_png "$name.png"
	echo "rendered $name.png ($(du -h "$name.png" | cut -f1))"
}

# PNG optimization; neither tool is auto-installed. pngquant first (palette
# quantization — visually lossless on flat-color diagrams, biggest win),
# then oxipng (lossless recompression of whatever the previous step left).
optimize_png() {
	local file=$1
	if command -v pngquant >/dev/null 2>&1; then
		# pngquant exits nonzero when --skip-if-larger declines to rewrite
		pngquant --force --skip-if-larger --ext .png -- "$file" || true
	else
		echo "warning: pngquant not found; skipping palette quantization" >&2
	fi
	if command -v oxipng >/dev/null 2>&1; then
		oxipng --quiet --opt 4 --strip safe "$file"
	else
		echo "warning: oxipng not found; skipping lossless recompression" >&2
	fi
}

render_d2 architecture-master
