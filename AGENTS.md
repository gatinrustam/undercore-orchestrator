# Orchestrator development

Read docs/internal/index.md and docs/public/api.md before changing lifecycle/API.
Follow docs/public/architecture.md, lifecycle.md and compatibility.md.
contracts/architecture.json enforces import boundaries; remove resolved exceptions,
and do not widen the baseline merely to make checks pass.
This repository owns technical VPN assignments only. The caller authorizes users,
paid slots and expiry. Never add passwords/payments to the orchestrator.
Preserve device/connection identity, idempotency, confirmed revocation and bounded
recovery. Real node mutations and deployments require user authorization.
All authored Markdown belongs in docs/internal or docs/public, except AGENTS.md and the root README.md entry point.
Keep credentials, production settings and journals outside Git. No secret-bearing
responses in logs or fixtures. Run pytest and scripts/check_contracts.py after changes.
Run scripts/check_architecture.py after changes to runtime modules or architecture policy.
Test fixture agent code is pinned; do not weaken its lifecycle to make tests pass.
Do not spawn subagents unless the user asks for them.
