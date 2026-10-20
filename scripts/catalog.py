#!/usr/bin/env python3
"""homelab-apps catalogue loader (migration step 5a, infra #127).

Layout (repo root):
  catalog/<app>/app.yaml            app definition (name, roles, health, ...)
  deployments/<zone>/<app>.yaml     instance: app, zone, hosts, vars
  environments.yaml                 optional: zone/environment metadata

Entry point:
  python3 scripts/catalog.py [--root DIR] validate
  python3 scripts/catalog.py [--root DIR] list
  python3 scripts/catalog.py [--root DIR] hosts [APP]
  python3 scripts/catalog.py [--root DIR] json
Exit 0 = ok, 1 = validation errors (printed one per line), 2 = usage.

Library: load(root) -> Catalog; Catalog.errors lists problems.
"""
import argparse
import json
import os
import re
import sys

import yaml

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
HOST_RE = re.compile(r"^hm1-[a-z]{2,4}-[a-z][a-z0-9]{1,7}[0-9]{2}$")


class Catalog:
    def __init__(self):
        self.apps = {}          # name -> dict (app.yaml)
        self.deployments = []   # list of dict, each with _path
        self.environments = {}
        self.errors = []

    def hosts(self, app=None):
        out = []
        for d in self.deployments:
            if app is None or d.get("app") == app:
                for h in d.get("hosts") or []:
                    if h not in out:
                        out.append(h)
        return sorted(out)

    def as_dict(self):
        return {
            "apps": self.apps,
            "deployments": [{k: v for k, v in d.items()} for d in self.deployments],
            "environments": self.environments,
        }


def _load_yaml(path, cat):
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        cat.errors.append(f"{path}: cannot read: {e}")
        return None
    if data is None:
        data = {}
    if not isinstance(data, dict):
        cat.errors.append(f"{path}: top level must be a mapping")
        return None
    return data


def load(root="."):
    cat = Catalog()
    cdir = os.path.join(root, "catalog")
    if os.path.isdir(cdir):
        for name in sorted(os.listdir(cdir)):
            if name.startswith("_") or name.startswith("."):
                continue  # _example and hidden dirs are not apps
            p = os.path.join(cdir, name, "app.yaml")
            if not os.path.isdir(os.path.join(cdir, name)):
                continue
            if not NAME_RE.match(name):
                cat.errors.append(f"catalog/{name}: invalid app name")
                continue
            if not os.path.isfile(p):
                cat.errors.append(f"catalog/{name}: missing app.yaml")
                continue
            data = _load_yaml(p, cat)
            if data is None:
                continue
            if data.get("name", name) != name:
                cat.errors.append(f"catalog/{name}/app.yaml: name '{data.get('name')}' != directory")
            data["name"] = name
            cat.apps[name] = data

    ddir = os.path.join(root, "deployments")
    if os.path.isdir(ddir):
        for zone in sorted(os.listdir(ddir)):
            zdir = os.path.join(ddir, zone)
            if not os.path.isdir(zdir) or zone.startswith("."):
                continue
            for fn in sorted(os.listdir(zdir)):
                if not fn.endswith(".yaml"):
                    continue
                rel = f"deployments/{zone}/{fn}"
                data = _load_yaml(os.path.join(zdir, fn), cat)
                if data is None:
                    continue
                stem = fn[:-5]
                app = data.get("app", stem)
                data["app"] = app
                if data.get("zone", zone) != zone:
                    cat.errors.append(f"{rel}: zone '{data.get('zone')}' != directory '{zone}'")
                data["zone"] = zone
                if app not in cat.apps:
                    cat.errors.append(f"{rel}: unknown app '{app}'")
                hosts = data.get("hosts")
                if not isinstance(hosts, list) or not hosts:
                    cat.errors.append(f"{rel}: hosts must be a non-empty list")
                    data["hosts"] = []
                else:
                    for h in hosts:
                        if not isinstance(h, str) or not HOST_RE.match(h):
                            cat.errors.append(f"{rel}: invalid host '{h}'")
                        elif h.split("-")[1] != zone:
                            cat.errors.append(f"{rel}: host '{h}' not in zone '{zone}'")
                data["_path"] = rel
                cat.deployments.append(data)

    ep = os.path.join(root, "environments.yaml")
    if os.path.isfile(ep):
        env = _load_yaml(ep, cat)
        if env is not None:
            cat.environments = env
    return cat


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=".")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate")
    sub.add_parser("list")
    hp = sub.add_parser("hosts")
    hp.add_argument("app", nargs="?")
    sub.add_parser("json")
    a = ap.parse_args(argv)
    cat = load(a.root)
    if a.cmd == "list":
        for d in cat.deployments:
            print(f"{d['app']}\t{d['zone']}\t{','.join(d['hosts'])}")
    elif a.cmd == "hosts":
        for h in cat.hosts(a.app):
            print(h)
    elif a.cmd == "json":
        print(json.dumps(cat.as_dict(), indent=2, sort_keys=True, default=str))
    for e in cat.errors:
        print(f"ERROR {e}", file=sys.stderr)
    return 1 if cat.errors else 0


if __name__ == "__main__":
    sys.exit(main())
