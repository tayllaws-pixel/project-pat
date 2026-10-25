# inference (CT 101)
Ollama behind a priority shim (PAT-024, not yet written). Provisioned by `patctl` step `svc-inference`
(Phase 2, not yet written): installs Ollama, drops `ollama.env` into a systemd override, pulls the default
candidate per role from `models.toml`, and runs `benchmarks/run.py` to record measured tokens/s, VRAM and
latency. No model is pinned until a benchmark exists.
