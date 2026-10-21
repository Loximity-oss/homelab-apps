"""Tests for scripts/render.py (migration step 5g, infra #127; Faris's review 2026-10-21 01:00).

render.py needs ansible-core >= 2.19. The tests run it as ci-pr.sh does: a subprocess, `python3 -I`, a minimal
environment. Interpreter: $RENDER_PYTHON, else /opt/ansible-apps/venv/bin/python3, else this python. Skipped when
that interpreter has no ansible-core. The bwrap case (--clearenv, no /etc/passwd = the CT 300 failure) runs only
where bwrap and /opt/ansible-apps exist.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REPO = os.path.dirname(SCRIPTS)
RENDER = os.path.join(SCRIPTS, "render.py")
VENV = "/opt/ansible-apps"
PY = os.environ.get("RENDER_PYTHON") or (f"{VENV}/venv/bin/python3" if os.path.exists(f"{VENV}/venv/bin/python3")
                                          else sys.executable)

PROTECTED = """vmids:
  100: hm1-mgt-aiops01 (OpenClaw)
  300: hm1-shr-cicd01 (DevOps plane)
  303: hm1-shr-vault01 (OpenBao)
templates: [9000, 9199]
bridges: [vmbr0]
subnets: [192.168.67.0/28]
"""
HOST = "hm1-stg-demo01"
APP = {
    "catalog/demo/app.yaml": "name: demo\nstatus: beta\n",
    "catalog/demo/role/defaults/main.yml": "demo_port: 8080\ndemo_image: nginx\n",
    "catalog/demo/role/tasks/main.yml": "- name: Placeholder\n  ansible.builtin.debug:\n    msg: demo\n",
    "catalog/demo/role/templates/compose.yaml.j2":
        "services:\n  demo:\n    image: {{ demo_image }}\n    ports:\n      - \"{{ demo_port }}:80\"\n",
    "catalog/demo/tests/l4.goss.yaml.j2": "port:\n  tcp:{{ demo_port }}:\n    listening: true\n",
}


def write(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)


def deployment(port):
    return f"app: demo\nhosts: [{HOST}]\nvars:\n  demo_port: {port}\n"


def has_ansible():
    try:
        r = subprocess.run([PY, "-I", "-c", "import ansible.release"], capture_output=True, timeout=60,
                           env={"PATH": "/usr/bin:/bin", "USER": "test", "HOME": tempfile.gettempdir()})
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


HAS_ANSIBLE = has_ansible()


class Source(unittest.TestCase):
    """Static: USER and LOGNAME get a default before anything imports ansible (needs no ansible-core)."""

    def test_user_default_before_ansible_import(self):
        with open(RENDER, encoding="utf-8") as f:
            src = f.read()
        firsts = [i for i in (src.find("from ansible"), src.find("import ansible")) if i >= 0]
        self.assertTrue(firsts, "render.py imports ansible")
        for var in ("USER", "LOGNAME"):
            marks = [src.find(f'os.environ.setdefault("{var}"'), src.find(f"os.environ.setdefault('{var}'")]
            marks = [i for i in marks if i >= 0]
            self.assertTrue(marks, f"render.py: os.environ.setdefault('{var}', ...) missing")
            self.assertLess(min(marks), min(firsts), f"{var} default must come before the first ansible import")


@unittest.skipUnless(HAS_ANSIBLE, f"{PY} has no ansible-core")
class Render(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        r = self.t.name
        self.base, self.head = os.path.join(r, "base"), os.path.join(r, "head")
        self.infra, self.out, self.home = os.path.join(r, "infra"), os.path.join(r, "out"), os.path.join(r, "home")
        os.makedirs(self.home)
        for tree in (self.base, self.head):
            for rel, text in APP.items():
                write(tree, rel, text)
            write(tree, "deployments/stg/.gitkeep", "")
        write(self.infra, f"terraform/stacks/hm1/stg/guests/{HOST}.yaml",
              f"hostname: {HOST}\nvmid: 450\nip: 192.168.80.50/24\nrole: app\ntype: lxc\nos: ubuntu2604\n")
        self.protected = os.path.join(r, "protected.yaml")
        write(r, "protected.yaml", PROTECTED)

    def tearDown(self):
        self.t.cleanup()

    def env(self, user=True):
        e = {"PATH": os.path.dirname(PY) + ":/usr/bin:/bin", "HOME": self.home, "LANG": "C.UTF-8",
             "ANSIBLE_LOCAL_TEMP": os.path.join(self.home, "ansible")}
        if user:
            e.update(USER="test", LOGNAME="test")
        return e

    def run_render(self, user=True):
        r = subprocess.run([PY, "-I", RENDER, "--base", self.base, "--head", self.head, "--infra", self.infra,
                            "--protected", self.protected, "--out", self.out],
                           capture_output=True, text=True, timeout=300, env=self.env(user))
        with open(os.path.join(self.out, "diff.md"), encoding="utf-8") as f:
            return r, f.read()

    def test_no_change(self):
        write(self.base, "deployments/stg/demo.yaml", deployment(8080))
        write(self.head, "deployments/stg/demo.yaml", deployment(8080))
        r, diff = self.run_render()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(diff, "")

    def test_no_deployments(self):
        r, diff = self.run_render()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(diff, "")

    def test_new_deployment(self):
        write(self.head, "deployments/stg/demo.yaml", deployment(9090))
        r, diff = self.run_render()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("9090", diff)
        for f in ("vars.yaml", "compose.yaml.j2", "l4.goss.yaml.j2"):
            self.assertIn(f, diff)

    def test_changed_var(self):
        write(self.base, "deployments/stg/demo.yaml", deployment(8080))
        write(self.head, "deployments/stg/demo.yaml", deployment(9090))
        r, diff = self.run_render()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("9090", diff)
        self.assertIn("8080", diff)
        for f in ("vars.yaml", "compose.yaml.j2", "l4.goss.yaml.j2"):
            self.assertIn(f, diff)

    def test_user_and_logname_unset(self):
        write(self.base, "deployments/stg/demo.yaml", deployment(8080))
        write(self.head, "deployments/stg/demo.yaml", deployment(9090))
        r, diff = self.run_render(user=False)
        self.assertNotIn("No username set", r.stderr)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("9090", diff)

    def test_template_error_names_file(self):
        write(self.head, "catalog/demo/role/templates/compose.yaml.j2", "{% if %}\n")
        write(self.head, "deployments/stg/demo.yaml", deployment(8080))
        r, diff = self.run_render()
        self.assertEqual(r.returncode, 1)
        self.assertIn("compose.yaml.j2", r.stderr + diff)

    def test_to_json_bare_placeholder(self):
        """A fact that does not exist offline, piped through to_json, renders as its placeholder (infra #127)."""
        write(self.head, "catalog/demo/role/templates/facts.json.j2", "{{ no_such_fact | to_json }}\n")
        write(self.head, "deployments/stg/demo.yaml", deployment(9090))
        r, diff = self.run_render()
        self.assertEqual(r.returncode, 0, r.stderr + diff)
        self.assertIn('"<<undefined:no_such_fact>>"', diff)
        self.assertNotIn("TypeError", r.stderr + diff)

    def test_to_json_dict_with_placeholder(self):
        """A placeholder inside a mapping is serialised too; the defined values keep their real value."""
        write(self.head, "catalog/demo/role/templates/peer.json.j2",
              "{{ {'addr': other_fact.ipv4, 'port': demo_port} | to_json }}\n")
        write(self.head, "deployments/stg/demo.yaml", deployment(9090))
        r, diff = self.run_render()
        self.assertEqual(r.returncode, 0, r.stderr + diff)
        self.assertIn('"addr": "<<undefined:other_fact>>"', diff)
        self.assertIn('"port": 9090', diff)
        self.assertNotIn("TypeError", r.stderr + diff)

    @unittest.skipUnless(shutil.which("bwrap") and os.path.isdir(VENV), "needs bwrap and /opt/ansible-apps")
    def test_bwrap_clearenv_no_passwd(self):
        """ci-pr.sh's sandbox: --clearenv (no USER/LOGNAME) and no /etc/passwd; this is the CT 300 failure."""
        write(self.base, "deployments/stg/demo.yaml", deployment(8080))
        write(self.head, "deployments/stg/demo.yaml", deployment(9090))
        os.makedirs(self.out)
        cmd = ["bwrap", "--unshare-all", "--die-with-parent", "--new-session", "--clearenv",
               "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin", "--symlink", "usr/sbin", "/sbin",
               "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
               "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
               "--ro-bind", VENV, VENV,
               "--ro-bind", REPO, "/w/main", "--ro-bind", self.base, "/w/base", "--ro-bind", self.head, "/w/head",
               "--ro-bind", self.infra, "/w/infra", "--ro-bind", self.protected, "/w/protected.yaml",
               "--bind", self.out, "/w/out", "--chdir", "/w",
               "--setenv", "PATH", f"{VENV}/venv/bin:/usr/bin:/bin", "--setenv", "HOME", "/tmp",
               "--setenv", "LANG", "C.UTF-8", "--setenv", "ANSIBLE_LOCAL_TEMP", "/tmp/ansible",
               f"{VENV}/venv/bin/python3", "-I", "/w/main/scripts/render.py", "--base", "/w/base", "--head", "/w/head",
               "--infra", "/w/infra", "--protected", "/w/protected.yaml", "--out", "/w/out"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        self.assertNotIn("No username set", r.stderr)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(self.out, "diff.md"), encoding="utf-8") as f:
            self.assertIn("9090", f.read())


if __name__ == "__main__":
    unittest.main()
