# Buddy2api 2.0

[English](README_EN.md) | [中文](README.md)

> Local consumer AI clients → one OpenAI-compatible API for Codex, OpenCode, Cherry Studio, NextChat, and similar agents. Work Buddy / CodeBuddy, QClaw, QwenWork, and TraeWork are on by default; pick one in the UI dropdown. Each request stays on one channel.

Release **2.1.9**. Local use only. Do not expose this on the public internet, and do not share credentials, API keys, or the database.

Default loopback startup opens the management page without an Admin Token. Restarting does not invalidate local access. Repeated startup opens the existing instance; a database can only be used by one process. Use `--no-browser` for background services. Setting `--admin-token` or `CB_GATEWAY_ADMIN_TOKEN` enables explicit token authentication and is required for non-loopback listeners. Enter that token in the management settings. Client API keys and upstream account credentials are unchanged.

## What is this?

Buddy2api listens on `http://127.0.0.1:8787/v1`. You stay signed into the official apps; this gateway imports those sessions and forwards chat. Typical clients use Chat Completions. Codex uses `/v1/responses`; create the key as type Codex in the UI to enable Codex prompt sanitization.

All four channels are on by default. A channel with no local login shows empty on Accounts; nothing is imported until you click Import.

```powershell
python -m buddy2api
```

| Channel | Default | Where logins live |
|---|---|---|
| WorkBuddy / CodeBuddy | on | `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth` |
| QClaw | on | `%APPDATA%\QClaw` |
| QwenWork | on | `%APPDATA%\QwenWorkCN` |
| TraeWork | on | Windows: `%APPDATA%\TRAE SOLO CN\User\globalStorage`; macOS: `~/Library/Application Support/TRAE SOLO CN/User/globalStorage` |

Narrow with `CB_GATEWAY_PROVIDERS=workbuddy` if you only want one.

## Before you start

1. **An empty Accounts page after startup is expected.** 2.0 does not import on boot. Pick a channel → Detect → Import. All four channels are in the dropdown.
2. **One API key is one channel.** Create the key with a channel selected. A WorkBuddy key uses `auto` / `glm-5.2`; a QwenWork key uses `auto` or `qwork-advanced`; a TraeWork key uses `auto` or `qwen-3.7-plus`. Mismatched model/key returns 400 or 403 — there is no cross-vendor failover.

   A key can also be **pinned to one account**: pick an account when creating or editing the key and every request from that key uses only that account, bypassing the scheduler. Use it when a key should spend one account's quota specifically. If the pinned account is unavailable (disabled, cooling down, or the wrong channel), the request fails instead of silently switching — otherwise you would think the quota was untouched while another account was being spent. Leave it unset for normal automatic routing.

   Note: that failure currently shares the same error as “no usable account in the channel” (503 `channel_unavailable`), so when you see it, check whether the pinned account is the one that went down.
3. **HTTP 503 `channel_unavailable`** means that channel has no imported account.
4. **Run QClaw / QwenWork with `python -m buddy2api` on Windows.** A Linux Docker container cannot decrypt those DPAPI files; the UI says so. WorkBuddy can stay on Docker.
5. If the chat client is itself in Docker, Base URL is `http://host.docker.internal:8787/v1`.

## Install

1. [Git](https://git-scm.com/downloads), [Miniconda](https://docs.conda.io/projects/miniconda/en/latest/) (Python 3.12), and sign into Work Buddy / CodeBuddy at least once.
2. Reopen the terminal, then:

```powershell
git --version
conda --version
git clone https://github.com/wicm84266964/Buddy2api.git
cd Buddy2api
conda create -n buddy2api python=3.12 -y
conda activate buddy2api
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m buddy2api
```

3. Open http://127.0.0.1:8787 → Accounts → Detect → Import → Test → API Keys (select a channel; pick Codex if the client is Codex) → point your client at `http://127.0.0.1:8787/v1`.

Windows script: `.\scripts\start.bat`. Docker helper: `.\scripts\start-docker-win.ps1` (WorkBuddy mount; use native Python for QClaw/QwenWork).

Later starts: `conda activate buddy2api` then `python -m buddy2api` in the project directory.

## FAQ

- WorkBuddy's collected responses, including the default tool-stall retry path, reject partial text without completion metadata instead of synthesizing a successful `stop`. An explicit `finish_reason` followed by EOF remains valid without `[DONE]`. A `[DONE]` event alone does not make text without a finish reason complete. This validation does not determine whether a model's explicit `stop` is premature or resolve every long-session stall.

- `conda` not found: use Miniconda Prompt, or `conda init powershell` and reopen the terminal.
- `No module named ...`: activate `buddy2api`, then `python -m pip install -r requirements.txt`.
- Port 8787 in use: stop the old process or `python -m buddy2api --port 8788`.
- No accounts in the UI: import has not been run yet.
- Key create fails: the channel dropdown is required.
- 503 `channel_unavailable`: the key's channel has no usable account; if the key is pinned to a specific account, the pinned account may be the one unavailable (disabled / cooling down / wrong channel). Re-pin it, or clear the pin to return to automatic routing.
- 403 `key_channel_mismatch`: the model prefix does not match the key’s channel.
- 400 `unknown_model`: that model does not belong to this key’s channel.

### Requests keep landing on one or two accounts?

Account selection looks at **how many requests each account actually served in a recent window** (15 minutes by default, tunable with `CB_GATEWAY_ROUTE_WINDOW_SECONDS`); the least-loaded account goes first. A brief skew toward one account is therefore normal and self-corrects as the window slides.

The **Requests** column in the accounts table is a **lifetime** counter. It only grows and **does not participate in routing**, so an old account's large historical count will never crowd out a newer one.

Within a priority tier the order is weight first, then least in-window requests per weight, with a stickiness layer on top: the same model prefers to return to its previous account to preserve the prompt cache, and only yields once that account has served more than one weight unit beyond the idlest peer.

### Other accounts unusable after one account hits its quota?

Upstream quota errors (HTTP 429, WorkBuddy `code 6004` "usage exceeds frequency limit") are tracked **per model**, not per account: while an account is rate-limited on deepseek it can still serve glm. The gateway follows that semantics:

- A rate-limited account is removed from the candidate pool for **that model only** (cooldown tunable with `CB_GATEWAY_RATE_LIMIT_COOLDOWN_SECONDS`, default 900s) and keeps serving every other model. When the cooldown expires the gateway tries it again; if it is still limited the timer restarts.
- The failover retry budget is 8 accounts (`CB_GATEWAY_MAX_ACCOUNT_ATTEMPTS`). This is the important part: with the old fixed budget of 3, a pool larger than 3 could burn every attempt on rate-limited accounts and never reach a healthy one — which shows up as "there are working accounts, yet requests keep failing".
- A rate-limited account is also excluded from the "refresh expired accounts" fallback path.
- When a key is pinned to a specific account and that account is rate-limited on the model, the request fails outright (pinning means "use this account only", not "keep hammering it").

### International site tool-turn continuations fail with `code 11155`

On the international site (`www.workbuddy.ai`), `code 11155` (`the reasoning content from the previous turn must be passed back in thinking mode`) has a separate cause that has **nothing to do with `reasoning_content`**: in thinking mode the international site validates a field named `reasoning`, while the field it streams back is called `reasoning_content` — same name, opposite direction.

The trigger is "thinking mode + the request carries `tools` + the last message is not a `user` message (i.e. continuing an unfinished turn)", with the first plain-text assistant message (no `tool_calls`) after the last `user` message missing or having an empty `reasoning`. The common case is a tool-turn continuation: `text assistant → tool_calls assistant → tool`. The upstream rejects the entire request.

The gateway fills in `reasoning` for that one message (mirroring a real `reasoning_content` when present, otherwise a single-space placeholder) and leaves every other message untouched. Set `CB_GATEWAY_REASONING_PASSTHROUGH=off` to disable this rewrite.

### The same model costs different amounts on the international and domestic sites

The international and domestic editions are billed separately, and **“free” is a property of the model × site pair, not of the site**:

| Model | International | Domestic |
| --- | --- | --- |
| `deepseek-v4.1-flash` | free (1750/1750 requests charged 0) | billed (485 of 497 requests charged) |
| `glm-5.3` | billed | — |

So the default site preference is **`auto`**: it reads the request log, measures what each model actually cost on each site, and prefers the side that is free or cheaper per call. The order is “is it completely free” first, then lower **average cost per call**. Equal billing on both sides means the site is not distinguished (no point giving up load balancing), and fewer than 5 samples also means no distinction until there is enough data.

To set it by hand, go to **Models → Site preference** and choose International / Domestic / Auto / No preference per model, or set a single default for models without their own entry.

### How do expiring credits get used first?

Account credits come in **time-limited packages** that are lost on expiry. So within the same tier of candidates the gateway **prefers the account whose credits expire soonest** — that is exactly what the “Expiring soon” column on the accounts page reports (the `30-day expiring` total plus the nearest expiry time).

The order is:

1. **Narrow to the cheaper side first** via site preference (free > lower unit price);
2. then, among that side's accounts, take the batch expiring **soonest** (within a 1-day slack, so it does not degrade into “always use the single earliest-expiring account”);
3. finally spread load within that batch using the usual in-window requests per weight.

The order cannot be reversed: picking by expiry first would push requests onto the paid site to burn credits that were about to expire anyway — a net loss.

The expiry data comes from the local cache written by **Refresh official quota** on the accounts page; **routing never calls upstream for it**. Accounts that were never refreshed, or whose refresh failed, simply do not take part in this step (they are not excluded and still compete on load), so behaviour without a refresh is exactly as before.

This is a pure preference: if the preferred side has no usable account, or all of its accounts have been tried, the request falls back to the other side rather than failing.

Each account name on the Accounts page shows its site underneath (`国际版 · www.workbuddy.ai` / `国内版 · www.codebuddy.cn`); detection is by domain suffix only.

## Upgrade from 1.4.x

The database migrates on startup. Existing keys stay on `workbuddy`. Startup no longer auto-imports; empty channel is 503; new keys must pick a channel; the official-balance column shows credits only.

## Client

| Field | Value |
|---|---|
| Base URL | `http://127.0.0.1:8787/v1` |
| API Key | Created in the UI, bound to one channel; optionally pinned to one account |
| Model | WorkBuddy `auto`; QClaw `auto`; QwenWork `qwork-advanced` |

Unprefixed `auto` follows the key’s channel. Use a separate key per channel. On the Models page, “一键读取供应模型” refreshes each channel’s supplier list separately; a TraeWork-only id such as Doubao is never merged into WorkBuddy.

### Model capacity discovery

Refreshing supplier models now retains upstream input/output capacities. `/v1/models` exposes `context_window` and `max_output_tokens`; WorkBuddy's `maxInputTokens` and `maxOutputTokens` map to these fields, rather than treating its desktop `contextWindow.defaultLength` as the maximum.

Missing fields independently fall back to 262,144 context tokens and 32,768 output tokens on every channel. The per-field `capacity_source` is `catalog` or `fallback`. Fallbacks are configuration defaults, not verified upstream limits. Explicit `max_tokens` and `max_completion_tokens` requests are clamped only when the catalog contains a known output limit; converted Responses `max_output_tokens` requests use the same path. Omitted budgets stay omitted, and unknown capacities do not impose a hard cap. Reaching a configured output budget can still produce `length`; discovery does not prevent all long-task truncation.

Channels whose catalog carries reasoning metadata (currently WorkBuddy only) publish the upstream `supportsReasoning`, `reasoning.supportedEfforts`, default tier, and `canDisableThinking` on `/v1/models`, for both the bare id and the `workbuddy/` prefix. Missing effort lists are never invented: QClaw, QwenWork, and TraeWork handle reasoning through their own protocols and expose no tiers in the model list. Refresh WorkBuddy supplier models once after upgrading; older catalogs lack these fields.

### Failure classes

Upstream failures are classified in the request log's `finish_reason`: `upstream_http` (the upstream returned an HTTP error), `upstream_disconnect` (the connection dropped), `incomplete_stream` (the stream ended early without `[DONE]` or a `finish_reason`), and `parse_error` (an SSE event could not be parsed). `error_msg` carries a `[class]` prefix; an empty body becomes `upstream HTTP 502, empty body`, and an httpx error without text keeps its exception type name. `/v1/responses` uses the same classes for `error.code`. Failover attempts still log `retry`, with the class prefix in the message. The log filter's "error" view stays keyed on `status_code`, so intermediate retry rows remain visible for troubleshooting.

### Reasoning effort

Agent clients can send top-level `reasoning_effort` to Chat Completions and the standard `reasoning: {"effort": "high"}` object to Responses. Compatibility forms used by OpenCode, DSH, Cherry, and Claude-style clients are also accepted: `reasoning.effort`, `reasoningEffort`, `thinking.type`, `thinking.effort`, `output_config.effort`, and `enable_thinking`. Accepted levels are `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, and `ultra`; `off` is an alias for `none`.

| Channel | Effective capability |
|---|---|
| WorkBuddy | DeepSeek V4 Pro/Flash supports `low` / `high` / `max`; standard levels are projected onto those tiers. The default is `high` and can be disabled with `CB_GATEWAY_DEFAULT_REASONING_EFFORT=off` |
| QClaw | The control is normalized to `reasoning_effort` and forwarded. Whether a tier takes effect depends on the selected upstream model; no gateway default is injected |
| QwenWork | The protocol exposes only an `is_reasoning` switch. `none` disables it and any other explicit tier enables it; distinct effort levels are unavailable |
| TraeWork | The current session protocol has no verified reasoning control field, so effort selection is not supported |

Chat streams preserve `reasoning_content`. Responses streams expose standard `response.reasoning_summary_*` events and accept valid reasoning-only completions.

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-cb-your-key" \
  -d '{"model":"auto","messages":[{"role":"user","content":"hello"}]}'
```

## Environment

`CB_GATEWAY_PROVIDERS` (default `workbuddy,qclaw,qwenwork,traework`), `CB_GATEWAY_AUTO_IMPORT` (default `0`), `CB_GATEWAY_ROUTE_WINDOW_SECONDS` (default `900`, the load-averaging window used for account selection), `CB_GATEWAY_RATE_LIMIT_COOLDOWN_SECONDS` (default `900`, how long an account is skipped for a model after a 429; tracked per account+model), `CB_GATEWAY_MAX_ACCOUNT_ATTEMPTS` (default `8`, how many accounts a single request may fail over through), `CB_GATEWAY_REASONING_PASSTHROUGH` (set `off` to disable the historical-assistant reasoning field rewrites), `CB_AUTH_DIR` / `CB_QCLAW_AUTH_DIR` / `CB_QWENWORK_AUTH_DIR` / `CB_TRAEWORK_AUTH_DIR`, `CB_TRAEWORK_OS_INFO` (the `OSInfo` reported on TraeWork refresh; Windows `windows`, macOS `mac`, Linux `linux`), `CB_TRAEWORK_DEVICE_NAME`, `CB_GATEWAY_ADMIN_TOKEN`, `CB_GATEWAY_MASTER_KEY`.

`CB_GATEWAY_DEFAULT_REASONING_EFFORT` controls the default reasoning effort for WorkBuddy DeepSeek V4 Pro/Flash. It accepts `low`, `high`, or `max`, defaults to `high`, and can be disabled with `off`. A Responses `reasoning.effort` or Chat Completions `reasoning_effort` value overrides the default.

Keep `--host 127.0.0.1`. Do not share the database, auth folders, or key screenshots.

## License

MIT
