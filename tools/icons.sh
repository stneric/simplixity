#!/usr/bin/env bash
# Regenerate the PWA raster icons from web/assets/icon.svg.
#
# Checked-in output, not a build step: the app ships as static files with no
# bundler, and adding a toolchain for four PNGs that change once a year would be
# the only build in the repo. Run this after editing the master SVG.
#
# Needs rsvg-convert (brew install librsvg).
set -euo pipefail

cd "$(dirname "$0")/.."
src="web/assets/icon.svg"
out="web/assets/icons"
mkdir -p "$out"

render() { rsvg-convert -w "$1" -h "$1" "$src" -o "$2"; echo "  $2 (${1}px)"; }

echo "rendering from $src"
render 192 "$out/icon-192.png"
render 512 "$out/icon-512.png"
# Same art: the glyph sits inside the maskable safe zone (the middle 80%), so a
# separately padded version would only shrink it for no reason.
render 512 "$out/icon-maskable-512.png"
# iOS ignores the manifest icons for the home screen and applies its own mask,
# so this one is opaque and square, never pre-rounded.
render 180 "$out/apple-touch-icon.png"

# The filenames never change, so rendering alone changes nothing a browser will
# look at. The favicon carries a ?v= and updates in the tab, but the home screen
# keeps showing the old icon unless apple-touch-icon.png and the manifest's icons
# carry the new version too.
cat <<'NOTE'

Now bump ?v= on every icon URL, together, or none of this is picked up:
  web/index.html · web/manifest.webmanifest
An icon already sitting on a home screen is never refetched, so re-add the app
to check it.
NOTE
