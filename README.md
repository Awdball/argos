# Argos

**A pluggable second-hand & first-hand deal intelligence engine with AI-assisted item induction, a visual companion web app, and interactive Telegram notifications.**

Argos watches arbitrary products (automotive parts, tools, electronics, outdoor gear, home goods) across local secondhand marketplaces, national online marketplaces, and trusted retailers. When you add an item, Argos researches retail baselines, secondary market pricing, negative exclusion guards, bundle/kit valuations, and reputable forum-recommended stores, caches a representative product photo, calculates a value-scaled local search radius, and notifies you the moment a qualified deal appears — without ever sending duplicate alerts.

```bash
# Single multi-arch container — state persists in PostgreSQL (or SQLite for local testing)
docker run -d --name argos \
  -p 8080:8080 \
  -e DATABASE_URL="postgresql+psycopg://argos:password@postgres:5432/argos" \
  -e TELEGRAM_BOT_TOKEN="your-bot-token" \
  -e TELEGRAM_CHAT_ID="your-chat-id" \
  ghcr.io/awdball/argos:latest
```

---

## Table of Contents

- [What Argos Does](#what-argos-does)
- [Architecture](#architecture)
- [Local LLM (Ollama) vs. Deterministic Responsibilities](#local-llm-ollama-vs-deterministic-responsibilities)
- [How It Works Under the Hood](#how-it-works-under-the-hood)
- [Alert-State & Quiesce Semantics](#alert-state--quiesce-semantics)
- [Dynamic Geo-Radius & Check Cadence](#dynamic-geo-radius--check-cadence)
- [Companion Web App](#companion-web-app)
- [Telegram Bot Commands](#telegram-bot-commands)
- [Quick Start](#quick-start)
- [Configuration Reference](#configuration-reference)
- [REST API Reference](#rest-api-reference)
- [Writing a Custom Provider Plugin](#writing-a-custom-provider-plugin)
- [Project Layout, Testing & Releasing](#project-layout-testing--releasing)

---

## What Argos Does

* **Universal Item Induction:** Accepts any plain-English product description (via Telegram `/add`, the web UI, or REST API) and conducts live retail, used-market, forum/Reddit, and local Craigslist research before synthesizing targets with an OpenAI-compatible LLM (e.g., Ollama `qwen3.8:27b`).
* **Automatic Product Photo Caching:** Discovers, verifies, and caches a product image (`<= 1 MB`) directly in the database during induction, falling back cleanly to a category SVG emblem when unavailable.
* **Bundle / Kit Intelligence:** Optional `bundle_components` (e.g., an MSD 8582 distributor listing that also includes an MSD 6AL ignition box and Blaster coil) dynamically raise the effective deal target when those companion components appear in the listing title or description.
* **Pluggable Zero-Cost Providers:** Ships with zero-cost providers out of the box (`craigslist` all-categories `search/sss`, `slickdeals` RSS, `woot`, `reddit_classifieds`, `salterracing`, `universal_retail` via `httpx` + `curl_cffi` + Exa MCP + Patchright Chromium, plus human-paced `facebook_browser`, `bestbuy_browser`, and `costco_browser`) alongside the official **eBay Browse API** (Sandbox & Production auto-detected; landed cost = price + shipping).
* **Value-Scaled Geo Radius:** Automatically adjusts local pickup radius from home coordinates based on retail value (`20 mi` under `$200`, `40 mi` for `$200–$499`, `60 mi` for `$500+`).
* **Zero-Duplicate Alerting & Price-Drop Detection:** Deduplicates per `(watch_item_id, provider, external_id)` and suppresses re-alerts unless the landed cost drops by at least `PRICE_DROP_REALERT_PCT` (default `10%`).
* **Interactive Quiesce Feedback Loop:** One-tap Telegram inline buttons and matching web UI controls record `Not Good Enough`, `Too Far`, `Good Deal, Bad Timing`, `Wrong Item`, or `Bought / Archive`.
* **Provider Gap Tracking:** Logs bot-blocked (`403`/`429`/`503`) sites, unstructured retail pages, and uncommon forum-recommended specialty shops into `provider_gaps` so custom scrapers can be added incrementally.

---

## Architecture

```mermaid
flowchart LR
    User["Human Observer<br/>(Web UI / Telegram)"] --> API["FastAPI + Companion UI<br/>(api.py, companion.py)"]
    User --> Bot["Telegram Bot<br/>(telegram_bot.py)"]
    Hermes["Hermes Agent<br/>(Optional AI Peer)"] --> API

    API --> Inductor["Item Inductor<br/>(inductor.py)"]
    Bot --> Inductor
    Inductor --> WebSearch["Web Search & CL Probe<br/>(web_search.py)"]
    Inductor --> ImgFetch["Image Fetcher<br/>(image_fetcher.py)"]
    Inductor --> Ollama["Ollama LLM<br/>(ollama_client.py)"]

    Sched["Background Scheduler<br/>(scheduler.py)"] --> Registry["Provider Registry<br/>(providers/registry.py)"]
    Registry --> CL["craigslist"]
    Registry --> SD["slickdeals"]
    Registry --> WT["woot"]
    Registry --> UR["universal_retail"]
    Registry --> EB["ebay (API key)"]
    Registry --> FB["facebook_browser (Patchright)"]
    Registry --> BB["bestbuy_browser / costco_browser"]
    Registry --> SR["salterracing / reddit_classifieds"]

    Sched --> Scoring["Scoring & Dedup<br/>(scoring.py)"]
    Scoring --> DB[("PostgreSQL / SQLite<br/>(models.py, db.py)")]
    API --> DB
    Bot --> DB
    Inductor --> DB
    Scoring --> Bot
```

---

## Local LLM (`Ollama`) vs. Deterministic Responsibilities

Argos is designed so **background deal hunting never stalls if your local LLM is busy or offline**. Only two narrow cognitive steps call Ollama (`OLLAMA_BASE_URL` / `OLLAMA_MODEL`, default `qwen3.8:27b`), and both have deterministic fallbacks:

### Handled by Local LLM (`Ollama`)

| Task | Module & Function | Why LLM Is Used | Fallback If Ollama Is Offline / Slow |
| :--- | :--- | :--- | :--- |
| **1. Item Induction Synthesis** | `inductor.py` -> `chat_json()` (`90s` timeout) | Reads live web, sold-comp, forum/Reddit, and Craigslist evidence to synthesize clean `search_queries`, `required_tokens` (`a\|b` synonyms), `negative_keywords`, `bundle_components`, `retail_baseline_usd`, `new_deal_target_usd`, `used_deal_target_usd`, `deal_likelihood`, and `trusted_retailers`. | Falls back to `_fallback_research_plan()` (regex tokenization + median price calculation) — or skips Ollama entirely when `override_payload` is supplied. |
| **2. Optional Deal Sanity Vet** | `scoring.py` -> `vet_deal_with_ai()` (`15s` timeout) | Last-mile check on candidates that already passed all hard filters, catching subtle accessory-only or wrong-variant listings before firing a Telegram alert. | Fails open (`True`) on timeout or connection error so valid deals are never dropped. |

*(Outside Argos's core Python process, **Hermes Agent** uses its own LLM loop on scheduled crons to inspect `provider_gaps`, author custom scrapers, and discover niche retailers.)*

### Handled Deterministically (Zero LLM Required)

| Subsystem | Module(s) | What It Does Without an LLM |
| :--- | :--- | :--- |
| **All Provider Scans & Scraping** | `scheduler.py`, `providers/*.py` | Runs Craigslist, eBay Browse API, Facebook/Best Buy/Costco Patchright browsers, Salter Racing, Reddit Classifieds, Slickdeals RSS, Woot, and Universal Retail (`httpx` + `curl_cffi` + Exa MCP + Patchright + `"$"` snippets). |
| **Hard Filtering, Bundle Math & Scoring** | `scoring.py`, `geo.py` | Computes `landed_cost_usd = price + shipping`, rejects scam/junk/expired/OOS listings, enforces `required_tokens` and `negative_keywords`, adds `bundle_components` allowances, and checks Haversine distance against `radius_miles`. |
| **Dedup, Quiesce & Price-Drop Re-Alerts** | `models.py`, `scoring.py`, `scheduler.py` | Deduplicates on `(watch_item_id, provider, external_id)`, tracks `quiesced_*` states (`price`, `distance`, `timing`, `wrong_item`, `expired`, `false_positive`, `bought`), and re-alerts on `>= 10%` drops. |
| **Product Image Fetching & Caching** | `image_fetcher.py` | Searches via `ddgs`, verifies HTTPS image MIME/bytes (`<= 1 MB`), supports cycling alternative images, and generates category SVGs. |
| **Telegram Bot & Companion Web UI** | `telegram_bot.py`, `companion.py`, `api.py`, `static/*` | Handles `/add`, `/check`, inline Quiesce callbacks, `historical_best_retail`, `target_gap`, `hermes_activities` log, and the 5-theme `awdash`-styled dashboard. |

---

## How It Works Under the Hood

1. **Induction (`inductor.py`):**
   * Runs web searches (`SerpApi` if keyed, else `mcp.exa.ai` / `ddgs`) for new street pricing, sold/used comps, and forum/Reddit retailer recommendations, plus a local Craigslist probe (`50 mi`).
   * Passes evidence outside any open DB transaction to Ollama (`qwen3.8:27b`), stripping `<think>` blocks and extracting structured `search_queries`, `required_tokens`, `negative_keywords`, `bundle_components`, `retail_baseline_usd`, `new_deal_target_usd`, `used_deal_target_usd`, `deal_likelihood`, and `trusted_retailers`. Falls back deterministically if Ollama is unreachable.
   * Fetches and stores a representative product photo via `image_fetcher.py` and logs any `uncommon` forum-recommended retailers into `provider_gaps`.
2. **Scoring & Filtering (`scoring.py`):**
   * Computes `landed_cost_usd = price_usd + shipping_usd`.
   * Rejects missing/placeholder scam prices (`<= $1`, `123`, `1234`, `12345`, `9999`, `99999`), universal junk terms (`wtb`, `box only`, `parts only`, `broken`, `core only`), item `negative_keywords`, missing `required_tokens` (supporting `a|b` synonym groups), and listings beyond `radius_miles * 1.15`.
   * Checks `bundle_components` for matching add-on tokens and adds their `used_value_usd` / `new_value_usd` to the effective target ceiling.
   * Optionally calls Ollama (`vet_deal_with_ai`, 15s timeout) to reject subtle accessory/variant mismatches before alerting.
3. **Deduplication & Gap Logging (`scheduler.py`):**
   * Upserts every evaluated candidate under unique key `(watch_item_id, provider, external_id)`.
   * Records `universal_retail` anti-bot rejections (`403`/`429`/`503`) or missing JSON-LD/OpenGraph price metadata on trusted domains into `provider_gaps`.

---

## Alert-State & Quiesce Semantics

Every tracked listing maintains an explicit `state` checked by `should_notify()` before any alert is sent:

| Listing State | Set By | Re-Alert Behavior |
| :--- | :--- | :--- |
| `new` | Evaluator passed target | Alerts immediately, then transitions to `notified`. |
| `ignored` | Below score / rejected | Silent unless price drops into target & passes filters. |
| `notified` | Alert delivered | Silent unless landed price drops by `>= 10%` (`PRICE_DROP_REALERT_PCT`). |
| `quiesced_price` | `💸 Not Good Enough` | Muted at notified price; **re-alerts only on `>= 10%` price drop**. |
| `quiesced_distance` | `📍 Too Far` | Permanently suppressed regardless of price changes. |
| `quiesced_timing` | `⏳ Good Deal, Bad Timing` | Snoozed for `TIMING_SNOOZE_DAYS` (default `14` days) without altering price targets; eligible after snooze expires. |
| `quiesced_wrong_item` | `🚫 Wrong Item` | Permanently suppressed regardless of price changes. |
| `bought` | `✅ Bought / Archive` | Marks listing `bought` and archives the parent `WatchItem` (`status = "archived"`). |

---

## Dynamic Geo-Radius & Check Cadence

Local search radius scales from your home location (`HOME_POSTAL`, `HOME_LAT`, `HOME_LON`) based on `retail_baseline_usd`:

* **Under `$200` (or unknown):** `RADIUS_BASE_MILES` (`20 mi` = 15 mi + 5 mi buffer)
* **`$200` to `$499.99`:** `RADIUS_MID_MILES` (`40 mi`)
* **`$500+`:** `RADIUS_HIGH_MILES` (`60 mi`)

All intervals are clamped by `effective_interval_minutes()` to at most `1440` minutes (at least once every 24 hours):

| Provider | Market Type | Auth Required | Normal Cadence | HOT Cadence | Cost / Call |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `craigslist` | Secondhand (geo) | None | `60m` | `20m` | `$0.00` |
| `ebay` | Secondhand / New (shipping) | `EBAY_CLIENT_ID` + `EBAY_CLIENT_SECRET` (Sandbox auto-detected) | `60m` | `20m` | `$0.00` (free tier) |
| `facebook_browser` | Secondhand (geo) | None (optional `FACEBOOK_STORAGE_STATE_PATH`) | `180m` | `60m` | `$0.00` |
| `reddit_classifieds` | Secondhand (shipping) | None | `180m` | `60m` | `$0.00` |
| `slickdeals` | Aggregator | None | `180m` | `60m` | `$0.00` |
| `woot` | Retail | Optional `WOOT_API_KEY` | `360m` | `120m` | `$0.00` |
| `bestbuy_browser` | Retail (Patchright) | None | `360m` | `90m` | `$0.00` |
| `costco_browser` | Retail (Patchright) | None | `360m` | `90m` | `$0.00` |
| `salterracing` | Retail | None | `360m` | `120m` | `$0.00` |
| `universal_retail` | Retail (`httpx` / `curl_cffi` / Exa / Patchright) | None (optional `SERPAPI_KEY`) | `720m` (12h) | `240m` (4h) | `$0.00` |

---

## Companion Web App

Served directly from the FastAPI container at `GET /` (with zero Node build step or external CDN dependencies):

* **Watchlist Grid:** Visual cards showing the cached product image, category/HOT badges, a proportional **price position gauge** (Used Target, New Target, MSRP, Best Retail Pin, Best 2nd-Hand Pin), Best Live Retail vs. Viable 2nd-Hand split boxes, search frequency/last-checked summary, and 1-click **Scan Now**.
* **Inspect & Edit Modal:** Native `<dialog>` detail drawer featuring **Refresh Photo**, market likelihood notes, bundle allowances, **Retailers & Current Prices** responsive table, **Viable 2nd-Hand Deals** with all 5 inline Quiesce buttons, per-provider cadence & next-due health table, and live target/query/negative-keyword editing.
* **2nd-Hand Deals Feed, Retailers & Gaps, & Providers/Budget Tabs:** Cross-watch feed of viable secondhand matches, prioritized `provider_gaps`, and monthly Apify budget utilization meter.

---

## Telegram Bot Commands

Authorized strictly to `TELEGRAM_CHAT_ID`:

* `/start`, `/help` — Show active watch count and command reference
* `/add [--hot] <item>` — Research & induct an item, sending an Induction Card with **Activate Watch**, **Activate Hot**, and **Cancel** buttons
* `/list` — List all watches with targets and search radius
* `/info <id>` — Show full watch parameters, cadence, and recent listings
* `/check <id>` — Trigger an immediate scan across all configured providers
* `/hot <id>` — Toggle HOT cadence
* `/pause <id>`, `/resume <id>`, `/archive <id>` — Manage watch lifecycle
* `/gaps` — List open `provider_gaps` needing custom scrapers
* `/providers`, `/status` — Show provider readiness, DB totals, and monthly metered spend

---

## Quick Start

### 1. Local Development (Python 3.10+ & SQLite)

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
# Edit .env with optional TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / OLLAMA_BASE_URL
python -m argos.main
```

Open `http://localhost:8080`.

### 2. Container Image

```bash
docker build -t argos:local .
docker run --rm -p 8080:8080 --env-file .env argos:local
```

### 3. Kubernetes

Deploy `ghcr.io/awdball/argos:latest` on port `8080` with `envFrom` referencing a Kubernetes `Secret` containing `DATABASE_URL` and optional tokens, plus `readinessProbe` / `livenessProbe` targeting `GET /api/health`.

---

## Configuration Reference

| Variable | Default | Description |
| :--- | :--- | :--- |
| `DATABASE_URL` | `sqlite:///argos.db` | SQLAlchemy DB URL (use `postgresql+psycopg://...` in production). |
| `TELEGRAM_BOT_TOKEN` | `""` | Bot token from `@BotFather`. |
| `TELEGRAM_CHAT_ID` | `None` | Authorized Telegram numeric chat ID. |
| `OLLAMA_BASE_URL` | `http://ollama:11434/v1` | OpenAI-compatible inference base URL. |
| `OLLAMA_MODEL` | `qwen3.8:27b` | Model name used for induction and optional deal vetting. |
| `OLLAMA_TIMEOUT_SECONDS` | `90` | Request timeout for induction LLM calls. |
| `HOME_POSTAL` | `"76227"` | Home ZIP code for local Craigslist searches. |
| `HOME_LAT` / `HOME_LON` | `33.2259` / `-96.9997` | Coordinates used for Haversine distance calculation. |
| `CRAIGSLIST_SITE` | `"dallas"` | Regional Craigslist subdomain. |
| `RADIUS_BASE_MILES` / `MID` / `HIGH` | `20` / `40` / `60` | Value-tiered local radius bounds (`<$200`, `$200–$499`, `$500+`). |
| `PRICE_DROP_REALERT_PCT` | `0.10` | Minimum price drop fraction (`10%`) required to re-alert. |
| `TIMING_SNOOZE_DAYS` | `14` | Snooze window for `Good Deal, Bad Timing` quiesce feedback. |
| `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` | `""` | Official eBay Browse API OAuth credentials (auto-detects `-SBX-` sandbox keys). |
| `WOOT_API_KEY` | `""` | Optional Woot Developer API key. |
| `ENABLE_FACEBOOK_BROWSER` / `BESTBUY` / `COSTCO` | `true` | Enable serialized Patchright Chromium browser providers. |
| `FACEBOOK_CITY_SLUG` | `"dallas"` | Marketplace city slug for `facebook_browser`. |
| `KEEPA_API_KEY` | `""` | Reserved for Keepa Amazon price-history integration. |
| `SERPAPI_KEY` | `""` | Optional SerpApi key for retail search queries. |
| `PORT` | `8080` | HTTP server listen port. |
| `ENABLE_TELEGRAM` | `true` | Enable Telegram polling and proactive alerts. |
| `ENABLE_SCHEDULER` | `true` | Enable background polling scheduler loop. |
| `SCHEDULER_TICK_SECONDS` | `60` | Scheduler wake-up interval in seconds. |
| `LOG_LEVEL` | `"INFO"` | Python logging level. |

---

## REST API Reference

| Method & Path | Description |
| :--- | :--- |
| `GET /` | Companion Web App HTML UI (bootstrapped with initial state). |
| `GET /api/health` | Database connectivity probe (`SELECT 1`), version, configured provider count, active watch count. |
| `GET /api/dashboard` | Unified companion payload: summary KPIs, enriched watch items (best retail, viable 2nd-hand, retailer matrix, cadence), cross-watch deals, providers, and gaps. |
| `GET /api/items` | List all watched items with benchmarks and run states. |
| `POST /api/items/induct` | Induct a new item (`raw_request`, `is_hot`, `auto_activate`, `notify_telegram`, `fetch_image`, optional `override_payload`). |
| `PATCH /api/items/{id}` | Update targets, radius, queries, required tokens, negative keywords, `is_hot`, `status`, or `active_providers`. |
| `DELETE /api/items/{id}` | Delete a watch item and cascade-remove its benchmarks and listings. |
| `POST /api/items/{id}/check` | Trigger an immediate scan across all configured providers for item `{id}`. |
| `GET /api/items/{id}/image` | Serve cached product photo bytes (`200`), external redirect (`302`), or category SVG fallback. |
| `POST /api/items/{id}/refresh-image` | Re-fetch or override (`{"image_url": "..."}`) a watch item's cached photo. |
| `GET /api/listings` | Query evaluated listings (`?watch_item_id=&state=&limit=`). |
| `POST /api/listings/{id}/feedback` | Record quiesce/purchase action (`price`, `distance`, `timing`, `wrong`, `bought`). |
| `GET /api/providers` | List all registered providers, capabilities, and current-month API usage. |
| `GET /api/provider-gaps` | List uncommon or bot-blocked retailers recorded during research or crawls. |
| `PATCH /api/provider-gaps/{id}` | Update gap status (`open`, `resolved`, `waf_blocked`) and reason. |
| `GET /api/hermes/status` / `POST /api/hermes/activity` | Read/update live Hermes operation status and task duration log. |
| `GET /api/stats` | Aggregate counts across watches, listings, alerts, and open gaps. |

---

## Writing a Custom Provider Plugin

1. Create `src/argos/providers/<retailer>.py`, subclass `BaseProvider`, define `capabilities = ProviderCapabilities(...)`, and decorate the class with `@register_provider`.
2. Import the module inside `load_all_providers()` in `src/argos/providers/registry.py`.
3. Add a unit test under `tests/` verifying HTML/API parsing and `ProviderResult` output.
4. Run `pytest` and publish a new container image.

---

## Project Layout, Testing & Releasing

```text
argos/
├── .github/workflows/docker-publish.yml   # Multi-arch GHCR build (linux/amd64, linux/arm64)
├── Dockerfile                             # python:3.12-slim runtime
├── pyproject.toml                         # Packaging & dependencies
├── AGENTS.md                              # Machine-readable architecture & engineering spec
├── src/argos/
│   ├── api.py                             # FastAPI routes & static mount
│   ├── companion.py                       # Companion read-model (retailer matrix, cadence, 2nd-hand feed)
│   ├── config.py                          # Pydantic BaseSettings
│   ├── db.py                              # Engine, sessions & ensure_schema_migrations()
│   ├── geo.py                             # Haversine distance & value-tiered radius
│   ├── image_fetcher.py                   # DDGS image search, HTTP verification & SVG fallback
│   ├── inductor.py                        # Web/CL research + Ollama induction + gap logging
│   ├── main.py                            # Uvicorn entrypoint
│   ├── models.py                          # SQLAlchemy 2.0 ORM tables
│   ├── ollama_client.py                   # OpenAI-compatible Ollama JSON helper
│   ├── scheduler.py                       # Cadence loop, budget checks, dedup & feedback transitions
│   ├── scoring.py                         # Scam/junk/bundle/landed-cost scoring & re-alert rules
│   ├── telegram_bot.py                    # @awdargos_bot commands & inline Quiesce cards
│   ├── providers/                         # craigslist, ebay, facebook_browser, bestbuy_browser, costco_browser, salterracing, reddit_classifieds, slickdeals, universal_retail, woot
│   ├── static/                            # app.css, app.js (zero-bundler modern web UI)
│   └── templates/                         # dashboard.html
└── tests/                                 # Pytest unit & API integration suite
```

### Running Tests

```bash
.venv/bin/pytest -v
```

### Releasing

Push commits or semantic tags (`vX.Y.Z`) to `main`. GitHub Actions builds and publishes `ghcr.io/awdball/argos` (`latest`, semver, and commit SHA tags).
