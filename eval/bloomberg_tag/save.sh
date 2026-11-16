#!/usr/bin/env bash
# Encrypt whatever results exist (out/ never goes into git in the clear), commit
# and push. Needs BLOOMBERG_EVAL_PASSPHRASE in the environment.
set -u
label="${1:-results}"
dir=eval/bloomberg_tag
[ -d "$dir/out" ] || exit 0
tar -czf - -C "$dir" out | openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -md sha256 -salt \
  -pass env:BLOOMBERG_EVAL_PASSPHRASE -out "$dir/out.tar.gz.enc"
git config user.name "eval-bot"
git config user.email "eval-bot@users.noreply.github.com"
git add "$dir/out.tar.gz.enc"
if git diff --cached --quiet; then
  exit 0
fi
git commit -q -m "bloomberg-tag: ${label} $(date -u +'%Y-%m-%d %H:%M') UTC [skip ci]"
branch="${GITHUB_REF_NAME:-eval/model-compare}"
for i in 1 2 3 4 5; do
  if git pull -q --rebase origin "$branch" && git push -q origin "HEAD:$branch"; then
    exit 0
  fi
  git rebase --abort 2>/dev/null || true
  sleep $((i * 10))
done
echo "::warning::could not push ${label}"
