"""Zapasowe źródło cen NOWYCH produktów (używane w trybie per_item, gdy Vinted nie ma z czym porównać).

Provider ma jedną metodę: lookup(query) -> lista cen nowego produktu (w walucie bota, np. PLN).
Pusta lista = nic nie znaleziono (oferta zostanie pominięta, bez zgadywania).

Dołączone: JsonFileProvider (ręczna lista cen, przydatna do testów i do kilku ulubionych modeli).
Prawdziwe źródło (Allegro API, płatne API cen itp.) dopisujesz jako kolejną klasę z metodą lookup()
i podpinasz w build_provider_from_env().
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import time
import unicodedata
from pathlib import Path
from typing import Protocol

from .pricing import title_matches
from .shops import DEFAULT_SHOPS, SHOPS, ShopFetcher

log = logging.getLogger(__name__)


class RetailPriceProvider(Protocol):
    def lookup(self, query: str) -> list[float]: ...


def _tokens(text: str) -> set[str]:
    text = unicodedata.normalize("NFKD", text.lower().replace("ł", "l"))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return set(re.findall(r"[a-z0-9]+", text))


class JsonFileProvider:
    """Ceny z pliku JSON: {"jbl flip 7": [449, 459, 499], "sony wh-1000xm5": [1099, 1199]}.

    Zapytanie z tytułu oferty pasuje do wpisu, gdy zawiera wszystkie jego słowa
    (np. „glosnik jbl flip 7” pasuje do „jbl flip 7”). Przy kilku trafieniach wygrywa najdłuższy wpis.
    Plik jest czytany przy każdym lookup(), więc zmiany działają bez restartu bota.
    """

    def __init__(self, path: str):
        self.path = Path(path)

    def lookup(self, query: str) -> list[float]:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        q = _tokens(query)
        best: tuple[int, list[float]] | None = None
        for name, prices in data.items():
            words = _tokens(name)
            if words and words <= q and (best is None or len(words) > best[0]):
                best = (len(words), [float(p) for p in prices])
        return best[1] if best else []


class ShopPriceProvider:
    """Ceny nowego produktu ze stron wyników kilku sklepów.

    Dla każdego sklepu: pobiera stronę wyszukiwania, zostawia tylko oferty, których tytuł pasuje do
    zapytania (bez akcesoriów, ze zgodnym modelem), i bierze pierwsze `per_shop` cen. Medianę z całości
    liczy MarketPricer (z odrzuceniem wartości skrajnych). Awaria jednego sklepu nie przerywa reszty.
    """

    def __init__(self, shop_names: list[str], per_shop: int = 5, delay: tuple[float, float] = (1.0, 2.5)):
        unknown = [n for n in shop_names if n not in SHOPS]
        if unknown:
            raise ValueError(f"Nieznane sklepy: {', '.join(unknown)} (dostępne: {', '.join(SHOPS)})")
        self.shops = [SHOPS[n] for n in shop_names]
        self.per_shop = per_shop
        self.delay = delay
        self.fetcher = ShopFetcher()

    def lookup(self, query: str) -> list[float]:
        prices: list[float] = []
        for idx, shop in enumerate(self.shops):
            if idx:
                time.sleep(random.uniform(*self.delay))
            try:
                found = self.fetcher.fetch_items(shop, query)
            except Exception as e:  # noqa: BLE001 – blokada/zmiana HTML jednego sklepu nie zatrzymuje reszty
                log.warning("[%s] nie udało się pobrać cen dla „%s”: %s", shop.name, query, e)
                continue
            matched = [p for title, p in found if title_matches(query, title)][: self.per_shop]
            log.debug("[%s] „%s”: ofert %d, pasujących %d %s", shop.name, query, len(found), len(matched), matched)
            prices += matched
        return prices


class ChainProvider:
    """Pierwszy provider, który zwróci jakieś ceny (np. ręczny plik, potem sklepy)."""

    def __init__(self, providers: list[RetailPriceProvider]):
        self.providers = providers

    def lookup(self, query: str) -> list[float]:
        for provider in self.providers:
            prices = provider.lookup(query)
            if prices:
                return prices
        return []


def build_provider_from_env() -> tuple[RetailPriceProvider | None, float]:
    """Zwraca (provider albo None, mnożnik ceny). Konfiguracja przez zmienne środowiskowe (.env):

      RETAIL_SHOPS=ceneo,xkom,morele   – sklepy do porównania (domyślnie jak wyżej; "off" wyłącza)
                                         dostępne: ceneo, xkom, morele, mediaexpert, euro
      RETAIL_PRICES_FILE=data/retail_prices.json – ręczne ceny, sprawdzane PRZED sklepami (opcjonalne)
      RETAIL_FACTOR=1.0                – mnożnik ceny sklepowej (np. 0.95 jeśli sklepowa cena jest zawyżona)
      RETAIL_PROXY=...                 – proxy dla zapytań do sklepów (opcjonalne)
    """
    try:
        factor = float(os.environ.get("RETAIL_FACTOR", "1.0"))
    except ValueError:
        log.warning("RETAIL_FACTOR nie jest liczbą – używam 1.0")
        factor = 1.0
    providers: list[RetailPriceProvider] = []
    path = os.environ.get("RETAIL_PRICES_FILE")
    if path:
        providers.append(JsonFileProvider(path))
    shops_env = os.environ.get("RETAIL_SHOPS", ",".join(DEFAULT_SHOPS)).strip()
    if shops_env.lower() not in ("off", "none", ""):
        providers.append(ShopPriceProvider([s.strip() for s in shops_env.split(",") if s.strip()]))
    if not providers:
        return None, factor
    log.info("Ceny rynkowe: %s (mnożnik %.2f)", " → ".join(type(p).__name__ for p in providers), factor)
    return ChainProvider(providers), factor
