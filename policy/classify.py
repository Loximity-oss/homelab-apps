#!/usr/bin/env python3
"""homelab-apps change classifier (proposal 5.2; migration step 5a, infra #127). Fail closed: anything
not listed as A or B is C.

Entry point:
  git diff --raw -M <base> <head> > changes.txt
  python3 policy/classify.py --base BASE_DIR --head HEAD_DIR --changes changes.txt   (or --changes - for stdin)
Prints JSON {"class": "A|B|C", "reasons": [{"path", "class", "why"}]}; exit 0 (2 = usage error).
BASE_DIR / HEAD_DIR are checkouts of the PR base and head; a missing one makes every YAML judgement C.

Library: classify(base_dir, head_dir, changes) -> (cls, reasons); changes = [(status, old_mode, new_mode, old, new)].
"""
import argparse
import json
import os
import re
import sys

import yaml

ORDER = {"A": 0, "B": 1, "C": 2}
A_PATHS = ["catalog/*/docs/**", "docs/**", "README.md"]
B_PATHS = ["catalog/*/role/{tasks,defaults,handlers,templates}/**", "catalog/*/files/**"]
APP_YAML = "catalog/*/app.yaml"
DEPLOYMENT = "deployments/*/*.yaml"
B_APP_LEAVES = [("catalog", "summary"), ("owner",), ("catalog", "size"), ("catalog", "os"),
                ("catalog", "ports"), ("rollout", "serial"), ("rollout", "order")]
B_DEPLOY_LEAVES = [("vars",), ("host_vars",), ("pin_until",)]
SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z.-]+)?$")
_MISSING = object()


# ---------- path matcher: '*' = one segment, '**' = any depth, '{a,b}' alternatives ----------
def _expand(pattern):
    m = re.search(r"\{([^{}]*)\}", pattern)
    if not m:
        return [pattern]
    out = []
    for alt in m.group(1).split(","):
        out += _expand(pattern[:m.start()] + alt + pattern[m.end():])
    return out


def _seg_match(pat, seg):
    rx = "^" + ".*".join(re.escape(p) for p in pat.split("*")) + "$"
    return re.match(rx, seg, re.S) is not None


def _match_segs(ps, ss):
    if not ps:
        return not ss
    if ps[0] == "**":
        return any(_match_segs(ps[1:], ss[i:]) for i in range(len(ss) + 1))
    return bool(ss) and _seg_match(ps[0], ss[0]) and _match_segs(ps[1:], ss[1:])


def path_match(pattern, path):
    ss = path.split("/")
    if "" in ss or "." in ss or ".." in ss:
        return False
    return any(_match_segs(p.split("/"), ss) for p in _expand(pattern))


# ---------- YAML leaves ----------
def _load(root, rel):
    """(data, error). data None + no error = file absent."""
    if root is None or not os.path.isdir(root):
        return None, "checkout missing"
    p = os.path.join(root, rel)
    if not os.path.lexists(p):
        return None, None
    if os.path.islink(p):
        return None, "symlink"
    try:
        with open(p, encoding="utf-8") as f:
            d = yaml.safe_load(f)
    except (OSError, yaml.YAMLError, UnicodeDecodeError) as e:
        return None, f"unparsable: {e.__class__.__name__}"
    if not isinstance(d, dict):
        return None, "not a mapping"
    return d, None


def leaves(obj, prefix=()):
    if isinstance(obj, dict) and obj:
        out = {}
        for k, v in obj.items():
            out.update(leaves(v, prefix + (str(k),)))
        return out
    return {prefix: obj}


def changed_leaves(base, head):
    lb, lh = leaves(base), leaves(head)
    return sorted(k for k in set(lb) | set(lh) if lb.get(k, _MISSING) != lh.get(k, _MISSING))


def _starts(p, prefixes):
    return any(p[:len(x)] == x for x in prefixes)


def semver(v):
    m = SEMVER.match(str(v).split("+")[0]) if v is not None else None
    return tuple(int(x) for x in m.groups()) if m else None


def bump(old, new):
    """'patch' | 'minor' | 'major' | None (unparsable)."""
    o, n = semver(old), semver(new)
    if o is None or n is None:
        return None
    if o[0] != n[0]:
        return "major"
    return "minor" if o[1] != n[1] else "patch"


def _known(app):
    k = ((app or {}).get("version") or {}).get("known")
    if isinstance(k, dict):
        return {str(a): b for a, b in k.items()}
    if isinstance(k, list):
        out = {}
        for e in k:
            if not isinstance(e, dict) or "version" not in e:
                return None
            out[str(e["version"])] = {a: b for a, b in e.items() if a != "version"}
        return out
    return {} if k is None else None


def _bump_c(*apps):
    return any(str(((a or {}).get("version") or {}).get("bump_class", "")).upper() == "C" for a in apps)


# ---------- judges ----------
def judge_app(base, head):
    """catalog/<app>/app.yaml changed in place -> list of (cls, why)."""
    out = []
    strict_c = _bump_c(base, head)
    kb, kh = _known(base), _known(head)
    for p in changed_leaves(base, head):
        name = ".".join(p)
        if _starts(p, B_APP_LEAVES):
            out.append(("B", name))
        elif p == ("version", "pin"):
            old, new = base.get("version", {}).get("pin"), head.get("version", {}).get("pin")
            b = bump(old, new)
            if strict_c:
                out.append(("C", f"{name}: bump_class C"))
            elif b in ("patch", "minor") and kh is not None and str(new) in kh:
                out.append(("B", f"{name}: {b} {old} -> {new}"))
            else:
                out.append(("C", f"{name}: {b or 'unparsable'} {old} -> {new} (or not in known)"))
        elif p[:2] == ("version", "known"):
            continue  # judged as a whole below
        else:
            out.append(("C", name))
    if kb != kh:
        if kb is None or kh is None:
            out.append(("C", "version.known: unparsable"))
        else:
            for v in sorted(set(kb) | set(kh)):
                if v in kb and v in kh and kb[v] != kh[v]:
                    out.append(("C", f"version.known.{v}: changed for existing version"))
                elif v in kb and v not in kh:
                    out.append(("C", f"version.known.{v}: removed"))
                elif v not in kb:
                    ref = max((semver(x) for x in kb if semver(x)), default=None)
                    refs = ".".join(map(str, ref)) if ref else None
                    b = bump(refs, v)
                    if strict_c:
                        out.append(("C", f"version.known.{v}: bump_class C"))
                    elif b in ("patch", "minor"):
                        out.append(("B", f"version.known.{v}: new {b}"))
                    else:
                        out.append(("C", f"version.known.{v}: {b or 'unparsable'} bump"))
    return out


def judge_deployment(base, head, head_app):
    out = []
    for p in changed_leaves(base, head):
        name = ".".join(p)
        if _starts(p, B_DEPLOY_LEAVES):
            out.append(("B", name))
        elif p == ("wave",):
            ok = all(isinstance(x, int) and not isinstance(x, bool) and x >= 2
                     for x in (base.get("wave"), head.get("wave")))
            out.append(("B" if ok else "C", f"wave {base.get('wave')} -> {head.get('wave')}"))
        elif p == ("version",):
            kh = _known(head_app)
            old, new = base.get("version"), head.get("version")
            b = bump(old, new)
            if head_app is None or kh is None or str(new) not in kh:
                out.append(("C", f"version {new}: not in catalog known"))
            elif _bump_c(head_app) or b not in ("patch", "minor"):
                out.append(("C", f"version {old} -> {new}: {b or 'unparsable'} / bump_class"))
            else:
                out.append(("B", f"version {old} -> {new}: {b}"))
        else:
            out.append(("C", name))
    return out


def judge_path(path, status, base_dir, head_dir):
    """One path (old or new side) -> list of (cls, why)."""
    if path.startswith('"') or "\\" in path:
        return [("C", "quoted/escaped path")]
    if path_match(APP_YAML, path) or path_match(DEPLOYMENT, path):
        if status in ("A", "D", "C"):
            return [("C", "new/deleted entry or deployment")]
        base, eb = _load(base_dir, path)
        head, eh = _load(head_dir, path)
        if eb or eh or base is None or head is None:
            return [("C", f"yaml: {eb or eh or 'missing side'}")]
        if path_match(APP_YAML, path):
            return judge_app(base, head)
        app = head.get("app") or path.split("/")[-1][:-5]
        happ, _ = _load(head_dir, f"catalog/{app}/app.yaml") if re.match(r"^[a-z0-9-]+$", str(app)) else (None, None)
        return judge_deployment(base, head, happ)
    if any(path_match(p, path) for p in A_PATHS):
        return [("A", "docs")]
    if any(path_match(p, path) for p in B_PATHS):
        return [("B", "role/files")]
    return [("C", "not listed as A or B")]


def classify(base_dir, head_dir, changes):
    reasons = []
    for status, om, nm, old, new in changes:
        st = status[:1]
        paths = [p for p in (old, new) if p]
        if om in ("120000", "160000") or nm in ("120000", "160000"):
            for p in paths:
                reasons.append({"path": p, "class": "C", "why": "symlink or submodule"})
            continue
        if st == "R":  # judge old and new path; content change of renamed YAML = new entry
            for p in paths:
                for c, w in judge_path(p, "A" if p == new else "D", base_dir, head_dir):
                    reasons.append({"path": p, "class": c, "why": "rename: " + w})
            continue
        if st not in ("A", "M", "D", "C", "T"):
            reasons.append({"path": new or old, "class": "C", "why": f"status {status}"})
            continue
        p = new or old
        for c, w in judge_path(p, "C" if st == "T" else st, base_dir, head_dir):
            reasons.append({"path": p, "class": c, "why": w})
    if not changes:
        return "A", reasons
    cls = max((r["class"] for r in reasons), key=ORDER.get, default="C")
    return cls, reasons


def parse_raw(text):
    """Parse `git diff --raw -M` lines."""
    out = []
    for line in text.splitlines():
        if not line.startswith(":"):
            continue
        meta, _, rest = line.partition("\t")
        f = meta[1:].split()
        if len(f) < 5:
            out.append(("X", "", "", "", line))
            continue
        parts = rest.split("\t")
        old = parts[0]
        new = parts[1] if len(parts) > 1 else parts[0]
        if f[4][:1] == "A":
            old = None
        if f[4][:1] == "D":
            new = None
        out.append((f[4], f[0], f[1], old, new))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="homelab-apps change classifier")
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", required=True)
    ap.add_argument("--changes", required=True, help="git diff --raw -M output file, or -")
    a = ap.parse_args(argv)
    text = sys.stdin.read() if a.changes == "-" else open(a.changes, encoding="utf-8").read()
    cls, reasons = classify(a.base, a.head, parse_raw(text))
    print(json.dumps({"class": cls, "reasons": reasons}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
