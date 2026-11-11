#!/usr/bin/env bash
# Commit and push whatever results exist so far (called after every attempt and at
# job end). Both model jobs push to the same branch, so rebase and retry on a race.
set -u
label="${1:-results}"
git config user.name "eval-bot"
git config user.email "eval-bot@users.noreply.github.com"
git add eval/model_compare/out 2>/dev/null || exit 0
if git diff --cached --quiet; then
  exit 0
fi
git commit -q -m "model-compare: ${label} $(date -u +'%Y-%m-%d %H:%M') UTC [skip ci]"
branch="${GITHUB_REF_NAME:-eval/model-compare}"
for i in 1 2 3 4 5; do
  if git pull -q --rebase origin "$branch" && git push -q origin "HEAD:$branch"; then
    exit 0
  fi
  git rebase --abort 2>/dev/null || true
  sleep $((i * 10))
done
echo "::warning::could not push ${label}"
