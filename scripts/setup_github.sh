#!/usr/bin/env bash
# One-time GitHub setup. Needs the GitHub CLI (gh) logged in as the repo owner.
# Usage: bash scripts/setup_github.sh <your-github-username> [repo-name]
# Safe to re-run: each step skips itself if already done.
set -euo pipefail
OWNER="$1"; REPO="${2:-forge}"

if grep -q OWNER_GITHUB_USERNAME CODEOWNERS; then
  sed -i.bak "s/OWNER_GITHUB_USERNAME/$OWNER/g" CODEOWNERS && rm -f CODEOWNERS.bak
  git add CODEOWNERS && git commit -m "Set CODEOWNERS owner" || true
fi

if ! git remote get-url origin >/dev/null 2>&1; then
  if gh repo view "$OWNER/$REPO" >/dev/null 2>&1; then
    git remote add origin "https://github.com/$OWNER/$REPO.git"
    git push -u origin main
  else
    gh repo create "$OWNER/$REPO" --private --source=. --remote=origin --push
  fi
else
  git push -u origin main || true
fi

gh variable set FORGE_OWNER --body "$OWNER" --repo "$OWNER/$REPO"
gh label create human-approved --color 0E8A16 --description "Owner approved a protected-path change" --repo "$OWNER/$REPO" 2>/dev/null || true

# Branch protection: nothing merges to main unless core-checks pass, admins included.
set +e
OUT=$(gh api -X PUT "repos/$OWNER/$REPO/branches/main/protection" --input - 2>&1 <<JSON
{
  "required_status_checks": {"strict": true, "contexts": ["core"]},
  "enforce_admins": true,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON
)
RC=$?
set -e
if [ $RC -ne 0 ]; then
  if echo "$OUT" | grep -qi "upgrade to github pro\|make this repository public"; then
    echo "BRANCH PROTECTION NOT ON: GitHub only protects branches of PRIVATE repos on a paid plan."
    echo "Choose one: upgrade to GitHub Pro (github.com/settings/billing), or make the repo public."
    echo "Then re-run: bash scripts/setup_github.sh $OWNER $REPO"
    exit 3
  fi
  echo "$OUT"; exit $RC
fi
echo "Done: $OWNER/$REPO is private, protected, and running core-checks."
