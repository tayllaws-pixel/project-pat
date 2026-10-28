"""Agent handlers for this supervisor — this file is what Wade generates per supervisor.

Contract: each handler is  fn(job, ctx) -> result | (result, evidence) | (result, evidence, state).
  job: {"id","kind","title","payload", ...}
  ctx: {"sentinel_url","requester_token","actor_id","inference_url","code_model"}
Handlers run in-process in this supervisor's container. They may compute, call the LLM at
ctx["inference_url"], and REQUEST approvals from Sentinel — but anything that changes the world must go
through a human-approved Sentinel approval. HANDLERS maps a job kind to its handler.
"""
import time


def echo(job, ctx):
    payload = job.get("payload") or {}
    result = {"echo": payload.get("message", "")}
    evidence = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    return result, evidence


HANDLERS = {
    "echo": echo,
}
