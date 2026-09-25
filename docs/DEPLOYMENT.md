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
> | deployed URL | **built and ran on App Runner, then abandoned** — see the row below |
> | AWS App Runner | **INCOMPATIBLE, do not retry.** Not "untried": the platform refuses every WebSocket upgrade at the edge and Nod is WebSocket end to end (ADR-064) |
> | phone-on-mobile-data smoke | **not done** |
>
> **25 Sep, Gate 5 — App Runner built, ran, and cannot serve this application.**
>
> `scripts/deploy_aws.sh` ran for the first time and needed four fixes to complete (a
> buildspec YAML break, a missing `ecr:UploadLayerPart`, a 3-character App Runner service
> name, and an ARN lookup still using the old name). It then produced a running service at
> `https://ntr7m54smn.us-east-1.awsapprunner.com` with `/healthz` 200, `/readyz` 200 and
> all four readiness conditions green.
>
> **It cannot complete a call, and no configuration change will fix it.** The evidence,
> in the order it settles the question:
>
> | probe | result |
> |---|---|
> | `wss://…/v1/console?session_id=<valid>` | **403** |
> | `wss://…/v1/stream?session_id=<valid>` | **403** |
> | `wss://…/definitely-not-a-route` | **403** — a path the app does not define |
> | `https://…/definitely-not-a-route` | **404**, from the application |
> | App Runner application log | **zero WebSocket attempts recorded**, single instance |
>
> A 403 on a route that does not exist, with nothing in the application log, places the
> refusal at the platform's ingress rather than in Nod. Plain HTTP reaches the app and
> answers 404 from the same path. `/healthz`, `/readyz`, `/metrics`, the demo page and the
> 501s all work, because all of them are plain HTTP; everything the product does is not.
>
> The ECR repository, the CodeBuild project and the App Runner service are **left in
> place** deliberately until a replacement is proven. `make deploy-teardown` removes them.

> **23 Sep, Gate 4e — two of the three blockers cleared, one remains.**
>
> The keychain unlocked and all six commits are **pushed**. There is still no container
> runtime here, so `scripts/deploy_aws.sh` was written to build **remotely**: `git archive`
> of `HEAD` to S3, CodeBuild (privileged, so Docker exists there) building the committed
> `Dockerfile` and pushing to ECR, App Runner running the image with TLS and a `/healthz`
> check. GitHub is deliberately not in that path — a CodeBuild GitHub source needs an
> OAuth connection made in the console and this repository is private.
>
> **The ECR repository was created and exists.** The script is otherwise **unrun**:
> creating the CodeBuild service role, the App Runner ECR-access role and the source
> bucket was refused by this session's permission layer. That is the whole of what is
> left. Run it with credentials permitted to create IAM roles:
>
>     ASSEMBLYAI_API_KEY=... sh scripts/deploy_aws.sh
>
> The buildspec also resolves and prints the `python:3.12-slim` digest and the image size
> — the two §3 items that have been outstanding precisely because they cannot be answered
> without a container runtime. Neither is verified until that build runs; the `Dockerfile`
> still ships a tag.
>
> **The repository is private.** The submission requires a public one, and the corpus
> consent line in TRACK_C_SCRIPT §8 assumes public too. Flipping it is a separate
> decision from deploying and is not made here.
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
                          ├─ /data volume:  traces/, nod.db
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
| `NOD_TRACE_DIR` | `data/traces` | relative by default so a clean clone is ready; every container path below sets `/data/traces` explicitly |

> **Breaking change.** `NOD_CACHE_DIR` was removed — it named a bench cache that
> was designed and never built (ADR-052). `Settings` is `extra="forbid"`, so an
> existing `.env` or task definition still carrying the key stops the process
> booting with `ValidationError: nod_cache_dir  Extra inputs are not permitted`.
> The error names the field, not the variable, which is the connection a reader
> will not make on their own. Delete the line.
| `NOD_DB_PATH` | `data/nod.db` | SQLite, WAL. Container sets `/data/nod.db` |
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

> **Metrics: four of seven are wired, three are not, and the split matters
> (ADR-057, ADR-058).** All of them were declared and never incremented for eleven
> gates; `/metrics` read `0.0` while a live session's trace read `patches_sent: 11`.
>
> | counter | state |
> |---|---|
> | `nod_turns_total` | **wired** — finalised caller turns, wordless flushes excluded |
> | `nod_config_patches_total` | **wired** — counts `config_applied`, i.e. patches the socket took |
> | `nod_session_active` | **wired** — gauge around the stream socket |
> | `nod_controller_errors_total` | **wired** — INV-8's SAFE path |
> | `nod_cuts_total` | **NOT wired**, and `cut.detected` is never published either, so the console's Cuts tile cannot leave zero |
> | `nod_decide_seconds` | **NOT wired**. INV-2's canary is covered by `tests/property/test_budget.py` (p99 < 5 ms over 100 000 iterations) and by nothing in production |
> | `nod_queue_dropped_total` | **NOT wired** |
>
> **For anything in the unwired half, read the trace, not the endpoint.**
> `controller_closed.payload` carries `turns_observed`, `patches_sent`,
> `controller_errors` and `rejected_updates` per session, and it is emitted
> unconditionally, so a session that did nothing still says so.

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
| Disk filling | trace rotation | traces rotate at 64 MiB. There is no bench cache to prune — it was designed and never built (ADR-052) |
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
