import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import guard  # noqa: E402

PROTECTED = """# comment
vmids:
  100: hm1-mgt-aiops01 (OpenClaw) [2 GB RAM, 4 vCPU]
  300: hm1-shr-cicd01 (DevOps plane)
  303: hm1-shr-vault01 (OpenBao)
templates: [9000, 9199]
bridges: [vmbr0]
subnets: [192.168.67.0/28]   # whole mgt zone
"""
NO_APPS = "vmids:\n  - 210   # hm1-dmz-app01\n"


def write(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)


def spec(root, zone, host, vmid, ip):
    write(root, f"terraform/stacks/hm1/{zone}/guests/{host}.yaml", f"hostname: {host}\nvmid: {vmid}\nip: {ip}\n")


def run_main(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = guard.main(argv)
    return rc, json.loads(buf.getvalue())


class Guard(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        r = self.infra = self.t.name
        spec(r, "wifi", "hm1-wifi-dns01", 501, "192.168.69.10")
        spec(r, "wifi", "hm1-wifi-dns02", 502, "192.168.69.11")
        spec(r, "shr", "hm1-shr-vault01", 303, "192.168.70.12")
        spec(r, "wifi", "hm1-wifi-tpl01", 9000, "192.168.69.99")
        spec(r, "wifi", "hm1-wifi-mgt01", 509, "192.168.67.10")
        spec(r, "dmz", "hm1-dmz-app01", 210, "192.168.60.10")
        spec(r, "dmz", "hm1-dmz-web01", 211, "192.168.60.11")
        write(r, "protected.yaml", PROTECTED)
        write(r, "no-apps.yaml", NO_APPS)
        self.specs = guard.load_specs(r)
        self.prot = guard.load_protected(os.path.join(r, "protected.yaml"))
        self.no_apps = guard.load_no_apps(os.path.join(r, "no-apps.yaml"))
        self.hosts = ["hm1-wifi-dns01", "hm1-wifi-dns02"]

    def tearDown(self):
        self.t.cleanup()

    def chk(self, hosts=None, zone="wifi", ah=None, dl=None):
        return guard.check(zone, hosts or self.hosts, self.specs, self.prot, ah, dl, self.no_apps)

    def test_protected_loaded(self):
        self.assertEqual(self.prot["vmids"], {100, 300, 303})
        self.assertEqual(self.prot["templates"], (9000, 9199))
        self.assertEqual([str(n) for n in self.prot["subnets"]], ["192.168.67.0/28"])

    def test_ok(self):
        self.assertEqual(self.chk(ah={"hm1-wifi-dns01": "192.168.69.10"}), [])

    def test_not_in_specs(self):
        self.assertTrue(self.chk(hosts=["hm1-wifi-dns09"]))

    def test_protected_vmid(self):
        self.assertTrue(any("protected" in e for e in self.chk(hosts=["hm1-shr-vault01"], zone="shr")))

    def test_template_vmid(self):
        self.assertTrue(any("template" in e for e in self.chk(hosts=["hm1-wifi-tpl01"])))

    def test_ansible_host_in_protected_subnet(self):
        errs = self.chk(ah={"hm1-wifi-dns01": "192.168.67.10"})
        self.assertTrue(any("protected subnet" in e for e in errs))
        self.assertTrue(any("!= spec ip" in e for e in errs))
        # also when the spec itself points into the mgt /28
        self.assertTrue(any("protected subnet" in e for e in self.chk(hosts=["hm1-wifi-mgt01"])))

    def test_ansible_host_mismatch(self):
        self.assertTrue(self.chk(ah={"hm1-wifi-dns01": "192.168.69.11"}))

    def test_zone_mismatch(self):
        self.assertTrue(any("zone" in e for e in self.chk(zone="iot")))

    def test_delegate_localhost_fails(self):  # N4: a delegate_to variable resolving to localhost
        for t in ("localhost", "127.0.0.1", "hm1-shr-cicd01"):
            self.assertTrue(self.chk(dl={"hm1-wifi-dns02": [t]}), t)

    def test_delegate_same_deployment_ok(self):
        self.assertEqual(self.chk(dl={"hm1-wifi-dns02": ["hm1-wifi-dns01"]}), [])

    def test_delegate_other_host_fails(self):
        self.assertTrue(self.chk(dl={"hm1-wifi-dns02": ["hm1-shr-vault01"]}))

    def test_cli(self):
        write(self.infra, "apps/deployments/wifi/adguard.yaml", "app: adguard\nhosts: [hm1-wifi-dns01, hm1-wifi-dns02]\n")
        base = ["--infra", self.infra, "--protected", os.path.join(self.infra, "protected.yaml"),
                "--deployment", os.path.join(self.infra, "apps/deployments/wifi/adguard.yaml")]
        self.assertEqual(guard.main(base + ["--host", "hm1-wifi-dns01=192.168.69.10"]), 0)
        self.assertEqual(guard.main(base + ["--delegate", "hm1-wifi-dns01=localhost"]), 1)
        self.assertEqual(guard.main(base + ["--host", "bad"]), 2)

    def test_empty_protected_fails_closed(self):
        write(self.infra, "p2.yaml", "vmids: {}\nsubnets: []\n")
        with self.assertRaises(ValueError):
            guard.load_protected(os.path.join(self.infra, "p2.yaml"))


class NoApps(Guard):
    """Faris's rule: VM 210 (hm1-dmz-app01) gets no apps (policy/no-apps.yaml)."""

    def dep_args(self, hosts, no_apps=None):
        write(self.infra, "apps/deployments/dmz/x.yaml", f"app: x\nhosts: [{', '.join(hosts)}]\n")
        argv = ["--infra", self.infra, "--protected", os.path.join(self.infra, "protected.yaml"),
                "--deployment", os.path.join(self.infra, "apps/deployments/dmz/x.yaml")]
        return argv + (["--no-apps", no_apps] if no_apps else [])

    def test_loaded(self):
        self.assertEqual(self.no_apps, {210})

    def test_repo_file_lists_210(self):
        self.assertIn(210, guard.load_no_apps(guard.DEFAULT_NO_APPS))

    def test_check_refuses_210(self):
        errs = self.chk(hosts=["hm1-dmz-app01"], zone="dmz")
        self.assertTrue(any("no apps" in e for e in errs), errs)
        self.assertEqual(self.chk(hosts=["hm1-dmz-web01"], zone="dmz"), [])

    def test_check_refuses_210_among_others(self):
        errs = self.chk(hosts=["hm1-dmz-web01", "hm1-dmz-app01"], zone="dmz")
        self.assertTrue(any("hm1-dmz-app01" in e and "no apps" in e for e in errs), errs)

    def test_cli_refuses_210_explicit_file(self):
        rc, out = run_main(self.dep_args(["hm1-dmz-app01"], os.path.join(self.infra, "no-apps.yaml")))
        self.assertEqual(rc, 1)
        self.assertFalse(out["ok"])
        self.assertTrue(any("no apps" in e for e in out["errors"]))

    def test_cli_refuses_210_default_file(self):  # no --no-apps: the repo's policy/no-apps.yaml is enforced
        rc, out = run_main(self.dep_args(["hm1-dmz-app01"]))
        self.assertEqual(rc, 1)
        self.assertFalse(out["ok"])
        rc, out = run_main(self.dep_args(["hm1-dmz-web01"]))
        self.assertEqual(rc, 0, out)

    def test_cli_missing_file_fails_closed(self):
        rc, out = run_main(self.dep_args(["hm1-dmz-web01"], os.path.join(self.infra, "nope.yaml")))
        self.assertEqual(rc, 2)
        self.assertFalse(out["ok"])

    def test_cli_bad_file_fails_closed(self):
        for i, text in enumerate(("vmids: [\n", "", "vmids: []\n", "- 210\n", "vmids: [abc]\n", "vmids: [true]\n",
                                  "vmids: 210\n", "other: [210]\n")):
            p = os.path.join(self.infra, f"bad{i}.yaml")
            write(self.infra, f"bad{i}.yaml", text)
            rc, out = run_main(self.dep_args(["hm1-dmz-web01"], p))
            self.assertEqual(rc, 2, text)
            self.assertFalse(out["ok"], text)


if __name__ == "__main__":
    unittest.main()
