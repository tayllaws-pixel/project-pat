# ADR-0006: Ollama first, config-driven router, benchmark before replacing

Status: accepted (2026-10-25) — backend choice is provisional pending PAT-034 benchmarks

- Phase 2 backend: Ollama (OpenAI-compatible `/v1`, GGUF model pulls, keep-alive and
  `OLLAMA_MAX_LOADED_MODELS` controls). It is the fastest path to a working, GPU-accelerated service.
- Known limits to re-evaluate in PAT-034: llama.cpp server exposes finer control (KV cache quantization,
  speculative decoding); vLLM is faster for concurrent requests but is heavier and expects 16 GB+ for
  comfortable 14B serving. The router and all callers speak the OpenAI chat/embeddings API so the backend
  is swappable.
- `services/inference/models.toml` declares model *roles* (chat, reason, code, classify, embed, vision)
  with candidates and VRAM estimates. The router picks the benchmarked winner per role; a role's model is
  only replaced when a recorded benchmark (`benchmarks/` table) beats the incumbent on that role's metric.
- VRAM policy: at most two "large" (>6 GB) models resident; embeddings model always resident; foreground
  (Pat) requests may evict background (Wade) models; Wade's requests carry `priority=background` and are
  queued behind foreground ones (ADR-0007).
