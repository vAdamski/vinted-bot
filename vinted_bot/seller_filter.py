"""Weryfikacja sprzedającego: ocena i liczba opinii (z cache w SQLite)."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import SellerConfig
from .scraper import Seller, VintedClient, VintedError
from .storage import Storage

log = logging.getLogger(__name__)


@dataclass
class SellerVerdict:
    ok: bool
    seller: Seller | None
    reason: str


class SellerFilter:
    def __init__(self, client: VintedClient, storage: Storage, cfg: SellerConfig):
        self.client = client
        self.storage = storage
        self.cfg = cfg

    def check(self, seller_id: int) -> SellerVerdict:
        if not seller_id:
            return SellerVerdict(False, None, "brak ID sprzedającego")

        seller = self.storage.get_seller(seller_id, self.cfg.cache_ttl_hours * 3600)
        if seller is None:
            try:
                seller = self.client.get_seller(seller_id)
            except VintedError as e:
                return SellerVerdict(False, None, f"nie udało się pobrać profilu: {e}")
            self.storage.save_seller(seller)

        if seller.reviews < self.cfg.min_reviews:
            return SellerVerdict(False, seller, f"za mało opinii ({seller.reviews} < {self.cfg.min_reviews})")
        if seller.rating < self.cfg.min_rating:
            return SellerVerdict(False, seller, f"za niska ocena ({seller.rating:.2f} < {self.cfg.min_rating})")
        return SellerVerdict(True, seller, "ok")
