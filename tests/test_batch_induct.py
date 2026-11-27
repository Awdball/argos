"""Tests for POST /api/items/induct-batch shortlist ingestion and deduplication."""

from fastapi.testclient import TestClient

from argos.api import create_app
from argos.config import Settings
from argos.db import init_db


def test_induct_batch_creates_items_and_skips_duplicates(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'batch.db'}"
    settings = Settings(database_url=db_url, enable_telegram=False)
    init_db(db_url)
    app = create_app(settings)
    client = TestClient(app)

    payload = {
        "skip_duplicates": True,
        "items": [
            {
                "raw_request": "Holley Sniper 2 EFI Master Kit 550-510-3XX",
                "auto_activate": True,
                "notify_telegram": False,
                "fetch_image": False,
                "override_payload": {
                    "name": "Holley Sniper 2 EFI Master Kit",
                    "category": "automotive",
                    "search_queries": ["Holley Sniper 2 EFI 550-510"],
                    "required_tokens": ["holley", "sniper"],
                    "negative_keywords": ["harness only"],
                    "retail_baseline_usd": 1499.0,
                    "new_deal_target_usd": 1250.0,
                },
            },
            {
                "raw_request": "Wilwood Dynalite Front Brake Kit 140-13476",
                "auto_activate": True,
                "notify_telegram": False,
                "fetch_image": False,
                "override_payload": {
                    "name": "Wilwood Dynalite Front Brake Kit",
                    "category": "automotive",
                    "search_queries": ["Wilwood 140-13476"],
                    "required_tokens": ["wilwood", "140-13476"],
                    "retail_baseline_usd": 950.0,
                    "new_deal_target_usd": 800.0,
                },
            },
            {
                # In-batch duplicate of the first item
                "raw_request": "Holley Sniper 2 EFI Master Kit 550-510-3XX",
                "fetch_image": False,
            },
        ],
    }

    resp = client.post("/api/items/induct-batch", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["created_count"] == 2
    assert data["skipped_count"] == 1
    assert data["error_count"] == 0
    assert data["created"][0]["status"] == "active"
    assert data["skipped"][0]["reason"] == "duplicate"

    # Re-posting the same shortlist skips both existing items
    resp2 = client.post("/api/items/induct-batch", json=payload)
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["created_count"] == 0
    assert data2["skipped_count"] == 3
