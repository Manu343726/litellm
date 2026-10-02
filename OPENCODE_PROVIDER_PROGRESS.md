# OpenCode provider: progress notes

Working notes for the branch `litellm_opencode_provider`. Companion to
`OPENCODE_PROVIDER_DESIGN.md`, which holds the design. This file records what is built, what
was verified, and what is not done yet, so a later session does not re-derive it.

Base when work started: `6e05a01089`. Version 1.104.0.

---

## 1. Where this stands

| Step | Status |
|---|---|
| 1. Provider registration | **done**, verified against the live API and the browser |
| 2. Bundled catalog with free/paid flag | **not started** |
| 3. Account-slugged naming end to end | **partly**: naming and resolution work, the UI model source does not |
| 4. Provider class and transformation | **done** for the direct path only, no session-API path |
| 5. Supervisor | **not started** |
| 6. Lifecycle vs readiness health | **not started** |
| 7. Model and instance views | **not started** |
| Credential validation | **done**, lands with step 2's commit |
| Fleet page | **not started**, and blocked on step 5, see §6 |

Two commits so far: `453663757d` registers the provider, `cdd3ddf607` fixes a UI bug found
while testing it. The credential validation work is uncommitted on top.

---

## 2. What step 1 actually is

`opencode` is a first-class `LlmProviders` member that behaves like any other provider:

```
model=opencode/<account>/<model>
```

`get_llm_provider` splits on the first `/` only, so `opencode/team-a/space-bunny-free`
resolves to `model='team-a/space-bunny-free'`, `provider='opencode'`. The account segment
survives resolution, which is the property the whole naming scheme rests on. The
transformation then strips it before the request goes upstream, because Zen does not know
the account and a leaked segment produces a 404.

Two accounts therefore end up as two distinct `model_name` values. Everything keyed on
`model_name` is then per-account for free: permissions, rate limits, health rows, access
groups. That is the reason for slugged names rather than one shared group.

Files touched: `litellm/types/utils.py` (enum member), `litellm/constants.py` (Zen base URL
and the `openai_compatible_providers` entry), `litellm/litellm_core_utils/get_llm_provider_logic.py`
(api_base resolution, which the chat handler needs separately or the request goes to
`api.openai.com`), `litellm/utils.py` (config factory), `litellm/llms/opencode/chat/transformation.py`,
`litellm/_lazy_imports_registry.py` (the `__init__.py` import block is under `TYPE_CHECKING`,
so without the registry entry the class does not exist at runtime),
`provider_create_fields.json`, and the UI `Providers` enum plus `provider_map` in
`provider_info_helpers.tsx`.

### Two traps worth remembering

`api_base` must be resolved in the per-provider chain in `get_llm_provider`, not only in the
config class. The chat handler reads `api_base` from the context it builds, so a config-only
override is ignored and the request goes to OpenAI with an OpenCode key.

`OpenCodeChatConfig` must be registered in `_lazy_imports_registry.py`. The import in
`litellm/__init__.py` sits inside `if TYPE_CHECKING:`, so at runtime the attribute is absent
and `get_llm_provider` raises `module 'litellm' has no attribute 'OpenCodeChatConfig'`.

---

## 3. Verified, not assumed

Against a real account key and the real Zen endpoint, through the proxy on port 4000:

- `/v1/models` lists `opencode/team-a/...`, `opencode/personal/...`, `opencode/ui-made/...`
- a chat completion returns `content: 'ok'` with real token counts, non-streaming
- streaming returns 9 chunks that reassemble to `'ok'`
- streaming with `stream_options.include_usage` carries usage on the final chunk
- the Playground, driven through a real browser, sent a message and got `ok` back in 2.13s
  with In 163, Cache Read 149, Out 11, Reasoning 10, Total 174
- the Logs page records the spend row with `Credential: opencode-team-a` in the tags, which is
  the per-account attribution that motivates the naming

**Free models report real usage, not zeros.** The design doc's §7 claim that free models
always return `usage: {0,0,0}` did not reproduce on the direct path: 163 in / 11 out, and the
Logs page shows `174(163+11)`. If the claim holds anywhere it is on the local-instance path,
which does not exist yet. Do not build on the claim until it is re-measured there, because
§7's conclusion that `tpm` can never be evaluated depends on it.

**Which free models answer a direct call.** `big-pickle` returns 403 `FreeTierError`
("OpenCode's free tier can only be used from within OpenCode"). `space-bunny-free` returns
200 with real usage. The other free models refuse. Spoofing `x-opencode-client` or the
User-Agent did not get past the gate.

---

## 4. Bugs found while testing, both fixed

**A stale `api_base` in the credential form.** Field rows in
`provider_specific_fields.tsx` were keyed by `field.key` alone, so switching providers reused
the same `Input`. Its value is `control.value ?? undefined`, which makes the input
uncontrolled the moment the form resets, and React then leaves the previous provider's DOM
value in place. Picking OpenCode rendered the right fields but submitted
`https://api.openai.com/v1`, which is the non-retryable 404 the design doc warns about. The
helper `resetCredentialFormOnProviderChange` documents this exact leak as its reason for
existing, so the intent was there and the implementation did not hold. Fixed by keying on the
provider as well. `cdd3ddf607`.

**The Admin UI is a static export and must be rebuilt.** The Add Model provider dropdown reads
live metadata from `/public/providers/fields`, so it showed OpenCode immediately. The
credential dropdown is built from a hardcoded enum compiled into
`litellm/proxy/_experimental/out/`, so it showed 114 providers without OpenCode until the
dashboard was rebuilt and rsynced. A UI source change is invisible until that happens:

```
cd ui/litellm-dashboard && npm run build
rsync -a --delete ui/litellm-dashboard/out/ litellm/proxy/_experimental/out/
```

Do not commit the rebuilt bundle. It is 537 files of release artifact, refreshed by the
release process, and the branch deliberately leaves it alone.

---

## 5. Credential validation

The create and update paths accepted any key, which left a deployment that looked configured
and failed on every request. Both now ask the provider first.

`OpenCodeChatConfig.verify_credential` sends one minimal completion and reports failure, and
`_verify_provider_credential` in `credential_endpoints/endpoints.py` calls it during create
and PATCH. It is a no-op for providers that do not implement the method, so nothing else
changes behaviour. `POST /credentials/validate` exposes the same check for the UI to call
before creating anything.

### The thing that makes this non-obvious

`GET /zen/v1/models` is **unauthenticated**. It answers 200 to a wrong key, and 200 to no key
at all. It cannot validate anything, so `get_models` no longer sends `Authorization` at all:
doing so implied a check that does not happen. Only a real completion returns 401, which is
why validation costs a request.

The probe model is `space-bunny-free`, the one free model that answers a direct call. Probing
any other free model would report a working key as broken, since those refuse outside the
OpenCode client. A rate-limit response counts as authenticated, because a throttled account
is still a valid account.

---

## 6. Why the fleet page is blocked

The requested page needs runtime, RAM and CPU per instance. None of that has a data source,
because **no instances exist**. The only `opencode serve` on the machine is the user's own
interactive session. Step 5 is not built.

What exists today per column: model and account from the deployment, request history from
`LiteLLM_SpendLogs.model_id`, health from `LiteLLM_HealthCheckTable.model_id`. Runtime, RAM and
CPU need a supervisor that tracks PIDs, and LiteLLM has no process-metrics machinery at all:
`debug_utils.py` has `_process_memory_usage` but it reads `/proc/self/statm`, the proxy's own
process.

Also worth knowing before designing the endpoint: `/v2/model/info` does **not** return
`api_base`. The base is resolved at request time, not persisted, so the URL has to come from
`spend_logs.api_base` or be recomputed from the provider default. And `api_base` there
currently holds two spellings, `https://opencode.ai/zen/v1` and the same with a trailing
slash, so grouping on the raw column would split one instance in two.

Per-deployment spend has precedent: `spend_management_endpoints.py` already runs a
`GROUP BY model_id` query.

### An open bug, unrelated to the above

The non-browser create path double-prefixes the provider. A model created by curl logs
`"model_map_key": "opencode/opencode/team-c/space-bunny-free"`. Browser-created models are
correct. Something re-prefixes a model that already carries `opencode/`. Worth pinning down
before building on top, since the doubled name is what the router and logs key on.

---

## 7. About OpenCode auth, checked in the source

Cloned `anomalyco/opencode` and read `packages/opencode/src/auth/index.ts`. Three credential
shapes live in one file:

```typescript
export const OAUTH_DUMMY_KEY = "opencode-oauth-dummy-key"
class Oauth     { type: "oauth"; refresh; access; expires; accountId?; enterpriseUrl? }
class Api       { type: "api"; key; metadata? }
class WellKnown { type: "wellknown"; key; token }
```

So there is no separate Zen console login to replicate. OAuth is one of three interchangeable
shapes in `auth.json`, and Zen's documented flow is sign in and copy the key, which is what
the TUI says too: *"Go to https://opencode.ai/zen to get a key"*. This machine has
`opencode-go: {"type": "api", "key": "sk-..."}`.

There is **no** `FreeTierError` string anywhere in the repo. The gate is server-side at
`packages/console`. The client models it as a retryable reason in
`packages/opencode/src/session/retry.ts` (`free_tier_limit`), and the console recognises these
paths in `log-processor.ts`: `/zen/v1/chat/completions`, `/zen/v1/messages`,
`/zen/v1/responses`, `/zen/v1/models/*`, plus the `/zen/go/v1` equivalents. The only client
identity marker is a User-Agent, `opencode/<channel>/<version>/<client>`, from
`packages/core/src/models-dev.ts`, defaulting to `"cli"`. Spoofing it did not work.

Building a fake-client path is not recommended: it is evasion of a vendor access control, and
it did not work when tried.

---

## 8. Next step

The Add Model dropdown. With OpenCode selected the model name is a free-text box, because
`getProviderModels` filters the cost map by `litellm_provider` and the map has zero opencode
entries. Adding entries to `model_prices_and_context_window.json` is the wrong file, per the
design doc's §8. The fix is a per-provider model source in the UI reading Zen's list, which is
unauthenticated and therefore safe to call. That is step 2's bundled catalog and step 3's UI
half, and it needs no new accounts or credentials.

After that: step 4's session-API path for the seven free models that refuse direct calls,
step 5's supervisor, and only then the fleet page.

---

## 9. Running it locally

```bash
docker run -d --name litellm-oc-test -e POSTGRES_USER=litellm -e POSTGRES_PASSWORD=litellm \
  -e POSTGRES_DB=litellm -p 5432:5432 postgres:16-alpine

# .env: LITELLM_MASTER_KEY, DATABASE_URL, OPENCODE_API_KEY, STORE_MODEL_IN_DB=True
.venv/bin/python litellm/proxy/proxy_cli.py --config /tmp/opencode/oc-test-config.yaml --port 4000
```

First boot takes about four minutes, nearly all of it downloading the Prisma CLI toolchain.
`STORE_MODEL_IN_DB=True` is required or `/model/new` returns 500.

Then two `POST /credentials` and two `POST /model/new` with
`model_name: opencode/<account>/<model>` and `litellm_credential_name` pointing at the
credential. Login to the UI is `admin` plus the master key.

Browser testing used Playwright MCP, configured in `opencode.json` at the project root and
pinned to `@playwright/mcp@0.0.83`. The MCP server only appears in a new session. Chromium is
installed under `~/.cache/ms-playwright`; system deps were not installed because that needs
sudo, and headless Chromium runs fine without them.