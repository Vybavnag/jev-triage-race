# Production hardening backlog

Deliberately not built. Each item has the condition that should trigger it —
until then it is cost or complexity without a reason.

| Item | Trigger |
|---|---|
| Durable race history (SQLite or Postgres instead of in-memory runs) | Someone asks to share a race result as a link, or wants results to survive a restart |
| Multi-user auth beyond the single `RACE_TOKEN` | More than one person needs their own quota or their own history |
| Redis-backed rate limiting and race lock | The app runs on more than one replica, where the in-process lock and bucket stop being global |
| Per-request spend accounting and a hard budget ceiling | The app is public and provider dashboard caps stop being sufficient |
| Structured logging or tracing export | A failure happens that stdout logs cannot explain |
| Dependency pinning with hashes plus scheduled `pip-audit` / `npm audit` | The repo gains external contributors or is used in a regulated setting |
| Dataset expansion and inter-annotator agreement | Someone wants to cite the accuracy numbers rather than use them as a smoke test |

Deliberate non-goals, not backlog items: this repo holds application code only.
Build, deploy, routing, TLS, and secret injection belong to the platform.
