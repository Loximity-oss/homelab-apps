#!/usr/bin/env python3
"""homelab-apps PR lint gate (proposal 5b-security; migration step 5a, infra #127).

Checks everything under catalog/<app>/ (role tasks/handlers/defaults/vars, every .j2, template srcs, all YAML
scalars) on the parsed Jinja AST, plus deployment values (no Jinja, no '__', banned keys, host_vars hosts).

Entry point:
  python3 scripts/lint_gate.py [--root DIR] [--allow policy/jinja-allow.yaml] [--app APP ...]
Prints 'ERROR <file>: <finding>' per finding; exit 0 = clean, 1 = findings, 2 = usage.
--allow defaults to policy/jinja-allow.yaml next to THIS script (main's copy), never the PR's.

Library: run(root, allow_dict, apps=None) -> sorted list of findings.
"""
import argparse
import os
import re
import sys

import yaml
from jinja2 import Environment, nodes
from jinja2.exceptions import TemplateSyntaxError

HOST_RE = re.compile(r"^hm1-[a-z]{2,4}-[a-z][a-z0-9]{1,7}[0-9]{2}$")
BANNED_VAR = re.compile(r"^(zone|vmid|role|type|os|bao_.*|deploy_.*)$")
LOCAL = {"localhost", "127.0.0.1", "::1", "ip6-localhost"}
LOOP_OK = {"with_items", "with_dict", "with_list"}
KEYWORDS = {"name", "when", "register", "vars", "loop", "loop_control", "changed_when", "failed_when", "until",
            "retries", "delay", "delegate_to", "delegate_facts", "run_once", "become", "become_user",
            "become_method", "become_flags", "tags", "notify", "listen", "ignore_errors", "ignore_unreachable",
            "no_log", "check_mode", "diff", "environment", "block", "rescue", "always", "args", "timeout",
            "throttle", "any_errors_fatal", "module_defaults", "connection", "local_action", "action", "async",
            "poll", "debugger", "collections"}
BANNED_MODULES = {"add_host", "group_by", "fetch", "synchronize", "script"}
READONLY = {"command", "shell", "slurp", "stat", "uri"}
EXPR_KEYS = ("when", "changed_when", "failed_when", "until")
BAD_NAMES = {"lipsum", "cycler", "joiner", "namespace", "self"}
LOOKUPS = {"lookup", "query", "q"}
BAD_METHODS = LOOKUPS | {"format", "format_map", "mro"}
MARKERS = ("{{", "{%", "{#")


def norm(name):
    for p in ("ansible.builtin.", "ansible.legacy."):
        if str(name).startswith(p):
            return str(name)[len(p):]
    return str(name)


def bad_str(s):
    return "__" in s or s == "mro"


def truthy(v):
    return v is True or str(v).lower() in ("yes", "true", "1")


def aslist(v):
    return v if isinstance(v, list) else ([] if v is None else [v])


def strings(obj, path=()):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                yield path, k
            yield from strings(v, path + (str(k),))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from strings(v, path + (str(i),))
    elif isinstance(obj, str):
        yield path, obj


def fold(n):
    """Constant-fold string building; non-constant parts become NUL."""
    if isinstance(n, nodes.Const):
        return n.value if isinstance(n.value, str) else "\0"
    if isinstance(n, nodes.Concat):
        return "".join(fold(c) for c in n.nodes)
    if isinstance(n, nodes.Add):
        return fold(n.left) + fold(n.right)
    if isinstance(n, nodes.Mul):
        if isinstance(n.left, nodes.Const) and isinstance(n.left.value, str) and \
                isinstance(n.right, nodes.Const) and isinstance(n.right.value, int):
            return n.left.value * max(0, min(n.right.value, 64))
    return "\0"


class Linter:
    def __init__(self, allow):
        self.filters = set((allow or {}).get("filters") or []) - {"attr"}
        self.tests = set((allow or {}).get("tests") or [])
        self.env = Environment()
        self.errors = []

    def err(self, where, msg):
        self.errors.append(f"{where}: {msg}")

    # ----- Jinja AST -----
    def jinja(self, where, src, expr=False):
        if expr and not any(m in src for m in MARKERS):
            src = "{{ " + src + " }}"
        try:
            ast = self.env.parse(src)
        except TemplateSyntaxError as e:
            self.err(where, f"jinja syntax: {e.message}")
            return None
        for n in ast.find_all(nodes.Node):
            self.node(where, n)
        return ast

    def attr_const(self, where, v, what="attribute"):
        if not (isinstance(v, nodes.Const) and isinstance(v.value, str)) or v.value.startswith("_") \
                or bad_str(v.value):
            self.err(where, f"{what} must be a constant not starting with '_'")

    def node(self, where, n):
        if isinstance(n, (nodes.Include, nodes.Import, nodes.FromImport, nodes.Extends)):
            self.err(where, "template include/import/extends")
        elif isinstance(n, nodes.Const):
            if isinstance(n.value, str) and bad_str(n.value):
                self.err(where, "constant contains '__' or is 'mro'")
        elif isinstance(n, nodes.Name):
            if n.name in BAD_NAMES:
                self.err(where, f"global '{n.name}'")
        elif isinstance(n, nodes.Getattr):
            if n.attr.startswith("_") or n.attr == "mro":
                self.err(where, f"attribute '{n.attr}'")
        elif isinstance(n, nodes.Getitem):
            a, base = n.arg, n.node
            is_groups = isinstance(base, nodes.Name) and base.name == "groups"
            if isinstance(a, nodes.Const):
                if isinstance(a.value, str) and (a.value.startswith("_") or a.value == "mro"):
                    self.err(where, f"subscript '{a.value}'")
                if is_groups and not (isinstance(a.value, str) and a.value.startswith("role_")):
                    self.err(where, "groups[] takes a constant 'role_<code>' only")
            elif isinstance(a, nodes.Slice):
                pass
            elif not (isinstance(base, nodes.Name) and base.name == "hostvars"):
                self.err(where, "variable subscript (only hostvars[<var>] allowed)")
        elif isinstance(n, nodes.Filter):
            self.filt(where, n)
        elif isinstance(n, nodes.Test):
            if norm(n.name) not in self.tests:
                self.err(where, f"test '{n.name}' not allow-listed")
            if n.dyn_args or n.dyn_kwargs:
                self.err(where, "dynamic test arguments")
        elif isinstance(n, nodes.Call):
            f = n.node
            if isinstance(f, nodes.Name):
                self.err(where, f"{'lookup' if f.name in LOOKUPS else 'call of global'} '{f.name}'")
            elif isinstance(f, nodes.Getattr) and f.attr in BAD_METHODS:
                self.err(where, f"call of '{f.attr}'")
            if n.dyn_args or n.dyn_kwargs:
                self.err(where, "dynamic call arguments")
        if isinstance(n, (nodes.Concat, nodes.Add, nodes.Mul)):
            s = fold(n)
            if any(m in s for m in MARKERS) or "__" in s:
                self.err(where, "builds a template string or '__'")

    def filt(self, where, n):
        name = norm(n.name)
        if name not in self.filters:
            self.err(where, f"filter '{n.name}' not allow-listed")
        if n.dyn_args or n.dyn_kwargs:
            self.err(where, "dynamic filter arguments")
        for kw in n.kwargs:
            if kw.key == "attribute":
                self.attr_const(where, kw.value)
        args = n.args
        if name == "map" and args:
            c = args[0]
            if not (isinstance(c, nodes.Const) and isinstance(c.value, str) and norm(c.value) in self.filters):
                self.err(where, "map() with a non-allow-listed filter name")
        if name in ("select", "reject") and args:
            c = args[0]
            if not (isinstance(c, nodes.Const) and isinstance(c.value, str) and norm(c.value) in self.tests):
                self.err(where, f"{name}() with a non-allow-listed test name")
        if name in ("selectattr", "rejectattr"):
            if args:
                self.attr_const(where, args[0])
            if len(args) > 1 and not (isinstance(args[1], nodes.Const) and norm(args[1].value) in self.tests):
                self.err(where, f"{name}() with a non-allow-listed test name")
        if name in ("sort", "groupby", "sum", "min", "max", "unique"):
            for a in args:
                if not isinstance(a, nodes.Const):
                    self.err(where, f"{name}() with a non-constant argument")
                elif isinstance(a.value, str):
                    self.attr_const(where, a)
        if name == "extract":
            for a in args:
                self.attr_const(where, a, "extract argument")

    def scalars(self, where, data):
        for p, s in strings(data):
            at = f"{where}:{'.'.join(p)}" if p else where
            if bad_str(s):
                self.err(at, "scalar contains '__' or is 'mro'")
            if any(m in s for m in MARKERS):
                self.jinja(at, s)

    # ----- tasks -----
    def banned_keys(self, where, what, d, extra_ansible=False):
        if d is None:
            return
        if not isinstance(d, dict):
            self.err(where, f"{what} must be a mapping")
            return
        for k in d:
            if BANNED_VAR.match(str(k)) or (extra_ansible and str(k).startswith("ansible_")):
                self.err(where, f"{what} sets banned name '{k}'")

    def resolve(self, where, val, ctx, sub):
        if not isinstance(val, str) or not val:
            self.err(where, "path must be a non-empty string")
            return None
        if val.startswith("/"):
            self.err(where, f"absolute path '{val}'")
            return None
        v = re.sub(r"\{\{\s*role_path\s*\}\}", lambda _m: ctx["role_dir"], val)
        if any(m in v for m in MARKERS):
            self.err(where, f"templated path not statically resolvable '{val}'")
            return None
        p = os.path.normpath(v if os.path.isabs(v) else os.path.join(ctx["role_dir"], sub, v))
        app = ctx["app_dir"] + os.sep
        if not (p + os.sep).startswith(app) or (os.path.exists(p) and not (os.path.realpath(p) + os.sep).startswith(app)):
            self.err(where, f"path escapes catalog/<app>/: '{val}'")
            return None
        return p

    def delegate(self, where, v):
        s = str(v).strip()
        if s.lower() in LOCAL or s.lower().startswith("localhost"):
            self.err(where, f"delegate_to '{s}'")
            return
        if not any(m in s for m in MARKERS):
            if not HOST_RE.match(s):
                self.err(where, f"delegate_to '{s}' is not a deployment host name")
            return
        ast = self.jinja(where, s)
        if ast is None:
            return
        if any(isinstance(c.value, str) and c.value.lower() in LOCAL for c in ast.find_all(nodes.Const)):
            self.err(where, "delegate_to resolves to localhost")
            return
        b = ast.body
        bare = len(b) == 1 and isinstance(b[0], nodes.Output) and len(b[0].nodes) == 1 \
            and isinstance(b[0].nodes[0], nodes.Name)
        grp = any(isinstance(g.node, nodes.Name) and g.node.name == "groups" and isinstance(g.arg, nodes.Const)
                  and str(g.arg.value).startswith("role_") for g in ast.find_all(nodes.Getitem))
        if not (bare or grp):
            self.err(where, "delegate_to must be a host, a bare variable or over groups['role_<code>']")

    def task_list(self, where, data, ctx):
        if data is None:
            return
        if not isinstance(data, list):
            self.err(where, "task file must be a list")
            return
        for i, t in enumerate(data):
            self.task(f"{where}[{i}]", t, ctx)

    def task(self, where, t, ctx):
        if not isinstance(t, dict):
            self.err(where, "task must be a mapping")
            return
        for k in t:
            if str(k).startswith("with_") and k not in LOOP_OK:
                self.err(where, f"loop '{k}' (lookup) not allowed")
        for k in ("local_action", "action", "collections"):
            if k in t:
                self.err(where, f"'{k}' not allowed")
        if "connection" in t:
            self.err(where, "'connection' not allowed")
        self.banned_keys(where, "vars", t.get("vars"))
        reg = t.get("register")
        if reg is not None and BANNED_VAR.match(str(reg)):
            self.err(where, f"register sets banned name '{reg}'")
        for k in EXPR_KEYS:
            for e in aslist(t.get(k)):
                if isinstance(e, str):
                    self.jinja(f"{where} {k}", e, expr=True)
        if "delegate_to" in t:
            self.delegate(where, t["delegate_to"])
        blocks = [b for b in ("block", "rescue", "always") if b in t]
        mods = [k for k in t if k not in KEYWORDS and not str(k).startswith("with_")]
        if blocks:
            for b in blocks:
                self.task_list(f"{where}.{b}", t[b], ctx)
            if mods:
                self.err(where, f"unknown keys on block: {mods}")
            return
        if len(mods) != 1:
            self.err(where, f"expected exactly one module, got {mods}")
            return
        mk = mods[0]
        mod, args = norm(mk), t[mk]
        margs = dict(args) if isinstance(args, dict) else {}
        if isinstance(t.get("args"), dict):
            margs.update(t["args"])
        if mod in BANNED_MODULES:
            self.err(where, f"module '{mk}' not allowed")
        if mod == "meta" and args != "flush_handlers":
            self.err(where, f"meta '{args}' not allowed (only flush_handlers)")
        if mod in ("include_role", "import_role"):
            name = args.get("name") if isinstance(args, dict) else args
            if name != "bao_create_secret":
                self.err(where, f"{mod} '{name}' (only bao_create_secret)")
            if isinstance(args, dict):
                self.banned_keys(where, f"{mod} vars", args.get("vars"))
        if mod == "set_fact":
            keys = list(args) if isinstance(args, dict) else \
                (re.findall(r"(\S+?)=", args) if isinstance(args, str) else [])
            for k in keys:
                if BANNED_VAR.match(str(k)):
                    self.err(where, f"set_fact sets banned name '{k}'")
        if mod == "assert":
            for e in aslist(margs.get("that")):
                if isinstance(e, str):
                    self.jinja(f"{where} assert.that", e, expr=True)
        if t.get("check_mode") is False or str(t.get("check_mode")).lower() in ("false", "no"):
            ok = t.get("changed_when") is False or str(t.get("changed_when")).lower() == "false"
            ok = ok and mod in READONLY
            if mod == "uri":
                ok = ok and str(margs.get("method", "GET")).upper() == "GET"
            if not ok:
                self.err(where, "check_mode: false needs changed_when: false and a read-only module")
        if mod in ("copy", "template", "unarchive", "assemble"):
            if not isinstance(args, dict) and args is not None and not margs:
                self.err(where, f"{mod} needs mapping arguments")
            elif not truthy(margs.get("remote_src")) and margs.get("src") is not None:
                p = self.resolve(where, margs["src"], ctx, "templates" if mod == "template" else "files")
                if p and mod == "template" and os.path.isfile(p) and not p.endswith(".j2"):
                    self.template_file(os.path.relpath(p, ctx["root"]), p)
        if mod == "include_vars":
            f = args if isinstance(args, str) else margs.get("file")
            if "dir" in margs:
                self.err(where, "include_vars dir: not allowed")
            p = self.resolve(where, f, ctx, "vars")
            if p and os.path.isfile(p):
                try:
                    with open(p, encoding="utf-8") as fh:
                        self.banned_keys(where, "include_vars file", yaml.safe_load(fh) or {}, True)
                except (OSError, yaml.YAMLError, UnicodeDecodeError):
                    self.err(where, "include_vars file unreadable")
        if mod in ("include_tasks", "import_tasks"):
            f = args if isinstance(args, str) else margs.get("file")
            self.resolve(where, f, ctx, "tasks")

    # ----- files -----
    def template_file(self, rel, path):
        try:
            with open(path, encoding="utf-8") as f:
                self.jinja(rel, f.read())
        except (OSError, UnicodeDecodeError) as e:
            self.err(rel, f"unreadable: {e.__class__.__name__}")

    def check_app(self, root, app):
        app_dir = os.path.realpath(os.path.join(root, "catalog", app))
        ctx = {"root": root, "app_dir": app_dir, "role_dir": os.path.join(app_dir, "role")}
        for dp, dns, fns in os.walk(app_dir, followlinks=False):
            for d in list(dns):
                if os.path.islink(os.path.join(dp, d)):
                    self.err(os.path.relpath(os.path.join(dp, d), root), "symlink")
                    dns.remove(d)
            for fn in sorted(fns):
                p = os.path.join(dp, fn)
                rel = os.path.relpath(p, root)
                if os.path.islink(p):
                    self.err(rel, "symlink")
                    continue
                sub = os.path.relpath(p, app_dir).split(os.sep)
                if fn.endswith(".j2"):
                    self.template_file(rel, p)
                elif fn.endswith((".yml", ".yaml")):
                    try:
                        with open(p, encoding="utf-8") as f:
                            data = yaml.safe_load(f)
                    except (OSError, yaml.YAMLError, UnicodeDecodeError) as e:
                        self.err(rel, f"unparsable YAML: {e.__class__.__name__}")
                        continue
                    self.scalars(rel, data)
                    if sub[0] == "role" and len(sub) > 2:
                        if sub[1] in ("tasks", "handlers"):
                            self.task_list(rel, data, ctx)
                        elif sub[1] in ("vars", "defaults"):
                            self.banned_keys(rel, sub[1], data, extra_ansible=True)

    def check_deployments(self, root):
        ddir = os.path.join(root, "deployments")
        if not os.path.isdir(ddir):
            return
        for zone in sorted(os.listdir(ddir)):
            zdir = os.path.join(ddir, zone)
            if not os.path.isdir(zdir):
                continue
            for fn in sorted(os.listdir(zdir)):
                if not fn.endswith(".yaml"):
                    continue
                rel = f"deployments/{zone}/{fn}"
                try:
                    with open(os.path.join(zdir, fn), encoding="utf-8") as f:
                        data = yaml.safe_load(f)
                except (OSError, yaml.YAMLError, UnicodeDecodeError) as e:
                    self.err(rel, f"unparsable YAML: {e.__class__.__name__}")
                    continue
                if not isinstance(data, dict):
                    self.err(rel, "not a mapping")
                    continue
                for p, s in strings(data):
                    if any(m in s for m in MARKERS):
                        self.err(f"{rel}:{'.'.join(p)}", "Jinja in a deployment value")
                    if bad_str(s):
                        self.err(f"{rel}:{'.'.join(p)}", "contains '__' or is 'mro'")
                hosts = data.get("hosts") if isinstance(data.get("hosts"), list) else []
                self.banned_keys(rel, "vars", data.get("vars"), True)
                hv = data.get("host_vars")
                if hv is not None:
                    if not isinstance(hv, dict):
                        self.err(rel, "host_vars must be a mapping")
                    else:
                        for h, vals in hv.items():
                            if h not in hosts:
                                self.err(rel, f"host_vars key '{h}' is not a host of this deployment")
                            self.banned_keys(rel, f"host_vars.{h}", vals, True)


def load_allow(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def run(root, allow, apps=None):
    lint = Linter(allow)
    cdir = os.path.join(root, "catalog")
    if apps is None:
        apps = sorted(d for d in os.listdir(cdir) if os.path.isdir(os.path.join(cdir, d)) and not d.startswith(".")) \
            if os.path.isdir(cdir) else []
    for a in apps:
        lint.check_app(root, a)
    lint.check_deployments(root)
    return sorted(set(lint.errors))


def main(argv=None):
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="homelab-apps PR lint gate")
    ap.add_argument("--root", default=".")
    ap.add_argument("--allow", default=os.path.join(here, "..", "policy", "jinja-allow.yaml"))
    ap.add_argument("--app", action="append")
    a = ap.parse_args(argv)
    errors = run(a.root, load_allow(a.allow), a.app)
    for e in errors:
        print(f"ERROR {e}")
    print(f"lint gate: {len(errors)} finding(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
