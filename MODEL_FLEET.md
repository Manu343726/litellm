# Model Fleet — a built-in LiteLLM feature

A **model fleet** supervises a set of child model-serving processes, exposes them behind
one published OpenAI-compatible port, and registers one LiteLLM deployment per model.
The proxy becomes the control plane for something that was previously a pile of docker
compose services.

Everything is DB-backed and editable in the Admin WebUI. **Nothing lives in `config.yaml`.**

- **Fork:** `git@github.com:Manu343726/litellm.git` (fork of `BerriAI/litellm`)
- **Upstream:** `git@github.com:BerriAI/litellm.git`
- **Base:** `0fe4028cd9`, version **1.104.0**
- **Field report (the working prototype this came from):** `~/docker-compose-services/litellm/README.md`

Section numbering is load-bearing: the field report's known-gaps list cites `MODEL_FLEET.md
§7.1`. Do not renumber.

---

## 1. What the feature is

Eight models sit on OpenCode Zen's free tier. Seven of them answer any direct API call with

> OpenCode's free tier can only be used from within OpenCode

and only `space-bunny-free` returns 200. The only way to reach the other seven is to run a
real `opencode` process per model, because opencode is the client Zen trusts.

```
litellm ──▶ fleet :8080/v1/chat/completions
               │  the `model` field selects the instance
               ├──▶ <model-a>   opencode backend :4200  + gateway :8200
               ├──▶ <model-b>   opencode backend :4201  + gateway :8201
               └──▶ <model-n>   opencode backend :420n  + gateway :820n
```

One published port, and the fleet dispatches internally by model name. LiteLLM registers
one deployment per model, all against the same base URL.

**Why one instance per model rather than a pool.** The OpenAI-to-opencode shim serialises
every request behind a single global mutex. One instance per model turns that global
ceiling into a per-model one.

**Why routing by model name rather than by port.** The prototype's first version gave each
model its own published port (`8600 + index`). That was two problems at once: seven
published ports is seven chances to put the fleet on a LAN, and the index mapping is a
silent-corruption hazard, because a model on the wrong port still answers HTTP 200 with a
*different* model's content.

### Non-goals

- **Not for paid models.** They work directly and report real token usage. This lane
  reports usage 0 because the shim hardcodes it. Routing paid models through a fleet trades
  accurate billing for reachability.
- **Not a general job runner.** It supervises model-serving children behind the
  OpenAI-shaped contract the router already speaks.

---

## 2. Design rules

### R1 — Everything is DB-backed and UI-editable

No fleet setting in `config.yaml`. The proxy already has most of the machinery (§4), and the
parts that are missing are enumerated in §7.

### R2 — Declare once, apply through an interface

Desired state is declared in one place and applied through an API, converging on re-run.
Never in two places (a file *and* a table) that can drift.

### R3 — Credentials are owned, not copied

The secret is stored **once**, in LiteLLM's encrypted credential table, and referenced by
name from the fleet row (`litellm_credential_name`). The supervisor decrypts it only at
child-provision time and writes it into that child's own store, so the fleet's runtime
volume is the only other copy.

The dependency runs one way: the proxy configures the fleet. **The fleet never holds
`LITELLM_MASTER_KEY`.** The prototype briefly accepted it, which inverted the ownership
(the fleet was configured *through* LiteLLM yet held LiteLLM's most powerful credential in
order to be configured), and it was removed.

For the opencode adapter this column is null and that is correct, not a gap. The free tier
is not credential-gated. It was verified by starting a throwaway opencode with an empty
`HOME` and a scrubbed environment (`env -i`, nothing inherited): it still reached Zen and was
answered with a rate limit, not an auth failure. **The UI shows `none (free tier)` rather
than a dash**, because a dash reads as "something failed to load".

### R4 — The fleet's two planes; LiteLLM's endpoints are all admin

This rule is about the *child fleet's* HTTP surface, not about LiteLLM's own fleet endpoints.
Conflating the two is the easiest mistake to make here.

| Plane | Endpoints | Auth |
|---|---|---|
| Fleet model plane | `GET /v1/models`, `POST /v1/chat/completions` | none |
| Fleet admin plane | deploy, teardown, accounts, topology | bearer |
| LiteLLM's fleet endpoints | everything under `/fleet`, `/v1/fleet` | `PROXY_ADMIN` |

The fleet model plane publishes no host port, so it is only reachable from the private
network the proxy runs on. Requiring a bearer there bought no isolation while creating a
real failure mode: a secret written in one place and checked in another, whose misalignment
surfaces as a 401 from an upstream whose logs say nothing.

The fleet admin plane stays locked even on the internal network, because it changes state
and hands out provider credentials.

### R5 — State the UI displays is measured, and "measured" has two meanings

This rule is the one the first draft got wrong, and the field report proves it.

A liveness probe is not a health probe. When Zen's free tier throttles, the opencode backend
returns **HTTP 500 with an empty body**, the gateway reports it as `Failed to create
OpenCode session`, and **the instance still reports healthy** because both the backend and
the gateway answer. A fleet under rate limiting and a fleet that is fine are
indistinguishable from `/health`.

So health is two independent facts, and the UI must be able to show both:

- **lifecycle** — is the child process up. Owned by the supervisor, which owns the pid.
  `unknown` → `starting` → `healthy` → `stopped`.
- **readiness** — can it serve a request right now. Measured by an actual probe through the
  fleet's own model plane. `ready` → `degraded` → `unavailable`.

A rate-limited instance is `lifecycle=healthy, readiness=degraded`. Collapsing these into
one column is what made the prototype's dashboard lie. `degraded` is the state the first
draft omitted, and it is the one that matters.

R5 also means token counts for this lane are 0 **because the shim reports 0**, not because
they were estimated, and an empty dashboard must be distinguishable from a broken one.

### R6 — The feature is generic; the adapter owns the command

"Fleet" is the abstraction, opencode is one adapter. A consequence worth stating loudly:
**an operator never supplies a command or argv.** The adapter owns the executable and the
flag shape; only adapter-specific, typed arguments are operator-supplied. This matters
because a fleet row spawns N children, a strictly larger blast radius than the one
subprocess MCP stdio already spawns from a DB row (§4.5).

---

## 3. Corrections to the earlier findings

Two claims made while prototyping **outside** the codebase were wrong, and both were
version-dependent.

| Claim (against 1.103.0) | Reality in 1.104.0 |
|---|---|
| "`/config/update` does not exist, so router policy is file-only" | **It exists** (`litellm/proxy/proxy_server.py:17721`) and writes `router_settings` including `routing_groups` to the DB, then hot-applies via `Router.update_settings`. Routing groups are creatable via API and UI. |
| "The Admin UI's Add Credential / Add MCP Server forms fire no network request" | **Not reproducible.** Both POST for real, with tests asserting it. The 1.103.0 observation was the MCP card's *optimistic append*: the row appears, then a refetch drops it for a non-admin submission. |

An automated pass attributed both to "fork-local changes". They are not. The fork is clean
(`0` ahead of `upstream/main`). **The delta was 1.103.0 → 1.104.0, not local modification.**
Worth stating plainly, because "probably a local change" is an expensive assumption to act on.

### Corrections to this document's own first draft

Found by cross-referencing the draft against the code at this commit:

1. **§7.1's numbers were wrong.** Not 26 silently-ignored router settings but **33**, and
   `Router.get_valid_args()` is **58**, not 87. The draft's list of 27 names was a strict
   subset of the real 33, missing `alerting_config`, `allowed_fails_policy`,
   `background_health_check_model_groups`, `cache_kwargs`, `cache_responses`, and
   `caching_groups`. The two caching omissions are the persuasive ones: an operator can turn
   on router caching through `/config/update`, get HTTP 200, and see no caching.
2. **`LEGACY_PAGE_ROUTES` is not optional.** `legacyPageRoutes.test.ts:38-46` walks every
   non-external sidebar leaf and asserts a redirect exists. The draft called it optional.
3. **`access-groups/` is the right structural model and the wrong transport.** Its four hooks
   use raw `fetch`, grandfathered in `eslint-suppressions.json:350-369`. Copy the file
   layout, keys factory and mutation shape; use `$api` for the calls.
4. **The page registrations are vitest-enforced, not lint-enforced.** Only `max-lines`,
   PascalCase filenames and the raw-`fetch` rule are lint.
5. **There is no `uiHref` registration.** It is a pure function at `src/utils/uiHref.ts:14`.
6. **`Section` has five members, not four.** `ui_settings` exists as a `DbRow` only.

---

## 4. What LiteLLM already gives us

Every path below is verified at this commit.

### 4.1 A file-vs-database arbitration layer

`ProxyConfig` holds four `SettingsStore`s (`proxy_server.py:5186-5201`). The merge rule at
`litellm/proxy/config_resolvers/settings_rules.py:93-103` is "Config wins. A key the config
file declares is config-owned, whatever the database holds", and writing a file-owned key
through the DB raises `ConfigOwnedKeyError`.

**This is exactly the mechanism R1 needs.** A new `fleet_settings` section becomes
file-*optional* by adding a `SettingsStore` and widening the `Section` `Literal`
(`settings_rules.py:11-17`). `DbRow` is a plain alias of `Section`, so widening it widens
both.

Consequence worth internalising: **`config.yaml` still wins per key.** "No config in file" is
achieved by *not declaring* the keys, not by adding a precedence rule. An operator who puts
a fleet key in the file gets the UI correctly refusing to edit it, and
`/config/update` correctly rejecting it via `reject_config_owned_writes`. The feature ships
with no precedence change at all.

Note `settings_rules.py:110-111`: a stored `null` counts as absent. That is the clearing
mechanism §7.1 depends on.

### 4.2 A DB-backed model list that reconciles

`ProxyConfig._get_models_from_db` (`proxy_server.py:7827-7853`) reads
`LiteLLM_ProxyModelTable`; `add_deployment` (`:7855-7884`) is the orchestrator, serialised on
the module-level `MODEL_RECONCILE_LOCK` (`:2541`) because `llm_router` is a module global.

Four details to copy rather than reinvent:

- `_get_models_from_db` returns **`None` on DB failure, never `[]`**, so a transient error
  cannot evict live deployments. Empty fleet and unreachable database stay distinguishable.
- `ReconcileOutcome` is captured **inside** the lock (`:7936-7941`). Mixing a snapshot taken
  under the lock with one taken after release makes a concurrent write look like collateral
  damage, so `raise_if_reload_degraded_serving` returns 500 rather than claiming success.
- `_delete_deployment` (`:7042-7125`) builds a combined id list of DB ids *and* config ids,
  so DB wins on collision and config-only deployments survive reconcile.
- The startup path (`proxy_server.py:6733-6754`) honours anything in `get_valid_args()`,
  which is why narrowing `/config/update` does not change what a file may set.

### 4.3 Cross-pod live reload

`config_sync_pubsub.py` publishes on `litellm_proxy.config_change` from any wrapped table
write; every pod's `ConfigSyncSubscriber` resyncs (debounce 1 s + jitter, 10 s minimum resync
interval). `PrismaTableRepository.table` wraps writes automatically
(`table_repositories.py:33-36`) **but only for table names in `_CONFIG_SYNCED_TABLE_NAMES`**
(`config_sync_pubsub.py:42-60`).

> A new fleet table added to that list gets cross-pod reload for free. A new table *not*
> added to it is visible only to the writing pod until the next 30 s poll: a bug that looks
> like flakiness under a multi-worker deployment and is invisible on a single pod.

There is a mirror list in `tests/test_litellm/proxy/common_utils/test_config_sync_pubsub.py:34-52`
that asserts equality, so the omission fails loudly rather than silently.

### 4.4 A live supervisor with readiness-by-pid

| Precedent | What to take |
|---|---|
| `PgBouncerProcess` (`litellm/proxy/db/pgbouncer.py:501-639`) | A real supervisor: spawn under a lock, free-port precheck, readiness probe, daemon thread, restart on unexpected exit, graceful stop. Generalise for N children. Synchronous, thread-based. |
| `start_metrics_server_process` (`prometheus_metrics_server.py:138-155`) | The child **echoes its own pid** (`PID_HEADER`, `:34`; `_answered_by`, `:110-116`), so a squatter on the port cannot pass. Directly relevant: this machine had a VS Code process squatting a port that answered health checks with 200. |
| `query_engine_reaper.py` | The orphan problem, already solved: `prctl(PR_SET_CHILD_SUBREAPER)` plus a `/proc` sweep filtered to **direct children**, with batched SIGTERM/SIGKILL so N orphans cost one grace period. Reuse rather than rewrite. |

Both children start from the **CLI supervisor**, not from `proxy_startup_event`:
`proxy_cli.py:1448` (pgbouncer), `:631` (reaper). The gunicorn master reserves itself for
forking via `reserve_process_for_forking`, but that guard is **Rust-specific** — it forbids
the Rust extension's native threads, and both pgbouncer and the reaper run Python threads in
the master happily. §6 depends on this.

### 4.5 The precedent for spawning processes from a DB row

MCP stdio already runs a subprocess configured by a DB row, and it is careful in three ways
the fleet must copy:

- A **command allowlist**, `MCP_STDIO_ALLOWED_COMMANDS` (`constants.py:202-205`), extensible
  by env var. The comment accepts the residual risk (allowlisted runtimes can still execute
  code via args) on the grounds that the endpoint requires `PROXY_ADMIN`.
- **Defense in depth at both layers**: a Pydantic validator rejects non-allowlisted commands
  on write (`proxy/_types.py:1578-1593`), and `_create_mcp_client` re-checks at spawn time
  to catch legacy rows predating the allowlist.
- **Refused from non-admins**, with the reasoning stated at
  `mcp_management_endpoints.py:1405-1421`: a team member proposing a server config that an
  admin rubber-stamps is local code execution.

A fleet row spawns **N** children rather than one, so the allowlist and the admin gate both
carry more weight, not less.

### 4.6 The UI model to copy

`src/app/(dashboard)/access-groups/` is the disciplined shape: a 9-line `page.tsx`, a
`_components/` tree, pure `.ts` hooks under `hooks/accessGroups/` with
`createQueryKeys(...)` and `invalidateQueries` in `onSuccess`, and payload building
extracted into a pure module with a co-located unit test.

Do **not** add to `networking.tsx`. It is ~5,900 lines and still contains raw `fetch`. New
calls go through `$api` from `@/lib/http/api`.

### 4.7 What LiteLLM does *not* have

Stated so nobody goes looking:

- **No base class or mixin for "DB-backed resource with health status."** MCP's lives on
  `MCPServerManager.health_check_server`, agents' on a module-level function in the endpoint
  file, models' in `litellm/proxy/health_check.py` plus an append-only history table. Three
  unrelated designs, and `status` is a bare `String` in all of them.
- **No canonical health field shape.** Four different shapes across four tables, and four
  different status vocabularies. The only shared *named* health type is
  `IntegrationHealthCheckStatus`, and it is for logging integrations.
- **No table that supervises a child process.** Nothing in `schema.prisma` tracks one.
- **No lease table with a reader.** `LiteLLM_CronJob` exists in all three schema copies and
  has **zero Python readers or writers**; every `cronjob_id` in the codebase is a
  `PodLockManager.acquire_lock()` keyword argument. Its `ttl` column has no comparison
  function anywhere and the `JobStatus` enum has no consumers. Its shape is right and its
  implementation does not exist.
- **No server-side capability enum.** `rolesWithCapability` is a UI-only TypeScript map. There
  is no Python equivalent, so a new server-side capability would be new ground.

---

## 5. Data model

```prisma
// The fleet itself: one row. Which adapter, which upstream, how it is reached.
model LiteLLM_ModelFleetTable {
  fleet_id        String   @id @default(uuid())
  fleet_name      String   @unique
  adapter         String                    // "opencode" | ...  (R6)
  base_url        String                    // the single published port
  enabled         Boolean  @default(true)
  settings        Json     @default("{}")   // non-secret, adapter-specific
  // R3: a reference to a credential stored once in LiteLLM's own table, never a copy
  litellm_credential_name String?
  // R5: lifecycle is supervised, readiness is probed
  status              String?  @default("unknown")
  readiness           String?  @default("unknown")
  last_health_check   DateTime?
  health_check_error  String?
  created_at DateTime @default(now()) @map("created_at")
  created_by String
  updated_at DateTime @default(now()) @updatedAt @map("updated_at")
  updated_by String
  @@index([status])
}

// One row per model the fleet serves.
model LiteLLM_FleetModelTable {
  fleet_model_id String @id @default(uuid())
  fleet_id       String
  model_name     String                    // the name clients call
  upstream_model String                    // what the adapter asks the backend for
  enabled        Boolean @default(true)
  status              String?  @default("unknown")
  readiness           String?  @default("unknown")
  health_detail       String?               // why degraded; see R5
  last_health_check   DateTime?
  health_check_error  String?
  child_pid       Int?                     // supervisor-owned, never operator-set
  backend_port    Int?
  created_at DateTime @default(now()) @map("created_at")
  created_by String
  updated_at DateTime @default(now()) @updatedAt @map("updated_at")
  updated_by String
  @@unique([fleet_id, model_name])
  @@index([fleet_id, status])
}
```

### 5.1 No `order`, `weight`, `rpm` or `tpm` on the fleet row

The first draft put all four on `LiteLLM_FleetModelTable`. That is a second source of truth
for knobs the router reads off the deployment, and it is the wrong one:

- `weight`, `rpm` and `tpm` are read **only** from `litellm_params` on
  `LiteLLM_ProxyModelTable`, by `_metric_weight` (`router_strategy/simple_shuffle.py:17-24`).
  A value on the fleet row would never be seen.
- `order` is read from `litellm_params` first and `model_info` second
  (`litellm/utils.py:5140-5150`), and `get_order_filtered_deployments` picks the **minimum**
  order group, so lower wins and higher tiers fail over.

So the fleet row is **identity and lifecycle only**. When the supervisor registers a
deployment per model it writes `litellm_params.model`, `.api_base` and `.rpm`, plus fleet
provenance in `model_info`. `order`, `weight` and `rpm` are then edited in the **existing
Models page**, alongside every other deployment, with no second UI to build and no drift.

`simple_shuffle` tries `weight`, then `rpm`, then `tpm`, and falls back to uniform
`random.choice` when all are unset, so a fleet needs no weights to balance.

### 5.2 No `tpm`, ever

`tpm` is **not** in the schema. A token cap cannot be evaluated on a lane whose adapter
hardcodes `usage` to zero, so a `tpm` here is a limit that either does nothing or blocks
everything depending on a rounding accident. `rpm` does double duty (rate limit *and*
shuffle weight) and is the only one the prototype uses.

For the generic case the adapter declares whether it reports usage, and the deployment
creation path omits `tpm` for adapters that do not. That is honest and it generalises; a
column with a comment saying it is meaningless for the only adapter is the thing that rots.

### 5.3 Real columns, not a JSON blob

`prisma-client-py` has no JSON path filtering. That is why `_get_team_deployments`
(`model_management_endpoints.py:1733-1765`) filters on a `model_name` string prefix and then
re-confirms `model_info.team_id` in Python. A fleet whose state lived in JSON would be
unqueryable in SQL and would need that workaround for every filter the UI wants. `status`,
`readiness` and `health_detail` are indexed because the UI's default view is "show me what is
not ready".

### 5.4 The encrypted-column trap

`litellm/proxy/db/master_key_migration.py:26-46` holds a **closed registry** of every
encrypted column, walked at boot when `LITELLM_MIGRATE_FROM_MASTER_KEY` is set. A new
encrypted column that is not registered there is **silently left behind on master-key
rotation**: it works today and rots later.

So: **if a new encrypted column is added, `_SecretColumn(...)` is part of the change, or
there is no new encrypted column.** Per R3 the latter is preferred, and it is what the schema
above does: `litellm_credential_name` is a reference, and `LiteLLM_CredentialsTable` is
already in the registry.

---

## 6. Supervisor design

### 6.1 Where it runs: the CLI supervisor, not the worker

This resolves the first open question from the first draft, and it changes the design.

The supervisor belongs in the **gunicorn master**, beside pgbouncer and the orphan reaper
(`proxy_cli.py:1448`, `:631`), not in `proxy_startup_event`. Three consequences:

- **Exactly one process owns the children.** N workers each spawning a child for the same
  model is a port race, and the loser is not idempotent. In the master that problem is
  designed out rather than leased around.
- **No Redis lease in the in-cluster case.** The lease exists to stop duplicate spawns
  across processes. If there is one owner by construction, `PodLockManager` is not needed
  and the fleet does not inherit a hard Redis dependency. That matters, because
  `PodLockManager.acquire_lock` returns `None` with no Redis and every caller then silently
  runs zero times.
- **Reconcile and router stay separated.** The master owns children and writes the DB. Each
  worker's `Router` converges through the existing `config_sync` publish, which is the
  mechanism LiteLLM already uses for exactly this.

Across **multiple pods** there is more than one master, and that is the case
`PodLockManager` is for: `acquire_lock("fleet_reconcile", ttl=...)` with `allow_reentrant=True`
so the holder keeps winning, and TTL sized **larger than the longest reconcile pass** or two
pods can both believe they own the fleet. The lease should be held around *spawn-and-probe*,
not the whole pass, because the probe is a blocking wait and holding a lease across it
extends the handover gap.

The "acquire and never release" idiom (`gateway_request_tracking.py:205-217`) is the better
fit than acquire/release-in-a-`finally`: a clean shutdown does not then hand the port race to
another pod mid-teardown, and the gap is bounded by TTL rather than by how fast anyone
notices the holder is gone (`proxy/db/gateway_request_tracking.py:205-217` states the reasoning).

### 6.2 The reconcile loop

```
fleet_reconcile            (CLI supervisor; APScheduler interval, staggered)
  ├─ acquire lease                       (multi-pod only)
  ├─ read LiteLLM_FleetModelTable
  ├─ for each enabled model with no live child: spawn
  │     └─ readiness probe that checks the CHILD'S OWN PID
  ├─ for each child whose row is gone or disabled: stop + reap
  └─ release lease

fleet write endpoint       (worker)
  └─ validate → persist → publish config_change → kick the reconcile
```

Decisions and why:

- **Reconcile, don't event.** A poller converges from any state (crash, restart, external DB
  edit) and needs no exactly-once delivery. An event bus adds a correctness problem to buy
  latency a UI does not notice.
- **Readiness must verify the child, not the port.** Port-only probes pass against whatever
  else holds the port. Echoing the pid is the existing fix
  (`prometheus_metrics_server.py:110-116`), and PgBouncer independently reaches the same
  conclusion two ways (`_port_open` *and* `_unix_socket_open`, `pgbouncer.py:558-573`).
- **Stop is surgical.** Signal the recorded pid only after confirming `/proc/<pid>/cmdline`
  matches what was spawned. A broad `pkill -f opencode` on a machine running opencode kills
  the operator's own agent session, which happened to this project.
- **Do not pass `jitter=` to `add_job`.** APScheduler's `normalize()`/`_apply_jitter()` was
  attributed 35 GB in a memray profile (`proxy_server.py:10332-10338`); the repo randomises
  the interval up front instead and has a `apply_scheduled_job_stagger` pass.

### 6.3 Readiness versus liveness, mechanically

R5 needs this to be real, not just documented.

- **Lifecycle** comes from the supervisor: `unknown` before the first spawn attempt,
  `starting` between `Popen` and readiness, `healthy` once the child has answered, `stopped`
  after a deliberate teardown. The supervisor is the single writer of child state.
- **Readiness** comes from a probe **through the fleet's own model plane**, not from the
  child's `/health`. This is the whole fix for the field report's worst finding. For the
  opencode adapter the probe classifies a bare HTTP 500 from the shim as `degraded` with
  `health_detail` naming the upstream rate limit, because that is what a 500 there *means*.

The probe must not read token counts to decide readiness, since this adapter reports zero.
It must not attempt a real completion either, or it burns quota and trips the rate limit it
is trying to detect.

### 6.4 Restore versus configure

On restart the supervisor restores children from the DB: restoration, not configuration. Two
properties follow, and both are bugs if missed:

- A model deleted through the API must **not** come back. So `DELETE` removes the row, and
  restore reads the rows.
- A child that is alive is not restarted. Readiness is per-model, and the supervisor is the
  single writer of child state.

The child's own directory is **derived** state, owned by the supervisor. It is created at
provision and removed at teardown, never read as an independent source of truth. That is the
rule that makes "deleted stays deleted" true rather than aspirational.

---

## 7. Gaps that must be closed

### 7.1 33 router settings are accepted, persisted, and silently ignored

`/config/update` accepts anything in `Router.get_valid_args()` minus
`ROUTER_SETTINGS_MANAGED_OUTSIDE_CONFIG`, persists to `LiteLLM_Config`, then hot-applies via
`Router.update_settings` (`litellm/router.py:12131-12186`), which applies only
`RUNTIME_UPDATABLE_ROUTER_SETTINGS` and drops everything else at **DEBUG** level:

```python
else:
    verbose_router_logger.debug("Setting %s is not allowed", var)
```

Computed at this commit: `get_valid_args()` 58, runtime-updatable 18, accepted 51, rejected
outright 8, and **accepted-but-ignored 33**:

```
alerting_config, allowed_fails_policy, background_health_check_model_groups,
cache_kwargs, cache_responses, caching_groups, client_ttl, content_policy_fallbacks,
debug_level, default_fallbacks, default_litellm_params,
default_max_parallel_requests, default_priority, deployment_affinity_ttl_seconds,
disable_cooldowns, enable_health_check_routing, enable_pre_call_checks, guardrail_list,
health_check_ignore_transient_errors, health_check_staleness_threshold, max_fallbacks,
model_group_affinity_config, plugins, polling_interval, provider_budget_config, redis_db,
redis_host, redis_password, redis_port, redis_url, set_verbose, stream_timeout,
tag_filtering_match_any
```

Each returns **HTTP 200**, is written to `LiteLLM_Config`, and does not take effect until the
proxy restarts. The only record is a DEBUG line. This is worse than a missing feature: a
green write that does nothing.

Two options, and the choice is a real design decision:

- **(a) Reject them.** Narrow the accepted set to `RUNTIME_UPDATABLE_ROUTER_SETTINGS` and
  return 400 with "requires a restart". Small and honest.
- **(b) Support them.** Extend `RUNTIME_UPDATABLE_ROUTER_SETTINGS` where the Router can
  genuinely re-derive the value, reject the rest. Larger, smaller file-only remainder.

**Recommend (a) as a standalone first PR**, independent of the fleet. Preserve the asymmetry:
the *startup* path honours anything in `get_valid_args()`, so narrowing `/config/update` does
not change what a file may set.

Three things the first draft missed, all of which change the size of the job:

1. **`/config/update` merges and never deletes.** Persistence is
   `{**existing, **router_settings_updates}` (`proxy_server.py:17910`). Keys removed from the
   accepted set are **never deleted** from the `router_settings` row, become **unwritable**
   through the endpoint, and become **dead** at read time. There is no per-key delete path, so
   an admin is left reading a setting as live while the router ignores it, and debugging the
   wrong layer. The escape already exists: a stored `null` counts as absent
   (`settings_rules.py:110-111`) and `JsonValue` admits `null`, so `"key": null` clears it.
   The accepted-set check must therefore admit null values, or the clear is rejected and the
   orphan is permanent.
2. **Already-persisted values keep working at boot.** They reach `Router(...)` through the
   startup path. Nothing breaks, but the honest outcome is that a value written before the
   fix stays effective until someone clears it.
3. **`_int_settings` is a second hardcoded list.** `router.py:12135-12141` casts
   `timeout`, `num_retries`, `retry_after`, `allowed_fails`, `cooldown_time` separately from
   `RUNTIME_UPDATABLE_ROUTER_SETTINGS`. Any change to the runtime set must keep the two
   agreeing or the value is stored as a string.

This is a public endpoint, so the change is breaking for anyone whose automation sends a
now-rejected key and currently receives 200. A **distinct error code or error key** for
"requires a restart" versus "invalid key" lets callers tell the two apart, which is the
difference between a caller that adapts and one that just breaks.

### 7.2 The encryption algorithm is chosen in `config.yaml`

`general_settings.encryption_algorithm` (`encrypt_decrypt_utils.py:37-56`) picks
`aes-256-gcm` or `xsalsa20-poly1305`. It is a `general_settings` key, so it is DB-settable,
but it is not exposed in the UI and it must be readable before the UI exists. Acceptable,
but should be stated rather than discovered.

### 7.3 Migration discipline is strict

- **Three copies** of `schema.prisma` must stay byte-identical (root, `litellm/proxy/`,
  `litellm-proxy-extras/litellm_proxy_extras/`). Symlinks are rejected by CI
  (`.github/workflows/check-schema-sync.yml`).
- Migrations are **generated**, not hand-written: `uv run --with testing.postgresql python
  ci_cd/run_migration.py "<name>"`. The generator refuses to emit `DROP` without
  `--allow-destructive`, and refuses to run if HEAD is behind the default branch.
- **Pure DDL only.** No `UPDATE`, `DELETE`, `MERGE`, or `INSERT ... SELECT`, enforced by
  `tests/code_coverage_tests/check_migrations_no_data_rewrites.py`. Prisma migrations apply
  synchronously at proxy boot before traffic is served, so a row rewrite is minutes of
  downtime plus a doubled heap. The runbook is
  `litellm-proxy-extras/migration_runbook.md`, whose **Step 0** is syncing the three files.

### 7.4 Adding any backend route breaks a generated-file gate

`Check UI API Types Sync` (`.github/workflows/check-ui-api-types.yml`) fails the PR unless
both are regenerated and committed:

```bash
uv run python -m litellm.proxy._lazy_openapi_snapshot     # → litellm/proxy/_lazy_openapi_snapshot.json
cd ui/litellm-dashboard && npm run gen:api               # → src/lib/http/schema.d.ts
```

The snapshot exists because routers are lazily imported to save ~700 MB at idle, so
`/openapi.json` would otherwise omit a fleet router's routes entirely. The generator hard
-fails if any lazy feature fails to import, so a fragment cannot silently vanish. **If the
fleet router is registered lazily, both files must move together.**

### 7.5 UI constraints that are real gates

- **Raw `fetch()` outside `src/lib/http/` is an eslint error** (`no-restricted-syntax`, selector
  `CallExpression[callee.name='fetch']`). New violations fail CI with no suppression credit.
  Use `$api`.
- `max-lines` 800 per file, `skipBlankLines`/`skipComments`, **not** in the budget file. `.tsx`
  filenames must be PascalCase; `.ts` is unconstrained, which is why hooks can be
  `useFleets.ts`. `local/no-ad-hoc-z-index` is a hard error. `@tremor/react` is banned.
- **The page registrations are vitest-enforced**, by `page_utils.test.ts` (descriptions
  present, no orphans), `leftnav.test.tsx` (unique keys, and a hardcoded list of
  admin-visible top-level labels) and `legacyPageRoutes.test.ts`. Inventing a new
  `groupLabel` also requires a `SECTION_DISPLAY` entry in `leftnav.tsx`.

### 7.6 A real bug to not copy

`search-tools/_components/CreateSearchTools.tsx:301` renders its **"Test Connection" button as
`type="submit"` inside the create form**, so clicking it also creates the search tool. There
is a test *locking in* that behaviour. The correct idiom is
`components/add_model/AddModelForm.tsx:429-437`, where the test button deliberately has no
`type`.

**Any fleet "Test connection" button must not be a submit button.** This is the kind of thing
mutation testing should be pointed at for the fleet PR.

---

## 8. Endpoints

Registered as a `LazyFeature` in `litellm/proxy/_lazy_features.py`. **Prefix choice is
load-bearing**: `path_prefixes` matching is plain `str.startswith`
(`_lazy_features.py:55`), so a bare prefix silently subsumes any sibling that shares it,
which is why `vector_store_management` uses a trailing slash and `policies` uses `/policy/`.
`/fleet` and `/v1/fleet` collide with nothing today; **re-verify against the 30-entry
registry before committing to that.**

`TestLazyFeatureRegistry` in `tests/test_litellm/proxy/test_proxy_server.py:10190` asserts
prefixes start with `/` and names are unique, but **there is no generic test that every
registered route is reachable through the matching prefix**. Write one for the fleet, modelled
on `test_llm_passthrough_prefixes_cover_every_route_the_module_registers:10300`.

| Method | Path | Auth |
|---|---|---|
| GET | `/fleet` | admin or admin-viewer |
| POST | `/fleet` | `PROXY_ADMIN` |
| GET | `/fleet/{fleet_id}` | admin or admin-viewer |
| PATCH/DELETE | `/fleet/{fleet_id}` | `PROXY_ADMIN` |
| GET | `/fleet/{fleet_id}/models` | admin or admin-viewer |
| POST | `/fleet/{fleet_id}/models` | `PROXY_ADMIN` |
| PATCH/DELETE | `/fleet/{fleet_id}/models/{fleet_model_id}` | `PROXY_ADMIN` |
| GET | `/fleet/{fleet_id}/health` | admin or admin-viewer |
| POST | `/fleet/{fleet_id}/models/{fleet_model_id}/test_connection` | `PROXY_ADMIN` |

Authorization has **two independent layers** and both are needed:

- `dependencies=[Depends(user_api_key_auth)]` authenticates. The in-body
  `LitellmUserRoles.PROXY_ADMIN` check authorizes. The decorator alone does not authorize.
- `litellm/proxy/auth/route_checks.py` enforces authorization **centrally** by route-string
  matching against `LiteLLMRoutes.management_routes` (`proxy/_types.py:693`, checked at
  `route_checks.py:477` and `:870`). A route missing from that list is
  rejected for non-admins. **Register `/fleet` there.**

Read-only admins are default-allowed on GET/HEAD/OPTIONS and default-denied on everything
else. A new **write** route therefore has to be added to
`_PROXY_ADMIN_VIEW_ONLY_BLOCKED_ROUTES` or the surrounding check permits it. The table above
splits read and write for exactly that reason.

Conventions: models live in the feature module (not `_types.py`), plain `pydantic.BaseModel`,
errors raised as `HTTPException` with `{"error": ...}`, `{"error": "..."}` rather than a bare
string, 201 on create, and a `raise_*` mapper pair so the same failure reads identically
wherever it surfaces.

**Health and test_connection return a typed response model**, mirroring
`CoordinationRedisTestResponse` rather than `/search_tools/test_connection`. That endpoint
returns HTTP 200 for both success and failure with a bare `dict[str, Any]` request body, has
no admin check, and no timeout of its own. None of that is a model. A fleet health check
spawns processes, so it must carry its own `asyncio.wait_for` and a named timeout constant;
none of the four existing probe endpoints wrap the call in one.

**Audit log on writes** via `create_object_audit_log` with a done-callback so failures are
logged rather than swallowed (as `/config/update`'s bare `asyncio.create_task` does). Note it
is **premium-gated twice over**: for OSS the call is a silent no-op.

### Registrations a new table needs

Seven independent places, and skipping one fails silently or in CI:

1. `schema.prisma` — all three byte-identical copies
2. A generated migration under `litellm-proxy-extras/litellm_proxy_extras/migrations/`
3. A Pydantic model, following `LiteLLM_<Table>` + `LiteLLMPydanticObjectBase`
4. A `PrismaTableRepository` subclass, added to both the imports and `__all__` in
   `litellm/repositories/__init__.py` (there is a test asserting those stay in sync)
5. `LitellmTableNames` in `litellm/proxy/_types.py` — **only needed for audit rows**, and
   `LiteLLM_AuditLogs.table_name` is typed with it, so an audit-writing endpoint will not
   type-check without a new member
6. `_CONFIG_SYNCED_TABLE_NAMES` in `config_sync_pubsub.py`, plus its mirror in
   `test_config_sync_pubsub.py`
7. `SupportedDBObjectType` in `litellm/proxy/_types.py` **and** a
   `_should_load_db_object(object_type="fleets")` branch in `_init_non_llm_objects_in_db`

On (7): `should_load_db_object` (`proxy_server.py:4986`) is **fail-open twice** — unset means
load everything, malformed means load everything. But a fleet left out of
`supported_db_objects` semantics means an operator who sets that list to everything-except
fleets gets no error, just a fleet that silently never loads. Add the branch, gated the same
way search tools is gated three times (startup, post-write refresh, read-through).

The enum is already inconsistent: `agents`, `search_tools` and `vector_store_indexes` are all
loaded at startup but are **not** members, so they cannot be named in the setting without
bypassing Pydantic. Add `"fleets"` properly rather than following that precedent.

---

## 9. WebUI

### 9.1 Layout

```
src/app/(dashboard)/fleet/
├── page.tsx                              # "use client", useAuthorized(), one render
├── detailNavigation.ts                   # nuqs query state, history: "push"
├── detailNavigation.test.ts
└── _components/
    ├── FleetPage.tsx
    ├── FleetTable.tsx
    ├── FleetTableColumns.tsx
    ├── FleetTableColumns.test.tsx
    ├── FleetInstancesTable.tsx
    ├── FleetStatusBadge.tsx
    ├── FleetCreateDialog.tsx
    ├── FleetCreateDialog.integration.test.tsx
    ├── fleetCreatePayload.ts
    └── fleetCreatePayload.test.ts

src/app/(dashboard)/hooks/fleet/
├── useFleets.ts                          # export const fleetKeys = createQueryKeys("fleets")
├── useFleets.test.ts
├── useFleetDetails.ts
├── useDeployFleetModel.ts
└── useTearDownFleetModel.ts
```

### 9.2 Registrations

| # | What | Path | Enforced by |
|---|---|---|---|
| 1 | route segment | `src/app/(dashboard)/fleet/page.tsx` | convention |
| 2 | sidebar entry | `src/components/leftnav.tsx` | `page_utils.test.ts`, `leftnav.test.tsx`, `legacyPageRoutes.test.ts` |
| 3 | description | `src/components/page_metadata.ts` | `page_utils.test.ts` |
| 4 | legacy route | `src/app/(dashboard)/legacyPageRoutes.ts` | `legacyPageRoutes.test.ts` |
| 5 | `uiHref` segment | `src/utils/uiHref.ts` — a pure function, **nothing to register** | convention |

Put the entry in the **existing** models group so `SECTION_DISPLAY` needs no change. If a new
top-level group is wanted, `leftnav.test.tsx` hardcodes the admin-visible top-level label list
and must be updated.

### 9.3 Conventions to match

- **Transport is `$api`** from `@/lib/http/api`, never raw `fetch`, never `networking.tsx`.
  Only `$api` is schema-typed, so the generated `components["schemas"]["..."]` types catch a
  backend rename at build time.
- **Status is the shared `StatusBadge`** from `@/components/shared/table_cells`, not a
  per-feature pill. Map with a `Record<string, StatusTone>` plus a `Record<string, number>`
  sort order and a `neutral` fallback, following `MCPHubTableColumns.tsx:48-55` and
  `HealthChecksTableColumns.tsx:38-59`. R5's two-axis health needs **two** columns or one
  composite badge; the field report's `none (free tier)` is the precedent for saying "nothing
  is configured and that is correct" rather than showing a dash.
- **Detail navigation via `nuqs`**, not `useState`. The dashboard is a static export, so
  there are no dynamic route segments anywhere; `nuqs` with `{ history: "push" }` gives a
  deep linkable URL and a working back button, which a supervised-process admin presses
  constantly.
- **Detail seeds from the list cache** via `initialData`, following
  `useAccessGroupDetails.ts:42-49`.
- **Mutations invalidate `fleetKeys.all`**, and `onError` calls `toast.fromError`.
- **Column factories take a named `deps` object**, because `local/no-large-inline-object-arg`
  warns at 4+ inline properties.
- **Extract payload building** into a pure `.ts` with a co-located unit test, following
  `access-group-create/mapper.ts`.

### 9.4 Polling

Adaptive, not constant, following `useShadowEval.ts:17-23`: poll fast while any instance is
`starting` or `deployed`, stop once everything is ready or failed. Export the interval
function as a pure function and unit-test it without React.

Two traps the prototype already hit:

- **Do not put the instance id in the polling query key.** An earlier version embedded a
  `serverIds` argument in the key, so deleting one row produced a new key and fired a health
  check for every remaining row. Use a constant key and merge per-row results with
  `setQueriesData`, not `invalidateQueries`, so one row's recheck does not flash the whole
  table.
- Overlapping requests are handled by React Query, not by code. If a backgrounded tab should
  stop polling, set `refetchIntervalInBackground: false` explicitly.

### 9.5 UI tests

Vitest, four projects, tests **colocated** next to the module (never `__tests__/`).

- `*.test.tsx` — unit tier: one module, collaborators doubled, milliseconds.
- `*.integration.test.tsx` — real component tree, only the network boundary stubbed.

Tests worth writing, in the order they pay:

1. **`fleetCreatePayload.test.ts`** — asserts exact request bodies. A dropped field fails it.
2. **`FleetTableColumns.test.tsx`** — per-row status, using `within(getByRole("row", {name}))`
   so each row is proven to have received *its own* status rather than the first row's.
3. **`useFleets.test.ts`** — the polling decision function, and the *absence* of a poll in
   the all-ready case. The negative assertion is the one with teeth.
4. **`FleetCreateDialog.integration.test.tsx`** — only the cases proving a form field reaches
   the right payload key. A test that renders a whole dialog to assert the shape of one
   object belongs in the unit tier.

---

## 10. Implementation order

Each step is independently mergeable and independently reviewable.

1. **Reject silently-ignored router settings** (§7.1, option a). Standalone correctness fix.
   Include the null-clearing path, the `management_routes` bookkeeping, and a distinct error
   shape for "requires a restart". Ordering rationale corrected from the first draft: the
   fleet's actual needs (routing groups, fallbacks, cooldowns, timeouts) are *already*
   runtime-updatable, so this is a fix worth doing on its own merits, not a fleet unblocker.
2. **Schema + migration** for the two tables, following §7.3 exactly. No behaviour. Update
   all three schema copies.
3. **Registration plumbing** — repository, `LitellmTableNames`,
   `_CONFIG_SYNCED_TABLE_NAMES` + its test mirror, `SupportedDBObjectType` + the
   `_init_non_llm_objects_in_db` branch. Split from CRUD so the generated-file gates stay
   reviewable.
4. **Fleet CRUD endpoints** — thin repositories in `table_repositories.py`, audit log on
   writes, `PROXY_ADMIN` + `management_routes`, admin-viewer blocked on writes.
5. **The opencode adapter**, standalone and testable without the proxy: the shim, the
   instance lifecycle, the v1-to-v2 patches. **Fail the build on any patch mismatch** so an
   upstream change cannot silently produce a fleet that no longer matches its source. This is
   where R6 is earned or lost: the adapter owns the command, so nothing operator-supplied
   reaches argv.
6. **Supervisor** — CLI-supervisor placement, readiness-by-pid, surgical stop, the
   lifecycle/readiness split (§6.3), pod-lock lease for the multi-pod case only.
7. **Register deployments on fleet write** — reconcile into `LiteLLM_ProxyModelTable` through
   the existing path, rather than special-casing the router. Return 500 when the triggered
   reload leaves the fleet's own model unserved, per `raise_if_reload_degraded_serving`.
8. **UI page** — copy `access-groups/` for layout, `$api` for transport. Instances table
   with both health axes, deploy/teardown, a non-submit test button.
9. **Docs + a PR upstream.** Steps 1-4 are upstreamable as-is. The opencode adapter (5) is
   the opinionated part; the abstraction (R6) is what makes it acceptable.

---

## 11. Open questions

**1. Upstream, or plugin?** Steps 1-4 are clearly upstreamable. The supervisor has opinions
about the proxy's process lifecycle that upstream may not want. Keeping that boundary means the
fleet can ship as a plugin without a fork, which is worth knowing before step 6.

**2. Is the opencode adapter a LiteLLM provider or a proxy-level subsystem?** A
`custom_llm_provider` would be the idiomatic shape, but none of them supervise processes. The
fleet is a proxy-level subsystem that *emits* ordinary `openai/…` deployments, which is also
why step 7 uses the existing reconcile path instead of a new router concept.

**3. Does the child fleet ship in-process, or as a separate container the proxy
configures?** This draft answers the *supervisor* placement (CLI supervisor, §6.1), which is
a different question from where the fleet's own HTTP server runs. A separate container is
cleaner for isolation; the prototype already runs one. For v1 the fleet server can stay in its
own container while the *supervisor* lives in the proxy, since the proxy only needs to spawn,
probe and tear down, and talks to it over its admin plane. Revisit if a leaked child can take
down the proxy.

**4. Should `LiteLLM_CronJob` get an implementation?** Its shape is exactly a lease and it is
dead. `PodLockManager` covers the need in production, with a Lua compare-and-delete and
dashboard coverage, so the table is redundant unless someone wants the lease queryable from
SQL. Leaving it dead is defensible; deleting it is not, since something may read it outside
this repo.
