---
schema: argos-agent-context/v1
project: argos
version: 0.2.0
repository: https://github.com/Awdball/argos.git
container_image: ghcr.io/awdball/argos:latest
runtime: python >= 3.10 (docker: python:3.12-slim)
database: PostgreSQL 16+ (psycopg) or SQLite (tests/local)
entrypoint: python -m argos.main
test_command: .venv/bin/pytest -v
homelab_runbook_ref: Awdball/infra:docs/ARGOS_RUNBOOK.md
---

# AGENTS.md — Argos Machine-Readable Architecture & Engineering Spec

This document provides complete, self-contained architectural context for AI coding agents and engineers working on `Awdball/argos`. Homelab-specific cluster topology, secret provisioning, and Flux CD deployment procedures live separately in the private infrastructure repository (`Awdball/infra:docs/ARGOS_RUNBOOK.md`).

---

## 1. Repository & Module Map

| Path | Key Symbols | Responsibility |
| :--- | :--- | :--- |
| `pyproject.toml` | `project.scripts.argos` | Package metadata (`0.2.0`), dependencies, `templates/*.html` & `static/*` package-data. |
| `Dockerfile` | `CMD ["python", "-m", "argos.main"]` | Multi-arch (`amd64`/`arm64`) `python:3.12-slim` image build. Requires `README.md` in build context. |
| `.github/workflows/docker-publish.yml` | `build-and-push` | Builds & pushes `ghcr.io/awdball/argos` on `main` and `v*.*.*` tags. |
| `src/argos/__init__.py` | `__version__` | Package version string (`"0.2.0"`). |
| `src/argos/config.py` | `Settings`, `get_settings()` | Pydantic `BaseSettings` reading `.env` / environment variables. |
| `src/argos/models.py` | `WatchItem`, `PriceBenchmark`, `Listing`, `FeedbackEvent`, `ProviderGap`, `ProviderRunState`, `ApiUsageLog`, `utcnow()` | SQLAlchemy 2.0 ORM definitions using naive UTC datetimes (`utcnow()`). |
| `src/argos/db.py` | `configure_db()`, `init_db()`, `ensure_schema_migrations()`, `get_session()`, `check_db_health()` | Engine binding (`StaticPool` for in-memory SQLite), additive `ALTER TABLE` column migrations, session scope. |
| `src/argos/geo.py` | `haversine_miles()`, `recommended_radius_miles()` | Great-circle distance and `<$200 -> 20`, `$200–$499 -> 40`, `$500+ -> 60` radius tiers. |
| `src/argos/ollama_client.py` | `strip_thinking()`, `extract_json_object()`, `chat_json()` | Calls `{OLLAMA_BASE_URL}/chat/completions`, strips `<think>...</think>` blocks, extracts JSON safely. |
| `src/argos/image_fetcher.py` | `is_valid_candidate_url()`, `fetch_product_image()`, `fallback_svg_for_category()` | DDGS image discovery + HTTPS download verification (`<= 1 MB`) + category SVG fallback. |
| `src/argos/inductor.py` | `induct_item()`, `upsert_provider_gap()` | Runs web/CL research + Ollama synthesis + image fetch outside DB transactions, then persists item/benchmarks/gaps. |
| `src/argos/scoring.py` | `evaluate_listing()`, `vet_deal_with_ai()`, `should_notify()`, `EvaluationResult` | Scam/negative/token/radius/bundle/landed-cost scoring, optional AI sanity check, and zero-dup/price-drop rules. |
| `src/argos/scheduler.py` | `SchedulerService`, `run_item_scan_now()`, `run_provider_for_item()`, `apply_listing_feedback()`, `effective_interval_minutes()` | 60s async tick scheduler, monthly budget guardrail, provider execution, dedup persistence, quiesce transitions. |
| `src/argos/telegram_bot.py` | `ArgosTelegramBot`, `format_induction_card()`, `format_deal_alert()`, `deal_quiesce_keyboard()` | `python-telegram-bot` v21 polling, `/add`/`/list`/`/info`/`/check`/`/hot`/`/pause`/`/resume`/`/archive`/`/gaps`/`/providers`, 5 inline Quiesce callbacks. |
| `src/argos/companion.py` | `build_dashboard_payload()`, `build_item_companion_view()` | Computes best retail, viable 2nd-hand deals, retailer price/gap matrix, and cadence schedules for `/api/dashboard` and `/`. |
| `src/argos/api.py` | `create_app()` | FastAPI lifespan (DB init, Telegram, Scheduler), `/static` mount, REST routes, Jinja2 HTML bootstrap. |
| `src/argos/main.py` | `run()` | Uvicorn runner on `0.0.0.0:$PORT`. |
| `src/argos/providers/base.py` | `BaseProvider`, `ProviderCapabilities`, `RawListing`, `ProviderResult` | Abstract plugin contract. |
| `src/argos/providers/registry.py` | `register_provider()`, `load_all_providers()`, `get_provider()`, `list_providers()` | Auto-discovers and registers all built-in providers. |
| `src/argos/providers/web_search.py` | `search_web()` | Zero-cost search (`SerpApi` if keyed -> `mcp.exa.ai` MCP -> `ddgs`). |
| `src/argos/providers/craigslist.py` | `CraigslistProvider`, `parse_craigslist_html()` | Scrapes `/search/sss` + `ItemList` JSON-LD coordinates; calculates distance via Haversine. |
| `src/argos/providers/ebay.py` | `EbayProvider` | Official eBay Browse API (`price + shipping`). Dormant without `EBAY_CLIENT_ID`/`EBAY_CLIENT_SECRET`. |
| `src/argos/providers/woot.py` | `WootProvider` | Woot Developer API (`WOOT_API_KEY`) or public deal HTML fallback. |
| `src/argos/providers/slickdeals.py` | `SlickdealsProvider` | Parses Slickdeals search RSS feeds (`rss=1`). |
| `src/argos/providers/universal_retail.py` | `UniversalRetailProvider`, `extract_product_metadata()` | Searches trusted retail domains, extracts `schema.org/Product` JSON-LD / OpenGraph price; emits `gaps` on `403`/`429`/`503` or missing metadata. |
| `src/argos/providers/facebook_apify.py` | `FacebookApifyProvider` | Apify FB Marketplace actor (`apify~facebook-marketplace-scraper`), dormant without `APIFY_TOKEN`, budget-checked via `ApiUsageLog`. |
| `src/argos/templates/dashboard.html` | Companion SPA shell | Zero-CDN HTML template with `<meta name="color-scheme">`, accessible `tablist`, native `<dialog closedby="any">` modals, and safe JSON bootstrap (`<script id="argos-bootstrap">`). |
| `src/argos/static/app.css` | Theme & layout tokens | `light-dark()` + `prefers-color-scheme` fallback, `@container` responsive tables, `dialog:modal` scroll containment, `[aria-selected="true"]::after` tab underline. |
| `src/argos/static/app.js` | Safe DOM SPA controller | Builds all dynamic DOM nodes via `document.createElement` + `textContent` + `safeHref()` (zero `innerHTML` on untrusted data), Safari `closedBy` fallback, `fetchpriority`/`loading` rules. |
| `tests/*.py` | `test_api.py`, `test_craigslist.py`, `test_geo_and_scoring.py`, `test_registry_and_dedup.py` | Offline unit & FastAPI `TestClient` suite using SQLite. |

---

## 2. Database Schema & Critical Transaction Invariants

### Tables (`src/argos/models.py`)
1. `watch_items`: Core watch config, pricing targets, `bundle_components` (JSON), `trusted_retailers` (JSON), `active_providers` (JSON), `radius_miles`, `is_hot`, `status`, plus `image_url`, `image_data` (`LargeBinary`), `image_mime`.
2. `price_benchmarks`: Historical/observed new & used prices recorded during induction.
3. `listings`: Unique constraint `uq_listing_item_provider_ext` on `(watch_item_id, provider, external_id)`. Stores `price_usd`, `shipping_usd`, `landed_cost_usd`, `distance_miles`, `matched_bundle`, `deal_score`, `state`, `last_notified_price_usd`, `snooze_until`, `telegram_message_id`.
4. `feedback_events`: Audit trail of Telegram/UI quiesce and purchase actions.
5. `provider_gaps`: Unique `domain` tracking uncommon or anti-bot blocked retailers (`occurrences`, `status`).
6. `provider_run_state`: Unique `(watch_item_id, provider)` tracking `last_run_at`, `last_status`, `items_found`, `new_alerts`.
7. `api_usage_log`: Unique `(provider, period_month)` tracking monthly metered calls & cost.

### Critical Invariants
* **No Long Transactions (PostgreSQL 60s Idle Timeout):** NEVER hold `get_session()` open across `await` calls (`search_web`, `httpx`, `fetch_product_image`, `chat_json`, or `send_message`). Perform all network/LLM I/O first, then open short `with get_session() as session:` blocks solely for reads/commits.
* **Cross-Dialect Naive UTC:** Always use `utcnow()` (`datetime.now(timezone.utc).replace(tzinfo=None)`) so timestamps compare identically under SQLite (which strips tzinfo) and PostgreSQL.
* **Additive Migrations:** Any new column added to an existing table MUST be mirrored in `ensure_schema_migrations(engine)` in `src/argos/db.py` (with `BYTEA` vs `BLOB` dialect handling).
* **Python 3.10+ Compatibility:** Keep source compatible with Python 3.10 (`timezone.utc`, `Optional`/`List`/`Dict` typing) while building containers on `python:3.12-slim`.

---

## 3. Provider Plugin Specification

To add a new marketplace or retailer plugin:
1. Subclass `BaseProvider` in `src/argos/providers/<name>.py`, set `capabilities = ProviderCapabilities(...)`, and decorate with `@register_provider`.
2. Implement `is_configured(self, settings: Settings) -> bool` (return `False` if required credentials are blank) and `async def search(self, watch_item: WatchItem, settings: Settings) -> ProviderResult`.
3. Return `RawListing` objects (setting `market_type="retail"` or `"secondhand"`) and optionally `gaps` dicts (`domain`, `retailer_name`, `recommended_by`, `reason`, `sample_item`).
4. Import the module in `load_all_providers()` inside `src/argos/providers/registry.py`.
5. Note: `scheduler.py` caps intervals with `min(1440, max(5, interval))`, enforcing at least one check per 24h on every active configured provider unless monthly spend exceeds `APIFY_MONTHLY_BUDGET_USD`.

---

## 4. API & Frontend Invariants

* **Zero Untrusted `innerHTML`:** `app.js` must construct scraped/external strings strictly via `textContent` and validate links via `safeHref()` (`http:` / `https:` only).
* **JSON Bootstrap Escaping:** `api.py` escapes `<`, `>`, `&` to `\u003c`, `\u003e`, `\u0026` before embedding `initial_json` into `dashboard.html`.
* **Modern Web Guidance Compliance:** Preserve `<meta name="color-scheme" content="light dark">` + `:root` `light-dark()` tokens, native `<dialog closedby="any">` opened via `showModal()` with `overscroll-behavior: contain` and Safari outside-click fallback, `role="tablist"` / `role="tabpanel"` initial `hidden` attributes, `@container` responsive tables, and `fetchpriority="high"` on first 2 cards / `loading="lazy"` on subsequent cards.
* **Benchmark Noise Filtering:** `companion.py` filters `new` benchmarks below `25%` of `retail_baseline_usd` (when baseline >= `$50`) so snippet discount text (e.g. `"save $64"`) is not displayed as a retailer price.

---

## 5. Build, Test & Release Playbook

1. Run offline tests before every commit:
   ```sh
   .venv/bin/pytest -v
   ```
   *(All unit tests pass offline; pass `fetch_image=False` in test induction payloads to avoid network calls.)*
2. **Docker Build Context Rule:** `pyproject.toml` references `readme = "README.md"` and `Dockerfile` executes `COPY pyproject.toml README.md ./`. **Do NOT** add `README.md` to `.dockerignore`.
3. Commit and push to `main` (and tag `vX.Y.Z` for releases). `.github/workflows/docker-publish.yml` publishes `ghcr.io/awdball/argos:latest` for `linux/amd64,linux/arm64`.

---

## 6. Security Invariants (`Awdball/argos` is PUBLIC)

* NEVER commit tokens, passwords, chat IDs, `.env` files, `.db` files, or private homelab LAN IPs/secrets into `Awdball/argos`.
* All runtime secrets belong strictly in external environment variables or an out-of-band Kubernetes Secret.
