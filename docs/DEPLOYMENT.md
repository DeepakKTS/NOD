# Nod — Deployment

> ## Status, 23 Sep: **nothing in this document has been executed.**
>
> No deployment exists. There is no URL. Every procedure below is written and unrun, and
> the ones that were "verified" at Gate 4b were verified by grepping the `Dockerfile`,
> which is how a `TODO` saying the digest pin was outstanding got scored as a digest pin
> being present (CLAUDE.md §5).
>
> | | state |
> |---|---|
> | `Dockerfile` | written to §3, **never built** — no container runtime on the dev machine |
> | image size vs 400 MB | **unverified** |
> | `HEALTHCHECK` | **never executed** |
> | base image digest pin | **outstanding**, tag only |
> | read-only root filesystem | **cannot** be set by a Dockerfile; a runtime flag the platform must pass |
> | `/healthz`, `/readyz`, `/metrics` | **green locally** against a real writable volume, all four readiness conditions genuinely satisfied |
> | deployed URL | **does not exist** |
> | phone-on-mobile-data smoke | **not done** |
>
> Two independent blockers, either of which is sufficient: the repository **cannot be
> pushed** (the macOS keychain has stopped returning credentials, so `git push` hangs on a
> username prompt — GitHub itself answers 200), so no platform can build from it; and
> there is no container runtime or platform CLI here to build and push an image instead.
>
> **The account budget is shared, and this is the part to decide before deploying.** The
> AssemblyAI account permits 5 concurrent streams, and Gate 4b measured that a *churning*
> workload sustains only one new session per 16 s (ADR-048). A deployed demo holds
> long-lived sessions rather than churning, so `NOD_MAX_SESSIONS = 2` leaves three slots —
> but the bench's start-rate gate assumes it has the whole account. **Running a live sweep
> while the demo URL is up will produce 1008s and abort the sweep** (a 1008 anywhere voids
> it). Sequence them; do not overlap them.

Target: one container running FastAPI and a static console page, deployed to a single
region with a persistent volume for traces and caches. No external database. **Not
Next.js** — ADR-038 made the demo screen a static HTML file served from the API
container.

## 1. Topology

```
client ──TLS──► edge ──► nod (uvicorn, 1 process, N workers=1) ──► AssemblyAI
                          │
                          ├─ /data volume:  traces/, .nodcache/, nod.db
                          └─ static console (Next.js export) served at /
```

One worker per container. The session registry is in-process, so horizontal scaling
requires sticky routing by `session_id`.

**The binding limit is upstream, not the worker.** A single worker would handle dozens of
sessions comfortably — the controller is `O(1)` per turn and audio forwarding is I/O bound
— but `NOD_MAX_SESSIONS` is **2** (ADR-042), derived from an AssemblyAI account that
permits 5 concurrent streams while a rotating session holds two. Scaling the container
changes nothing until the account changes. Gate 4b measured the constraint as harsher
still for a *churning* workload: a closed session keeps its slot for tens of seconds, so
sustained throughput is roughly one new session per 15 s regardless of concurrency.

## 2. Environment

| Variable | Default | Notes |
|---|---|---|
| `NOD_ENV` | `prod` | `dev` re-raises controller errors after the call (INV-8) |
| `ASSEMBLYAI_API_KEY` | — | required, server-side only |
| `NOD_MODEL` | `universal-streaming-english` | confidence-based turn detection |
| `NOD_API_TOKEN` | — | bearer token for mutating routes |
| `NOD_AUTH` | `off` | auth is cut and unimplemented (ADR-042); `required` is honoured by no route |
| `NOD_ALLOWED_ORIGINS` | console origin | CORS allow-list, never `*` |
| `NOD_MAX_SESSIONS` | `2` | per worker, refused with 429 above it. Derived: the account permits 5 concurrent upstream streams (measured) and a rotating session holds 2 (ADR-042) |
| `NOD_CEILING_MS` | `2600` | default latency ceiling |
| `NOD_PRESET` | `balanced` | default preset |
| `NOD_MODE_DEFAULT` | `adapt` | default mode for new sessions; §5 rolls out with `observe` |
| `NOD_TRACE_RAW` | `0` | `1` disables redaction; requires a documented reason |
| `NOD_TRACE_DIR` | `/data/traces` | on the persistent volume |
| `NOD_CACHE_DIR` | `/data/.nodcache` | TTS and bench caches |
| `NOD_DB_PATH` | `/data/nod.db` | SQLite, WAL |
| `NOD_LOG_LEVEL` | `info` | structlog |
| `LLM_PROVIDER` / `LLM_API_KEY` | — | agent brain |
| `TTS_PROVIDERS` | `browser` | comma-separated fallback chain, in order |
| `TTS_API_KEY_*` | — | per cloud provider |
| `NOD_EXACT_QUANTILES` | `0` | validation only, slower |

Secrets are injected by the platform's secret store. `.env` is git-ignored;
`.env.example` lists every key with an empty value and a comment.

## 3. Container

- Multi-stage: build the Python wheels in stage one, copy into a slim runtime in stage
  two. **No Next.js stage** — ADR-038 made the demo screen a static HTML file served from
  the API container, so there is nothing to compile.
- Non-root user (uid 10001, fixed so a volume's ownership can be set without inspecting
  the image), `/data` the only writable mount.
- **Read-only root filesystem is a *runtime* flag, not a Dockerfile directive** — `docker
  run --read-only` or compose's `read_only: true`. It is listed here under Container and
  cannot be satisfied by the image, so it has to be set on the platform; an image built to
  this spec is not thereby read-only. Checked at Gate 4b, where a first pass "verified" it
  by grepping the Dockerfile and would have reported it present forever.
- `HEALTHCHECK` hits `/healthz`.
- Pinned base image by digest, not by tag. **Outstanding: the committed `Dockerfile`
  uses `python:3.12-slim` by tag.** The digest cannot be resolved on a machine without a
  container runtime, and writing an unverified one would be worse than the tag. Resolve
  and pin it on the machine that first builds the image; until then the build is not
  reproducible.
- **Unbuilt and unmeasured.** No container runtime exists on the development machine, so
  the image has never been built: its size is unverified against the 400 MB target, the
  `HEALTHCHECK` has never executed, and the digest pin below is outstanding for the same
  reason. The `Dockerfile` is written to this spec and is committed; nothing here has been
  run.
- Target image under 400 MB. `librosa` and `soundfile` are bench-only dependencies and are
  excluded from the runtime image via an extras group.

## 4. Health semantics

| Endpoint | Green when |
|---|---|
| `/healthz` | the process is alive; never touches upstream |
| `/readyz` | config loaded, `/data` writable, SQLite reachable, capability probe cached |
| `/metrics` | always; Prometheus format |

`/readyz` deliberately does not call AssemblyAI. An upstream outage must not take the
container out of rotation, because `observe` mode and replay mode still work.

## 5. Rollout

1. Deploy with `NOD_MODE_DEFAULT=observe`. Nod profiles and traces but sends no patches.
2. Watch for 24 h (or one demo session): `nod_decide_seconds` p99, `nod_queue_dropped_total`,
   `nod_upstream_reconnects_total`, `controller_error` count.
3. Flip to `adapt` for a fraction of sessions via `nod_mode` on the connection URL.
4. Compare cut rate between the two cohorts using committed traces.

This staged path exists because `observe` mode is genuinely zero-risk: it cannot change a
call's behaviour. It is the honest answer to "how do I trust this in production".

## 6. Rollback

- Config-level: set `nod_mode=off` on the connection URL. Nod becomes a transparent proxy
  immediately, no redeploy.
- Image-level: previous image digest, `/data` is forward and backward compatible because
  traces are append-only with a versioned line schema.

## 7. Runbook

| Symptom | First check | Action |
|---|---|---|
| Agent feels slow for everyone | `nod_config_patches_total` and the ceiling | lower `NOD_CEILING_MS`; check for a stuck `FROZEN` state |
| Cuts rising | `controller_error` count, capability flags | if `capability_degraded` fired, the confidence axis is off and the silence axis alone is carrying; check the model name |
| `decide()` p99 above 5 ms | profiler exact-quantile flag | ensure `NOD_EXACT_QUANTILES=0` in prod |
| Queue drops climbing | `nod_queue_dropped_total{queue}` | telemetry drops are benign; audio drops are not — investigate upstream latency |
| Reconnect loop | upstream status, key validity | Nod stops after 5 attempts and closes cleanly; check credits |
| Disk filling | trace rotation, cache size | traces rotate at 64 MiB; prune `.nodcache` with `make bench-clean` |
| Console janky | blurred layer count | six-layer budget; confirm the waveform is not inside a blurred stack |

## 8. Data retention

- Traces default to redacted and are retained 30 days, then deleted by a scheduled job.
- Raw retention requires `NOD_TRACE_RAW=1` **and** a per-session flag, and is off by
  default in every preset.
- Caller audio is never persisted unless a session is explicitly marked for retention.
- The SQLite index holds metadata and file pointers only, never transcript text.

## 9. Pre-submission checklist

- [ ] `make check` green on a clean clone
- [ ] `make bench` green offline with no API key
- [ ] `docs/RESULTS.md` regenerated, traces committed
- [ ] every README number traceable to a bench run id
- [ ] deployed URL loads on a phone over mobile data
- [ ] replay demo works with the API key removed from the environment
- [ ] no secret in the client bundle (grep the build output)
- [ ] `pip-audit` clean, dependencies pinned
- [ ] honest-scope section present and unsoftened
- [ ] LICENSE (MIT), CONTRIBUTING, and corpus license manifest present
