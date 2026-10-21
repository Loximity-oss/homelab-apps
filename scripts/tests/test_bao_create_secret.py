"""Tests for playbooks/roles/bao_create_secret/files/bao_create_secret.py (no OpenBao, no network)."""
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "..", "playbooks", "roles", "bao_create_secret", "files", "bao_create_secret.py")
spec = importlib.util.spec_from_file_location("bcs", SCRIPT)
bcs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bcs)

HOST = "hm1-wifi-dns02"
APP_YAML = """\
name: adguard
catalog:
  secrets:
    - {path: "<zone>/secret/apps/adguard/<host>", keys: [username, password], by: role}
    - {path: "<zone>/secret/apps/adguard/escrow", keys: [token], by: role}
    - {path: "wifi/secret/apps/k3s/x", keys: [token], by: role}
"""
SPECS_PY = """\
import pathlib, yaml
def load_specs(root):
    out = []
    for p in sorted(pathlib.Path(root).glob("terraform/stacks/*/*/guests/*.yaml")):
        s = yaml.safe_load(p.read_text()) or {}
        s["_zone"] = p.parent.parent.name
        out.append(s)
    return out
"""


def put(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)


class Resp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Opener:
    """Records the request; answers with a status or an HTTPError(code, body)."""
    def __init__(self, status=200, error=None):
        self.status, self.error, self.reqs = status, error, []

    def open(self, req, timeout=None):
        self.reqs.append(req)
        if self.error:
            code, body = self.error
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO(body.encode()))
        return Resp(self.status)


class Base(unittest.TestCase):
    def setUp(self):
        self.rd = tempfile.mkdtemp()
        inv = {"all": {"vars": {"apps_deployment_app": "adguard", "apps_deployment_zone": "wifi"}}}
        put(self.rd, "inventory.yml", json.dumps(inv))
        put(self.rd, "hosts", HOST + "\n")
        put(self.rd, "env", "APPS_APP=adguard\nBAO_ZONE_MODE=namespace\n")
        put(self.rd, "token", "child-token\n")
        put(self.rd, "src/infra/scripts/specs.py", SPECS_PY)
        put(self.rd, f"src/infra/terraform/stacks/hm1/wifi/guests/{HOST}.yaml", f"hostname: {HOST}\nvmid: 902\n")
        put(self.rd, "src/apps/catalog/adguard/app.yaml", APP_YAML)

    def tearDown(self):
        shutil.rmtree(self.rd)

    def req(self, **kw):
        r = {"host": HOST, "path": "<zone>/secret/apps/adguard/<host>", "keys": ["username", "password"],
             "data": {"username": "admin"}, "generate": {"key": "password", "length": 28,
                                                         "chars": ["ascii_letters", "digits"]}, "hash": None}
        r.update(kw)
        return r

    def run_ok(self, opener=None, **kw):
        o = opener or Opener()
        return bcs.run(self.rd, self.req(**kw), addr="https://bao.test", ca="/dev/null", opener=o), o

    def refused(self, msg, **kw):
        with self.assertRaises(bcs.Refused) as c:
            self.run_ok(**kw)
        self.assertIn(msg, str(c.exception))
        return str(c.exception)


class Write(Base):
    def test_created_namespace_mode(self):
        out, o = self.run_ok()
        self.assertEqual(out, {"status": "created", "path": f"wifi/secret/apps/adguard/{HOST}", "hash": None})
        r = o.reqs[0]
        self.assertEqual(r.full_url, f"https://bao.test/v1/secret/data/apps/adguard/{HOST}")
        self.assertEqual(r.get_header("X-vault-namespace"), "wifi")
        self.assertEqual(r.get_header("X-vault-token"), "child-token")
        body = json.loads(r.data)
        self.assertEqual(body["options"], {"cas": 0})
        self.assertEqual(body["data"]["username"], "admin")
        pw = body["data"]["password"]
        self.assertEqual(len(pw), 28)
        self.assertTrue(pw.isalnum())

    def test_mount_mode(self):
        put(self.rd, "env", "BAO_ZONE_MODE=mount\n")
        _, o = self.run_ok()
        self.assertEqual(o.reqs[0].full_url, f"https://bao.test/v1/secret-wifi/data/apps/adguard/{HOST}")
        self.assertIsNone(o.reqs[0].get_header("X-vault-namespace"))

    def test_cas_mismatch_is_exists(self):
        out, _ = self.run_ok(Opener(error=(400, '{"errors":["check-and-set parameter did not match"]}')))
        self.assertEqual(out["status"], "exists")
        self.assertIsNone(out["hash"])

    def test_403_fails_closed(self):
        with self.assertRaises(bcs.Refused) as c:
            self.run_ok(Opener(error=(403, '{"errors":["permission denied"]}')))
        self.assertIn("403", str(c.exception))

    def test_other_http_error(self):
        with self.assertRaises(bcs.Refused):
            self.run_ok(Opener(error=(500, "boom")))

    def test_data_only(self):
        """escrow shape (k3s): values read from the guest, nothing generated, no hash."""
        out, o = self.run_ok(path="<zone>/secret/apps/adguard/escrow", keys=["token"], data={"token": "abc"},
                             generate=None)
        self.assertEqual(out, {"status": "created", "path": "wifi/secret/apps/adguard/escrow", "hash": None})
        self.assertEqual(json.loads(o.reqs[0].data)["data"], {"token": "abc"})


class Refusals(Base):
    def test_path_not_in_catalog(self):
        self.refused("not listed", path="<zone>/secret/apps/adguard/other")

    def test_other_app_path(self):
        self.refused("not under wifi/secret/apps/adguard/", path="wifi/secret/apps/k3s/x", keys=["token"],
                     data={"token": "abc"}, generate=None)

    def test_caller_zone_ignored(self):
        """include_role vars like deploy_zone/zone never reach the script; a request key 'zone' is refused."""
        self.refused("request keys", zone="int")

    def test_host_not_in_play(self):
        self.refused("not a host of this app play", host="hm1-wifi-dns01")

    def test_spec_zone_mismatch(self):
        os.remove(os.path.join(self.rd, f"src/infra/terraform/stacks/hm1/wifi/guests/{HOST}.yaml"))
        put(self.rd, f"src/infra/terraform/stacks/hm1/iot/guests/{HOST}.yaml", f"hostname: {HOST}\n")
        self.refused("is not the deployment zone")

    def test_no_spec(self):
        os.remove(os.path.join(self.rd, f"src/infra/terraform/stacks/hm1/wifi/guests/{HOST}.yaml"))
        self.refused("0 infra specs")

    def test_keys_must_match_catalog(self):
        self.refused("not the catalogue keys", keys=["password"], data={})

    def test_generate_key_in_data(self):
        self.refused("generate.key", data={"username": "a", "password": "b"})

    def test_missing_key(self):
        self.refused("data + generate give keys", generate=None)

    def test_bad_chars(self):
        self.refused("generate.chars", generate={"key": "password", "length": 20, "chars": ["emoji"]})

    def test_short_length(self):
        self.refused("16..128", generate={"key": "password", "length": 8})

    def test_hash_needs_generate(self):
        self.refused("hash needs generate", data={"username": "a", "password": "b"}, generate=None, hash="bcrypt")

    def test_bad_hash(self):
        self.refused("hash:", hash="md5")

    def test_empty_token(self):
        put(self.rd, "token", "")
        self.refused("no child token")

    def test_bad_zone_mode(self):
        put(self.rd, "env", "BAO_ZONE_MODE=namespace\nBAO_ZONE_MODE=mount\n")
        self.refused("BAO_ZONE_MODE")

    def test_symlinked_catalog(self):
        p = os.path.join(self.rd, "src/apps/catalog/adguard/app.yaml")
        os.rename(p, p + ".real")
        os.symlink(p + ".real", p)
        self.refused("not a regular file")

    def test_segment_escape(self):
        put(self.rd, "src/apps/catalog/adguard/app.yaml",
            APP_YAML.replace("<zone>/secret/apps/adguard/<host>", "<zone>/secret/apps/adguard/../k3s"))
        self.refused("not under", path="<zone>/secret/apps/adguard/../k3s")

    def test_no_value_in_messages(self):
        msg = self.refused("not the catalogue keys", keys=["username"], data={"username": "s3cr3t-value"},
                           generate=None)
        self.assertNotIn("s3cr3t-value", msg)


class Hash(Base):
    def test_bcrypt(self):
        try:
            import bcrypt
        except ImportError:
            msg = self.refused("bcrypt is not installed", hash="bcrypt")
            self.assertIn("/opt/ansible-apps/venv", msg)
            return
        out, o = self.run_ok(hash="bcrypt")
        pw = json.loads(o.reqs[0].data)["data"]["password"]
        self.assertTrue(out["hash"].startswith("$2b$"))
        self.assertTrue(bcrypt.checkpw(pw.encode(), out["hash"].encode()))
        self.assertNotIn(pw, json.dumps(out))


class Main(unittest.TestCase):
    def test_refuses_outside_run_dir(self):
        r = subprocess.run([sys.executable, "-I", SCRIPT], input="{}", capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not inside a run dir", r.stderr)
        self.assertEqual(r.stdout, "")


if __name__ == "__main__":
    unittest.main()
