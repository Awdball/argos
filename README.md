# Argos (`Awdball/argos`)

Universal second-hand & first-hand deal-hunting intelligence framework powered by pluggable marketplace/retail providers, in-cluster AI research (Hermes + Ollama), PostgreSQL persistence, and interactive Telegram notifications.

## Features (Phase 1 — Zero-Cost Core)

- **Universal Item Induction:** Accepts any item (automotive parts, tools, electronics, outdoor gear, home goods) and researches retail baselines, secondary targets, deal likelihood, negative keywords, bundle/kit valuations, and reputable retailers.
- **Dynamic Geo-Radius:** Scales local search distance automatically from home ZIP (`76227`) based on item value (`20 mi` under \$200, `40 mi` for \$200–\$499, `60 mi` for \$500+).
- **Pluggable Provider Registry:**
  - `craigslist`: All-category local search (`search/sss`) with JSON-LD coordinate distance calculation.
  - `slickdeals`: RSS frontpage/deal-feed monitor.
  - `woot`: Developer API + public deal feed support.
  - `universal_retail`: Web search + `schema.org/Product` JSON-LD / OpenGraph price extraction across trusted retailers.
  - `ebay`: Official eBay Browse API (`price + shipping` landed cost), dormant until credentials are supplied.
  - `facebook_apify`: Managed Apify Facebook Marketplace actor with hard monthly budget guardrail, dormant until `APIFY_TOKEN` is supplied.
- **Zero-Duplicate Alerting & Price-Drop Detection:** Deduplicates per `(watch_item_id, provider, external_id)` and only re-alerts on meaningful price drops (default ≥10%).
- **Interactive Telegram Quiesce Loop:** Inline buttons on every deal alert (`Not Good Enough`, `Too Far`, `Good Deal, Bad Timing`, `Wrong Item`, `Bought / Archive`) feed back into future filtering.
- **Provider Gap Tracking:** Logs bot-blocked or unstructured specialty retailers discovered during research/crawling into `provider_gaps` so bespoke plugins can be added incrementally.

## Local Development & Tests

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## Configuration

Copy `.env.example` to `.env` for local runs. In Kubernetes, all secrets live in `argos-secret` (never committed to git).
