"""Wycena rynkowa: mediana cen ofert dla frazy z odrzuceniem wartości skrajnych (IQR)."""
from __future__ import annotations

import dataclasses
import json
import logging
import re
import statistics
import time
import unicodedata
from dataclasses import dataclass

from .config import PricingConfig, SearchConfig
from .scraper import Item, VintedClient

log = logging.getLogger(__name__)

RETAIL_TTL_HOURS = 24   # ceny sklepowe zmieniają się wolno – odpytuj źródło raz na dobę na model
RETAIL_MIN_SAMPLE = 3   # tyle cen ze sklepów wystarczy do wyceny


@dataclass
class MarketStats:
    median: float
    sample_size: int   # ile ofert pobrano
    used_size: int     # ile zostało po odrzuceniu skrajnych
    low: float         # dolna granica akceptowanych cen
    high: float        # górna granica
    computed_at: float
    basis: str = "phrase"  # "phrase" – mediana frazy, "title" – z tytułu oferty (per_item), "retail" – ceny nowych ze sklepów
    query: str = ""        # dla basis="title": zapytanie zbudowane z tytułu


def robust_median(prices: list[float], iqr_k: float = 1.5) -> tuple[float, list[float], float, float] | None:
    """Mediana po odrzuceniu wartości spoza [Q1 - k*IQR, Q3 + k*IQR].

    Zwraca (mediana, użyte ceny, dolna granica, górna granica) lub None dla pustej listy.
    """
    clean = sorted(p for p in prices if p and p > 0)
    if not clean:
        return None
    if len(clean) < 4:
        return statistics.median(clean), clean, clean[0], clean[-1]
    q1, _, q3 = statistics.quantiles(clean, n=4, method="inclusive")
    iqr = q3 - q1
    low, high = q1 - iqr_k * iqr, q3 + iqr_k * iqr
    kept = [p for p in clean if low <= p <= high] or clean
    return statistics.median(kept), kept, low, high


class MarketPricer:
    def __init__(self, client: VintedClient, cfg: PricingConfig, retail=None, retail_factor: float = 1.0,
                 retail_only: bool = False):
        """retail: opcjonalny RetailPriceProvider (retail_pricing.py) – zapasowe źródło cen nowych
        produktów, używane w trybie per_item, gdy Vinted nie ma dość porównywalnych ofert.
        retail_factor: mnożnik ceny sklepowej (np. 0.7, gdy porównujesz z używanymi).
        retail_only: wycena wyłącznie z cen sklepowych (bez porównań z ofertami z Vinted)."""
        self.client = client
        self.cfg = cfg
        self.retail = retail
        self.retail_factor = retail_factor
        self.retail_only = retail_only
        self._cache: dict[str, MarketStats | None] = {}
        self._cache_ts: dict[str, float] = {}

    def _cached(self, search: SearchConfig) -> tuple[str, bool, MarketStats | None]:
        key = self._cache_key(search)
        fresh = key in self._cache and time.time() - self._cache_ts[key] < self.cfg.cache_ttl_minutes * 60
        return key, fresh, self._cache.get(key) if fresh else None

    def _store(self, key: str, stats: MarketStats | None) -> None:
        self._cache[key] = stats
        self._cache_ts[key] = time.time()

    def get_stats(self, search: SearchConfig) -> MarketStats | None:
        """Mediana dla frazy (z cache). None = za mała próbka, nie da się ocenić okazji."""
        key, fresh, cached = self._cached(search)
        if fresh:
            return cached

        # Próbka BEZ filtra price_max – inaczej mediana byłaby sztucznie zaniżona.
        items = self.client.search(search, limit=self.cfg.sample_size, order=self.cfg.order, with_price_filter=False)
        stats = self._compute([i.price for i in items], self.cfg.min_sample)
        self._store(key, stats)

        if stats is None:
            log.warning("[%s] za mało ofert do wyceny (%d < %d) – frazę pomijam do kolejnego przeliczenia",
                        search.name, len(items), self.cfg.min_sample)
        else:
            log.info("[%s] mediana rynkowa %.2f (próbka %d, po odrzuceniu skrajnych %d, zakres %.2f–%.2f)",
                     search.name, stats.median, stats.sample_size, stats.used_size, stats.low, stats.high)
        return stats

    def is_item_cached(self, search: SearchConfig, item: Item) -> bool:
        """True = wycena tej oferty nie wymaga nowych zapytań do Vinted (limit per_item_max_per_cycle)."""
        query = title_query(item.title)
        if not query:
            return True  # nie ma czego wyceniać, zapytanie nie poleci
        if self.retail_only:
            key = "retail:" + query
            return key in self._cache and time.time() - self._cache_ts[key] < RETAIL_TTL_HOURS * 3600
        _, fresh, cached = self._cached(item_search(search, query))
        # Zapytanie do Vinted kosztuje tylko wtedy, gdy cache jest nieświeży.
        # Zapasowe źródło sklepowe to inne źródło – nie liczy się do limitu Vinted.
        return fresh

    def get_item_stats(self, search: SearchConfig, item: Item) -> MarketStats | None:
        """Tryb per_item: najpierw mediana podobnych ofert z Vinted; gdy ich brakuje – cena nowego
        produktu ze źródła sklepowego (jeśli skonfigurowane). None = nie da się wycenić (oferta pomijana)."""
        query = title_query(item.title)
        if not query:
            return None
        if self.retail_only:
            return self._retail_stats(query) if self.retail is not None else None
        stats = self._vinted_item_stats(search, item, query)
        if stats is None and self.retail is not None:
            stats = self._retail_stats(query)
        return stats

    def _vinted_item_stats(self, search: SearchConfig, item: Item, query: str) -> MarketStats | None:
        """Mediana z wyszukiwania po tytule oferty, w tej samej kategorii/marce/stanie.
        Wycena przybliżona – tytuły bywają ogólnikowe albo w innym języku."""
        derived = item_search(search, query)
        key, fresh, cached = self._cached(derived)
        if fresh:
            return cached
        # relevance: przy konkretnym modelu daje więcej trafnych wyników niż newest_first.
        items = self.client.search(derived, limit=self.cfg.per_item_sample, order="relevance",
                                   with_price_filter=False)
        # Wyszukiwarka Vinted jest luźna (etui, końcówki, inne modele) – zostaw tylko oferty,
        # których tytuł pasuje do zapytania. Bez tego mediana jest przypadkowa.
        matched = [i.price for i in items if i.id != item.id and title_matches(query, i.title)]
        stats = self._compute(matched, self.cfg.per_item_min_sample)
        if stats is not None:
            stats.basis, stats.query = "title", query
        self._store(key, stats)
        log.debug("[%s] mediana dla „%s”: %s", search.name, query,
                  f"{stats.median:.2f} (pasujących {len(matched)}/{len(items)})" if stats
                  else f"brak – pasujących ofert {len(matched)}/{len(items)} (min {self.cfg.per_item_min_sample})")
        return stats

    def _retail_stats(self, query: str) -> MarketStats | None:
        """Cena nowego produktu ze źródła sklepowego (cache RETAIL_TTL_HOURS, także negatywny)."""
        key = "retail:" + query
        if key in self._cache and time.time() - self._cache_ts[key] < RETAIL_TTL_HOURS * 3600:
            return self._cache[key]
        try:
            prices = self.retail.lookup(query)
        except Exception as e:  # noqa: BLE001 – awaria źródła nie może zatrzymać bota; nie cache'ujemy błędu
            log.warning("Źródło cen sklepowych nie odpowiedziało dla „%s”: %s", query, e)
            return None
        stats = self._compute([p * self.retail_factor for p in prices], RETAIL_MIN_SAMPLE)
        if stats is not None:
            stats.basis, stats.query = "retail", query
        self._store(key, stats)
        log.debug("Ceny sklepowe dla „%s”: %s", query,
                  f"mediana {stats.median:.2f} (n={stats.used_size})" if stats else f"brak ({len(prices)} cen)")
        return stats

    @staticmethod
    def _cache_key(search: SearchConfig) -> str:
        """Nazwa + filtry wpływające na próbkę – usunięcie i ponowne dodanie wyszukiwania
        o tej samej nazwie, ale innych filtrach, nie użyje starej mediany."""
        data = dataclasses.asdict(search)
        for k in ("price_max", "price_min", "deal_threshold", "mode", "channel_id", "source"):
            data.pop(k)
        return json.dumps(data, sort_keys=True)

    def _compute(self, prices: list[float], min_sample: int) -> MarketStats | None:
        result = robust_median(prices, self.cfg.iqr_k)
        if result is None:
            return None
        median, kept, low, high = result
        if len(kept) < min_sample:
            return None
        return MarketStats(median=median, sample_size=len(prices), used_size=len(kept),
                           low=low, high=high, computed_at=time.time())


def discount(price: float, median: float) -> float:
    """Ułamek, o ile oferta jest tańsza od mediany (0.3 = 30% taniej)."""
    return 1 - price / median if median > 0 else 0.0


# Słowa, które nic nie mówią o produkcie – wycinane z tytułu przed wyszukiwaniem (PL + częste z innych krajów).
_STOPWORDS = {
    "nowy", "nowa", "nowe", "nowka", "nówka", "new", "nou", "noua", "nouă", "neu", "neue", "nuevo", "nuovo", "uj", "új",
    "nový", "nova", "sprzedam", "okazja", "tanio", "oryginalny", "oryginalna", "oryginalne", "original", "originál",
    "metka", "metką", "metki", "tag", "tags", "tagged", "with", "bez", "brand", "sealed", "folia", "zafoliowany",
    "z", "i", "w", "na", "do", "dla", "and", "the", "for", "a", "de", "cu", "si", "și", "und", "mit", "es", "és",
    # Kolory i opisy stanu – nie określają modelu, a zawężają zapytanie (mniej porównywalnych ofert).
    "czarny", "czarna", "czarne", "biały", "biała", "białe", "szary", "szara", "szare", "srebrny", "srebrna",
    "srebrne", "złote", "niebieskie", "czerwone", "zielone", "różowe", "złoty", "złota", "niebieski", "niebieska", "czerwony", "czerwona", "zielony", "zielona", "różowy", "różowa",
    "black", "white", "grey", "gray", "silver", "gold", "blue", "red", "green", "pink",
    "używany", "używana", "używane", "uzywany", "idealny", "idealna", "idealne", "stan", "stanie", "bardzo", "dobry",
    "dobrym", "komplet", "kompletny", "gwarancja", "gwarancją", "paragon", "faktura", "sprawny", "sprawna",
    "sprawne", "polecam", "super", "ładny", "ładna", "jak", "nowego", "od", "po", "oraz", "lub",
}
_TITLE_WORDS = 5


def title_query(title: str) -> str:
    """'Słuchawki Sony WH-1000XM5 NOWE z metką!' -> 'słuchawki sony wh-1000xm5'"""
    words = re.findall(r"[\w][\w\-]*", title.lower())
    kept = [w for w in words if w not in _STOPWORDS and (len(w) > 1 or w.isdigit())]
    return " ".join(kept[:_TITLE_WORDS])


def _norm_tokens(text: str) -> list[str]:
    """Małe litery, bez polskich/obcych znaków diakrytycznych: 'Głośnik JBL Flip-7' -> ['glosnik','jbl','flip','7']"""
    text = unicodedata.normalize("NFKD", text.lower().replace("ł", "l"))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.findall(r"[a-z0-9]+", text)


# Akcesoria i części – psują medianę urządzenia (po normalizacji, bez polskich znaków).
_ACCESSORY_WORDS = {
    "etui", "case", "cover", "pokrowiec", "obudowa", "folia", "szklo", "szybka", "ochronne", "protector",
    "ladowarka", "charger", "kabel", "cable", "przewod", "adapter", "przejsciowka", "uchwyt", "stojak", "holder",
    "glowica", "koncowka", "koncowki", "nakladka", "nakladki", "wkladki", "pasek", "strap", "bransoleta",
    "pudelko", "box", "karton", "instrukcja", "naklejka", "naklejki", "skin", "czesci", "uszkodzony", "broken",
    "husa", "hulle", "tok", "pouzdro",
}


def title_matches(query: str, title: str, min_ratio: float = 0.7) -> bool:
    """Czy tytuł oferty dotyczy tego samego przedmiotu co zapytanie.

    Wymaga wszystkich słów z cyframi (oznaczenia modelu: s9, xm5, flip 7 / flip7) i ≥ 70% pozostałych.
    Odrzuca akcesoria (etui, ładowarka, głowica…), chyba że zapytanie samo ich dotyczy.
    """
    q = _norm_tokens(query)
    if not q:
        return False
    t = _norm_tokens(title)
    tokens, compact = set(t), "".join(t)
    if (tokens & _ACCESSORY_WORDS) - set(q):
        return False
    hits = 0
    for i, word in enumerate(q):
        ok = word in tokens or (len(word) >= 3 and word in compact)
        if not ok and word.isdigit() and i > 0:
            ok = q[i - 1] + word in compact  # "flip 7" pasuje do "Flip7"
        if not ok and any(ch.isdigit() for ch in word):
            return False
        hits += ok
    return hits >= max(1, round(min_ratio * len(q)))


def item_search(search: SearchConfig, query: str) -> SearchConfig:
    """Wyszukiwanie do wyceny jednej oferty: jej tytuł + kategoria/marka/stan z wyszukiwania.
    Rozmiar i ceny pomijamy – zawężałyby próbkę bez wpływu na wartość przedmiotu."""
    return dataclasses.replace(search, query=query, size_ids=[], price_min=None, price_max=None)
