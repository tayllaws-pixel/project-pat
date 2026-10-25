import os, tempfile, unittest
from infra.installer.patctl.engine import Engine, State, Step, StepError
from infra.installer.patctl.steps import load_guests, pct_create_args, qm_create_args, guest_firewall, build_steps
from infra.installer.patctl.host import Host

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FlagHost:
    """Simulated host where each step is 'applied' when a flag is set; apply can be told to fail."""
    def __init__(self):
        self.flags = {}
        self.fail = set()
        self.log = []

    def step(self, sid, phase=1, **kw):
        def apply():
            self.log.append(sid)
            if sid in self.fail:
                raise RuntimeError("boom")
            self.flags[sid] = True
        return Step(sid, phase, f"step {sid}", check=lambda: self.flags.get(sid, False), apply=apply, **kw)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = os.path.join(self.tmp.name, "state.json")
        self.out = []

    def engine(self, steps):
        return Engine(steps, State(self.state_path), out=self.out.append)

    def test_idempotent_rerun_applies_nothing_second_time(self):
        h = FlagHost()
        steps = [h.step("a"), h.step("b", depends=["a"])]
        r1 = self.engine(steps).install()
        self.assertEqual(r1["applied"], ["a", "b"])
        r2 = self.engine(steps).install()
        self.assertEqual(r2["applied"], [])
        self.assertEqual(r2["satisfied"], ["a", "b"])
        self.assertEqual(h.log, ["a", "b"])

    def test_failure_is_recorded_and_resume_continues_from_failed_step(self):
        h = FlagHost(); h.fail.add("b")
        steps = [h.step("a"), h.step("b", depends=["a"]), h.step("c", depends=["b"])]
        r = self.engine(steps).install()
        self.assertEqual(r["status"], "failed"); self.assertEqual(r["failed"], "b")
        st = State(self.state_path)
        self.assertEqual(st.rec("b").status, "failed"); self.assertIn("boom", st.rec("b").error)
        h.fail.discard("b")
        r = self.engine(steps).install()          # resume: a satisfied, b+c applied
        self.assertEqual(r["applied"], ["b", "c"]); self.assertEqual(r["status"], "ok")

    def test_destructive_step_requires_explicit_token(self):
        h = FlagHost()
        steps = [h.step("wipe", destructive=True)]
        r = self.engine(steps).install()
        self.assertEqual(r["skipped"], ["wipe"]); self.assertEqual(h.log, [])
        self.assertTrue(any("DESTRUCTIVE" in line for line in self.out))
        r = self.engine(steps).install(understood=("wipe",))
        self.assertEqual(r["applied"], ["wipe"])

    def test_failed_destructive_step_is_not_blindly_repeated(self):
        h = FlagHost(); h.fail.add("wipe")
        steps = [h.step("wipe", destructive=True)]
        self.engine(steps).install(understood=("wipe",))
        h.fail.discard("wipe")
        r = self.engine(steps).install(understood=("wipe",))   # even with the token, a failed destructive step is skipped
        self.assertEqual(r["skipped"], ["wipe"])
        r = self.engine(steps).repair("wipe", understood=("wipe",))   # repair is the explicit path
        self.assertEqual(r["applied"], ["wipe"])

    def test_reboot_after_stops_run_and_resume_clears_it(self):
        h = FlagHost()
        steps = [h.step("nvidia", reboot_after=True), h.step("after", depends=["nvidia"])]
        r = self.engine(steps).install()
        self.assertEqual(r["status"], "reboot_required"); self.assertEqual(r["applied"], ["nvidia"])
        e = self.engine(steps)
        self.assertEqual(e.install()["status"], "reboot_required")   # refuses until admin confirms reboot
        e.clear_reboot()
        self.assertEqual(e.install()["applied"], ["after"])

    def test_phase_filter_and_dependency_blocking(self):
        h = FlagHost()
        steps = [h.step("p1", 1), h.step("p2", 2, depends=["p1"]), h.step("p3", 3, depends=["p2"])]
        r = self.engine(steps).install(phase=2)
        self.assertEqual(r["applied"], ["p1", "p2"])
        self.assertNotIn("p3", r["applied"])

    def test_unknown_dependency_is_rejected(self):
        h = FlagHost()
        with self.assertRaises(StepError):
            self.engine([h.step("x", depends=["nope"])])

    def test_apply_that_does_not_converge_is_a_failure(self):
        h = FlagHost()
        s = Step("lying", 1, "says it applied but check still false", check=lambda: False, apply=lambda: None)
        r = self.engine([s]).install()
        self.assertEqual(r["status"], "failed")
        self.assertIn("still reports unsatisfied", State(self.state_path).rec("lying").error)

    def test_dry_run_changes_nothing_and_assumes_deps_succeed(self):
        h = FlagHost()
        steps = [h.step("a"), h.step("b", depends=["a"])]
        r = self.engine(steps).install(dry_run=True)
        self.assertEqual(r["applied"], ["a", "b"]); self.assertEqual(h.log, [])
        self.assertEqual(State(self.state_path).rec("a").status, "pending")

    def test_corrupt_state_is_preserved_not_discarded(self):
        with open(self.state_path, "w") as f:
            f.write("{not json")
        State(self.state_path)
        self.assertTrue(os.path.exists(self.state_path.replace(".json", ".corrupt")))


class GuestDefinitionTests(unittest.TestCase):
    def test_guests_load_and_ids_unique_and_memory_budget(self):
        gs = load_guests(REPO)
        self.assertEqual([g["id"] for g in gs], [100, 101, 102, 103, 104, 105, 106, 110])
        total = sum(g["memory"] for g in gs)
        self.assertLessEqual(total, 56 * 1024, "guest caps must leave >= 8 GB for host + ARC on 64 GB")
        self.assertTrue(all(g["kind"] in ("lxc", "vm") for g in gs))
        self.assertEqual([g["name"] for g in gs if g.get("gpu")], ["inference"])
        self.assertEqual([g["name"] for g in gs if g["network"] == "lab"], ["forge"])
        self.assertEqual({g["name"] for g in gs if g.get("pool") == "autonomous"}, {"wade", "forge"})

    def test_pct_args_are_unprivileged_with_firewall_and_caps(self):
        g = next(g for g in load_guests(REPO) if g["id"] == 101)
        args = pct_create_args(g, "local:vztmpl/debian-13-standard_13.0-1_amd64.tar.zst")
        self.assertEqual(args[:3], ["pct", "create", "101"])
        self.assertIn("--unprivileged", args); self.assertEqual(args[args.index("--unprivileged") + 1], "1")
        self.assertIn("name=eth0,bridge=vmbr0,ip=dhcp,firewall=1", args)
        self.assertEqual(args[args.index("--memory") + 1], "16384")
        self.assertEqual(args[args.index("--cpuunits") + 1], "2048")

    def test_vm_args_put_forge_on_lab_bridge(self):
        g = next(g for g in load_guests(REPO) if g["id"] == 110)
        args = qm_create_args(g)
        self.assertIn("virtio,bridge=vmbr1,firewall=1", args)
        self.assertEqual(args[args.index("--cpulimit") + 1], "6")
        self.assertEqual(args[args.index("--balloon") + 1], "4096")
        self.assertIn("autonomous", args)

    def test_lab_firewall_blocks_lan_and_tailnet(self):
        forge = next(g for g in load_guests(REPO) if g["network"] == "lab")
        fw = guest_firewall(forge)
        self.assertIn("OUT DROP -dest +lan", fw); self.assertIn("OUT DROP -dest +tailnet", fw)
        self.assertIn("policy_in: DROP", fw)
        lan = next(g for g in load_guests(REPO) if g["network"] == "lan")
        self.assertNotIn("OUT DROP", guest_firewall(lan))

    def test_build_steps_has_expected_ids_and_destructive_recreates(self):
        host = Host(REPO, dry_run=True, out=lambda s: None)
        steps = build_steps(host, REPO, nvidia_version="580.0.0")
        ids = [s.id for s in steps]
        for sid in ["10-post-install", "20-nvidia-host", "40-network", "secrets", "pool-autonomous", "guest-100", "gpu-101", "fw-110", "guest-110"]:
            self.assertIn(sid, ids)
        destructive = [s.id for s in steps if s.destructive]
        self.assertTrue(all(d.startswith("recreate-") for d in destructive))
        self.assertTrue(all(not s.destructive for s in steps if s.id.startswith("guest-")))
        gpu = next(s for s in steps if s.id == "gpu-101")
        self.assertIn("20-nvidia-host", gpu.depends)
        forge = next(s for s in steps if s.id == "guest-110")
        self.assertIn("40-network", forge.depends)


if __name__ == "__main__":
    unittest.main()
