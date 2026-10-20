#!/usr/bin/env bash
# homelab-apps PR pipeline. Migration step 5b (infra #127); proposal docs/proposals/homelab-apps 2.2 B, 5.1, 5.2, 5b.
# Runs on pull_request_target (runner host-plan, user runner, no guest SSH key). This script, scripts/ and policy/
# come from main ($BASE). The PR tree ($HEAD_DIR) is only READ: linted, validated, classified and, once
# scripts/render.py exists on main, rendered offline in bwrap without network. No PR code is executed, nothing reaches
# a guest or OpenBao app paths, and nothing is merged here: the merge broker (infra #135) re-computes the class from
# main and merges. Class C.
#
# Entry point:  scripts/ci-pr.sh            (no arguments; environment set by .gitea/workflows/pr.yml)
#   SERVER           Gitea base URL (http://192.168.70.20:3000)
#   REPO             homelab/homelab-apps
#   PR               PR number                      HEAD_SHA  PR head commit (40 hex)
#   HEAD_REF         PR branch name                 BASE_REF  PR base branch: must be "main" (else refused)
#   TITLE            PR title (WIP prefix = plan only)
#   GITEA_API_TOKEN  token with issue/PR write on THIS repo (the job token): comment, labels, review request.
#                    Clones use gitea_ci_token from OpenBao AppRole ci-plan (read on infra, standards, apps).
# Output: $OUT = /var/lib/devops/plans/apps-<HEAD_SHA>/ : changes.txt (git diff --raw -M main head), class.json,
#         report.md, comment.md, <check>.log, render/ (runner-side inputs for the broker; pruned after 14 days).
# Exit:   0 = every check green (whatever the class, WIP included); 1 = a check failed or the run was refused.
set -euo pipefail
export PATH=/usr/local/bin:/usr/bin:/bin LC_ALL=C.UTF-8
BASE=$(cd "$(dirname "$0")/.." && pwd); WORK=$(dirname "$BASE")
: "${SERVER:?}" "${REPO:?}" "${PR:?}" "${HEAD_SHA:?}" "${HEAD_REF:?}" "${TITLE?}" "${GITEA_API_TOKEN:?job token required}"
export SERVER REPO GITEA_API_TOKEN
[[ $PR =~ ^[0-9]+$ ]] && [[ $HEAD_SHA =~ ^[0-9a-f]{40}$ ]] || { echo "refused: bad PR or HEAD_SHA"; exit 1; }
# pull_request_target checks out the BASE branch and runs its scripts: only main's scripts are trusted.
[ "${BASE_REF:-}" = main ] || { echo "refused: PR base is '${BASE_REF:-?}', not main (a non-main base would run unreviewed scripts)"; exit 1; }

PLANS=/var/lib/devops/plans
export BAO_ADDR=https://192.168.70.12:8200 BAO_CACERT=/etc/openbao/ca.crt
BAO_TOKEN=$(bao write -field=token auth/approle/login role_id=@/etc/openbao/ci-plan/role_id secret_id=@/etc/openbao/ci-plan/secret_id)
export BAO_TOKEN
CLONE_TOKEN=$(bao kv get -mount=secret -field=gitea_ci_token hm1/platform/ci/plan)
bao token revoke -self >/dev/null 2>&1 || true; unset BAO_TOKEN
clone() { git clone -q --depth 1 "${SERVER%%://*}://ci:$3@${SERVER#*://}/$1.git" "$2" && git -C "$2" remote set-url origin "${SERVER%/}/$1.git"; }

find "$PLANS" -mindepth 1 -maxdepth 1 -type d -name 'apps-*' -mtime +14 -exec rm -rf {} + 2>/dev/null || true
OUT="$PLANS/apps-$HEAD_SHA"; rm -rf "$OUT"; install -d -m 0750 "$OUT" "$OUT/render"
INFRA="$OUT/infra"; STD="$OUT/standards"
clone homelab/homelab-infra "$INFRA" "$CLONE_TOKEN"
clone homelab/homelab-standards "$STD" "$CLONE_TOKEN"
unset CLONE_TOKEN
GITEA=(python3 "$INFRA/scripts/gitea.py")      # infra main's client: SERVER, REPO, GITEA_API_TOKEN

MAIN=$(git -C "$BASE" rev-parse HEAD)            # the main commit whose scripts run now = the classification base
git -C "$BASE" fetch -q origin "refs/pull/$PR/head"
[ "$(git -C "$BASE" rev-parse FETCH_HEAD)" = "$HEAD_SHA" ] || { echo "PR head moved; a newer run will handle it"; exit 0; }
HEAD_DIR="$WORK/apps-head-$PR"
git -C "$BASE" worktree remove --force "$HEAD_DIR" >/dev/null 2>&1 || rm -rf "$HEAD_DIR"
git -C "$BASE" worktree add -q -f --detach "$HEAD_DIR" "$HEAD_SHA"
trap 'git -C "$BASE" worktree remove --force "$HEAD_DIR" >/dev/null 2>&1 || true' EXIT
REPORT="$OUT/report.md"; : > "$REPORT"
note() { echo "$*" >> "$REPORT"; }
WIP=""; case "$TITLE" in WIP*) WIP=wip;; esac

# Branch behind main: update it (a new run follows on the updated head); if Gitea refuses, the check is red.
if ! git -C "$BASE" merge-base --is-ancestor "$MAIN" "$HEAD_SHA"; then
  if "${GITEA[@]}" update-branch "$PR" >/dev/null 2>&1; then
    echo "Branch was behind \`main\`; merged \`main\` into it. A new run will check the updated branch." > "$OUT/comment.md"
    "${GITEA[@]}" comment "$PR" "$OUT/comment.md"; exit 0
  fi
  echo "## ❌ Branch is behind \`main\` and could not be updated automatically: merge \`main\` into it." > "$OUT/comment.md"
  "${GITEA[@]}" labels "$PR" "${WIP}" || true; "${GITEA[@]}" comment "$PR" "$OUT/comment.md"; exit 1
fi

# What changed (input for render-needed, classify and the broker). Paths stay quoted by git if unusual (-> C).
git -C "$BASE" diff --raw -M --no-abbrev "$MAIN" "$HEAD_SHA" > "$OUT/changes.txt"

# ---- checks: main's code (or standards main) over the PR tree
fail=0
run_check() { local name=$1; shift; local log="$OUT/$name.log"
  if "$@" > "$log" 2>&1; then note "- ✅ $name"
  else fail=1; note "- ❌ **$name**"; note '```'; head -40 "$log" | tr -d '`' >> "$REPORT"; note '```'; fi; }
note "### Checks (main \`${MAIN:0:8}\`, infra \`$(git -C "$INFRA" rev-parse --short HEAD)\`, standards \`$(git -C "$STD" rev-parse --short HEAD)\`)"
run_check standards python3 "$STD/lint/naming.py" --repo apps --apps "$HEAD_DIR" --standards "$STD" --branch "$HEAD_REF" --title "$TITLE"
run_check catalog python3 "$BASE/scripts/catalog.py" --root "$HEAD_DIR" validate
run_check lint-gate python3 "$BASE/scripts/lint_gate.py" --root "$HEAD_DIR"          # allow-list: main's copy
YAMLLINT=(-d relaxed); [ -f "$BASE/.yamllint" ] && YAMLLINT=(-c "$BASE/.yamllint")   # config from main, never the PR
run_check yamllint yamllint "${YAMLLINT[@]}" "$HEAD_DIR"
GITLEAKS=(); [ -f "$BASE/.gitleaks.toml" ] && GITLEAKS=(-c "$BASE/.gitleaks.toml")
run_check gitleaks gitleaks dir "$HEAD_DIR" --no-banner --redact "${GITLEAKS[@]}"

# Deploy guard, static (proposal 2.2 A: a deployment host not yet in the infra specs is a warning, not an error).
guard_all() {
  local d rc=0 out
  shopt -s nullglob
  for d in "$HEAD_DIR"/deployments/*/*.yaml; do
    out=$(python3 "$BASE/scripts/guard.py" --infra "$INFRA" --protected "$STD/protected.yaml" --deployment "$d") && continue
    case $? in
      1) if python3 -c 'import json,sys; e=json.loads(sys.argv[1])["errors"]; sys.exit(0 if e and all(x.endswith(": not in infra specs") for x in e) else 1)' "$out"; then
           echo "WARNING ${d#"$HEAD_DIR"/}: host(s) not in infra specs yet (skipped at deploy): $out"
         else echo "ERROR ${d#"$HEAD_DIR"/}: $out"; rc=1; fi ;;
      *) echo "ERROR ${d#"$HEAD_DIR"/}: guard input error: $out"; rc=1 ;;
    esac
  done
  return $rc
}
run_check guard guard_all
grep -h '^WARNING' "$OUT/guard.log" 2>/dev/null | head -10 | sed 's/^/  - ⚠️ /' | tr -d '`' >> "$REPORT" || true

# Offline render (5b): main's scripts/render.py in bwrap, no network, PR/infra/standards trees read-only, one
# writable output dir. Until render.py exists on main, a PR that changes anything a deploy renders is red (fail closed).
RENDER_PATHS=$(cut -f2- "$OUT/changes.txt" | tr '\t' '\n' \
  | grep -E '^(catalog/|deployments/|playbooks/|environments\.yaml$|ansible\.cfg$|requirements\.yml$)' \
  | grep -Ev '^catalog/[^/]+/docs/' || true)
render() {
  bwrap --unshare-all --die-with-parent --new-session --clearenv \
    --ro-bind /usr /usr --symlink usr/bin /bin --symlink usr/sbin /sbin --symlink usr/lib /lib --symlink usr/lib64 /lib64 \
    --proc /proc --dev /dev --tmpfs /tmp \
    --ro-bind /etc/resolv.conf /etc/resolv.conf --ro-bind /etc/ssl /etc/ssl \
    --ro-bind /opt/ansible-apps /opt/ansible-apps \
    --ro-bind "$BASE" /w/main --ro-bind "$HEAD_DIR" /w/head --ro-bind "$INFRA" /w/infra --ro-bind "$STD" /w/standards \
    --bind "$OUT/render" /w/out --chdir /w \
    --setenv PATH /opt/ansible-apps/venv/bin:/usr/bin:/bin --setenv HOME /tmp --setenv LANG C.UTF-8 \
    --setenv ANSIBLE_COLLECTIONS_PATH /opt/ansible-apps/collections --setenv ANSIBLE_LOCAL_TEMP /tmp/ansible \
    /opt/ansible-apps/venv/bin/python3 -I /w/main/scripts/render.py \
      --base /w/main --head /w/head --infra /w/infra --protected /w/standards/protected.yaml --out /w/out
}
if [ -f "$BASE/scripts/render.py" ]; then
  run_check render render
  [ -s "$OUT/render/diff.md" ] && { note ""; note "<details><summary><b>Render diff</b></summary>"; note ""; head -c 30000 "$OUT/render/diff.md" >> "$REPORT"; note ""; note "</details>"; }
elif [ -n "$RENDER_PATHS" ]; then
  fail=1; note "- ❌ **render**: this PR changes deployable content but main has no \`scripts/render.py\` yet (fail closed):"
  echo "$RENDER_PATHS" | head -10 | sed 's/^/  - /' >> "$REPORT"
else
  note "- ➖ render: nothing deployable changed"
fi

# ---- classify (main's policy, base = main checkout, head = PR tree) + gate. Labels are informational only:
# the broker never reads them. A red run removes stale class labels.
if [ $fail -ne 0 ]; then
  "${GITEA[@]}" labels "$PR" "$WIP" || true
  { echo "## ❌ Checks failed"; echo; cat "$REPORT"; } > "$OUT/comment.md"
  "${GITEA[@]}" comment "$PR" "$OUT/comment.md"; exit 1
fi
python3 "$BASE/policy/classify.py" --base "$BASE" --head "$HEAD_DIR" --changes "$OUT/changes.txt" > "$OUT/class.json"
CLASS=$(python3 -c 'import json,sys; c=json.load(open(sys.argv[1]))["class"]; print(c if c in ("A","B","C") else "C")' "$OUT/class.json" || echo C)
"${GITEA[@]}" labels "$PR" "class/$CLASS${WIP:+,wip}"
case "$CLASS$WIP" in
  A) GATE="Class A — the merge broker merges it once this check is green." ;;
  B) GATE="Class B — needs approval from the Reviewer (\`oc-rev\`); the merge broker merges after it."
     "${GITEA[@]}" review-request "$PR" oc-rev || true ;;
  C) GATE="Class C — needs Faris's approval; the merge broker merges after it."
     "${GITEA[@]}" review-request "$PR" faris || true ;;
  *wip) GATE="WIP — checked only; remove \`WIP\` from the title to proceed." ;;
esac
python3 - "$OUT/class.json" "$REPORT" "$GATE" > "$OUT/comment.md" <<'PY'
import json, sys
c = json.load(open(sys.argv[1]))
print(f"## Class {c['class']}\n\n{sys.argv[3]}\n")
rs = c.get("reasons") or []
if rs:
    print("| Path | Class | Why |\n|---|---|---|")
    for r in rs[:25]:
        print(f"| `{r.get('path') or '-'}` | {r.get('class')} | {str(r.get('why', '')).replace('|', '/')} |")
    if len(rs) > 25:
        print(f"\n... and {len(rs) - 25} more (runner: class.json)")
else:
    print("No changes against main.")
print()
print(open(sys.argv[2]).read())
PY
"${GITEA[@]}" comment "$PR" "$OUT/comment.md"
