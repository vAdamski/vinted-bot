"""Ceny nowych produktów ze stron wyników wyszukiwania popularnych sklepów z elektroniką.

⚠️ NIEZWERYFIKOWANE NA ŻYWYCH STRONACH: adresy wyszukiwarek poniżej i sposób wyciągania cen to
najlepsze przybliżenie. Sklepy zmieniają HTML i blokują boty, więc po wdrożeniu uruchom:

    python -m vinted_bot.retail_check "jbl flip 7" --save

Skrypt pokaże dla każdego sklepu: status HTTP, ile ofert znalazł i ile pasuje do zapytania.
Jeśli sklep zwraca 0 ofert, ustaw dla niego selektory CSS (item_sel / title_sel / price_sel) –
selektory znajdziesz w Chrome: DevTools (F12) → prawy klik na kartę produktu → Inspect.

Dwie metody wyciągania ofert ze strony:
  1. dane strukturalne JSON-LD (schema.org Product/ItemList) – działa bez selektorów, jeśli sklep je ma,
  2. selektory CSS (opcjonalne, wymagają: pip install beautifulsoup4) – gdy podasz item_sel/title_sel/price_sel.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from urllib.parse import quote_plus

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Shop:
    name: str
    search_url: str            # {q} = zapytanie (zakodowane w URL)
    item_sel: str | None = None   # selektor karty produktu na liście wyników
    title_sel: str | None = None  # selektor tytułu wewnątrz karty
    price_sel: str | None = None  # selektor ceny wewnątrz karty


# ⚠️ Adresy wyszukiwarek – zweryfikuj w przeglądarce (wpisz coś w wyszukiwarce sklepu i porównaj URL).
SHOPS: dict[str, Shop] = {s.name: s for s in (
    Shop("ceneo", "https://www.ceneo.pl/;szukaj-{q}"),
    Shop("xkom", "https://www.x-kom.pl/szukaj?q={q}"),
    Shop("morele", "https://www.morele.net/wyszukiwarka/?q={q}"),
    Shop("mediaexpert", "https://www.mediaexpert.pl/search?query[querystring]={q}"),
    Shop("euro", "https://www.euro.com.pl/search.bhtml?keyword={q}"),
)}
DEFAULT_SHOPS = ("ceneo", "xkom", "morele")


# ------------------------------------------------------------------ parsowanie
_PRICE_RE = re.compile(r"\d[\d\s\u00a0\u202f.,]*\d|\d")


def parse_price(value) -> float | None:
    """'1 299,00 zł' / '1299.00' / '1.299,00 PLN' / 1299 -> 1299.0; brak liczby -> None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    m = _PRICE_RE.search(str(value))
    if not m:
        return None
    s = re.sub(r"[\s\u00a0\u202f]", "", m.group(0))
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif s.count(".") > 1 or re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
        s = s.replace(".", "")
    try:
        price = float(s)
    except ValueError:
        return None
    return price if price > 0 else None


def _offer_price(offers) -> float | None:
    if isinstance(offers, list):
        prices = [p for o in offers if (p := _offer_price(o))]
        return min(prices) if prices else None
    if isinstance(offers, dict):
        for key in ("price", "lowPrice"):
            if offers.get(key) is not None:
                return parse_price(offers[key])
        if offers.get("priceSpecification") is not None:
            return _offer_price(offers["priceSpecification"])
    return None


def _walk(node, out: list[tuple[str, float]]) -> None:
    if isinstance(node, list):
        for n in node:
            _walk(n, out)
    elif isinstance(node, dict):
        t = node.get("@type")
        if "Product" in (t if isinstance(t, list) else [t]) and node.get("name"):
            price = _offer_price(node.get("offers"))
            if price:
                out.append((str(node["name"]), price))
        for key, value in node.items():
            if key != "offers":
                _walk(value, out)


_JSONLD_RE = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)


def extract_jsonld(html: str) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    for m in _JSONLD_RE.finditer(html):
        try:
            _walk(json.loads(m.group(1)), out)
        except ValueError:
            continue
    return list(dict.fromkeys(out))  # bez duplikatów, z zachowaniem kolejności


def extract_selectors(html: str, shop: Shop) -> list[tuple[str, float]]:
    if not (shop.item_sel and shop.title_sel and shop.price_sel):
        return []
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        log.warning("Sklep %s ma selektory CSS, ale brak biblioteki: pip install beautifulsoup4", shop.name)
        return []
    soup = BeautifulSoup(html, "html.parser")
    out: list[tuple[str, float]] = []
    for card in soup.select(shop.item_sel):
        title_el, price_el = card.select_one(shop.title_sel), card.select_one(shop.price_sel)
        if title_el is None or price_el is None:
            continue
        price = parse_price(price_el.get_text(" ", strip=True))
        if price:
            out.append((title_el.get_text(" ", strip=True), price))
    return out


def extract_items(html: str, shop: Shop) -> list[tuple[str, float]]:
    """(tytuł, cena) z strony wyników: selektory (jeśli ustawione), w razie braku wyników – JSON-LD."""
    return extract_selectors(html, shop) or extract_jsonld(html)


# --------------------------------------------------------------------- pobieranie
class ShopFetcher:
    """Osobna sesja curl_cffi (odcisk Chrome) do stron sklepów – niezależna od sesji Vinted."""

    def __init__(self, timeout: float = 20):
        self.timeout = timeout
        self._session = None

    def _get_session(self):
        if self._session is None:
            from curl_cffi import requests
            proxy = os.environ.get("RETAIL_PROXY")
            self._session = requests.Session(impersonate="chrome", timeout=self.timeout,
                                             proxies={"http": proxy, "https": proxy} if proxy else None)
            self._session.headers.update({"Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7"})
        return self._session

    def get(self, shop: Shop, query: str) -> tuple[int, str]:
        url = shop.search_url.format(q=quote_plus(query))
        r = self._get_session().get(url)
        return r.status_code, r.text

    def fetch_items(self, shop: Shop, query: str) -> list[tuple[str, float]]:
        status, html = self.get(shop, query)
        if status >= 400:
            raise RuntimeError(f"HTTP {status}")
        return extract_items(html, shop)
