import json, tempfile, unittest
from pydantic import ValidationError
from patshared.schemas import (ApprovalRequest, Actor, ActorKind, TaskMessage, Event, canonical_json, sha256_hex,
                               export_json_schemas, SupervisorManifest)
from patshared.router import Router


class SchemaTests(unittest.TestCase):
    def test_task_rejects_unbounded_retries(self):
        with self.assertRaises(ValidationError):
            TaskMessage(kind="dev.implement", title="x", requested_by=Actor(kind=ActorKind.pat, id="pat"), max_attempts=0)
        with self.assertRaises(ValidationError):
            TaskMessage(kind="dev.implement", title="x", requested_by=Actor(kind=ActorKind.pat, id="pat"), max_attempts=99)

    def test_payload_hash_is_canonical(self):
        a = ApprovalRequest(requester=Actor(kind=ActorKind.supervisor, id="wade"), action="deploy.production", reason="r",
                            payload={"b": 1, "a": [1, 2]})
        b = ApprovalRequest(requester=Actor(kind=ActorKind.supervisor, id="wade"), action="deploy.production", reason="r",
                            payload={"a": [1, 2], "b": 1})
        self.assertEqual(a.payload_sha256, b.payload_sha256)
        self.assertEqual(a.payload_sha256, sha256_hex(canonical_json({"a": [1, 2], "b": 1})))

    def test_event_roundtrip_json(self):
        e = Event(type="job.failed", actor=Actor(kind=ActorKind.supervisor, id="wade"), subject="job-1", severity="warning", notify=True)
        self.assertEqual(Event.model_validate_json(e.model_dump_json()).subject, "job-1")

    def test_manifest_defaults_to_not_activated(self):
        m = SupervisorManifest(name="shuri", version="0.1.0", mission="engineering", permissions=["inference.chat"])
        self.assertFalse(m.activated)

    def test_schema_export(self):
        with tempfile.TemporaryDirectory() as d:
            files = export_json_schemas(d)
            self.assertEqual(len(files), 10)
            json.load(open(files[0]))


class RouterTests(unittest.TestCase):
    CFG = "services/inference/models.toml"

    def test_default_is_first_candidate_not_largest(self):
        r = Router(self.CFG)
        self.assertEqual(r.choose("chat").name, "qwen3:8b")
        self.assertEqual(r.choose("code").name, "qwen2.5-coder:14b")

    def test_benchmark_wins_over_default(self):
        r = Router(self.CFG, benchmarks={"qwen3:8b": {"quality": 0.71}, "qwen3:14b": {"quality": 0.78}})
        self.assertEqual(r.choose("chat").name, "qwen3:14b")

    def test_vram_policy_blocks_two_14b_models(self):
        r = Router(self.CFG)
        coder = r.choose("code"); reason = next(c for c in r.roles["reason"].candidates if c.name == "qwen3:14b")
        embed = r.choose("embed")
        self.assertTrue(r.fits([embed], coder))
        self.assertFalse(r.fits([embed, coder], reason), "14B coder + 14B reasoner exceed 15 GB budget")

    def test_eviction_prefers_background_and_never_embeddings(self):
        r = Router(self.CFG)
        embed = r.choose("embed"); chat = r.choose("chat"); coder = r.choose("code")
        order = r.eviction_order([(embed, "foreground"), (chat, "foreground"), (coder, "background")])
        self.assertEqual([c.name for c in order], ["qwen2.5-coder:14b", "qwen3:8b"])


if __name__ == "__main__":
    unittest.main()
