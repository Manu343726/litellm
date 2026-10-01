# OpenCode as a native LiteLLM provider — working design

Working notes for a feature that does not exist yet. Not a spec, not committed: this is a
handover document so a later session can pick up mid-thought.

- **Repo:** `/home/manuel/Projects/litellm` (fork of `BerriAI/litellm`, `origin` is the fork)
- **Base when written:** `0fe4028cd9`, version 1.104.0
- **Reference prototype:** `~/docker-compose-services/litellm/` (running, compose-based)
- **Prototype field report:** `~/docker-compose-services/litellm/README.md`

Section numbering is not load-bearing here, unlike the deleted `MODEL_FLEET.md`. Nothing
outside this file cites it.

---

## 1. The goal, and why it exists

Use multiple models from multiple OpenCode accounts through LiteLLM.

OpenCode Zen's free tier refuses direct API calls. Eight models sit on the free tier; seven
answer any direct call with

> OpenCode's free tier can only be used from within OpenCode

and only `space-bunny-free` returns 200. opencode itself is the client Zen trusts, so the
only way to reach the other seven is to run a real opencode process.

Two more constraints shape everything:

- **The shim serialises requests behind one global mutex.** One opencode instance therefore
  serves one model at a time, so instances scale with (accounts × active models), not with
  request volume.
- **Free models report `usage: {0,0,0}`** because the gateway hardcodes it. Spend reads $0
  because the models are free, not because anything was measured.

The UX requirement: registering opencode accounts must feel like registering any other
provider. Users see accounts as providers, and models from those accounts. The same model
(gpt-5) from two accounts must be two different models, so a user picks the account.

---

## 2. Key finding: LiteLLM does NOT already support this

The question worth answering first, because it nearly ended the work: **does LiteLLM already
support multiple accounts of one provider, and how does it set up models from them?**

No, and the mechanism is the opposite of what is needed.

There is no account entity anywhere: no account table, no account id, no per-account model
list. What exists:

- `LiteLLM_ProxyModelTable` has **no uniqueness constraint on `model_name`**
  (`schema.prisma:54-64`). Multiple rows sharing a `model_name` are normal.
- `Router.model_name_to_deployment_indices` is `dict[str, list[int]]` (`litellm/router.py:992`),
  so one name maps to N deployments.
- Each deployment carries its own credential via `litellm_credential_name`, resolved per
  deployment by `Router.get_deployment_credentials` (`litellm/router.py:10188-10201`).

So two Claude accounts are two rows both named `claude-sonnet-4-6`, and `simple_shuffle`
picks between them per request. **One model, backed by either account.** Nobody picks.

**The cost map is not the account mechanism.** Verified: 21 anthropic entries in
`model_prices_and_context_window.json`, and **zero entries mention credentials at all**. The
cost map is a global, static catalog (name, pricing, context window, provider). It populates
the provider dropdown's model list and nothing else.

| | Two accounts today | What is needed here |
|---|---|---|
| Model name | `claude-sonnet-4-6` (one) | `opencode/team-a/gpt-5`, `opencode/personal/gpt-5` |
| Deployment rows | 2, same name | 1 each |
| Account chosen by | nothing, router shuffles | naming the model |
| Permission isolation | per-deployment `team_id`, but accounts indistinguishable to the caller | inherent in the name |
| Instances needed | n/a | 1 per (account, model), per the mutex |

Distinct names are what make accounts legible and permissionable. That is the whole reason
this is not already solved.

---

## 3. What exists to build on

Verified at this commit.

### 3.1 opencode as a native provider is nearly free

A provider is native by being a member of `LlmProviders` (`litellm/types/utils.py:3991`),
from which `litellm.provider_list` is derived (`litellm/__init__.py:2388-2399`), plus a
`litellm/llms/opencode/` package. The chat dispatch in `litellm/main.py:5909-5935` routes a
provider to the OpenAI-compatible handler when it is in `openai_compatible_providers`
(`litellm/constants.py:957-1028`), a list of ~70 providers. This is a well-trodden path.

### 3.2 Account-slugged model names survive parsing

**This is the load-bearing verification.** `get_llm_provider`
(`litellm/litellm_core_utils/get_llm_provider_logic.py:158-256`) splits on the **first** `/`
only, so a multi-segment remainder survives intact. Confirmed by running it with `opencode`
added to both lists at runtime:

```
opencode/personal/gpt-5    -> model='personal/gpt-5',    provider='opencode'
opencode/team-a/big-pickle -> model='team-a/big-pickle', provider='opencode'
```

Today it raises `LLM Provider NOT provided` because `opencode` is in neither
`provider_list` nor `openai_compatible_endpoints`. Once added, nothing else is needed.

Consequence: `opencode/team-a/gpt-5` and `opencode/personal/gpt-5` become distinct
`model_name` values, so they get separate deployments, separate `LiteLLM_ProxyModelTable`
rows, separate health-check rows, separate rate limits, and separate team/access-group
permissions. **Per-account permissions cost nothing.**

Also verified: `parse_routing_groups` (`litellm/router_utils/routing_groups.py:38-100`)
treats model names as opaque strings and only **warns** on names absent from `model_list`
rather than rejecting them, so account-slugged names need no special handling.

### 3.3 The UI provider dropdown is data-driven

`/public/providers/fields` (`litellm/proxy/public_endpoints/public_endpoints.py:399-419`)
reads a static JSON file with 124 entries, validated into
`ProviderCreateInfo` (`litellm/types/proxy/public_endpoints/public_endpoints.py:28-33`).
Adding an `opencode` entry puts opencode in the dropdown with an API-key field, with no
structural UI change.

### 3.4 The UI model list is the one new UI mechanism

`AddModelPanel.tsx:61` computes `providerModels` via
`getProviderModels(provider, modelCostMapData)`
(`ui/litellm-dashboard/src/components/provider_info_helpers.tsx:468-521`), which filters
`model_prices_and_context_window.json` by `litellm_provider`.

**There are zero opencode entries in that file, and adding them is the wrong move**, because
the list is not static: it depends on which accounts are registered. However, all accounts
share one catalog, so the per-account list is one catalog, prefixed.

The seam is good: `providerModels` is a plain `string[]` passed down as a prop
(`litellm_model_name.tsx:12`), so injecting a new source means changing where that prop is
computed, not restructuring the component.

### 3.5 Credentials already work per deployment

The Add Model form has an **Existing Credentials** selector
(`AddModelForm.tsx:294-308`) that sets `litellm_credential_name`, and provider-specific
fields are hidden when one is chosen. Registering an opencode account is registering a
credential; there is no new account entity.

### 3.6 Supervisor precedents

| Precedent | What to take |
|---|---|
| `PgBouncerProcess` (`litellm/proxy/db/pgbouncer.py:501-639`) | Real supervisor: spawn under a lock, free-port precheck, readiness probe, daemon thread, restart on unexpected exit, graceful stop. Single child, synchronous, thread-based. |
| `prometheus_metrics_server.py:34,110-155` | Child **echoes its own pid** (`PID_HEADER`, `_answered_by`), so a squatter on the port cannot pass readiness. Relevant: this host had a VS Code process squatting a port that answered health checks with 200. |
| `litellm/proxy/db/query_engine_reaper.py` | Orphan handling via `prctl(PR_SET_CHILD_SUBREAPER)` + a `/proc` sweep filtered to **direct children**, batched SIGTERM/SIGKILL so N orphans cost one grace period. |
| `PodLockManager` (`litellm/proxy/db/db_transaction_queue/pod_lock_manager.py`) | Redis `SET NX EX` lease with a Lua compare-and-delete. `acquire_lock` returns **None** (not False) with no Redis, and callers then silently run zero times. |
| `LiteLLM_ProxyWorkerHeartbeat` (`litellm/proxy/db/proxy_worker_heartbeat.py:32-50`) | Timestamps written with SQL `NOW()`, not Python, so pods with skewed clocks agree. |

Both existing children start from the **CLI supervisor**, not `proxy_startup_event`:
`proxy_cli.py:1448` (pgbouncer), `:631` (reaper). The gunicorn master reserves itself for
forking via `reserve_process_for_forking`, but that guard is **Rust-specific**, so Python
supervisor threads in the master are fine.

`LiteLLM_CronJob` exists in all three schema copies and has **zero Python readers or
writers**; every `cronjob_id` in the codebase is a `PodLockManager.acquire_lock()` keyword
argument. Its shape is a lease; its implementation does not exist.

### 3.7 Security precedent for spawning processes from a DB row

MCP stdio already spawns a subprocess configured by a DB row, and handles it carefully:

- Command allowlist, `MCP_STDIO_ALLOWED_COMMANDS` (`litellm/constants.py:202-205`),
  extensible by env var.
- Defense in depth at both layers: a Pydantic validator rejects non-allowlisted commands on
  write (`litellm/proxy/_types.py:1578-1593`) and `_create_mcp_client` re-checks at spawn to
  catch legacy rows.
- Refused from non-admins, with the reasoning written out
  (`mcp_management_endpoints.py:1405-1421`): a team member proposing a server config that an
  admin rubber-stamps is local code execution.

Mitigation that removes most of the risk here: **the provider owns the executable and the
flag shape**, so an operator never types a command.

### 3.8 Health: nothing exists

There is no base class or mixin for "DB-backed resource with health status", no canonical
field shape (four different shapes across four tables), and no shared status vocabulary.
`status` is a bare `String` everywhere.

Worse for this feature: **no health model distinguishes liveness from readiness.** A
liveness probe cannot tell a rate-limited fleet from a working one, because the backend and
gateway both still answer. This is the field report's worst bug and it must not be repeated.

### 3.9 Router settings are a trap

`/config/update` (`litellm/proxy/proxy_server.py:17721`) accepts anything in
`Router.get_valid_args()` minus `ROUTER_SETTINGS_MANAGED_OUTSIDE_CONFIG`, persists it, then
hot-applies via `Router.update_settings` (`litellm/router.py:12131-12186`), which applies
only `RUNTIME_UPDATABLE_ROUTER_SETTINGS` and drops the rest at **DEBUG** level.

Computed at this commit: `get_valid_args()` is **58**, runtime-updatable 18, accepted 51,
rejected outright 8, and **33 accepted-but-ignored**. Each returns HTTP 200, is written to
`LiteLLM_Config`, and does nothing until restart.

Relevant keys: `max_parallel_requests` is read from `litellm_params` and is **not**
runtime-updatable. `cache_responses` and `caching_groups` are silently ignored, so an
operator can turn on router caching and see nothing happen.

Also: persistence merges and never deletes (`{**existing, **router_settings_updates}`,
`:17910`), so a key removed from the accepted set is orphaned, unwritable, and dead at read
time. The escape is that a stored `null` counts as absent
(`litellm/proxy/config_resolvers/settings_rules.py:110-111`).

---

## 4. The design

### 4.1 The gateway shim goes

The prototype's chain is:

```
litellm -> fleet dispatcher (:8080) -> gateway shim (:8200+) -> opencode backend (:4200+) -> Zen
```

I carried the vendored `opencode-to-openai` shim forward as given for several turns. **It
should not be in the design.** It exists because opencode's API is session-based
(`/api/session`, `/api/session/{id}/generate`), not OpenAI-shaped, and translating between
those is exactly what a LiteLLM provider transformation class is for. `litellm/llms/opencode/`
can do it in-process, in Python, with no vendored JS and no HTTP hop.

The mutex needs care, because it is the reason the prototype used one instance per model.
**The mutex is a property of one gateway process holding one SDK client.** With N instances
there are N clients, and LiteLLM's own per-deployment `max_parallel_requests` expresses the
same ceiling as a first-class knob instead of an accident of a lock in someone else's code.
Per-deployment rather than global, visible in the UI, adjustable.

Benefits:

- Instance cost drops from ~200 MB and 2 processes to ~175 MB and 1.
- The largest maintenance liability in the field report disappears: five v1-to-v2 patches
  against vendored JavaScript, requiring "fail the build on any patch mismatch". A native
  provider fails loudly against real v2 routes instead of silently patching someone else's JS.
- One fewer HTTP hop, though latency is upstream-bound anyway (measured avg 3-40 s per
  request), so the win is memory and operations, not speed.

### 4.2 Topology

```
┌─────────────────────────────────────────────────────────────────────┐
│ LiteLLM proxy — one Python process                                 │
│                                                                     │
│  ┌──────────────┐   ┌──────────────────────┐   ┌─────────────────┐  │
│  │   Router     │   │  opencode provider   │   │  Supervisor     │  │
│  │              │   │                      │   │                 │  │
│  │ deployments  │──>│  chat ──> session    │   │ reconcile       │  │
│  │ (from DB)    │   │    ──> generate      │   │ spawn / adopt   │  │
│  │              │   │    ──> poll          │   │ readiness-by-pid│  │
│  └──────────────┘   │    ──> ChatResponse  │   │ surgical stop   │  │
│         ^           │                      │   │                 │  │
│         │           │  client per backend, │   └────────┬────────┘  │
│         │           │  no shared lock      │            │           │
│         │           └──────────┬───────────┘            │           │
└─────────┼──────────────────────┼────────────────────────┼───────────┘
          │                      │  httpx, loopback only  │
          │ ┌─────────────┼─────────────────────┐
          │        │  spawned detached, own $HOME each
          │        v v            v
          │   ┌─────────┐   ┌─────────┐   ┌─────────┐
          │   │opencode │   │opencode │   │opencode │   ... one per
          │   │ :42101 │   │ :42102  │   │ :42103  │     configured
          │   │$HOME=   │   │$HOME=   │   │$HOME=   │    free
          │   │ inst/A │   │ inst/B  │   │ inst/A  │   (account,
          │   │auth.json│   │auth.json│   │auth.json│    model)
          │   └────┬────┘   └────┬────┘   └────┬────┘
          │        │             │             │
          └────────┴─────────────┴─────────────┘ │
                        ▼ OpenCode Zen

NOT present: fleet dispatcher process, gateway shim processes, a separate fleet
container, published fleet ports.
```

Everything spawned is loopback-only, so there is no fleet port to expose and no second auth
plane. Deployments reach their backend through an in-process client, not a URL.

**Adoption instead of persisted state.** Children are spawned detached, so they outlive a
proxy restart. Nothing is persisted: `/proc/<pid>/cmdline` contains
`opencode serve --port 42101`, so the supervisor reads the port straight out of the command
line to re-adopt a running instance, and the same read confirms identity before it signals
anything.

```
proxy restart        proxy crash         instance crash
     |                    |                     |
     v                    v                     v
 re-adopt on next    reaper kills         supervisor rescans
 boot, nothing       orphans, adopts     /proc, respawns
 respawned           survivors
```

### 4.3 Request path

```
user A ─> POST /chat/completions  model=opencode/team-a/gpt-5
             |
             v
     Router picks the deployment whose model_name is exactly
     "opencode/team-a/gpt-5"
             |
             v
     opencode provider: resolve account=team-a, model=gpt-5
             |  max_parallel_requests = 1   <- the mutex, as a config knob
             v
     client[team-a/gpt-5] ──> 127.0.0.1:42101  (A's $HOME, A's key)
                                        |
                                        v
                                  OpenCode Zen
             |
             <── ChatCompletionResponse ──┘

user B ─> POST /chat/completions  model=opencode/personal/gpt-5
             |
             v
     a DIFFERENT deployment row, different credential, different
     client, different port, different $HOME
             |
             v
     client[personal/gpt-5] ──> 127.0.0.1:42102 ──> Zen
```

A paid model skips all of it: no instance, straight to the API through the normal
OpenAI-compatible path, with real token usage.

### 4.4 Setup flow

```
 ┌─ 1. CREDENTIALS ──────────────────────────────────────────────┐
 │  UI: Add Credential                                            │
 │      provider  [ OpenCode            v ]                      │
 │      account  [ team-a              ]  <- becomes the slug     │
 │      api key  [ ................   ]  <- stored encrypted     │
 │                                        once, rotatable here   │
 │  POST /credentials                                             │
 └───────────────────────────┬───────────────────────────────────┘
                             v
 ┌─ 2. PROVIDER APPEARS (no structural UI change) ───────────────┐
 │  Add Model > provider dropdown lists "OpenCode", because      │
 │  provider_create_fields.json gained one entry.                │
 │  Model list = bundled opencode catalog, prefixed per account.  │
 └───────────────────────────┬───────────────────────────────────┘
                             v
 ┌─ 3. USER CONFIGURES ──────────────────────────────────────────┐
 │  Add Model > provider    [ OpenCode        v ]                 │
 │            credential [ team-a          v ]  <- "Existing     │
 │            models  [x] gpt-5                                  │
 │                   [x] big-pickle                             │
 │                   [ ] claude-opus-5-5   (paid -> direct)     │
 │                                                                  │
 │  POST /model/new  x2, one row per (account, model)             │
 │                                                                  │
 │    model_name = opencode/team-a/gpt-5        litellm_params:   │
 │    model_name = opencode/team-a/big-pickle     model=openai/gpt-5
 │                                                 max_parallel_requests=1
 └───────────────────────────┬───────────────────────────────────┘
                             v
 ┌─ 4. SUPERVISOR CONVERGES ─────────────────────────────────────┐
 │  Reconciles the set of enabled opencode deployments that need  │
 │  an instance: spawn if absent, adopt if running, stop if the   │
 │  row was deleted. Readiness verified by the CHILD'S OWN pid,   │
 │  never by the port alone.                                       │
 └─────────────────────────────────────────────────────────────────┘
```

Registering a second account is the same four steps with a different slug. The two accounts'
models coexist as distinct names.

### 4.5 UX

Add Model, opencode selected:

```
┌─ Add Model ───────────────────────────────────────────────────────┐
│  provider      [ OpenCode                                   v ]   │
│                                                                  │
│  Existing Credentials                                            │
│                [ team-a                                   v ]   │
│                                                                  │
│  models  [x] gpt-5                                               │
│         [x] big-pickle                                           │
│         [ ] claude-opus-5-5                                      │
│         [ ] gpt-6-astra                                          │
│         ─────────────────────────────────────                    │
│         via instance [x]   (free models only)                    │
│         direct        [ ]   (paid models)                        │
│                                                                  │
│  LiteLLM model names that will be created:                       │
│    opencode/team-a/gpt-5                                         │
│    opencode/team-a/big-pickle                                    │
│                                                                  │
│  max_parallel_requests  [ 1 ]   per-deployment ceiling          │
│                              [ Add Models ]                      │
└──────────────────────────────────────────────────────────────────┘
```

The account slug is fixed by the credential, so the user never types a prefix. The
`via instance` toggle is inferred from the catalog, not typed: free models show it on, paid
models show it off with the reason.

Models + Endpoints, three accounts registered:

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Models + Endpoints          [ search... ] [ Add Model ]             │
├──────────┬───────────┬──────────┬─────────┬────────────┬───────┬─────────────┤
│ Model    │ Provider │ Account  │ Path    │ Lifecycle  │ Ready │ Concurrency │
├──────────┼───────────┼──────────┼─────────┼────────────┼───────┼─────────────┤
│ gpt-5    │ opencode  │ team-a   │ inst    | - healthy  | v ok  | 1 / 1       │
│ big-pick…│ opencode  │ team-a   │ inst    | - healthy  | ! slow| 1 / 1       │
│ gpt-5    │ opencode  │ person.  │ inst    | - starting | -     | 0 / 1       │
│ gpt-5    │ opencode  │ team-b   │ inst    | o stopped  | -     | 0 / 1       │
│ opus-5-5 │ opencode  │ team-a   │ direct  | n/a        | v ok  | 12/40       │
└──────────┴───────────┴──────────┴─────────┴────────────┴───────┴─────────────┘
```

Two things a user has never had before, both free:

- **Account is a column**, so the two gpt-5 rows are distinguishable, filterable, and
  separable into different access groups.
- **Lifecycle and readiness are separate columns.** This is the fix for the field report's
  worst bug: a rate-limited instance reports lifecycle `healthy` because the backend answers,
  and readiness `degraded` because a probe through the model plane got the upstream rate
  limit. Collapsing them into one "status" is what made the prototype's dashboard lie.

Runtime view:

```
┌─ OpenCode instances ──────────────────────────────────────────────────┐
│ instance        │ pid │ port   │ lifecycle │ ready │ err             │
├──────────────────┼─────┼────────┼───────────┼───────┼────────────────┤
│ team-a / gpt-5   │ 31021│ 42101  │ healthy   │ ok    │ -              │
│ team-a / big-pic…│ 31058│ 42102  │ healthy   │ slow  │ rate...        │
│ personal / gpt-5 │ 31095│ 42103  │ starting  │ -     │ -              │
└──────────────────┴─────┴────────┴───────────┴───────┴────────────────┘
```

### 4.6 The matrix, and which cells cost an instance

```
                 gpt-5   big-pickle   nemotron-3   claude-opus
 ┌─────────┬────────────┬─────────────┬──────────────┐
  team-a       │ * inst  │ * inst     │ * inst      │ o direct     │
               ├──────────┼────────────┼─────────────┼──────────────┤
  personal     │ * inst  │ * inst     │ o not cfg   │ o direct     │
               ├──────────┼────────────┼─────────────┼──────────────┤
  team-b       │ * inst  │ o not cfg  │ o not cfg   │ o not cfg    │
               └──────────┴────────────┴─────────────┴──────────────┘

  * instance = 1 process, ~175 MB      o not cfg = no row, nothing runs
  o direct  = 0 processes, real usage
  7 instances ~= 1.2 GB
```

Three rules keep this bounded rather than multiplicative:

- **Instances follow configured deployments, not available combinations.** Nine cells
  configured, seven spawn. A registered account with nothing selected costs nothing.
- **Paid models never instantiate.**
- **The free/paid flag comes from the bundled catalog**, which is needed anyway for pricing
  and for deciding which models need an instance. It is not a per-account property, which is
  the consequence of all accounts sharing one model set.

### 4.7 Measured cost

From the running `opencode-fleet` container, 7 instances, 43 hours up:

| Component | RSS per instance |
|---|---|
| opencode backend (`opencode serve --port 42xx`) | 144-174 MB |
| gateway shim (`node index.js`) | 48-50 MB |
| **prototype total, 2 processes** | **~200 MB** |
| **native provider, 1 process** | **~175 MB** |
| 7 instances, measured container total | 1438 MB |

Instances scale linearly with (accounts × active free models):

| Accounts x models | Instances | Processes | RSS |
|---|---|---|---|
| 1 x 7 (today) | 7 | 14 | ~1.4 GB |
| 2 x 7 | 14 | 28 | ~2.9 GB |
| 3 x 5 | 15 | 30 | ~3.0 GB |
| 3 x 20 | 60 | 120 | ~12 GB |

Two things make the tail worse than a memory number: boot was ~2 s per instance in the logs
(`started` timestamps 4200→4201→4202 at 2 s intervals), so 60 instances is ~2 minutes before
the last answers; and each instance is a directory holding its own `$HOME`, which is what
makes per-account credentials possible but also makes teardown a real operation.

**A framing I got wrong, corrected here so it is not carried forward.** I presented per-account
instances as a trade-off, implying a shared instance could give the same UX for less. That is
wrong on the merits. If accounts A and B share one gpt-5 instance, a user calling
`opencode/team-a/gpt-5` may be served by an instance authenticated as account B, so separate
names would look like account isolation while providing none of the quota separation that
matters when paying for two accounts. **Per-account instances are a requirement of account
isolation, not a choice between isolation and cheapness.**

---

## 5. Open question that could still change the design

**Is the OpenCode free-tier rate limit per-account or per-IP?**

The field report proves the free tier is **not credential-gated**: a throwaway opencode with
an empty `HOME` and a scrubbed environment (`env -i`, nothing inherited) still reached Zen and
was answered with `503 Rate limit exceeded`. So it is volume-gated rather than account-gated.

- **If per-IP**: N accounts buy no extra throughput, and per-account instances multiply
  processes while isolating nothing real. The honest configuration becomes several
  account-scoped model names pointing at one shared instance.
- **If per-account**: per-account instances are correct.

The design supports both without rework, because **instance granularity is a property of the
deployment row**, not hardcoded. The topology, naming, permissions, and supervisor are
identical either way. Deliberately left as a per-deployment choice rather than blocking on the
answer.

Second open question, lower stakes: whether the fleet supervisor should run in the **gunicorn
master** (the pgbouncer and reaper precedent, one owner by construction, no Redis lease
in-cluster) or in a worker with a `PodLockManager` lease. The master placement is recommended
because N workers each spawning the same child is a port race and the loser is not idempotent.

---

## 6. What has to be built

| Piece | Status |
|---|---|
| `opencode` as a provider (enum, dispatch) | ~70 providers already do this |
| Account-slugged model names | **verified working**, no code needed beyond adding the provider |
| Per-account permissions, rate limits, health | keyed on `model_name`, so free |
| Credential per account | exists (`litellm_credential_name` per deployment) |
| Provider dropdown entry | one entry in `provider_create_fields.json` |
| Per-account model list source | **new**, replaces cost-map filtering for this provider |
| `litellm/llms/opencode/` provider + transformation | **new**, replaces the vendored shim |
| Bundled catalog with free/paid and upstream family | **new**, replaces a 47-entry hand-transcribed dict |
| Supervisor: reconcile, spawn, adopt by pid, surgical stop | **new**, mirrors `PgBouncerProcess` |
| Lifecycle vs readiness health | **new**; nothing distinguishes them today |
| Command allowlist + admin-only | reuse the MCP stdio treatment |

### Prototype facts worth keeping

From reading `~/docker-compose-services/litellm/`, since re-deriving them is expensive:

- opencode must come from the **v2** install channel (`https://opencode.ai/v2/install`).
  `npm install -g opencode-ai` and `https://opencode.ai/install` both give **v1**, whose
  missing routes answer with the SPA shell, so every request dies parsing `<!doctype` as JSON.
- The installer puts the binary under `$HOME`; when the build runs as root it lands in
  `/root`, mode 700, untraversable by an unprivileged user. The Dockerfile **copies** it to
  `/usr/local/bin`. When wrong, every instance log says `Permission denied`.
- **Zen does not serve one API.** Each model is pinned to `/chat/completions`, `/messages`,
  `/responses`, or `/models/<id>`. The `/messages` and Gemini families are unverified because
  every such model is paid and the test account returns 402.
- The base URL **must end in `/v1`**. LiteLLM's OpenAI provider appends `/chat/completions`
  to whatever `api_base` it is given; without `/v1` the 404 is non-retryable, so the
  deployment drops into a 300-second cooldown and reports "No deployments available".
- Free-tier throttle surfaces as **HTTP 500 with an empty body**, reported as
  `Failed to create OpenCode session`, while the instance still reports `healthy`. This is the
  origin of the lifecycle/readiness split.
- Tokens are always 0, so **`tpm` can never be evaluated** on this lane. `rpm` does double
  duty (rate limit and shuffle weight) and is the only usable limit.
- Because LiteLLM resolves `os.environ/...` at registration time, rotating the env var alone
  does not update the database.

---

## 7. Dead ends and rejected approaches

Recorded so they are not re-litigated:

- **A separate fleet container plus a dispatcher process.** The prototype's shape. Dropped:
  the shim belongs in the provider, and a loopback-only supervisor needs no published port.
- **One instance per model shared across accounts.** Rejected in §4.7; it breaks the
  per-account isolation that distinct names imply.
- **Adding opencode models to `model_prices_and_context_window.json`.** Wrong file: it is a
  global static catalog with no per-account or per-credential concept (verified: zero entries
  mention credentials).
- **A `tpm` limit for this lane.** Cannot be evaluated, since usage is always 0. Omit it.
- **Instances for paid models.** They work directly and report real usage; routing them
  through the shim trades accurate billing for nothing.
- **Persisting instance state (pid, port) in the DB.** Unnecessary: re-adopt by reading
  `/proc/<pid>/cmdline`, which carries the port.
- **A `fleet_settings` DB section.** Dropped with the deleted `MODEL_FLEET.md`. The design no
  longer needs fleet settings to be config-file-free; instance intent is derived from
  configured deployments.

---

## 8. Suggested build order

Each step independently mergeable. Ordered so the pieces with no dependencies land first.

1. **Provider registration only.** Add `opencode` to `LlmProviders`, route it in
   `get_llm_provider`, send it to the OpenAI-compatible handler, add the
   `provider_create_fields.json` entry, add the UI `Providers` enum member. At this point
   opencode works as a plain OpenAI-compatible provider for **paid** models, which needs no
   instances. Fully useful on its own.
2. **Bundled catalog** with free/paid and upstream family, replacing the hand-transcribed dict.
3. **Account-slugged naming end to end**, verified against §3.2, with the per-account model
   list source in the UI.
4. **Provider class and transformation**, replacing the vendored shim. Testable against a
   real instance with no supervisor.
5. **Supervisor**: reconcile, spawn, adopt by pid, surgical stop, readiness-by-pid.
6. **Lifecycle vs readiness health**, and the two-column UI.
7. **Model and instance views** showing Account as a column.
8. `/config/update` router-settings fix, if a fleet-adjacent key turns out to need it.

Steps 1-3 are upstreamable as-is. Steps 4-6 have opinions about the proxy's process lifecycle
that upstream may not want, which is worth knowing before step 5.