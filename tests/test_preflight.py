import json, unittest
from infra.preflight import run_preflight
from tests.fakes import healthy_host, FakeRunner


class PreflightTests(unittest.TestCase):
    def test_healthy_host_has_no_blocking_problems(self):
        rep = run_preflight(healthy_host())
        self.assertEqual([c.name for c in rep.blocking], [])
        self.assertEqual(rep.facts["pve_version"], "9.1")
        self.assertEqual(rep.facts["mem_gib"], 62.4)
        self.assertTrue(rep.facts["iommu_enabled"])
        self.assertIn("4070 Ti SUPER", rep.facts["nvidia_pci"][0])

    def test_missing_vmx_and_old_pve_are_blocking(self):
        r = healthy_host()
        r.files["/proc/cpuinfo"] = r.files["/proc/cpuinfo"].replace(" vmx", "")
        r.cmds["pveversion"] = (0, "pve-manager/8.4.1/x (running kernel: 6.8)\n")
        rep = run_preflight(r)
        names = {c.name for c in rep.blocking}
        self.assertIn("VT-x (vmx flag)", names)
        self.assertIn("Proxmox VE", names)

    def test_unavailable_sensors_are_reported_as_unknown_not_invented(self):
        r = healthy_host()
        del r.cmds["dmesg"]
        rep = run_preflight(r)
        c = next(c for c in rep.checks if c.name == "IOMMU (VT-d)")
        self.assertIsNone(c.ok)
        self.assertIn("unavailable", c.value)

    def test_loss_inventory_lists_existing_guests(self):
        r = healthy_host()
        r.cmds["pct list"] = (0, "VMID Status Lock Name\n101 running  old-ollama\n102 stopped  old-pat\n")
        r.cmds["qm list"] = (0, "VMID NAME STATUS\n200 wade-dev running\n")
        rep = run_preflight(r)
        self.assertEqual(len(rep.loss_inventory["lxc_containers"]), 2)
        self.assertEqual(rep.loss_inventory["virtual_machines"], ["200 wade-dev running"])
        self.assertIn("What a reinstall would destroy", rep.to_markdown())

    def test_json_roundtrip_and_markdown_symbols(self):
        rep = run_preflight(healthy_host())
        data = json.loads(rep.to_json())
        self.assertIn("checks", data)
        md = rep.to_markdown()
        self.assertIn("✓", md)
        self.assertIn("No blocking problems", md)

    def test_preflight_is_read_only(self):
        r = healthy_host()
        run_preflight(r)
        writes = [c for c in r.calls if c[0] in {"pct", "qm", "zfs", "zpool", "apt-mark"} and len(c) > 1 and c[1] not in {"list", "status", "showhold"}]
        self.assertEqual(writes, [])
        self.assertTrue(all(c[0] not in {"rm", "dd", "mkfs", "sed", "tee"} for c in r.calls))


if __name__ == "__main__":
    unittest.main()
