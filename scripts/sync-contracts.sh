#!/usr/bin/env bash
# sync-contracts.sh — pin vendored GOV contracts to Aftergraph/after-graph-governance remote main.
#
# The conformance suite (tests/conftest.py) falls back to the vendored copy in
# contracts/ when after-graph-governance is not checked out side-by-side. THIS
# script is how that copy stays exact-head: it fetches each contract from GOV
# remote main via the GitHub API and refuses to update the vendored copy when
# the fetch fails or the content is not valid JSON.
#
# Usage:
#   bash scripts/sync-contracts.sh             # fetch + replace vendored copies
#   bash scripts/sync-contracts.sh --check     # verify only; exit 1 on drift/failure
#
# Exit codes: 0 = vendored copies match GOV remote main (or were updated)
#             1 = drift or unreachable GOV (no mutation performed)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST_DIR="$REPO_ROOT/contracts"
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

if ! command -v gh >/dev/null 2>&1; then
  echo "ERROR: gh CLI required (GitHub API fetch)." >&2
  exit 1
fi

GOV_REPO="Aftergraph/after-graph-governance"

# path-in-GOV -> vendored relative path
CONTRACTS=(
  "docs/contracts/frozen/kernel.budget.schema.json:frozen/kernel.budget.schema.json"
  "docs/contracts/frozen/evidence.schema.schema.json:frozen/evidence.schema.schema.json"
  "docs/contracts/frozen/identity.schema.json:frozen/identity.schema.json"
  "docs/contracts/frozen/policy.token.schema.json:frozen/policy.token.schema.json"
  "docs/contracts/frozen/cpi.schema.json:frozen/cpi.schema.json"
  "docs/contracts/frozen/proto.charter.schema.json:frozen/proto.charter.schema.json"
  "docs/contracts/frozen/shell.contracts.schema.json:frozen/shell.contracts.schema.json"
  "docs/contracts/mission-state/1.0.json:mission-state/1.0.json"
)

failed=0
for entry in "${CONTRACTS[@]}"; do
  gov_path="${entry%%:*}"
  rel="${entry#*:}"
  dest="$DEST_DIR/$rel"

  remote_sha=$(gh api "repos/$GOV_REPO/contents/$gov_path?ref=main" --jq '.sha')
  if [ -z "$remote_sha" ]; then
    echo "ERROR: could not fetch $gov_path from $GOV_REPO main." >&2
    failed=1
    continue
  fi

  tmp="$DEST_DIR/.sync-tmp.$$"
  gh api "repos/$GOV_REPO/contents/$gov_path?ref=main" --jq '.content' | tr -d '\n' | base64 -d > "$tmp"

  # Structural sanity: must be valid JSON before any mutation
  if ! jq -e . < "$tmp" >/dev/null 2>&1; then
    echo "ERROR: fetched $gov_path is not valid JSON. Not updating." >&2
    rm -f "$tmp"
    failed=1
    continue
  fi

  if [ "$CHECK_ONLY" = 1 ]; then
    if cmp -s "$tmp" "$dest"; then
      echo "OK: $rel matches GOV main ($remote_sha)"
    else
      echo "DRIFT: $rel != GOV main $remote_sha — run sync-contracts.sh to re-pin." >&2
      failed=1
    fi
    rm -f "$tmp"
    continue
  fi

  mkdir -p "$(dirname "$dest")"
  cmp -s "$tmp" "$dest" && { echo "OK: $rel up to date ($remote_sha)"; rm -f "$tmp"; continue; }
  mv "$tmp" "$dest"
  echo "Updated $rel (GOV main $remote_sha)"
done

if [ "$CHECK_ONLY" = 1 ] && [ "$failed" = 1 ]; then
  exit 1
fi
exit 0