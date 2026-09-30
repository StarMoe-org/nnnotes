#!/usr/bin/env bash
# A built ournotes-player checkout ($PLAYER_REPOSITORY at $PLAYER_REF) in $1: nnnotes writes its page files into the site.
set -euo pipefail
dir="$1"
if [ ! -f "$dir/dist/ournotes-player.element.min.js" ] || [ "$(git -C "$dir" rev-parse HEAD 2>/dev/null)" != "$(git -C "$dir" rev-parse "$PLAYER_REF^{commit}" 2>/dev/null)" ]; then
  rm -rf "$dir"
  git clone --quiet --filter=blob:none "https://github.com/$PLAYER_REPOSITORY.git" "$dir"
  git -C "$dir" checkout --quiet "$PLAYER_REF"
  (cd "$dir" && npm ci --ignore-scripts --no-audit --no-fund && npm run build)
fi
for file in index.html songs.js catalog.js text.js replay-panel.js replay-worker.js replay-preset.js; do
  if [ ! -f "$dir/examples/songs/$file" ]; then
    echo "player revision lacks examples/songs/$file: set STORY_PLAYER_REF to the current replay-enabled player commit" >&2
    exit 1
  fi
done
echo "ournotes-player $(git -C "$dir" rev-parse --short HEAD) ($(node -p "require('$dir/package.json').version"))"
