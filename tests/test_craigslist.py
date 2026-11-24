"""Tests for Craigslist HTML/JSON-LD parsing and Universal Retail JSON-LD extraction."""

from argos.config import Settings
from argos.providers.craigslist import parse_craigslist_html
from argos.providers.universal_retail import extract_price_from_text, extract_product_metadata


SAMPLE_CRAIGSLIST_HTML = """
<!DOCTYPE html>
<html>
<head>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "ItemList",
  "itemListElement": [
    {
      "@type": "ListItem",
      "position": 1,
      "item": {
        "@type": "Product",
        "name": "Holley 750 4150 Double Pumper Carburetor",
        "image": ["https://images.craigslist.org/sample1.jpg"],
        "offers": {
          "@type": "Offer",
          "price": "325.00",
          "priceCurrency": "USD",
          "availableAtOrFrom": {
            "@type": "Place",
            "address": {"addressLocality": "Little Elm", "addressRegion": "TX"},
            "geo": {"latitude": 33.1768, "longitude": -96.9583}
          }
        }
      }
    },
    {
      "@type": "ListItem",
      "position": 2,
      "item": {
        "@type": "Product",
        "name": "MSD 8582 Ford 302 Distributor",
        "offers": {
          "@type": "Offer",
          "price": "220.00",
          "priceCurrency": "USD",
          "availableAtOrFrom": {
            "@type": "Place",
            "address": {"addressLocality": "Dallas", "addressRegion": "TX"},
            "geo": {"latitude": 32.7767, "longitude": -96.7970}
          }
        }
      }
    }
  ]
}
</script>
</head>
<body>
<ol class="cl-static-search-results">
  <li class="cl-static-search-result" title="Holley 750 4150 Double Pumper Carburetor">
    <a href="https://dallas.craigslist.org/ndf/pts/d/little-elm-holley-750-4150/7812345678.html">
      <div class="title">Holley 750 4150 Double Pumper Carburetor</div>
      <div class="details">
        <div class="price">$325</div>
        <div class="location">Little Elm</div>
      </div>
    </a>
  </li>
  <li class="cl-static-search-result" title="MSD 8582 Ford 302 Distributor">
    <a href="https://dallas.craigslist.org/dal/pts/d/dallas-msd-8582/7812349999.html">
      <div class="title">MSD 8582 Ford 302 Distributor</div>
      <div class="details">
        <div class="price">$220</div>
        <div class="location">Dallas</div>
      </div>
    </a>
  </li>
</ol>
</body>
</html>
"""


def test_parse_craigslist_html_extracts_geo_and_ids():
    settings = Settings(database_url="sqlite:///:memory:", enable_telegram=False)
    items = parse_craigslist_html(SAMPLE_CRAIGSLIST_HTML, settings)
    assert len(items) == 2

    first = items[0]
    assert first.external_id == "7812345678"
    assert first.price_usd == 325.0
    assert first.location_text == "Little Elm"
    assert first.distance_miles is not None
    assert 2.0 <= first.distance_miles <= 7.0
    assert first.image_url == "https://images.craigslist.org/sample1.jpg"

    second = items[1]
    assert second.external_id == "7812349999"
    assert second.distance_miles is not None
    assert 25.0 <= second.distance_miles <= 40.0


def test_extract_product_metadata_jsonld_and_og():
    html_ld = """
    <html><head>
    <script type="application/ld+json">
    {"@context": "https://schema.org", "@graph": [
      {"@type": "WebPage", "name": "Ignore"},
      {"@type": "Product", "name": "MSD 8582 Pro-Billet", "offers": {"@type": "Offer", "price": "456.95", "itemCondition": "https://schema.org/NewCondition"}}
    ]}
    </script>
    </head></html>
    """
    meta = extract_product_metadata(html_ld)
    assert meta is not None
    assert meta["price"] == 456.95
    assert meta["condition"] == "new"
    assert "MSD 8582" in meta["title"]

    html_og = """
    <html><head>
      <meta property="og:title" content="Quick Fuel Brawler 750" />
      <meta property="product:price:amount" content="419.99" />
    </head></html>
    """
    meta2 = extract_product_metadata(html_og)
    assert meta2 is not None
    assert meta2["price"] == 419.99
    assert meta2["title"] == "Quick Fuel Brawler 750"


def test_extract_price_from_text_exa_and_split_cents():
    holley_txt = "In Stock Advertised Price $2,599.95 $2,338.95 Save $261.00 Free Shipping"
    assert extract_price_from_text(holley_txt, baseline_usd=2510.95) == 2338.95

    jegs_txt = "Billet Distributor\n$ 456 95 $507.95 You Save$51.00\n4 left in stock"
    assert extract_price_from_text(jegs_txt, baseline_usd=507.95) == 456.95

    ship_only = "Free shipping over $99! Accessories starting at $19.99."
    assert extract_price_from_text(ship_only, baseline_usd=500.0) is None
