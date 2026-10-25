# ADR-0005: Approval tokens and tamper-evident audit

Status: accepted (2026-10-25)

- An approval is a Sentinel DB row: requester, action type, payload hash, reason, affected resources,
  expected consequences, resource needs, estimated external cost, validation evidence URI, rollback
  procedure, `expires_at`, `decided_by`, `decided_at`.
- On approval Sentinel issues a token = base64url(JSON claims) `.` base64url(Ed25519 signature) with claims
  `{aid, action, payload_sha256, scope, nbf, exp, nonce}`. Executors (deploy controller, tool executor)
  verify the signature with Sentinel's public key, check `exp`, check the payload hash matches what they
  are about to do, and record the nonce in `used_nonces` inside the same transaction as the action
  (replay protection). Tokens are single-use.
- Private key lives only in CT 104 at `/etc/project-pat/sentinel/approval.key` (0600). Wade's container
  never has it.
- Audit table: `audit(id, ts, actor, action, details jsonb, prev_hash, hash)` with
  `hash = sha256(prev_hash || canonical_json(row))`. A `verify_chain()` routine runs daily and from the
  Pat UI. Hash chain detects edits/deletions; it does not prevent a hypervisor-root attacker from
  rewriting everything consistently — the daily chain head is therefore also appended to the off-box backup
  log.
