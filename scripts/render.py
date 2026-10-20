#!/usr/bin/env python3
"""homelab-apps offline render for the PR check (migration step 5g, infra #127; proposal 2.2 B, 5b 'PR render').

Run by main's scripts/ci-pr.sh inside bwrap: no network, every tree read-only, one writable output dir, the
root-owned venv /opt/ansible-apps/venv (python -I). For each deployment the PR adds, changes or removes it renders,
on main (--base) and on the PR (--head), every template of the catalogue entry plus the merged variables of every
host, and writes a unified diff. No task runs; nothing reaches a guest, OpenBao or the network. Facts, registered
results and secrets do not exist offline: they render as placeholders (<<undefined:NAME>>). Class C.

Entry point:
  render.py --base MAIN --head PR --infra INFRA --protected protected.yaml --out OUT
Output: OUT/diff.md      markdown, unified diff main -> PR per deployment and host; EMPTY when no deployment changed
        OUT/render.json  summary {ansible_core, deployments: {path: {status, hosts, notes[, error]}}, errors}
Exit:   0 = rendered (or nothing to render)
        1 = a render error on the PR side (stderr and diff.md name the file), or a setup error (ansible-core
            missing or < 2.19, protected.yaml / policy/jinja-allow.yaml / no-apps unreadable). Main-side errors are
            notes only. 2 = usage.

Selection (file contents of base vs head; .git ignored):
  deployments/<zone>/<x>.yaml added / changed / removed      -> that deployment
  catalog/<app>/** except catalog/<app>/docs/**              -> every deployment of <app> (base or head)
  playbooks/**, environments.yaml, ansible.cfg, requirements.yml -> every deployment
Per host (inventory as ci-deploy.sh builds it): infra spec values ansible_host, vmid, zone, role, keyctl, type, os;
  groups role_<code>, zone_<zone>, deployment_<zone>_<app>. Variable order (later wins): role defaults < apps_* <
  deployment vars < version vars (version.vars from pin / url / known) < spec values < host_vars < role vars.
Rendered: role/templates/** (all), files/**/*.j2, tests/**/*.j2; other text files in files/ and tests/ are shown
  as-is, binaries / files > 1 MiB by size or sha256. Jinja: ImmutableSandboxedEnvironment, trim_blocks,
  keep_trailing_newline; only the filters/tests of main's policy/jinja-allow.yaml, ansible-core's implementation
  where it has one, Jinja's built-in otherwise.
"""
import argparse
import difflib
import glob
import hashlib
import json
import os
import re
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import catalog  # noqa: E402  main's copies: this script always runs from main
import guard  # noqa: E402

MIN_ANSIBLE = (2, 19)
ALLOW = os.path.join(HERE, "..", "policy", "jinja-allow.yaml")
WATCH_DIRS = ("catalog", "deployments", "playbooks")
WATCH_FILES = ("environments.yaml", "ansible.cfg", "requirements.yml")
GLOBAL_RE = re.compile(r"^(playbooks/|environments\.yaml$|ansible\.cfg$|requirements\.yml$)")
DEP_RE = re.compile(r"^deployments/([^/]+)/([^/]+)\.yaml$")
DOCS_RE = re.compile(r"^catalog/[^/]+/docs/")
# same rule as ci-deploy.sh (inventory): a deployment may not set these
BANNED = re.compile(r"^(zone|vmid|role|type|os|keyctl|hostvars|group_names|groups|omit|environment|playbook_dir|role_path"
                    r"|inventory_.*|ansible_.*|bao_.*|delegate_.*|deploy_.*|apps_.*)$")
JINJA_MARKS = ("{{", "{%", "{#")
MAX_FILE = 1 << 20
SPEC_KEYS = ("ansible_host", "vmid", "zone", "role", "keyctl", "type", "os")
MAGIC_HIDDEN = ("inventory_hostname", "inventory_hostname_short", "groups", "role_path", "playbook_dir", "omit")


class SetupError(Exception):
    """The render environment itself is unusable (fail closed)."""


class RenderError(Exception):
    """A problem in one tree's content; the message starts with the repo-relative file."""


# ---------------------------------------------------------------- environment
def ansible_plugins():
    """-> (ansible-core version, filters, tests, allowed filter names, allowed test names)."""
    try:
        from ansible.release import __version__ as version
    except ImportError as e:
        raise SetupError(f"ansible-core is not importable ({e}); run with /opt/ansible-apps/venv/bin/python3") from None
    nums = tuple(int(x) for x in re.findall(r"\d+", version)[:2])
    if nums < MIN_ANSIBLE:
        raise SetupError(f"ansible-core {version} is older than 2.19 (proposal 5b)")
    try:
        with open(ALLOW, encoding="utf-8") as f:
            allow = yaml.safe_load(f)
        names_f = [str(x) for x in allow.get("filters") or []]
        names_t = [str(x) for x in allow.get("tests") or []]
    except (OSError, yaml.YAMLError, AttributeError) as e:
        raise SetupError(f"policy/jinja-allow.yaml: {e}") from None
    try:
        from ansible.plugins.filter.core import FilterModule
        from ansible.plugins.test.core import TestModule
        af, at = dict(FilterModule().filters()), dict(TestModule().tests())
    except Exception as e:  # noqa: BLE001 - any import/plugin failure = unusable venv
        raise SetupError(f"ansible-core {version}: core filter/test plugins: {type(e).__name__}: {e}") from None
    return version, af, at, names_f, names_t


def make_env(af, at, names_f, names_t):
    import jinja2
    from jinja2.sandbox import ImmutableSandboxedEnvironment

    class Placeholder(jinja2.ChainableUndefined):
        __slots__ = ()

        def __str__(self):
            return f"<<undefined:{self._undefined_name or '?'}>>"

    env = ImmutableSandboxedEnvironment(undefined=Placeholder, trim_blocks=True, keep_trailing_newline=True,
                                        autoescape=False)
    bf, bt = env.filters, env.tests
    env.filters = {n: af.get(n) or bf[n] for n in names_f if n in af or n in bf}
    env.tests = {n: at.get(n) or bt[n] for n in names_t if n in at or n in bt}
    return env


def render_text(env, text, ctx, where):
    import jinja2
    try:
        return env.from_string(text).render(ctx)
    except jinja2.UndefinedError as e:   # needs a runtime value (fact, register, lookup): not an error offline
        return f"<<not rendered offline: {e}>>\n"
    except jinja2.TemplateSyntaxError as e:
        raise RenderError(f"{where}:{e.lineno}: template error: {e.message}") from None
    except Exception as e:  # noqa: BLE001 - SecurityError, filter errors, TypeError, ...: the file is named
        raise RenderError(f"{where}: {type(e).__name__}: {e}") from None


# ---------------------------------------------------------------- trees
def _path(root, rel):
    p = os.path.join(root, rel)
    top = os.path.realpath(root)
    if os.path.islink(p) or not os.path.realpath(p).startswith(top + os.sep):
        raise RenderError(f"{rel}: symlink or path outside the tree (refused)")
    return p


def load_yaml(root, rel):
    try:
        with open(_path(root, rel), encoding="utf-8") as f:
            return yaml.safe_load(f)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        raise RenderError(f"{rel}: cannot read: {e}") from None


def tree_digest(root):
    out = {}

    def add(p):
        rel = os.path.relpath(p, root).replace(os.sep, "/")
        if os.path.islink(p):
            out[rel] = "link:" + os.readlink(p)
            return
        h = hashlib.sha256()
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        out[rel] = h.hexdigest()

    for f in WATCH_FILES:
        if os.path.lexists(os.path.join(root, f)):
            add(os.path.join(root, f))
    for top in WATCH_DIRS:
        for d, dirs, files in os.walk(os.path.join(root, top)):
            for x in [x for x in dirs if os.path.islink(os.path.join(d, x))]:
                add(os.path.join(d, x))
                dirs.remove(x)
            dirs.sort()
            for f in sorted(files):
                add(os.path.join(d, f))
    return out


def changed_paths(base, head):
    a, b = tree_digest(base), tree_digest(head)
    return sorted(p for p in set(a) | set(b) if a.get(p) != b.get(p))


def list_deployments(root):
    out = []
    top = os.path.join(root, "deployments")
    if os.path.isdir(top):
        for zone in sorted(os.listdir(top)):
            if zone.startswith(".") or not os.path.isdir(os.path.join(top, zone)):
                continue
            out += [f"deployments/{zone}/{f}" for f in sorted(os.listdir(os.path.join(top, zone))) if f.endswith(".yaml")]
    return out


def dep_app(root, rel):
    try:
        d = load_yaml(root, rel)
    except RenderError:
        d = None
    stem = rel.rsplit("/", 1)[1][:-5]
    return str(d.get("app", stem)) if isinstance(d, dict) else stem


def select_deployments(base, head, changes):
    deps = {}
    for root in (base, head):
        for rel in list_deployments(root):
            deps.setdefault(rel, set()).add(dep_app(root, rel))
    every = any(GLOBAL_RE.match(p) for p in changes)
    apps = {p.split("/")[1] for p in changes if p.startswith("catalog/") and p.count("/") >= 2 and not DOCS_RE.match(p)}
    files = {p for p in changes if DEP_RE.match(p)}
    return sorted(r for r, a in deps.items() if every or r in files or a & apps)


def load_specs(infra):
    """Inventory host values per host, as ci-deploy.sh writes them (from infra main's specs)."""
    specs = {}
    for p in sorted(glob.glob(os.path.join(infra, "terraform", "stacks", "hm1", "*", "guests", "*.yaml"))):
        with open(p, encoding="utf-8") as f:
            s = yaml.safe_load(f) or {}
        if not isinstance(s, dict):
            continue
        host = s.get("hostname") or os.path.basename(p)[:-5]
        specs[host] = {"ansible_host": str(s.get("ip", "")).split("/")[0], "vmid": s.get("vmid"),
                       "zone": p.split(os.sep)[-3], "role": s.get("role"), "keyctl": bool(s.get("keyctl", False)),
                       "type": s.get("type", "lxc"), "os": s.get("os")}
    return specs


# ---------------------------------------------------------------- one deployment
def clean(where, d):
    if not isinstance(d, dict):
        raise RenderError(f"{where}: not a mapping")
    for k in d:
        if not isinstance(k, str) or BANNED.match(k):
            raise RenderError(f"{where}: banned key {k!r}")

    def walk(v):
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, str) and (any(m in v for m in JINJA_MARKS + ("__",)) or v == "mro"):
            raise RenderError(f"{where}: value {v[:40]!r} has Jinja, '__' or 'mro'")
    walk(d)
    return dict(d)


def role_vars(root, entry_dir, kind):
    base = f"{entry_dir}/role/{kind}"
    cands = [f"{base}/main.yml", f"{base}/main.yaml"]
    d = os.path.join(root, base, "main")
    if os.path.isdir(d):
        cands += sorted(f"{base}/main/{f}" for f in os.listdir(d) if f.endswith((".yml", ".yaml")))
    out, origin = {}, {}
    for rel in cands:
        if not os.path.lexists(os.path.join(root, rel)):
            continue
        data = load_yaml(root, rel)
        if data is None:
            continue
        if not isinstance(data, dict):
            raise RenderError(f"{rel}: top level must be a mapping")
        for k, v in data.items():
            out[str(k)] = v
            origin[str(k)] = rel
    return out, origin


def version_vars(rel, entry, dep, entry_dir):
    ver = entry.get("version")
    if not isinstance(ver, dict):
        return {}
    where = f"{entry_dir}/app.yaml"
    mp = ver.get("vars") or {}
    if not isinstance(mp, dict):
        raise RenderError(f"{where}: version.vars must be a mapping")
    known = ver.get("known") or {}
    known = {str(k): v for k, v in known.items()} if isinstance(known, dict) else {}
    want = dep.get("version", "catalog")
    pin = ver.get("pin") if want in (None, "catalog") else want
    pin = None if pin is None else str(pin)
    if want not in (None, "catalog") and pin not in known:
        raise RenderError(f"{rel}: version '{pin}' is not in {where} version.known")
    vals = {"version": pin, "sha256": known.get(pin),
            "url": ver["url"].replace("{version}", pin) if isinstance(ver.get("url"), str) and pin else None}
    out = {}
    for k in ("version", "url", "sha256"):
        name = mp.get(k)
        if name is None:
            continue
        if not isinstance(name, str) or BANNED.match(name):
            raise RenderError(f"{where}: version.vars.{k}: banned or invalid name {name!r}")
        out[name] = vals[k]
    return out


def template_files(root, entry_dir):
    out = []
    for sub, always in (("role/templates", True), ("files", False), ("tests", False)):
        top = os.path.join(root, entry_dir, sub)
        if not os.path.isdir(top):
            continue
        _path(root, f"{entry_dir}/{sub}")
        for d, dirs, files in os.walk(top):
            for x in dirs:
                if os.path.islink(os.path.join(d, x)):
                    raise RenderError(f"{os.path.relpath(os.path.join(d, x), root)}: symlink (refused)")
            dirs.sort()
            for f in sorted(files):
                rel = os.path.relpath(os.path.join(d, f), root).replace(os.sep, "/")
                p = _path(root, rel)
                size = os.path.getsize(p)
                if size > MAX_FILE:
                    out.append((rel, "raw", f"<<not rendered: {size} bytes>>\n"))
                    continue
                with open(p, "rb") as fh:
                    raw = fh.read()
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    out.append((rel, "raw", f"<<binary, sha256 {hashlib.sha256(raw).hexdigest()}>>\n"))
                    continue
                out.append((rel, "j2" if always or f.endswith(".j2") else "raw", text))
    return out


def _tmpl(env, v, ctx, where):
    if isinstance(v, str) and any(m in v for m in JINJA_MARKS):
        return render_text(env, v, ctx, where)
    if isinstance(v, dict):
        return {k: _tmpl(env, x, ctx, where) for k, x in v.items()}
    if isinstance(v, list):
        return [_tmpl(env, x, ctx, where) for x in v]
    return v


def resolve_vars(env, v, origin):
    cur = v
    for _ in range(5):    # chained defaults (a: "{{ b }}", b: "{{ c }}")
        nxt = {k: _tmpl(env, x, cur, f"{origin.get(k, 'vars')} (var {k})") for k, x in cur.items()}
        if nxt == cur:
            break
        cur = nxt
    return cur


def render_deployment(env, root, rel, specs, gspecs, protected, no_apps):
    """-> ({host: {file: text}}, notes), or (None, []) when the file does not exist in this tree."""
    m = DEP_RE.match(rel)
    zone = m.group(1)
    if not os.path.lexists(os.path.join(root, rel)):
        return None, []
    dep = load_yaml(root, rel)
    if not isinstance(dep, dict):
        raise RenderError(f"{rel}: top level must be a mapping")
    app = str(dep.get("app", m.group(2)))
    hosts = dep.get("hosts")
    if not isinstance(hosts, list) or not hosts or not all(isinstance(h, str) for h in hosts):
        raise RenderError(f"{rel}: hosts must be a non-empty list of host names")
    entry_dir = f"catalog/{app}"
    if not catalog.NAME_RE.match(app) or not os.path.isfile(os.path.join(root, entry_dir, "app.yaml")):
        raise RenderError(f"{rel}: unknown app '{app}' (no {entry_dir}/app.yaml)")
    entry = load_yaml(root, f"{entry_dir}/app.yaml")
    if not isinstance(entry, dict):
        raise RenderError(f"{entry_dir}/app.yaml: top level must be a mapping")
    gvars = clean(f"{rel}: vars", dep.get("vars") or {})
    hvars = dep.get("host_vars") or {}
    if not isinstance(hvars, dict):
        raise RenderError(f"{rel}: host_vars: not a mapping")
    for h in hvars:
        if h not in hosts:
            raise RenderError(f"{rel}: host_vars: {h} is not a host of this deployment")
    hvars = {h: clean(f"{rel}: host_vars.{h}", v or {}) for h, v in hvars.items()}
    defaults, d_origin = role_vars(root, entry_dir, "defaults")
    rvars, r_origin = role_vars(root, entry_dir, "vars")
    vvars = version_vars(rel, entry, dep, entry_dir)
    templates = template_files(root, entry_dir)

    notes, inv = [], {}
    for h in hosts:
        s = specs.get(h)
        if s is None:
            notes.append(f"{h}: not in infra specs yet: inventory values are placeholders")
            inv[h] = {}
            continue
        errs = guard.check_host(h, zone, gspecs, protected, None, no_apps)
        if errs:
            notes.append(f"{h}: not rendered, the deploy guard refuses it: {'; '.join(errs)}")
            continue
        inv[h] = {k: s[k] for k in SPEC_KEYS}
    grp = "deployment_" + re.sub(r"[^a-z0-9_]", "_", f"{zone}_{app}")
    groups = {"all": sorted(inv), grp: sorted(inv), f"zone_{zone}": sorted(inv)}
    for h, hv in inv.items():
        if hv.get("role"):
            groups.setdefault(f"role_{hv['role']}", []).append(h)
    groups = {g: sorted(v) for g, v in groups.items()}
    apps_vars = {"apps_deployment": rel, "apps_deployment_zone": zone, "apps_deployment_app": app,
                 "apps_sha": "<<apps_sha>>", "apps_infra_sha": "<<infra_sha>>"}   # constant: no sha-only diffs
    origin = dict(d_origin, **{k: f"{rel} (vars)" for k in gvars}, **r_origin)

    resolved = {}
    for h, hv in inv.items():
        v = {}
        for layer in (defaults, apps_vars, gvars, vvars, hv, hvars.get(h) or {}, rvars):
            v.update(layer)
        v.update({"inventory_hostname": h, "inventory_hostname_short": h.split(".")[0], "groups": groups,
                  "group_names": sorted(g for g, mem in groups.items() if g != "all" and h in mem),
                  "role_path": f"{entry_dir}/role", "playbook_dir": "playbooks", "omit": "<<omit>>"})
        resolved[h] = resolve_vars(env, v, origin)

    out = {}
    for h, v in resolved.items():
        ctx = dict(v, hostvars=resolved)
        shown = {k: x for k, x in v.items() if k not in MAGIC_HIDDEN}
        files = {"vars.yaml": yaml.safe_dump(shown, sort_keys=True, default_flow_style=False, allow_unicode=True)}
        for trel, kind, text in templates:
            files[trel] = render_text(env, text, ctx, trel) if kind == "j2" else text
        out[h] = files
    return out, notes


# ---------------------------------------------------------------- output
def host_diff(host, b, h):
    out = []
    for f in sorted(set(b) | set(h)):
        x, y = b.get(f), h.get(f)
        if x == y:
            continue
        out += difflib.unified_diff([] if x is None else x.splitlines(), [] if y is None else y.splitlines(),
                                    fromfile=f"a/{host}/{f}" if x is not None else "/dev/null",
                                    tofile=f"b/{host}/{f}" if y is not None else "/dev/null", lineterm="")
    return out


def fence(text):
    return "`" * max([3] + [len(r) + 1 for r in re.findall(r"`{3,}", text)])


def md(s):
    return str(s).replace("`", "'")


def main(argv=None):
    ap = argparse.ArgumentParser(description="homelab-apps offline render (PR check)")
    for k in ("--base", "--head", "--infra", "--protected", "--out"):
        ap.add_argument(k, required=True)
    a = ap.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    diff_md = os.path.join(a.out, "diff.md")
    open(diff_md, "w", encoding="utf-8").close()       # empty first: a crash never leaves a stale diff
    summary = {"ansible_core": None, "deployments": {}, "errors": []}

    def finish(rc, text=""):
        with open(diff_md, "w", encoding="utf-8") as f:
            f.write(text)
        with open(os.path.join(a.out, "render.json"), "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, sort_keys=True)
        return rc

    try:
        version, af, at, names_f, names_t = ansible_plugins()
        summary["ansible_core"] = version
        protected = guard.load_protected(a.protected)
        no_apps = guard.load_no_apps(guard.DEFAULT_NO_APPS)      # main's policy/no-apps.yaml
        specs, gspecs = load_specs(a.infra), guard.load_specs(a.infra)
        changes = changed_paths(a.base, a.head)
    except (SetupError, OSError, ValueError, TypeError, AttributeError, yaml.YAMLError) as e:
        print(f"render: setup error: {e}", file=sys.stderr)
        summary["errors"].append(f"setup: {e}")
        return finish(1)
    selected = select_deployments(a.base, a.head, changes)
    if not selected:
        print("render: no deployment added, changed or removed")
        return finish(0)
    env = make_env(af, at, names_f, names_t)

    lines = [f"Offline render, main -> PR (ansible-core {md(version)}, {len(selected)} deployment(s)). Facts, "
             "registered results and secrets do not exist offline and show as '<<undefined:NAME>>'.", ""]
    for rel in selected:
        eb, eh = os.path.lexists(os.path.join(a.base, rel)), os.path.lexists(os.path.join(a.head, rel))
        status = "added" if not eb else "removed" if not eh else "changed" if rel in changes else "re-rendered"
        info = {"status": status, "hosts": [], "notes": []}
        side = {}
        for name, root in (("base", a.base), ("head", a.head)):
            try:
                side[name], notes = render_deployment(env, root, rel, specs, gspecs, protected, no_apps)
                if name == "head" or not eh:
                    info["notes"] += notes
            except RenderError as e:
                side[name] = None
                if name == "head":
                    info["error"] = str(e)
                    summary["errors"].append(str(e))
                    print(f"render: ERROR {e}", file=sys.stderr)
                else:
                    info["notes"].append(f"main side not rendered: {e}")
        b, h = side.get("base") or {}, side.get("head") or {}
        info["hosts"] = sorted(set(b) | set(h))
        summary["deployments"][rel] = info
        lines.append(f"#### {md(rel)} ({status})")
        lines += [f"- ⚠️ {md(n)}" for n in info["notes"]]
        if "error" in info:
            lines += [f"- ❌ **render error**: {md(info['error'])}", ""]
            continue
        any_diff = False
        for host in info["hosts"]:
            d = host_diff(host, b.get(host, {}), h.get(host, {}))
            if d:
                any_diff = True
                body = "\n".join(d)
                fc = fence(body)
                lines += [f"**{md(host)}**", "", fc + "diff", body, fc, ""]
        if not any_diff:
            lines += ["- no rendered difference", ""]
    return finish(1 if summary["errors"] else 0, "\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.exit(main())
