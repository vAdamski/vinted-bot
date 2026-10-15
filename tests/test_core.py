from vinted_bot.config import VintedConfig
from vinted_bot.pricing import discount, robust_median
from vinted_bot.scraper import VintedClient, _parse_money


def test_robust_median_drops_outliers():
    prices = [100, 105, 110, 95, 102, 98, 101, 1, 5000]
    median, kept, _, _ = robust_median(prices)
    assert 1 not in kept and 5000 not in kept
    assert median == 101


def test_robust_median_small_sample():
    assert robust_median([10, 20])[0] == 15
    assert robust_median([]) is None


def test_discount():
    assert round(discount(75, 100), 2) == 0.25


def test_parse_money_variants():
    assert _parse_money("45.0", "PLN") == (45.0, "PLN")
    assert _parse_money({"amount": "12.5", "currency_code": "EUR"}, "PLN") == (12.5, "EUR")
    assert _parse_money(None, "PLN") == (None, "PLN")


def test_parse_item():
    client = VintedClient(VintedConfig())
    item = client._parse_item({
        "id": 123, "title": "Buty", "price": {"amount": "99.00", "currency_code": "PLN"},
        "total_item_price": {"amount": "105.65", "currency_code": "PLN"},
        "photo": {"url": "https://img/x.jpg"}, "user": {"id": 7, "login": "ania"},
        "url": "https://www.vinted.pl/items/123-buty", "brand_title": "Nike", "size_title": "42",
    })
    assert item.price == 99.0 and item.total_price == 105.65 and item.seller_id == 7


def test_parse_item_new_format():
    client = VintedClient(VintedConfig())
    item = client._parse_item({
        "id": 5, "title": "Lego", "price": {"amount": "60.00", "currency_code": "PLN"},
        "photos": [{"url": "https://img/a.webp"}], "user": {"id": 9, "login": "x"},
        "url": "/items/5-lego", "item_box": {"first_line": "LEGO", "second_line": "116 cm · Bardzo dobry"},
    })
    assert item.url == "https://www.vinted.pl/items/5-lego"
    assert item.photo_url == "https://img/a.webp" and item.brand == "LEGO" and item.details == "116 cm · Bardzo dobry"


def test_search_params_use_attribute_ids():
    from vinted_bot.config import SearchConfig
    client = VintedClient(VintedConfig())
    p = client._search_params(SearchConfig(name="a", query="q", price_max=100, catalog_ids=[1242], size_ids=[784, 785],
                                           attributes={"color": [1]}), 1, 30, "newest_first", True)
    assert p["attribute_ids[catalog]"] == "1242" and p["attribute_ids[size]"] == "784,785"
    assert p["attribute_ids[color]"] == "1" and p["price_to"] == 100
    assert client.api_url == "https://api.vinted.pl"


def test_rating():
    assert VintedClient._rating({"feedback_reputation": 0.96}) == 4.8
    assert VintedClient._rating({"positive_feedback_count": 9, "negative_feedback_count": 1}) == 4.6
