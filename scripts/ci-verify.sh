#!/usr/bin/env bash
# homelab-apps L4 verification (+ app drift). Migration step 5b (infra #127); proposal 2.3, 2.5, 2.6, 4.2, 5.1, 5b.
# Runs as runner-apps on runner host-apps, ONLY on main: nightly 03:15 (from cutover, step 10), on dispatch, and from
# ci-deploy.sh after each apply wave. L4 runs exactly like a deploy play, through the run-dir contract and the root
# wrapper as runner-apps-play (never as runner-apps, which can read the AppRole files): this script calls
# ci-deploy.sh with APPS_MODE=verify, which plans the deployments, takes the host locks (unless the calling deploy
# holds them), prepares one run dir per deployment (no child token, no snapshot) and calls
# `sudo /usr/local/sbin/apps-play-run <run-id>`; the wrapper runs playbooks/verify.yml read-only.
# Per host: issue "app-verify: <app>/<host>" (label verify) is opened/commented on failure and closed when green.
# Prerequisite (infra host/apps-runner, Class C, not in this PR): the wrapper accepts APPS_MODE=verify and then runs
# playbooks/verify.yml (no --check). Until then it refuses (exit 2, "env key APPS_MODE not allowed") = fail closed.
# Class C.
#
# Entry point:  scripts/ci-verify.sh        (no arguments; environment set by .gitea/workflows/verify.yml)
#   SERVER, REPO      Gitea base URL, homelab/homelab-apps
#   GITHUB_REF        must be refs/heads/main          SHA   the main commit checked out in $BASE
#   EVENT             schedule | workflow_dispatch | deploy (deploy = called by ci-deploy.sh between waves)
#   HOSTS             optional csv of hostnames (dispatch input / the wave's hosts); empty = every deployment host
#   APPS              optional csv of catalogue entry names
#   APPS_LOCKS_HELD   1 only when called by ci-deploy.sh, which already holds these hosts' locks
#   APPS_TIMEOUT      s per verify play (default 1800)
# Exit: 0 = every selected host green (or nothing to verify); 1 = a host failed, refused or lock timeout.
set -euo pipefail
export PATH=/usr/local/bin:/usr/bin:/bin LC_ALL=C.UTF-8
BASE=$(cd "$(dirname "$0")/.." && pwd)
: "${SERVER:?}" "${REPO:?}" "${SHA:?}" "${EVENT:?}"
case "$EVENT" in
  schedule|workflow_dispatch) export APPS_LOCKS_HELD=0 ;;        # only the deploy run may skip lock_hosts
  deploy) [ "${APPS_LOCKS_HELD:-0}" = 1 ] || { echo "ci-verify: refused: EVENT=deploy without held locks" >&2; exit 1; } ;;
  *) echo "ci-verify: refused: event '$EVENT'" >&2; exit 1 ;;
esac
export APPS_MODE=verify EVENT=workflow_dispatch INFRA_PR= BEFORE= HOSTS="${HOSTS:-}" APPS="${APPS:-}"
export APPS_TIMEOUT="${APPS_TIMEOUT:-1800}" LOCK_WORKFLOW=verify
exec bash "$BASE/scripts/ci-deploy.sh"
