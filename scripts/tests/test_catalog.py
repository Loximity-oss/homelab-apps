import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import catalog  # noqa: E402


def write(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        write(self.root, "catalog/adguard/app.yaml", "name: adguard\n")
        write(self.root, "catalog/_example/app.yaml", "name: _example\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid(self):
        write(self.root, "deployments/wifi/adguard.yaml",
              "app: adguard\nhosts: [hm1-wifi-dns01, hm1-wifi-dns02]\n")
        cat = catalog.load(self.root)
        self.assertEqual(cat.errors, [])
        self.assertEqual(list(cat.apps), ["adguard"])  # _example skipped
        self.assertEqual(cat.hosts("adguard"), ["hm1-wifi-dns01", "hm1-wifi-dns02"])
        self.assertEqual(catalog.main(["--root", self.root, "validate"]), 0)

    def test_k3s_hosts(self):
        write(self.root, "catalog/k3s/app.yaml", "name: k3s\n")
        write(self.root, "deployments/shr/k3s.yaml", "hosts: [hm1-shr-k3s01, hm1-shr-k3s02]\n")
        self.assertEqual(catalog.load(self.root).errors, [])

    def test_unknown_app(self):
        write(self.root, "deployments/shr/k3s.yaml", "hosts: [hm1-shr-k3s01]\n")
        cat = catalog.load(self.root)
        self.assertTrue(any("unknown app 'k3s'" in e for e in cat.errors))

    def test_zone_mismatch(self):
        write(self.root, "deployments/iot/adguard.yaml",
              "zone: wifi\nhosts: [hm1-iot-dns01]\n")
        cat = catalog.load(self.root)
        self.assertTrue(any("!= directory" in e for e in cat.errors))

    def test_host_wrong_zone(self):
        write(self.root, "deployments/iot/adguard.yaml", "hosts: [hm1-wifi-dns01]\n")
        cat = catalog.load(self.root)
        self.assertTrue(any("not in zone" in e for e in cat.errors))

    def test_empty_hosts(self):
        write(self.root, "deployments/iot/adguard.yaml", "hosts: []\n")
        self.assertEqual(catalog.main(["--root", self.root, "validate"]), 1)

    def test_invalid_host(self):
        write(self.root, "deployments/iot/adguard.yaml", "hosts: [localhost]\n")
        cat = catalog.load(self.root)
        self.assertTrue(any("invalid host" in e for e in cat.errors))

    def test_empty_repo(self):
        cat = catalog.load(tempfile.mkdtemp())
        self.assertEqual(cat.errors, [])
        self.assertEqual(cat.deployments, [])


if __name__ == "__main__":
    unittest.main()
