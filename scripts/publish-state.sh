#!/usr/bin/env bash
set -euo pipefail

snapshot=${1:?Informe o snapshot}
branch=refs/heads/state
remote=$(git ls-remote origin "$branch")
previous=${remote%%$'\t'*}
blob=$(git hash-object -w "$snapshot")
tree=$(printf '100644 blob %s\tsnapshot.json\n' "$blob" | git mktree)

if [[ -n "$previous" ]]; then
  git fetch --no-tags origin "$previous"
  if [[ "$(git rev-parse "$previous^{tree}")" == "$tree" ]]; then
    exit 0
  fi
fi

commit=$(git -c user.name='github-actions[bot]' \
  -c user.email='41898282+github-actions[bot]@users.noreply.github.com' \
  commit-tree "$tree" -m 'Atualizar estado dos backups')
git push --force-with-lease="$branch:$previous" origin "$commit:$branch"
