import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import classify as cl  # noqa: E402

APP = """name: adguard
owner: cfg
catalog: {summary: DNS, size: small, role: app_adguard, status: stable}
rollout: {serial: 1, stop_on_failure: true}
version:
  pin: 0.107.50
  source: github
  known:
    0.107.50: {sha256: aaa}
"""
DEP = "app: adguard\nhosts: [hm1-wifi-dns01]\nwave: 2\nversion: 0.107.50\nvars: {a: 1}\n"


def w(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(text)


class Base(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.b = os.path.join(self.t.name, "b")
        self.h = os.path.join(self.t.name, "h")
        for r in (self.b, self.h):
            w(r, "catalog/adguard/app.yaml", APP)
            w(r, "deployments/wifi/adguard.yaml", DEP)

    def tearDown(self):
        self.t.cleanup()

    def one(self, path, st="M", om="100644", nm="100644", old=None):
        ch = [(st, om, nm, old or (None if st == "A" else path), None if st == "D" else path)]
        return cl.classify(self.b, self.h, ch)[0]


class Paths(Base):
    def test_matcher(self):
        self.assertTrue(cl.path_match("catalog/*/role/{tasks,templates}/**", "catalog/adguard/role/tasks/main.yml"))
        self.assertTrue(cl.path_match("catalog/*/files/**", "catalog/adguard/files/sub/x.j2"))
        self.assertFalse(cl.path_match("catalog/*/files/**", "catalog/a/b/files/x"))
        self.assertFalse(cl.path_match("docs/**", "docs/../policy/x"))
        self.assertTrue(cl.path_match("README.md", "README.md"))

    def test_class_a(self):
        for p in ("docs/x.md", "README.md", "catalog/adguard/docs/runbook.md"):
            self.assertEqual(self.one(p), "A", p)

    def test_class_b(self):
        for p in ("catalog/adguard/role/tasks/main.yml", "catalog/adguard/role/defaults/main.yml",
                  "catalog/adguard/role/handlers/main.yml", "catalog/adguard/role/templates/AdGuardHome.yaml.j2",
                  "catalog/adguard/files/sub/x.j2"):
            self.assertEqual(self.one(p), "B", p)

    def test_class_c(self):
        for p in (".gitea/workflows/pr.yml", "policy/classify.py", "scripts/catalog.py", "playbooks/deploy.yml",
                  "ansible.cfg", "requirements.yml", ".gitleaks.toml", ".yamllint", ".ansible-lint",
                  "catalog/adguard/tests/l4.yml", "catalog/adguard/role/meta/main.yml",
                  "catalog/adguard/role/library/x.py", "catalog/adguard/role/filter_plugins/f.py",
                  "catalog/adguard/role/lookup_plugins/l.py", "catalog/adguard/role/action_plugins/a.py",
                  "catalog/adguard/role/module_utils/m.py", "catalog/adguard/role/vars/main.yml",
                  "environments.yaml", "unknown/file"):
            self.assertEqual(self.one(p), "C", p)

    def test_symlink_submodule(self):
        self.assertEqual(self.one("docs/x", nm="120000"), "C")
        self.assertEqual(self.one("docs/x", st="A", om="000000", nm="160000"), "C")

    def test_rename_judges_both(self):
        self.assertEqual(self.one("docs/x.md", st="R100", old="policy/x.md"), "C")
        self.assertEqual(self.one("docs/y.md", st="R100", old="docs/x.md"), "A")

    def test_new_deleted_entry(self):
        self.assertEqual(self.one("catalog/k3s/app.yaml", st="A"), "C")
        self.assertEqual(self.one("deployments/iot/adguard.yaml", st="A"), "C")
        self.assertEqual(self.one("deployments/wifi/adguard.yaml", st="D"), "C")

    def test_empty(self):
        self.assertEqual(cl.classify(self.b, self.h, [])[0], "A")


class AppLeaves(Base):
    def app(self, text):
        w(self.h, "catalog/adguard/app.yaml", text)
        return self.one("catalog/adguard/app.yaml")

    def test_b_leaves(self):
        self.assertEqual(self.app(APP.replace("summary: DNS", "summary: DNS filter")), "B")
        self.assertEqual(self.app(APP.replace("owner: cfg", "owner: infra")), "B")
        self.assertEqual(self.app(APP.replace("serial: 1", "serial: 2")), "B")

    def test_c_leaves(self):
        self.assertEqual(self.app(APP.replace("role: app_adguard", "role: x")), "C")
        self.assertEqual(self.app(APP.replace("status: stable", "status: beta")), "C")
        self.assertEqual(self.app(APP.replace("stop_on_failure: true", "stop_on_failure: false")), "C")
        self.assertEqual(self.app(APP.replace("source: github", "source: x")), "C")
        self.assertEqual(self.app(APP + "health: {cmd: x}\n"), "C")

    def test_minor_bump(self):
        t = APP.replace("pin: 0.107.50", "pin: 0.108.0") + "    0.108.0: {sha256: bbb}\n"
        self.assertEqual(self.app(t), "B")

    def test_major_bump(self):
        t = APP.replace("pin: 0.107.50", "pin: 1.0.0") + "    1.0.0: {sha256: bbb}\n"
        self.assertEqual(self.app(t), "C")

    def test_pin_not_known(self):
        self.assertEqual(self.app(APP.replace("pin: 0.107.50", "pin: 0.107.51")), "C")

    def test_sha_changed(self):
        self.assertEqual(self.app(APP.replace("sha256: aaa", "sha256: zzz")), "C")

    def test_bump_class_c_stricter(self):
        base = APP.replace("source: github", "source: github\n  bump_class: C")
        w(self.b, "catalog/adguard/app.yaml", base)
        head = base.replace("pin: 0.107.50", "pin: 0.107.51") + "    0.107.51: {sha256: bbb}\n"
        self.assertEqual(self.app(head), "C")
        # removing bump_class in the same PR does not help (stricter of base and head)
        self.assertEqual(self.app(head.replace("\n  bump_class: C", "")), "C")

    def test_unparsable(self):
        self.assertEqual(self.app("name: ["), "C")
        self.assertEqual(self.app("- a\n- b\n"), "C")

    def test_missing_checkout(self):
        ch = [("M", "100644", "100644", "catalog/adguard/app.yaml", "catalog/adguard/app.yaml")]
        self.assertEqual(cl.classify(self.b, "/nonexistent", ch)[0], "C")


class DeployLeaves(Base):
    def dep(self, text):
        w(self.h, "deployments/wifi/adguard.yaml", text)
        return self.one("deployments/wifi/adguard.yaml")

    def test_b(self):
        self.assertEqual(self.dep(DEP.replace("a: 1", "a: 2")), "B")
        self.assertEqual(self.dep(DEP + "host_vars: {hm1-wifi-dns01: {x: 1}}\n"), "B")
        self.assertEqual(self.dep(DEP + "pin_until: 2026-11-01\n"), "B")
        self.assertEqual(self.dep(DEP.replace("wave: 2", "wave: 3")), "B")

    def test_c(self):
        self.assertEqual(self.dep(DEP.replace("wave: 2", "wave: 1")), "C")
        self.assertEqual(self.dep(DEP.replace("hm1-wifi-dns01", "hm1-wifi-dns02")), "C")
        self.assertEqual(self.dep(DEP + "zone: iot\n"), "C")
        self.assertEqual(self.dep(DEP + "canary_waiver: true\n"), "C")
        self.assertEqual(self.dep(DEP.replace("app: adguard", "app: k3s")), "C")

    def test_version_from_known(self):
        happ = APP + "    0.107.51: {sha256: bbb}\n"
        w(self.h, "catalog/adguard/app.yaml", happ)
        self.assertEqual(self.dep(DEP.replace("version: 0.107.50", "version: 0.107.51")), "B")
        self.assertEqual(self.dep(DEP.replace("version: 0.107.50", "version: 0.107.99")), "C")


class Raw(unittest.TestCase):
    def test_parse(self):
        txt = (":100644 100644 aaa bbb M\tdocs/x.md\n"
               ":100644 100644 aaa bbb R090\tdocs/a.md\tdocs/b.md\n"
               ":000000 100644 000 bbb A\tREADME.md\n")
        r = cl.parse_raw(txt)
        self.assertEqual(r[0], ("M", "100644", "100644", "docs/x.md", "docs/x.md"))
        self.assertEqual(r[1][3:], ("docs/a.md", "docs/b.md"))
        self.assertEqual(r[2][3:], (None, "README.md"))

    def test_quoted_path_is_c(self):
        r = cl.parse_raw(':100644 100644 a b M\t"docs/\\303\\244.md"\n')
        self.assertEqual(cl.classify("/x", "/y", r)[0], "C")


if __name__ == "__main__":
    unittest.main()
