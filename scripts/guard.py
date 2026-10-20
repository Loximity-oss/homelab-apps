#!/usr/bin/env python3
"""homelab-apps deploy guard (proposal 5.1 'Deploy guard', N4; migration step 5a, infra #127).
Defence in depth before any role runs: every target and every resolved delegate_to target must be
  - a host of THIS deployment and present in the infra specs (terraform/stacks/hm1/<zone>/guests/<host>.yaml),
  - not a protected VMID and not in the template range (homelab-standards protected.yaml `vmids`, `templates`),
  - ansible_host == spec IP, and neither in a protected.yaml `subnets` network (e.g. 192.168.67.10 in the mgt /28),
  - in the deployment's zone; never localhost / loopback / the controller.

Entry point:
  python3 scripts/guard.py --infra INFRA_CLONE --protected STANDARDS/protected.yaml \
      --deployment deployments/<zone>/<app>.yaml [--host HOST=ANSIBLE_HOST ...] [--delegate HOST=TARGET ...]
  --host: the resolved inventory ansible_host per host (default: spec IP, i.e. only the spec checks).
  --delegate: a resolved delegate_to target seen for HOST (repeatable).
Prints JSON {"ok": bool, "errors": [...]}; exit 0 = ok, 1 = refused, 2 = usage/unreadable input (fail closed).

Library: load_specs(infra), load_protected(path), check(zone, hosts, specs, protected, ansible_hosts, delegates).
"""
import argparse
import glob
import ipaddress
import json
import os
import sys

import yaml

LOCAL = {"localhost", "127.0.0.1", "::1", "ip6-localhost", "hm1-shr-cicd01"}


def load_specs(infra):
    specs = {}
    for p in sorted(glob.glob(os.path.join(infra, "terraform", "stacks", "hm1", "*", "guests", "*.yaml"))):
        zone = p.split(os.sep)[-3]
        with open(p, encoding="utf-8") as f:
            d = yaml.safe_load(f) or {}
        host = d.get("hostname") or os.path.basename(p)[:-5]
        specs[host] = {"vmid": d.get("vmid"), "ip": str(d.get("ip", "")).split("/")[0], "zone": zone}
    return specs


def load_protected(path):
    with open(path, encoding="utf-8") as f:
        d = yaml.safe_load(f) or {}
    vmids = {int(v) for v in (d.get("vmids") or {})}
    tpl = d.get("templates") or []
    subnets = [ipaddress.ip_network(str(s), strict=False) for s in (d.get("subnets") or [])]
    if not vmids or not subnets:
        raise ValueError("protected.yaml: vmids and subnets must be non-empty")
    return {"vmids": vmids, "templates": (int(tpl[0]), int(tpl[1])) if len(tpl) == 2 else None, "subnets": subnets}


def _ip_errors(label, ip, protected):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return [f"{label}: '{ip}' is not an IP address"]
    errs = []
    if a.is_loopback or a.is_unspecified:
        errs.append(f"{label}: {ip} is loopback/unspecified")
    for n in protected["subnets"]:
        if a.version == n.version and a in n:
            errs.append(f"{label}: {ip} is in protected subnet {n}")
    return errs


def check_host(host, zone, specs, protected, ansible_host=None):
    if not isinstance(host, str) or host.lower() in LOCAL:
        return [f"{host}: local/controller host"]
    s = specs.get(host)
    if s is None:
        return [f"{host}: not in infra specs"]
    errs = []
    try:
        vmid = int(s["vmid"])
    except (TypeError, ValueError):
        vmid = None
        errs.append(f"{host}: spec has no valid vmid")
    if vmid is not None and vmid in protected["vmids"]:
        errs.append(f"{host}: VMID {vmid} is protected")
    t = protected["templates"]
    if vmid is not None and t and t[0] <= vmid <= t[1]:
        errs.append(f"{host}: VMID {vmid} is in the template range")
    if s["zone"] != zone:
        errs.append(f"{host}: spec zone '{s['zone']}' != deployment zone '{zone}'")
    errs += _ip_errors(f"{host} spec ip", s["ip"], protected)
    ah = s["ip"] if ansible_host is None else str(ansible_host)
    if ah != s["ip"]:
        errs.append(f"{host}: ansible_host {ah} != spec ip {s['ip']}")
    if ah != s["ip"] or ansible_host is not None:
        errs += [e for e in _ip_errors(f"{host} ansible_host", ah, protected) if e not in errs]
    return errs


def check(zone, hosts, specs, protected, ansible_hosts=None, delegates=None):
    ansible_hosts = ansible_hosts or {}
    errs = []
    if not hosts:
        errs.append("deployment has no hosts")
    for h in hosts:
        errs += check_host(h, zone, specs, protected, ansible_hosts.get(h))
    for h in ansible_hosts:
        if h not in hosts:
            errs.append(f"{h}: --host given but not a host of this deployment")
    for h, targets in (delegates or {}).items():
        if h not in hosts:
            errs.append(f"{h}: delegate source not a host of this deployment")
        for t in targets:
            ts = str(t).strip()
            if ts.lower() in LOCAL:
                errs.append(f"{h}: delegate_to '{ts}' is local/controller")
            elif ts not in hosts:
                errs.append(f"{h}: delegate_to '{ts}' is not a host of this deployment")
            else:
                errs += [e for e in check_host(ts, zone, specs, protected, ansible_hosts.get(ts)) if e not in errs]
    return errs


def _pairs(items, flag):
    out = {}
    for it in items or []:
        k, sep, v = it.partition("=")
        if not sep or not k or not v:
            raise ValueError(f"{flag} expects HOST=VALUE, got '{it}'")
        out.setdefault(k, []).append(v)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="homelab-apps deploy guard")
    ap.add_argument("--infra", required=True)
    ap.add_argument("--protected", required=True)
    ap.add_argument("--deployment", required=True)
    ap.add_argument("--host", action="append")
    ap.add_argument("--delegate", action="append")
    a = ap.parse_args(argv)
    try:
        with open(a.deployment, encoding="utf-8") as f:
            dep = yaml.safe_load(f)
        if not isinstance(dep, dict):
            raise ValueError("deployment is not a mapping")
        zone = os.path.basename(os.path.dirname(os.path.abspath(a.deployment)))
        hosts = dep.get("hosts") if isinstance(dep.get("hosts"), list) else []
        specs = load_specs(a.infra)
        protected = load_protected(a.protected)
        ah = {k: v[-1] for k, v in _pairs(a.host, "--host").items()}
        dl = _pairs(a.delegate, "--delegate")
    except (OSError, ValueError, yaml.YAMLError) as e:
        print(json.dumps({"ok": False, "errors": [f"input: {e}"]}))
        return 2
    errs = check(zone, hosts, specs, protected, ah, dl)
    if dep.get("zone", zone) != zone:
        errs.append(f"deployment zone '{dep.get('zone')}' != directory '{zone}'")
    print(json.dumps({"ok": not errs, "errors": errs}, indent=2))
    return 0 if not errs else 1


if __name__ == "__main__":
    sys.exit(main())
