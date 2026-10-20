import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import lint_gate as lg  # noqa: E402

ALLOW_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "policy", "jinja-allow.yaml")
ALLOW = lg.load_allow(ALLOW_FILE)
TASKS = "catalog/demo/role/tasks/main.yml"

GOOD = r"""
- name: read-only probe
  ansible.builtin.command: swapon --show
  register: _k3s_swap
  check_mode: false
  changed_when: false
- ansible.builtin.debug:
    msg: "{{ _k3s_swap.stdout | trim | default('x') }}"
  when: _k3s_swap.stdout is search('x')
- ansible.builtin.meta: flush_handlers
- ansible.builtin.copy:
    src: "{{ role_path }}/../files/a.conf"
    dest: /etc/a.conf
- ansible.builtin.template:
    src: a.conf.j2
    dest: /etc/a.conf
- ansible.builtin.command: echo
  delegate_to: "{{ svc_k3s_init_host }}"
- ansible.builtin.command: echo
  delegate_to: "{{ groups['role_k3s'][0] }}"
- ansible.builtin.command: echo
  delegate_to: hm1-shr-k3s02
- ansible.builtin.debug:
    msg: "{{ hostvars[item]['ansible_host'] }}"
  with_items: "{{ groups['role_k3s'] }}"
- ansible.builtin.include_role:
    name: bao_create_secret
  vars:
    path: apps/demo
"""

BAD = {
    "local_action": "- local_action: command echo\n",
    "connection local": "- command: echo\n  connection: local\n",
    "add_host": "- add_host: name=x\n",
    "fetch": "- ansible.builtin.fetch: {src: /a, dest: /b}\n",
    "synchronize": "- synchronize: {src: a, dest: b}\n",
    "script": "- script: x.sh\n",
    "lookup": "- debug:\n    msg: \"{{ lookup('pipe', 'id') }}\"\n",
    "query": "- debug:\n    msg: \"{{ query('pipe', 'id') }}\"\n",
    "q": "- debug:\n    msg: \"{{ q('pipe', 'id') }}\"\n",
    "fqcn lookup": "- debug:\n    msg: \"{{ ansible.builtin.lookup('pipe', 'id') }}\"\n",
    "with_fileglob": "- debug: {msg: x}\n  with_fileglob: ['*']\n",
    "when lookup": "- debug: {msg: x}\n  when: lookup('pipe','id')\n",
    "when dunder": "- debug: {msg: x}\n  when: x.__class__\n",
    "attr filter": "- debug:\n    msg: \"{{ x | attr('_'~'_init_'~'_') }}\"\n",
    "underscore mul": "- debug:\n    msg: \"{{ x[('_'*2) ~ 'class' ~ ('_'*2)] }}\"\n",
    "format": "- debug:\n    msg: \"{{ '%c%c' | format(95, 95) }}\"\n",
    "x5f escape": r"""- debug:
    msg: '{{ x[''\x5f\x5fclass\x5f\x5f''] }}'
""",
    "map attribute var": "- debug:\n    msg: \"{{ y | map(attribute=x) }}\"\n",
    "map attribute _": "- debug:\n    msg: \"{{ y | map(attribute='_x') }}\"\n",
    "var subscript": "- debug:\n    msg: \"{{ foo[var] }}\"\n",
    "groups built": "- debug:\n    msg: \"{{ groups['role_' ~ c] }}\"\n",
    "map attr": "- debug:\n    msg: \"{{ y | map('attr', 'z') }}\"\n",
    "extract groups": "- debug:\n    msg: \"{{ x | extract(groups) }}\"\n",
    "retemplate": "- debug:\n    msg: \"{{ '{' ~ '{ lipsum.' ~ ('_'*2) ~ 'globals' ~ ('_'*2) ~ ' }}' }}\"\n",
    "x._y": "- debug:\n    msg: \"{{ x._y }}\"\n",
    "include_role vars zone": "- include_role:\n    name: bao_create_secret\n  vars:\n    deploy_zone: int\n",
    "include_role other": "- include_role:\n    name: foo\n",
    "set_fact zone": "- set_fact:\n    zone: int\n",
    "register vmid": "- command: echo\n  register: vmid\n",
    "delegate localhost": "- command: echo\n  delegate_to: localhost\n",
    "delegate 127": "- command: echo\n  delegate_to: 127.0.0.1\n",
    "delegate const localhost": "- command: echo\n  delegate_to: \"{{ 'localhost' }}\"\n",
    "delegate var suffix": "- command: echo\n  delegate_to: \"{{ x }}-a\"\n",
    "check_mode no changed_when": "- command: echo\n  check_mode: false\n",
    "check_mode copy": "- copy: {src: a, dest: /b}\n  check_mode: false\n  changed_when: false\n",
    "check_mode uri post": "- uri: {url: 'http://x', method: POST}\n  check_mode: false\n  changed_when: false\n",
    "meta end_play": "- meta: end_play\n",
    "src escape": "- template: {src: ../../../../etc/passwd, dest: /x}\n",
    "src absolute": "- copy: {src: /etc/passwd, dest: /x}\n",
    "src templated": "- copy:\n    src: \"{{ x }}/a.conf\"\n    dest: /x\n",
    "include_tasks escape": "- include_tasks: ../../../x.yml\n",
    "two modules": "- command: echo\n  shell: echo\n",
}


def write(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)


class LintGate(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.root = self.t.name
        write(self.root, "catalog/demo/app.yaml", "name: demo\n")

    def tearDown(self):
        self.t.cleanup()

    def lint(self, rel, text):
        write(self.root, rel, text)
        return lg.run(self.root, ALLOW)

    def test_allow_file(self):
        self.assertIn("trim", ALLOW["filters"])
        self.assertNotIn("attr", ALLOW["filters"])
        self.assertIn("search", ALLOW["tests"])

    def test_good(self):
        self.assertEqual(self.lint(TASKS, GOOD), [])

    def test_each_bypass(self):
        for name, text in BAD.items():
            with self.subTest(name):
                self.assertNotEqual(self.lint(TASKS, text), [], name)

    def test_defaults(self):
        self.assertNotEqual(self.lint("catalog/demo/role/defaults/main.yml", "a: __class__\n"), [])
        self.assertNotEqual(self.lint("catalog/demo/role/defaults/main.yml", "ansible_become: true\n"), [])
        self.assertNotEqual(self.lint("catalog/demo/role/vars/main.yml", "zone: int\n"), [])
        self.assertEqual(self.lint("catalog/demo/role/defaults/main.yml", "app_demo_port: 80\n"), [])

    def test_j2_anywhere(self):
        self.assertEqual(self.lint("catalog/demo/files/sub/x.j2", "{{ x | trim }}\n"), [])
        self.assertNotEqual(self.lint("catalog/demo/files/sub/x.j2", "{{ x.__class__ }}\n"), [])
        self.assertNotEqual(self.lint("catalog/demo/files/sub/x.j2", "{% include 'y' %}\n"), [])

    def test_template_src_non_j2(self):
        write(self.root, "catalog/demo/role/templates/a.conf", "{{ x.__class__ }}\n")
        self.assertNotEqual(self.lint(TASKS, "- template: {src: a.conf, dest: /a}\n"), [])

    def test_symlink(self):
        os.makedirs(os.path.join(self.root, "catalog/demo/files"), exist_ok=True)
        os.symlink("/etc/passwd", os.path.join(self.root, "catalog/demo/files/p"))
        self.assertNotEqual(lg.run(self.root, ALLOW), [])

    def test_deployment(self):
        dep = "deployments/wifi/demo.yaml"
        ok = "app: demo\nhosts: [hm1-wifi-dns01]\nvars: {a: 1}\nhost_vars: {hm1-wifi-dns01: {b: 2}}\n"
        self.assertEqual(self.lint(dep, ok), [])
        self.assertNotEqual(self.lint(dep, ok.replace("a: 1", "a: '{{ x }}'")), [])
        self.assertNotEqual(self.lint(dep, ok.replace("a: 1", "a: __globals__")), [])
        self.assertNotEqual(self.lint(dep, ok.replace("a: 1", "zone: int")), [])
        self.assertNotEqual(self.lint(dep, ok.replace("a: 1", "ansible_host: 1.2.3.4")), [])
        self.assertNotEqual(self.lint(dep, ok.replace("{hm1-wifi-dns01: {b", "{hm1-wifi-dns02: {b")), [])

    def test_cli(self):
        write(self.root, TASKS, GOOD)
        self.assertEqual(lg.main(["--root", self.root, "--allow", ALLOW_FILE]), 0)
        write(self.root, TASKS, BAD["lookup"])
        self.assertEqual(lg.main(["--root", self.root, "--allow", ALLOW_FILE]), 1)


if __name__ == "__main__":
    unittest.main()
