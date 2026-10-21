"""bao_create_secret - the only way a catalogue role writes a secret (proposal 5.1). Pipeline-owned, Class C.

Runs on CT 300 (delegate_to localhost) as runner-apps-play, from <run dir>/src/apps/playbooks/roles/bao_create_secret/.
Request = one JSON object on stdin, built by tasks/main.yml from the caller's role vars:
  {"host": inventory_hostname, "path": "<zone>/secret/apps/<app>/<host>", "keys": [...],
   "data": {key: value, ...}, "generate": {"key": k, "length": 16..128, "chars": [...]} | null, "hash": "bcrypt" | null}
Trusted inputs come only from the run dir that holds this file (written by ci-deploy.sh as runner-apps), never from
variables or the environment a caller can set: app and zone from inventory.yml, the host's zone from the infra specs
(src/infra), allowed paths and keys from catalog/<app>/app.yaml `catalog.secrets`, the zone mode from `env`, the
child token from `token`. The OpenBao address and CA are fixed.
Write: KV v2 create-only (cas=0). Output (stdout, JSON): {"status": "created" | "exists", "path": ..., "hash": ...};
`hash` is the bcrypt hash of the generated value when asked for, else null. Plaintext never leaves this process and
no message contains a value. Exit 1 = refused or failed (stderr says why).
"""
import json
import os
import re
import secrets
import string
import sys
import urllib.error
import urllib.request

import yaml

BAO_ADDR = "https://192.168.70.12:8200"
CA = "/etc/ssl/devops/openbao-ca.crt"
RUN_DIR = re.compile(r"^/run/devops/apps/[0-9]+$")
HOST = re.compile(r"^hm1-(dmz|shr|int|stg|wifi|iot)-[a-z][a-z0-9]*[0-9]{2}$")
NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")                  # secret key names
SEG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")                 # path segments after apps/<app>
CHARS = {"ascii_letters": string.ascii_letters, "ascii_lowercase": string.ascii_lowercase,
         "ascii_uppercase": string.ascii_uppercase, "digits": string.digits, "hexdigits": "0123456789abcdef",
         "punctuation": "-_.,:;+=@%"}                          # shell/YAML-safe subset
REQUEST_KEYS = {"host", "path", "keys", "data", "generate", "hash"}


class Refused(Exception):
    pass


def load_yaml(path):
    if os.path.islink(path) or not os.path.isfile(path):
        raise Refused(f"{os.path.basename(path)}: missing or not a regular file")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def context(rd, host):
    """-> (app, zone) from the run dir, after checking host against hosts, inventory and the infra spec."""
    inv = load_yaml(os.path.join(rd, "inventory.yml"))
    v = ((inv.get("all") or {}).get("vars") or {})
    app, zone = v.get("apps_deployment_app"), v.get("apps_deployment_zone")
    if not (isinstance(app, str) and isinstance(zone, str) and app and zone):
        raise Refused("inventory.yml: no apps_deployment_app / apps_deployment_zone")
    with open(os.path.join(rd, "hosts"), encoding="utf-8") as f:
        if host not in [h.strip() for h in f]:
            raise Refused(f"{host} is not a host of this app play")
    sys.path.insert(0, os.path.join(rd, "src", "infra", "scripts"))
    from specs import load_specs   # noqa: E402  infra main, copied into the run dir by ci-deploy.sh
    spec = [s for s in load_specs(os.path.join(rd, "src", "infra")) if s.get("hostname") == host]
    if len(spec) != 1:
        raise Refused(f"{host}: {len(spec)} infra specs")
    if spec[0]["_zone"] != zone or host.split("-")[1] != zone:
        raise Refused(f"{host}: spec zone {spec[0]['_zone']} is not the deployment zone {zone}")
    return app, zone


def zone_mode(rd):
    with open(os.path.join(rd, "env"), encoding="utf-8") as f:
        modes = [line.strip().split("=", 1)[1] for line in f if line.startswith("BAO_ZONE_MODE=")]
    if modes not in (["namespace"], ["mount"]):
        raise Refused("env: BAO_ZONE_MODE must be set once to namespace or mount")
    return modes[0]


def resolve(entry, app, zone, host, req):
    """-> (kv path 'secret/apps/<app>[/...]', full path, keys) after matching the request to catalog.secrets."""
    allowed = [s for s in ((entry.get("catalog") or {}).get("secrets") or []) if isinstance(s, dict)]
    match = [s for s in allowed if s.get("path") == req["path"]]
    if len(match) != 1:
        raise Refused(f"path {req['path']!r} is not listed once in catalog/{app}/app.yaml catalog.secrets")
    full = req["path"].replace("<zone>", zone).replace("<host>", host)
    parts = full.split("/")
    if parts[:4] != [zone, "secret", "apps", app] or not all(SEG.match(p) for p in parts[4:]):
        raise Refused(f"path {full!r} is not under {zone}/secret/apps/{app}/")
    keys = req["keys"]
    if (not isinstance(keys, list) or not keys or len(set(keys)) != len(keys) or
            not all(isinstance(k, str) and NAME.match(k) for k in keys)):
        raise Refused("keys: a non-empty list of distinct names")
    if sorted(keys) != sorted(match[0].get("keys") or []):
        raise Refused(f"keys {sorted(keys)} are not the catalogue keys {sorted(match[0].get('keys') or [])}")
    return "/".join(parts[1:]), full, keys


def build(req, keys):
    """-> (data, plaintext of the generated key or None). Values are never echoed."""
    data, gen, hsh = req.get("data") or {}, req.get("generate"), req.get("hash")
    if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) and v for k, v in data.items()):
        raise Refused("data: a mapping of key -> non-empty string")
    out, pw = dict(data), None
    if gen is not None:
        if not isinstance(gen, dict) or set(gen) - {"key", "length", "chars"}:
            raise Refused("generate: {key, length, chars}")
        k, n, chars = gen.get("key"), gen.get("length", 28), gen.get("chars", ["ascii_letters", "digits"])
        if k not in keys or k in data:
            raise Refused("generate.key must be one of keys and not in data")
        if not isinstance(n, int) or isinstance(n, bool) or not 16 <= n <= 128:
            raise Refused("generate.length: 16..128")
        if not isinstance(chars, list) or not chars or any(c not in CHARS for c in chars):
            raise Refused(f"generate.chars: a list of {sorted(CHARS)}")
        alphabet = "".join(sorted(set("".join(CHARS[c] for c in chars))))
        pw = "".join(secrets.choice(alphabet) for _ in range(n))
        out[k] = pw
    if hsh not in (None, "bcrypt"):
        raise Refused("hash: bcrypt or null")
    if hsh and pw is None:
        raise Refused("hash needs generate (only a generated value is hashed)")
    if sorted(out) != sorted(keys):
        raise Refused(f"data + generate give keys {sorted(out)}, not {sorted(keys)}")
    return out, pw


def bcrypt_hash(pw):
    try:
        import bcrypt
    except ImportError:
        raise Refused("hash: bcrypt is not installed in /opt/ansible-apps/venv (infra host/apps-runner)") from None
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def write(addr, ca, mode, zone, kv, token, data, opener=None):
    """KV v2 create-only. -> 'created' | 'exists'. Raises Refused on anything else."""
    rest = kv.split("/", 1)[1]                                 # kv = "secret/apps/<app>/..." -> apps/<app>/...
    mount = "secret" if mode == "namespace" else f"secret-{zone}"
    headers = {"X-Vault-Token": token, "Content-Type": "application/json"}
    if mode == "namespace":
        headers["X-Vault-Namespace"] = zone
    req = urllib.request.Request(f"{addr}/v1/{mount}/data/{rest}", method="POST", headers=headers,
                                 data=json.dumps({"options": {"cas": 0}, "data": data}).encode())
    if opener is None:
        import ssl
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ca)))
    try:
        with opener.open(req, timeout=30) as r:
            if r.status in (200, 204):
                return "created"
            raise Refused(f"OpenBao answered {r.status}")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        if e.code == 400 and "check-and-set" in body:
            return "exists"
        if e.code == 403:   # create-only token: also what OpenBao answers when the secret already exists
            raise Refused("OpenBao 403: the secret exists already (token is create-only) or the path is not this "
                          "app's; the guest marker is gone, so the stored value is not reused (see role README)") from None
        raise Refused(f"OpenBao {e.code}: {body[:160]}") from None
    except urllib.error.URLError as e:
        raise Refused(f"OpenBao unreachable: {e.reason}") from None


def run(rd, req, addr=BAO_ADDR, ca=CA, opener=None):
    if not isinstance(req, dict) or set(req) - REQUEST_KEYS or not {"host", "path", "keys"} <= set(req):
        raise Refused(f"request keys must be {sorted(REQUEST_KEYS)} (host, path, keys required)")
    host = req["host"]
    if not isinstance(host, str) or not HOST.match(host) or not isinstance(req["path"], str):
        raise Refused("host / path malformed")
    app, zone = context(rd, host)
    entry = load_yaml(os.path.join(rd, "src", "apps", "catalog", app, "app.yaml"))
    if entry.get("name") != app:
        raise Refused(f"catalog/{app}/app.yaml: name is not {app}")
    kv, full, keys = resolve(entry, app, zone, host, req)
    data, pw = build(req, keys)
    hsh = bcrypt_hash(pw) if req.get("hash") else None        # before the write: no secret stored without its hash
    with open(os.path.join(rd, "token"), encoding="utf-8") as f:
        token = f.read().strip()
    if not token:
        raise Refused("no child token (check mode, or ci-deploy.sh did not mint one)")
    status = write(addr, ca, zone_mode(rd), zone, kv, token, data, opener)
    return {"status": status, "path": full, "hash": hsh if status == "created" else None}


def main():
    rd = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), *[".."] * 6))
    try:
        if not RUN_DIR.match(rd) or os.path.islink(rd):
            raise Refused(f"not inside a run dir /run/devops/apps/<id> ({rd})")
        req = json.loads(sys.stdin.read())
        print(json.dumps(run(rd, req)))
        return 0
    except Refused as e:
        print(f"bao_create_secret: refused: {e}", file=sys.stderr)
    except (OSError, ValueError, KeyError, yaml.YAMLError) as e:
        print(f"bao_create_secret: failed: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
