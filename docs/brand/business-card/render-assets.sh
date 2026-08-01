#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIST_DIR="$ROOT_DIR/dist"

if ! command -v inkscape >/dev/null 2>&1; then
  echo "Inkscape is required to render the business card assets." >&2
  exit 1
fi

mkdir -p "$DIST_DIR"

for side in ru en; do
  source_svg="$ROOT_DIR/arvectum-business-card-${side}.svg"

  inkscape "$source_svg" \
    --export-type=png \
    --export-width=1530 \
    --export-filename="$DIST_DIR/arvectum-business-card-${side}.png"

  inkscape "$source_svg" \
    --export-type=png \
    --export-width=900 \
    --export-filename="$DIST_DIR/arvectum-email-signature-${side}.png"

  inkscape "$source_svg" \
    --export-type=pdf \
    --export-filename="$DIST_DIR/arvectum-business-card-${side}.pdf"
done

printf 'Rendered business card assets to %s\n' "$DIST_DIR"
