#!/usr/bin/env bash
# homelab-apps deploy. Migration step 5b (infra #127); proposal 2.1, 2.3, 2.5, 5.1, 5b; run-dir contract in infra
# host/apps-runner/README.md. Runs as runner-apps on runner host-apps, ONLY on main (push to main or dispatch).
# This script (main's code) logs in to OpenBao (AppRole ci-apps), clones infra + standards main read-only, plans the
# deployments, takes ALL host locks in one lock_hosts call (held for the whole run), and per wave: pre-deploy
# snapshots (apply only), then once per app play: run dir /run/devops/apps/<run-id>/ (2750 setgid, group
# runner-apps-play, umask 027), src/ copies without .git, inventory.yml, hosts, env, a child token minted with an
# explicit -ttl (apply only; check mode writes an empty token file), an ssh-agent holding the ci-apps key, the deploy
# guard, then `sudo /usr/local/sbin/apps-play-run <run-id>` (exit 2 = refused, stderr names the file). Class C.
#
# Entry point:  scripts/ci-deploy.sh        (no arguments; environment set by .gitea/workflows/deploy.yml)
#   SERVER, REPO           Gitea base URL, homelab/homelab-apps
#   GITHUB_REF             must be refs/heads/main (anything else is refused)
#   SHA                    the main commit checked out in $BASE (must be on origin/main)
#   EVENT                  push | workflow_dispatch
#   BEFORE                 push only: previous main sha (change detection; missing/zero = every deployment)
#   HOSTS                  dispatch only, optional: csv of hostnames (intersected with deployments and infra specs)
#   APPS                   dispatch only, optional: csv of catalogue entry names
#   INFRA_PR               dispatch only, optional: infra PR number; the report is commented there, and its hosts'
#                          SSH host keys are re-learned (the infra apply created/replaced them)
#   APPS_MODE              check (default) | apply. Set literally in deploy.yml, never from dispatch inputs.
#   APPS_TIMEOUT           s per app play, 60..14400 (default 3600)    APPS_TOKEN_TTL  child token TTL s (default 1800)
#   WAVE_DELAY             s between waves in apply mode (default 3600; 0 in check mode)
#   APPS_SSH_KEY           private key of ci-apps (default ~/.ssh/ci-apps)
# Exit: 0 = every selected play ok (or nothing to deploy); 1 = refused, lock timeout, guard refusal or a failed play.
set -euo pipefail
export PATH=/usr/local/bin:/usr/bin:/bin LC_ALL=C.UTF-8
BASE=$(cd "$(dirname "$0")/.." && pwd)
: "${SERVER:?}" "${REPO:?}" "${SHA:?}" "${EVENT:?}"
APPS_MODE=${APPS_MODE:-check}; APPS_TIMEOUT=${APPS_TIMEOUT:-3600}; APPS_TOKEN_TTL=${APPS_TOKEN_TTL:-1800}
HOSTS=${HOSTS:-}; APPS=${APPS:-}; INFRA_PR=${INFRA_PR:-}; BEFORE=${BEFORE:-}
APPS_SSH_KEY=${APPS_SSH_KEY:-$HOME/.ssh/ci-apps}
refuse() { echo "ci-deploy: refused: $*" >&2; exit 1; }
[ "${GITHUB_REF:-}" = refs/heads/main ] || refuse "ref '${GITHUB_REF:-?}' is not refs/heads/main"
[[ $SHA =~ ^[0-9a-f]{40}$ ]] || refuse "bad SHA"
[[ $EVENT =~ ^(push|workflow_dispatch)$ ]] || refuse "event '$EVENT'"
[[ $APPS_MODE =~ ^(check|apply)$ ]] || refuse "APPS_MODE '$APPS_MODE'"
[[ $HOSTS =~ ^[a-z0-9,-]*$ ]] && [[ $APPS =~ ^[a-z0-9,_-]*$ ]] && [[ $INFRA_PR =~ ^[0-9]*$ ]] && [[ $BEFORE =~ ^([0-9a-f]{40})?$ ]] || refuse "bad dispatch input"
[[ $APPS_TIMEOUT =~ ^[0-9]{2,5}$ ]] && [ "$APPS_TIMEOUT" -ge 60 ] && [ "$APPS_TIMEOUT" -le 14400 ] || refuse "APPS_TIMEOUT 60..14400"
[[ $APPS_TOKEN_TTL =~ ^[0-9]{2,5}$ ]] || refuse "APPS_TOKEN_TTL"
if [ "$APPS_MODE" = apply ]; then WAVE_DELAY=${WAVE_DELAY:-3600}; else WAVE_DELAY=0; fi
[[ $WAVE_DELAY =~ ^[0-9]+$ ]] || refuse "WAVE_DELAY"
[ "$(git -C "$BASE" rev-parse HEAD)" = "$SHA" ] || refuse "checkout is not $SHA"
git -C "$BASE" fetch -q origin main
git -C "$BASE" merge-base --is-ancestor "$SHA" FETCH_HEAD || refuse "$SHA is not on main"

# ---- OpenBao (AppRole ci-apps): parent token kept for minting child tokens; never exported to a play.
export BAO_ADDR=https://192.168.70.12:8200 BAO_CACERT=/etc/ssl/devops/openbao-ca.crt
PARENT=$(bao write -field=token auth/approle/login role_id=@/etc/openbao/ci-apps/role_id secret_id=@/etc/openbao/ci-apps/secret_id)
export GITEA_API_TOKEN; GITEA_API_TOKEN=$(BAO_TOKEN=$PARENT bao kv get -mount=secret -field=gitea_apps_token hm1/platform/ci/apps)
PVE_TOKEN=$(BAO_TOKEN=$PARENT bao kv get -mount=secret -field=pve_token_apps hm1/platform/ci/apps)
W=$(mktemp -d); REPORT="$W/deploy.md"; : > "$REPORT"
note() { echo "$*" >> "$REPORT"; }
RD=""; AGENT_PID=""; CHILD=""
end_play() {   # kill this play's agent (by recorded pid, after checking it is ours), revoke its child token, remove run dir
  if [ -n "$AGENT_PID" ] && [ "$(ps -o user= -p "$AGENT_PID" 2>/dev/null | tr -d ' ')" = "$(id -un)" ] \
     && [ "$(ps -o comm= -p "$AGENT_PID" 2>/dev/null)" = ssh-agent ]; then kill "$AGENT_PID" 2>/dev/null || true; fi
  [ -n "$CHILD" ] && { BAO_TOKEN=$CHILD bao token revoke -self >/dev/null 2>&1 || true; }
  [ -n "$RD" ] && [ -d "$RD" ] && [ ! -L "$RD" ] && rm -rf --one-file-system "$RD"
  RD=""; AGENT_PID=""; CHILD=""; }
cleanup() { end_play; declare -F unlock_hosts >/dev/null && unlock_hosts
  BAO_TOKEN=$PARENT bao token revoke -self >/dev/null 2>&1 || true; rm -rf "$W"; }
trap cleanup EXIT

clone() { git clone -q --depth 1 "${SERVER%%://*}://ci:$3@${SERVER#*://}/$1.git" "$2" && git -C "$2" remote set-url origin "${SERVER%/}/$1.git"; }
INFRA="$W/infra"; STD="$W/standards"
clone homelab/homelab-infra "$INFRA" "$GITEA_API_TOKEN" || refuse "infra clone failed (fail closed)"
clone homelab/homelab-standards "$STD" "$GITEA_API_TOKEN" || refuse "standards clone failed: protected.yaml unavailable (fail closed)"
source "$INFRA/scripts/ci-lib.sh"                 # lock_hosts / unlock_hosts, gitea.py (infra main, read-only)
export BAO_CACERT=/etc/ssl/devops/openbao-ca.crt LC_ALL=C.UTF-8
LOCK_WORKFLOW=deploy
INFRA_SHA=$(git -C "$INFRA" rev-parse HEAD)
note "## homelab-apps deploy ($APPS_MODE) \`${SHA:0:8}\` (infra \`${INFRA_SHA:0:8}\`, $EVENT)"

# ---- plan: which deployments, which hosts, in which wave
if [ "$EVENT" = push ]; then
  if [ -n "$BEFORE" ] && [ "$BEFORE" != 0000000000000000000000000000000000000000 ] && git -C "$BASE" cat-file -e "$BEFORE^{commit}" 2>/dev/null; then
    git -C "$BASE" diff --name-only --no-renames "$BEFORE" "$SHA" > "$W/changed.txt"
  else echo "*ALL*" > "$W/changed.txt"; fi
else : > "$W/changed.txt"; fi
python3 - "$BASE" "$INFRA" "$EVENT" "$W/changed.txt" "$HOSTS" "$APPS" > "$W/plan.json" <<'PY'
import json, os, re, sys
base, infra, event, changed_file, hosts_in, apps_in = sys.argv[1:7]
sys.path.insert(0, os.path.join(base, "scripts")); import catalog                # noqa: E402
sys.path.insert(0, os.path.join(infra, "scripts")); from specs import load_specs  # noqa: E402
cat = catalog.load(base)
specs = {s.get("hostname"): s for s in load_specs(infra)}
out = {"waves": [], "notes": [], "errors": list(cat.errors)}
deps = cat.deployments
if event == "push":
    files = [l.strip() for l in open(changed_file) if l.strip()]
    allp, apps, keys = False, set(), set()
    for f in files:
        if f == "*ALL*" or f.startswith("playbooks/") or f in ("environments.yaml", "ansible.cfg", "requirements.yml"):
            allp = True
        elif re.match(r"^catalog/[^/]+/docs/", f):
            continue
        elif f.startswith("catalog/"):
            apps.add(f.split("/")[1])
        elif re.match(r"^deployments/[^/]+/[^/]+\.yaml$", f):
            keys.add(f)
            if not os.path.exists(os.path.join(base, f)):
                out["notes"].append(f"{f} removed: nothing is undeployed (remove flow 2.2 D)")
    deps = [d for d in deps if allp or d["app"] in apps or d["_path"] in keys]
else:
    want_h = {h for h in hosts_in.split(",") if h}
    want_a = {a for a in apps_in.split(",") if a}
    for a in sorted(want_a - set(cat.apps)):
        out["notes"].append(f"APPS: unknown entry '{a}' ignored")
    for h in sorted(want_h - {h for d in deps for h in d["hosts"]}):
        out["notes"].append(f"HOSTS: {h} is in no deployment: ignored")
    deps = [d for d in deps if (not want_a or d["app"] in want_a) and (not want_h or want_h & set(d["hosts"]))]
    for d in deps:
        if want_h:
            d["hosts"] = [h for h in d["hosts"] if h in want_h]
waves = {}
for d in deps:
    hs = []
    for h in d["hosts"]:
        if h in specs:
            hs.append(h)
        else:
            out["notes"].append(f"{d['_path']}: {h} not in infra specs yet: skipped")
    if not hs:
        continue
    env = (cat.environments or {}).get(d["zone"]) or {}
    w = d.get("wave", env.get("wave"))
    if isinstance(w, bool) or not isinstance(w, int) or w < 1 or (d["zone"] == "stg") != (w == 1) \
            or (isinstance(env.get("wave"), int) and w < env["wave"]):
        out["errors"].append(f"{d['_path']}: invalid wave {w!r} (stg = 1, others >= 2 and >= environments.yaml)")
        continue
    waves.setdefault(w, []).append({"app": d["app"], "zone": d["zone"], "path": d["_path"], "hosts": sorted(hs),
                                    "auto_rollback": bool(env.get("auto_rollback", False))})
out["waves"] = [{"wave": w, "deployments": sorted(waves[w], key=lambda x: x["path"])} for w in sorted(waves)]
print(json.dumps(out))
PY
pj() { python3 -c "import json,sys; d=json.load(open('$W/plan.json')); $1"; }
pj 'print("\n".join(d["notes"]))' | sed '/^$/d' | sed 's/^/- ⚠️ /' >> "$REPORT"
if [ -n "$(pj 'print("\n".join(d["errors"]))')" ]; then
  note "- ❌ **Plan refused** (catalogue errors on main):"; pj 'print("\n".join(d["errors"]))' | sed 's/^/  - /' >> "$REPORT"
  cat "$REPORT"; exit 1
fi
ALL_HOSTS=$(pj 'print(",".join(sorted({h for w in d["waves"] for x in w["deployments"] for h in x["hosts"]})))')
if [ -z "$ALL_HOSTS" ]; then note "- Nothing to deploy."; cat "$REPORT"; exit 0; fi

# ---- host locks: every host of the run, one call, held until exit (proposal 2.3)
if lock_hosts "$ALL_HOSTS"; then
  note "- 🔐 Host locks: $(locked_hosts)"; for n in "${LOCK_NOTES[@]}"; do note "  - ⚠️ $n"; done
else
  note "- ❌ **Host locks** not acquired; nothing deployed (issue \`pipeline: lock wait timeout <host>\` names the holder)"
  for n in "${LOCK_NOTES[@]}"; do note "  - $n"; done; cat "$REPORT"; exit 1
fi

# ---- SSH host keys: runner-apps keeps its own known_hosts (TOFU once per IP via ssh-keyscan); hosts of a
# dispatching infra PR are re-learned (the apply created/replaced them). The play gets only its hosts' lines.
KH="$HOME/.ssh/known_hosts_apps"; install -d -m 0700 "$HOME/.ssh"; [ -f "$KH" ] || install -m 0600 /dev/null "$KH"
ip_of() { python3 -c 'import os,sys; sys.path.insert(0, os.path.join(sys.argv[1], "scripts")); from specs import load_specs; print(next(str(s["ip"]).split("/")[0] for s in load_specs(sys.argv[1]) if s.get("hostname") == sys.argv[2]))' "$INFRA" "$1"; }
for h in ${ALL_HOSTS//,/ }; do
  ip=$(ip_of "$h")
  if [ -n "$INFRA_PR" ] && [[ ",$HOSTS," == *",$h,"* ]]; then ssh-keygen -R "$ip" -f "$KH" >/dev/null 2>&1 || true; fi
  if ! ssh-keygen -F "$ip" -f "$KH" >/dev/null 2>&1; then
    if ssh-keyscan -T 10 -t ed25519 "$ip" 2>/dev/null | grep -v '^#' >> "$KH"; then note "- 🔑 learned SSH host key of $h ($ip)"
    else note "- ⚠️ could not scan the SSH host key of $h ($ip)"; fi
  fi
done

# ---- one app play (one deployment, its selected hosts)
FAILED=0
run_play() {   # wave app zone path hosts_csv
  local wave=$1 app=$2 zone=$3 path=$4 hosts=$5 rc=0 h ip rid ttl out
  rid="$(date +%s)$$$((RANDOM % 1000))"; RD="/run/devops/apps/$rid"
  install -d -m 2750 -g runner-apps-play "$RD"
  umask 027
  # -m 2750 keeps setgid: a plain install -d chmods to 0755, which clears the inherited setgid bit, so src/apps and
  # src/infra got runner-apps's own group and apps-play-run refused the run dir (deploy run 593, first real deployments).
  install -d -m 2750 "$RD/src" "$RD/src/apps" "$RD/src/infra"
  git -C "$BASE" archive --format=tar "$SHA" | tar -x -C "$RD/src/apps"     # no .git; a symlink makes the wrapper refuse
  git -C "$INFRA" archive --format=tar HEAD | tar -x -C "$RD/src/infra"
  tr ',' '\n' <<<"$hosts" > "$RD/hosts"
  : > "$RD/known_hosts"
  for h in ${hosts//,/ }; do ip=$(ip_of "$h"); ssh-keygen -F "$ip" -f "$KH" 2>/dev/null | grep -v '^#' >> "$RD/known_hosts" || true; done
  if ! python3 - "$RD" "$INFRA" "$BASE" "$path" "$hosts" "$SHA" "$INFRA_SHA" > "$W/inv.err" 2>&1 <<'PY'
import os, re, sys, yaml
rd, infra, base, path, hosts, sha, infra_sha = sys.argv[1:8]
sys.path.insert(0, os.path.join(infra, "scripts")); from specs import load_specs  # noqa: E402
specs = {s.get("hostname"): s for s in load_specs(infra)}
dep = yaml.safe_load(open(os.path.join(base, path), encoding="utf-8")) or {}
_, zone, fn = path.split("/"); app = fn[:-5]
hosts = hosts.split(",")
# 1.7: deployment vars -> group vars of deployment_<zone>_<app>; host_vars -> host vars, refused on inventory keys
BANNED = re.compile(r"^(zone|vmid|role|type|os|keyctl|hostvars|group_names|groups|omit|environment|playbook_dir|role_path"
                    r"|inventory_.*|ansible_.*|bao_.*|delegate_.*|deploy_.*|apps_.*)$")
def clean(where, d):
    if not isinstance(d, dict):
        sys.exit(f"{where}: not a mapping")
    for k in d:
        if not isinstance(k, str) or BANNED.match(k):
            sys.exit(f"{where}: banned key {k!r}")
    def walk(v):
        if isinstance(v, dict):
            for x in v.values(): walk(x)
        elif isinstance(v, list):
            for x in v: walk(x)
        elif isinstance(v, str) and (any(m in v for m in ("{{", "{%", "{#", "__")) or v == "mro"):
            sys.exit(f"{where}: value {v[:40]!r} has Jinja, '__' or 'mro'")
    walk(d)
    return d
hv = {}
for h in hosts:
    s = specs[h]
    hv[h] = {"ansible_host": str(s["ip"]).split("/")[0], "vmid": s["vmid"], "zone": s["_zone"], "role": s["role"],
             "keyctl": bool(s.get("keyctl", False)), "type": s.get("type", "lxc"), "os": s["os"]}
gvars = clean("vars", dep.get("vars") or {})
hvars = dep.get("host_vars") or {}
if not isinstance(hvars, dict):
    sys.exit("host_vars: not a mapping")
for h, v in hvars.items():
    if h not in (dep.get("hosts") or []):
        sys.exit(f"host_vars: {h} is not a host of this deployment")
    if h in hv:
        hv[h].update(clean(f"host_vars.{h}", v or {}))
grp = "deployment_" + re.sub(r"[^a-z0-9_]", "_", f"{zone}_{app}")
children = {grp: {"hosts": hv, "vars": gvars}}
for h in hosts:
    for g in (f"role_{hv[h]['role']}", f"zone_{zone}"):
        children.setdefault(g, {"hosts": {}})["hosts"][h] = None
kh = os.path.join(rd, "known_hosts")
inv = {"all": {"vars": {
    "ansible_ssh_common_args": f"-o UserKnownHostsFile={kh} -o GlobalKnownHostsFile=/dev/null -o StrictHostKeyChecking=yes",
    "apps_deployment": path, "apps_deployment_zone": zone, "apps_deployment_app": app,
    "apps_sha": sha, "apps_infra_sha": infra_sha}, "children": children}}
with open(os.path.join(rd, "inventory.yml"), "w", encoding="utf-8") as f:
    yaml.safe_dump(inv, f, sort_keys=True)
PY
  then note "  - ❌ $path: inventory refused: $(tail -1 "$W/inv.err" | tr -d '`')"; end_play; return 1; fi
  # deploy guard (main's scripts/guard.py) on the exact inventory values, before anything runs
  local gargs=(); for h in ${hosts//,/ }; do gargs+=(--host "$h=$(ip_of "$h")"); done
  if ! out=$(python3 "$BASE/scripts/guard.py" --infra "$RD/src/infra" --protected "$STD/protected.yaml" \
             --deployment "$RD/src/apps/$path" "${gargs[@]}" 2>&1); then
    note "  - ❌ $path: deploy guard refused"; note '```'; echo "$out" | tr -d '`' >> "$REPORT"; note '```'; end_play; return 1
  fi
  # child token: this app only, explicit TTL (<= token role max); check mode needs none (bao_create_secret is a no-op)
  if [ "$APPS_MODE" = apply ]; then
    ttl=$(( APPS_TOKEN_TTL < APPS_TIMEOUT ? APPS_TOKEN_TTL : APPS_TIMEOUT ))
    if ! CHILD=$(BAO_TOKEN=$PARENT bao token create -role="apps-$app" -ttl="${ttl}s" -field=token 2> "$W/token.err"); then
      CHILD=""; note "  - ❌ $path: child token apps-$app not minted: $(tail -1 "$W/token.err" | tr -d '`')"; end_play; return 1
    fi
  fi
  printf '%s' "$CHILD" > "$RD/token"
  printf 'APPS_APP=%s\nAPPS_MODE=%s\nAPPS_TIMEOUT=%s\nAPPS_WAVE=%s\nBAO_ZONE_MODE=namespace\nANSIBLE_FORCE_COLOR=0\nANSIBLE_STDOUT_CALLBACK=default\n' \
    "$app" "$APPS_MODE" "$APPS_TIMEOUT" "$wave" > "$RD/env"
  # ssh-agent for this play only; -t = play timeout, so a missed cleanup still expires the key
  out=$(ssh-agent -s -a "$RD/agent.sock" -t "$APPS_TIMEOUT") || { note "  - ❌ $path: ssh-agent failed"; end_play; return 1; }
  AGENT_PID=$(sed -n 's/^SSH_AGENT_PID=\([0-9]*\);.*/\1/p' <<<"$out"); echo "$AGENT_PID" > "$RD/agent.pid"
  chmod 0660 "$RD/agent.sock"
  SSH_AUTH_SOCK="$RD/agent.sock" ssh-add -q "$APPS_SSH_KEY" 2> "$W/agent.err" || { note "  - ❌ $path: ci-apps key not loaded"; end_play; return 1; }
  sudo -n /usr/local/sbin/apps-play-run "$rid" > "$W/play-$rid.log" 2>&1 || rc=$?
  case $rc in
    0) note "  - ✅ $path ($hosts): $(grep -E '^[a-z0-9-]+ +: ok=' "$W/play-$rid.log" | sed 's/  */ /g' | paste -sd';' - | tr -d '`')" ;;
    2) note "  - ❌ $path: **apps-play-run refused** the run dir"; note '```'; grep 'refused' "$W/play-$rid.log" | tail -3 | tr -d '`' >> "$REPORT"; note '```' ;;
    *) note "  - ❌ $path ($hosts): play failed rc=$rc"; note '```'; tail -40 "$W/play-$rid.log" | tr -d '`' >> "$REPORT"; note '```' ;;
  esac
  cat "$W/play-$rid.log"
  end_play
  return $(( rc == 0 ? 0 : 1 ))
}

# ---- waves (2.5): in order; a failed wave stops the run; apply mode waits WAVE_DELAY between waves
NW=$(pj 'print(len(d["waves"]))')
for ((i = 0; i < NW; i++)); do
  WAVE=$(pj "print(d['waves'][$i]['wave'])")
  WHOSTS=$(pj "print(','.join(sorted({h for x in d['waves'][$i]['deployments'] for h in x['hosts']})))")
  note ""; note "### Wave $WAVE"
  SNAP=""
  if [ "$APPS_MODE" = apply ]; then
    SNAP="pre-deploy_$(date +%Y%m%d_%H%M)"
    if ! PROXMOX_VE_API_TOKEN="$PVE_TOKEN" INFRA_ROOT="$INFRA" python3 "$INFRA/scripts/snapshot.py" take --name "$SNAP" --hosts "$WHOSTS" > "$W/snap.txt" 2>&1; then
      note "- ❌ **Snapshot \`$SNAP\`** failed; wave not deployed (fail closed)"; note '```'; tr -d '`' < "$W/snap.txt" >> "$REPORT"; note '```'
      FAILED=1; break
    fi
    note "- 📸 Snapshot \`$SNAP\`: $WHOSTS"
  else
    note "- check mode: no snapshot, no child token, no changes expected"
  fi
  while IFS=$'\t' read -r app zone path hosts rb; do
    if ! run_play "$WAVE" "$app" "$zone" "$path" "$hosts"; then
      FAILED=1
      if [ "$APPS_MODE" = apply ] && [ "$rb" = True ] && [ -n "$SNAP" ]; then   # environments.yaml auto_rollback (stg)
        PROXMOX_VE_API_TOKEN="$PVE_TOKEN" INFRA_ROOT="$INFRA" python3 "$INFRA/scripts/snapshot.py" rollback --name "$SNAP" --hosts "$hosts" > "$W/rb.txt" 2>&1 || true
        note "  - ↩️ auto-rollback to \`$SNAP\`:"; note '```'; tr -d '`' < "$W/rb.txt" >> "$REPORT"; note '```'
      fi
    fi
  done < <(pj "[print('\t'.join([x['app'], x['zone'], x['path'], ','.join(x['hosts']), str(x['auto_rollback'])])) for x in d['waves'][$i]['deployments']]")
  [ $FAILED -eq 0 ] || { note "- ⛔ wave $WAVE failed: later waves not started"; break; }
  if [ "$APPS_MODE" = apply ]; then   # L4 of this wave before the next one (locks are held by this run)
    if ! APPS_LOCKS_HELD=1 HOSTS="$WHOSTS" bash "$BASE/scripts/ci-verify.sh" > "$W/l4.txt" 2>&1; then
      note "- ❌ L4 after wave $WAVE failed: later waves not started"; note '```'; tail -30 "$W/l4.txt" | tr -d '`' >> "$REPORT"; note '```'
      FAILED=1; break
    fi
    note "- ✅ L4 after wave $WAVE"
    if [ $((i + 1)) -lt "$NW" ] && [ "$WAVE_DELAY" -gt 0 ]; then note "- ⏳ wave_delay ${WAVE_DELAY}s"; sleep "$WAVE_DELAY"; fi
  fi
done

# ---- report: job log, the dispatching infra PR, or the apps PR that produced this commit
cat "$REPORT"
if [ -n "$INFRA_PR" ]; then
  REPO=homelab/homelab-infra python3 "$INFRA/scripts/gitea.py" comment "$INFRA_PR" "$REPORT" || echo "warning: comment on infra PR #$INFRA_PR failed"
elif [ "$EVENT" = push ]; then
  read -r APR _ <<<"$(python3 "$INFRA/scripts/gitea.py" pr-of-commit "$SHA" 2>/dev/null || true)" || true
  [ -n "${APR:-}" ] && { python3 "$INFRA/scripts/gitea.py" comment "$APR" "$REPORT" || echo "warning: comment on PR #$APR failed"; }
fi
exit $FAILED
