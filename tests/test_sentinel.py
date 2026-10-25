import os, tempfile, unittest
from patshared.schemas import ApprovalRequest, Actor, ActorKind, ApprovalDecision
from sentinel.core import (Approvals, Audit, Gate, KeyPair, MemoryStore, EStopEngaged, SentinelError, ApprovalRejected,
                           classify_action, GENESIS)
from sentinel.budget import BudgetMeter, ProviderBudget, BudgetDenied

WADE = Actor(kind=ActorKind.supervisor, id="wade")


class Clock:
    def __init__(self, t=1_700_000_000.0): self.t = t
    def __call__(self): return self.t


def make(clock=None):
    store = MemoryStore(); audit = Audit(store); keys = KeyPair.generate()
    ap = Approvals(store, audit, keys)
    if clock: ap._clock = clock
    return store, audit, keys, ap


def req(action="deploy.production", payload=None, ttl=3600, affected=None):
    return ApprovalRequest(requester=WADE, action=action, reason="test", payload=payload or {"rev": "abc123"},
                           ttl_seconds=ttl, affected=affected or [])


class ApprovalTokenTests(unittest.TestCase):
    def test_happy_path_single_use(self):
        store, audit, keys, ap = make()
        r = req(); ap.request(r)
        tok = ap.decide(r.id, ApprovalDecision.approved, by="taylor")
        claims = ap.verify_and_consume(tok, "deploy.production", {"rev": "abc123"}, executor="deploy-controller")
        self.assertEqual(claims.aid, r.id)
        with self.assertRaises(ApprovalRejected) as cm:
            ap.verify_and_consume(tok, "deploy.production", {"rev": "abc123"}, executor="deploy-controller")
        self.assertIn("replay", str(cm.exception))

    def test_payload_tampering_is_rejected(self):
        _, _, _, ap = make()
        r = req(); ap.request(r)
        tok = ap.decide(r.id, ApprovalDecision.approved, by="taylor")
        with self.assertRaises(ApprovalRejected):
            ap.verify_and_consume(tok, "deploy.production", {"rev": "evil"}, executor="deploy-controller")

    def test_wrong_action_and_out_of_scope_resource_rejected(self):
        _, _, _, ap = make()
        r = req(affected=["ct:102"]); ap.request(r)
        tok = ap.decide(r.id, ApprovalDecision.approved, by="taylor")
        with self.assertRaises(ApprovalRejected):
            ap.verify_and_consume(tok, "guest.destroy", {"rev": "abc123"}, executor="x")
        with self.assertRaises(ApprovalRejected):
            ap.verify_and_consume(tok, "deploy.production", {"rev": "abc123"}, executor="x", resource="ct:104")
        ap.verify_and_consume(tok, "deploy.production", {"rev": "abc123"}, executor="x", resource="ct:102")

    def test_forged_token_with_other_key_rejected(self):
        _, _, _, ap = make()
        _, _, _, other = make()
        r = req(); ap.request(r); other.request(r)
        forged = other.decide(r.id, ApprovalDecision.approved, by="attacker")
        with self.assertRaises(ApprovalRejected) as cm:
            ap.verify_and_consume(forged, "deploy.production", {"rev": "abc123"}, executor="x")
        self.assertIn("signature", str(cm.exception))

    def test_token_expiry(self):
        clock = Clock(); _, _, _, ap = make(clock)
        r = req(ttl=600); ap.request(r)
        tok = ap.decide(r.id, ApprovalDecision.approved, by="taylor")
        clock.t += 601
        with self.assertRaises(ApprovalRejected) as cm:
            ap.verify_and_consume(tok, "deploy.production", {"rev": "abc123"}, executor="x")
        self.assertIn("expired", str(cm.exception))

    def test_request_expires_before_decision(self):
        clock = Clock(); _, _, _, ap = make(clock)
        r = req(ttl=60); ap.request(r); clock.t += 61
        with self.assertRaises(ApprovalRejected):
            ap.decide(r.id, ApprovalDecision.approved, by="taylor")

    def test_non_human_cannot_approve(self):
        store, _, _, ap = make()
        r = req(); ap.request(r)
        with self.assertRaises(ApprovalRejected):
            ap.decide(r.id, ApprovalDecision.approved, by="wade", human_authenticated=False)
        self.assertEqual(store.audit[-1]["action"], "approval.decision_denied")

    def test_denied_and_revoked_tokens_do_not_work(self):
        _, _, _, ap = make()
        r = req(); ap.request(r)
        self.assertIsNone(ap.decide(r.id, ApprovalDecision.denied, by="taylor"))
        r2 = req(); ap.request(r2)
        tok = ap.decide(r2.id, ApprovalDecision.approved, by="taylor")
        ap.revoke(r2.id, by="taylor")
        with self.assertRaises(ApprovalRejected):
            ap.verify_and_consume(tok, "deploy.production", {"rev": "abc123"}, executor="x")

    def test_verifier_without_private_key_can_verify_but_not_issue(self):
        store, audit, keys, ap = make()
        r = req(); ap.request(r)
        tok = ap.decide(r.id, ApprovalDecision.approved, by="taylor")
        with tempfile.TemporaryDirectory() as d:
            keys.save(os.path.join(d, "k.pem"), os.path.join(d, "k.pub"))
            pub_only = KeyPair.load(None, os.path.join(d, "k.pub"))
        verifier = Approvals(store, audit, pub_only)
        verifier.verify_and_consume(tok, "deploy.production", {"rev": "abc123"}, executor="executor-ct")
        r3 = req(); verifier.request(r3)
        with self.assertRaises(SentinelError):
            verifier.decide(r3.id, ApprovalDecision.approved, by="taylor")

    def test_action_classification_defaults_to_explicit(self):
        self.assertEqual(classify_action("forge.test"), "automatic")
        self.assertEqual(classify_action("storage.wipe"), "explicit")
        self.assertEqual(classify_action("something.new"), "explicit")


class EStopTests(unittest.TestCase):
    def test_estop_blocks_work_and_requires_human_release(self):
        store = MemoryStore(); audit = Audit(store)
        with tempfile.TemporaryDirectory() as d:
            gate = Gate(store, audit, flag_file=os.path.join(d, "ESTOP"))
            gate.allow_autonomous_work("wade/agent/coder-1")
            gate.engage_estop("taylor", "testing")
            with self.assertRaises(EStopEngaged):
                gate.allow_autonomous_work("wade")
            with self.assertRaises(SentinelError):
                gate.release_estop("wade", human_authenticated=False)
            self.assertTrue(gate.estop_engaged())
            # DB flag cleared out-of-band (e.g. Wade found DB creds): file flag still blocks
            store.estop["engaged"] = False
            self.assertTrue(gate.estop_engaged())
            gate.release_estop("taylor", human_authenticated=True)
            gate.allow_autonomous_work("wade")

    def test_estop_blocks_new_approvals_and_token_use_except_release(self):
        store, audit, keys, ap = make()
        gate = Gate(store, audit)
        r = req(); ap.request(r); tok = ap.decide(r.id, ApprovalDecision.approved, by="taylor")
        gate.engage_estop("taylor", "incident")
        with self.assertRaises(EStopEngaged):
            ap.request(req())
        with self.assertRaises(ApprovalRejected):
            ap.verify_and_consume(tok, "deploy.production", {"rev": "abc123"}, executor="x")
        ap.request(req(action="estop.release", payload={}))   # the one allowed request

    def test_disable_and_pause_are_separate_and_inherit(self):
        store = MemoryStore(); gate = Gate(store, Audit(store))
        gate.set_agent("wade/agent/tester", paused=True, by="taylor")
        with self.assertRaises(SentinelError): gate.allow_autonomous_work("wade/agent/tester")
        gate.allow_autonomous_work("wade/agent/coder")
        gate.set_agent("wade", disabled=True, by="taylor")
        with self.assertRaises(SentinelError): gate.allow_autonomous_work("wade/agent/coder")


class AuditChainTests(unittest.TestCase):
    def test_chain_verifies_and_detects_edit_delete_reorder(self):
        store = MemoryStore(); audit = Audit(store)
        for i in range(5):
            audit.record("sentinel", f"evt.{i}", {"i": i})
        self.assertEqual(audit.verify_chain(), (True, None))
        self.assertEqual(store.audit[0]["prev_hash"], GENESIS)
        import copy
        edited = copy.deepcopy(store.audit); edited[2]["details"]["i"] = 99
        store.audit = edited; self.assertEqual(audit.verify_chain()[1], 2)
        store.audit = copy.deepcopy(edited); store.audit[2]["details"]["i"] = 2
        del store.audit[3]; self.assertEqual(audit.verify_chain()[1], 3)
        store.audit = copy.deepcopy(edited); store.audit[2]["details"]["i"] = 2
        store.audit[1], store.audit[2] = store.audit[2], store.audit[1]
        self.assertEqual(audit.verify_chain()[1], 1)


class BudgetTests(unittest.TestCase):
    def test_disabled_by_default_then_caps(self):
        m = BudgetMeter({"anthropic": ProviderBudget("anthropic")})
        with self.assertRaises(BudgetDenied): m.authorize("anthropic", "issue-1", 0.10, 3, True)
        m.budgets["anthropic"].enabled = True
        with self.assertRaises(BudgetDenied): m.authorize("anthropic", "issue-1", 0.10, 2, True)   # <3 local attempts
        m.authorize("anthropic", "issue-1", 0.10, 3, True)
        for _ in range(3): m.record("anthropic", "issue-1", 0.10)
        with self.assertRaises(BudgetDenied): m.authorize("anthropic", "issue-1", 0.10, 3, True)   # 3 escalations
        with self.assertRaises(BudgetDenied): m.authorize("anthropic", "issue-2", 4.80, 3, True)   # cap 5.00
        with self.assertRaises(BudgetDenied): m.authorize("anthropic", "issue-2", 0.10, 3, False)  # redaction
        with self.assertRaises(BudgetDenied): m.authorize("anthropic", "issue-2", 0.10, 3, True, contains_sensitive=True)


if __name__ == "__main__":
    unittest.main()
