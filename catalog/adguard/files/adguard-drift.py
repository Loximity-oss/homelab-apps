#!/usr/bin/env python3
"""Compare the managed AdGuard config with the live one (installed by the adguard role, run as root on the guest).

Usage: hm1-adguard-drift MANAGED.yaml LIVE.yaml

Every key the managed file sets must be present with the same value in the live file (dicts: subset match, so keys
AdGuard adds itself, such as filter last_updated, are ignored; lists: same length, item by item). Duration strings
(720h, 30d, 168h0m0s) are compared by value, because AdGuard rewrites them in its own format (168h -> 7d). Prints only the
PATHS that differ (never values: the config holds the admin hash). Exit 0 = in sync, 1 = drift, 2 = cannot compare.
"""
import re
import sys

import yaml

DURATION = re.compile(r"^(?:\d+(?:d|h|m|s))+$")
PART = re.compile(r"(\d+)(d|h|m|s)")
UNIT = {"d": 86400, "h": 3600, "m": 60, "s": 1}


def seconds(v):
    """Duration string -> seconds; anything else -> None."""
    if isinstance(v, str) and DURATION.match(v):
        return sum(int(n) * UNIT[u] for n, u in PART.findall(v))
    return None


def same_scalar(m, live):
    if m == live:
        return True
    sm, sl = seconds(m), seconds(live)
    return sm is not None and sl is not None and sm == sl


def diff(m, live, path, out):
    if isinstance(m, dict):
        if not isinstance(live, dict):
            out.append(path or "/")
            return
        for k, v in m.items():
            if k not in live:
                out.append(f"{path}.{k}")
            else:
                diff(v, live[k], f"{path}.{k}", out)
    elif isinstance(m, list):
        if live is None:
            live = []
        if not isinstance(live, list) or len(m) != len(live):
            out.append(path)
            return
        for i, (a, b) in enumerate(zip(m, live)):
            diff(a, b, f"{path}[{i}]", out)
    elif not same_scalar(m, live):
        out.append(path)


def main():
    try:
        with open(sys.argv[1]) as f:
            managed = yaml.safe_load(f)
        with open(sys.argv[2]) as f:
            live = yaml.safe_load(f)
    except Exception as e:  # unreadable or invalid YAML counts as 'cannot compare'
        print(f"cannot compare: {type(e).__name__}")
        return 2
    out = []
    diff(managed, live, "", out)
    for p in out:
        print(p.lstrip("."))
    return 1 if out else 0


if __name__ == "__main__":
    sys.exit(main())
