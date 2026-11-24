"""API integration, schema migration, and product image tests."""

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from argos.api import create_app
from argos.config import Settings
from argos.db import ensure_schema_migrations, get_session
from argos.image_fetcher import fallback_svg_for_category, is_valid_candidate_url
from argos.models import Listing, PriceBenchmark


def test_image_fetcher_filters_and_svg():
    assert is_valid_candidate_url("https://cdn.example.com/parts/msd-8582.jpg", 800, 600)
    assert not is_valid_candidate_url("http://cdn.example.com/insecure.jpg", 800, 600)
    assert not is_valid_candidate_url("https://cdn.example.com/site-logo.png", 800, 600)
    assert not is_valid_candidate_url("https://cdn.example.com/icon.svg", 800, 600)
    assert not is_valid_candidate_url("https://cdn.example.com/thumb.jpg", 120, 120)

    svg = fallback_svg_for_category("automotive", "MSD 8582 <Test>")
    assert b"<svg" in svg
    assert b"AUTO" in svg
    assert b"&lt;Test&gt;" in svg


def test_legacy_watch_items_schema_migration(tmp_path):
    db_path = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE watch_items ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "name VARCHAR(255) NOT NULL, "
                "raw_request TEXT NOT NULL, "
                "category VARCHAR(100) NOT NULL DEFAULT 'general'"
                ")"
            )
        )
    ensure_schema_migrations(engine)
    cols = {c["name"] for c in inspect(engine).get_columns("watch_items")}
    assert {"image_url", "image_data", "image_mime"}.issubset(cols)


def test_api_health_dashboard_and_images(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path}/api_test.db"
    cfg = Settings(
        database_url=db_url,
        enable_telegram=False,
        enable_scheduler=False,
    )
    app = create_app(settings=cfg, enable_background=False)

    with TestClient(app) as client:
        resp = client.get("/api/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["providers_configured"] >= 4

        induct_payload = {
            "raw_request": "Holley Mid-Mount accessory drive for Ford 302",
            "is_hot": True,
            "auto_activate": True,
            "notify_telegram": False,
            "fetch_image": False,
            "override_payload": {
                "name": "Holley Mid-Mount SBF 302 Accessory Drive",
                "category": "automotive",
                "search_queries": ["holley mid mount 302", "holley mid-mount sbf"],
                "required_tokens": ["holley", "mid"],
                "negative_keywords": ["sbc", "ls1", "chevy"],
                "bundle_components": [],
                "retail_baseline_usd": 2510.95,
                "new_deal_target_usd": 2150.0,
                "used_deal_target_usd": 1650.0,
                "deal_likelihood": "Low local secondhand liquidity; watch open-box and Holley promos.",
                "trusted_retailers": [
                    {
                        "name": "LMR (Late Model Restoration)",
                        "domain": "lmr.com",
                        "notes": "Forum-recommended Mustang specialist",
                        "uncommon": True,
                    }
                ],
                "induction_summary": "High-ticket SBF drive; 60mi radius set.",
            },
        }
        res_ind = client.post("/api/items/induct", json=induct_payload)
        assert res_ind.status_code == 200
        item = res_ind.json()
        item_id = item["id"]
        assert item["radius_miles"] == 60
        assert item["is_hot"] is True
        assert item["status"] == "active"
        assert item["has_image"] is False

        # Fallback SVG renders cleanly before an image is cached
        img_fallback = client.get(f"/api/items/{item_id}/image")
        assert img_fallback.status_code == 200
        assert "image/svg+xml" in img_fallback.headers["content-type"]

        # Refresh-image endpoint stores image_data + serves cached bytes
        async def fake_fetch_image(**kwargs):
            return {
                "image_url": "https://cdn.example.com/holley-mid-mount.png",
                "image_data": b"\x89PNG\r\n\x1a\n" + b"\x00" * 2048,
                "image_mime": "image/png",
            }

        monkeypatch.setattr("argos.api.fetch_product_image", fake_fetch_image)
        res_img = client.post(f"/api/items/{item_id}/refresh-image")
        assert res_img.status_code == 200
        assert res_img.json()["has_image"] is True
        assert res_img.json()["image_bytes"] > 2000

        img_cached = client.get(f"/api/items/{item_id}/image")
        assert img_cached.status_code == 200
        assert img_cached.headers["content-type"] == "image/png"

        # Seed a benchmark and a viable secondhand listing to verify companion dashboard payload
        with get_session() as session:
            session.add(
                PriceBenchmark(
                    watch_item_id=item_id,
                    source="summitracing.com",
                    market_type="new",
                    price_usd=2499.0,
                    url="https://www.summitracing.com/parts/hly-20-370",
                    title="Holley 20-370",
                )
            )
            session.add(
                Listing(
                    watch_item_id=item_id,
                    provider="craigslist",
                    external_id="88001",
                    url="https://dallas.craigslist.org/88001.html",
                    title="Holley Mid-Mount 20-370 SBF Complete",
                    price_usd=1500.0,
                    shipping_usd=0.0,
                    landed_cost_usd=1500.0,
                    distance_miles=18.5,
                    location_text="Denton, TX",
                    deal_score=78.0,
                    deal_reason="$1500 (9% under $1650 target)",
                    state="notified",
                )
            )

        dash_api = client.get("/api/dashboard")
        assert dash_api.status_code == 200
        d = dash_api.json()
        assert d["summary"]["watches_active"] == 1
        assert d["summary"]["viable_secondhand_count"] == 1
        row = d["items"][0]
        assert row["best_secondhand"]["landed_cost_usd"] == 1500.0
        assert any(r["domain"] == "lmr.com" and r["status"] == "unstructured" for r in row["retailer_matrix"])
        assert any(r["domain"] == "summitracing.com" and r["price_usd"] == 2499.0 for r in row["retailer_matrix"])

        dash = client.get("/")
        assert dash.status_code == 200
        assert "Holley Mid-Mount SBF 302" in dash.text
        assert client.get("/static/app.css").status_code == 200
        assert client.get("/static/app.js").status_code == 200

        # Test POST /api/ingest matching & deduplication
        ingest_body = {
            "title": "Holley Mid-Mount 20-370 Ford 302 Complete",
            "price_usd": 1450.0,
            "url": "https://www.facebook.com/marketplace/item/999000111/",
            "source": "facebook_saved_search",
            "location_text": "Frisco, TX",
            "distance_miles": 15.0,
        }
        ing1 = client.post("/api/ingest", json=ingest_body)
        assert ing1.status_code == 200
        assert ing1.json()["matched_watches"] == 1
        assert ing1.json()["alerts_sent"] == 1

        # Re-ingesting same item at same price emits 0 new alerts
        ing2 = client.post("/api/ingest", json=ingest_body)
        assert ing2.status_code == 200
        assert ing2.json()["alerts_sent"] == 0
