"""Wycena rynkowa: mediana cen ofert dla frazy z odrzuceniem wartości skrajnych (IQR)."""
from __future__ import annotations

import dataclasses
import json
import logging
import statistics
import time
from dataclasses import dataclass

from .config import PricingConfig, SearchConfig
from .scraper import VintedClient

log = logging.getLogger(__name__)


@dataclass
class MarketStats:
    median: float
    sample_size: int   # ile ofert pobrano
    used_size: int     # ile zostało po odrzuceniu skrajnych
    low: float         # dolna granica akceptowanych cen
    high: float        # górna granica
    computed_at: float


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
    def __init__(self, client: VintedClient, cfg: PricingConfig):
        self.client = client
        self.cfg = cfg
        self._cache: dict[str, MarketStats | None] = {}
        self._cache_ts: dict[str, float] = {}

    def get_stats(self, search: SearchConfig) -> MarketStats | None:
        """Mediana dla frazy (z cache). None = za mała próbka, nie da się ocenić okazji."""
        ttl = self.cfg.cache_ttl_minutes * 60
        key = self._cache_key(search)
        if key in self._cache and time.time() - self._cache_ts[key] < ttl:
            return self._cache[key]

        # Próbka BEZ filtra price_max – inaczej mediana byłaby sztucznie zaniżona.
        items = self.client.search(search, limit=self.cfg.sample_size, order=self.cfg.order, with_price_filter=False)
        stats = self._compute([i.price for i in items])
        self._cache[key] = stats
        self._cache_ts[key] = time.time()

        if stats is None:
            log.warning("[%s] za mało ofert do wyceny (%d < %d) – frazę pomijam do kolejnego przeliczenia",
                        search.name, len(items), self.cfg.min_sample)
        else:
            log.info("[%s] mediana rynkowa %.2f (próbka %d, po odrzuceniu skrajnych %d, zakres %.2f–%.2f)",
                     search.name, stats.median, stats.sample_size, stats.used_size, stats.low, stats.high)
        return stats

    @staticmethod
    def _cache_key(search: SearchConfig) -> str:
        """Nazwa + filtry wpływające na próbkę – usunięcie i ponowne dodanie wyszukiwania
        o tej samej nazwie, ale innych filtrach, nie użyje starej mediany."""
        data = dataclasses.asdict(search)
        for k in ("price_max", "price_min", "deal_threshold", "channel_id", "source"):
            data.pop(k)
        return json.dumps(data, sort_keys=True)

    def _compute(self, prices: list[float]) -> MarketStats | None:
        result = robust_median(prices, self.cfg.iqr_k)
        if result is None:
            return None
        median, kept, low, high = result
        if len(kept) < self.cfg.min_sample:
            return None
        return MarketStats(median=median, sample_size=len(prices), used_size=len(kept),
                           low=low, high=high, computed_at=time.time())


def discount(price: float, median: float) -> float:
    """Ułamek, o ile oferta jest tańsza od mediany (0.3 = 30% taniej)."""
    return 1 - price / median if median > 0 else 0.0
