"""API integration tests using FastAPI TestClient."""

from fastapi.testclient import TestClient

from argos.api import create_app
from argos.config import Settings


def test_api_health_induction_and_gaps(tmp_path):
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
            "override_payload": {
                "name": "Holley Mid-Mount SBF 302 Accessory Drive",
                "category": "automotive",
                "search_queries": ["holley mid mount 302", "holley mid-mount sbf"],
                "required_tokens": ["holley", "mid"],
                "negative_keywords": ["sbc", "ls1", "chevy"],
                "bundle_components": [],
                "retail_baseline_usd": 2250.0,
                "new_deal_target_usd": 1850.0,
                "used_deal_target_usd": 1400.0,
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
        assert item["radius_miles"] == 60
        assert item["is_hot"] is True
        assert item["status"] == "active"

        items = client.get("/api/items").json()
        assert len(items) == 1

        gaps = client.get("/api/provider-gaps").json()
        assert len(gaps) == 1
        assert gaps[0]["domain"] == "lmr.com"

        dash = client.get("/")
        assert dash.status_code == 200
        assert "Holley Mid-Mount SBF 302" in dash.text
