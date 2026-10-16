import pytest

from vinted_bot.vinted_bot import VintedUrlError, parse_catalog_url


def test_full_catalog_url():
    r = parse_catalog_url(
        "https://www.vinted.pl/catalog?search_text=nike%20air%20max%2090&catalog[]=1242&size_ids[]=784"
        "&size_ids[]=785&brand_ids[]=53&status_ids[]=6&color_ids[]=1&price_from=50&price_to=250"
        "&order=newest_first&time=1792012444&search_id=123")
    assert r["query"] == "nike air max 90"
    assert r["catalog_ids"] == [1242] and r["size_ids"] == [784, 785]
    assert r["brand_ids"] == [53] and r["status_ids"] == [6]
    assert r["attributes"] == {"color": [1]}
    assert r["price_min"] == 50 and r["price_max"] == 250


def test_encoded_brackets_and_catalog_path():
    r = parse_catalog_url("https://www.vinted.pl/catalog/1242-mens-sneakers?size_ids%5B%5D=784")
    assert r["catalog_ids"] == [1242] and r["size_ids"] == [784] and r["query"] == ""


def test_brand_path_and_other_country():
    r = parse_catalog_url("https://www.vinted.de/brand/53-nike")
    assert r["brand_ids"] == [53]


@pytest.mark.parametrize("url", [
    "https://evil.example/catalog?search_text=x",
    "https://www.vinted.pl.evil.com/catalog?search_text=x",
    "https://www.vinted.pl/items/123-buty",
    "https://www.vinted.pl/catalog?price_to=100",
    "https://www.vinted.pl/catalog?search_text=x&size_ids[]=abc",
])
def test_rejected(url):
    with pytest.raises(VintedUrlError):
        parse_catalog_url(url)
