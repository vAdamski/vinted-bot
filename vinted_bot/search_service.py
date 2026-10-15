"""Zarządzanie wyszukiwaniami: config.yaml (tylko odczyt) + SQLite (z Discorda).

Bez zależności od discord.py – używane przez komendy Discorda i przez pętlę monitora.
"""
from __future__ import annotations

import re

from .config import MODES, SearchConfig
from .storage import Storage, StoredSearch

NAME_RE = re.compile(r"^[\w .,+&#'()\-]{1,50}$")

MODE_LABELS = {
    "deals": "okazje (mediana frazy)",
    "all": "wszystkie nowe oferty",
    "per_item": "okazje (mediana z tytułu oferty)",
}


class SearchError(Exception):
    pass


def parse_ids(text: str | None, label: str) -> list[int]:
    """'1242, 1243' -> [1242, 1243]"""
    if not text:
        return []
    ids = []
    for part in re.split(r"[,\s]+", text.strip()):
        if not part:
            continue
        if not part.isdigit():
            raise SearchError(f"{label}: {part!r} nie jest liczbą (podaj ID oddzielone przecinkami)")
        ids.append(int(part))
    return sorted(set(ids))


def describe(search: SearchConfig, default_threshold: float | None = None) -> str:
    """Krótki opis filtrów, np. „air max 90 · kategoria 1242 · ≤ 250 zł · próg 70%”."""
    parts = [f"„{search.query}”" if search.query else "(bez frazy)"]
    for label, ids in (("kategoria", search.catalog_ids), ("rozmiar", search.size_ids),
                       ("marka", search.brand_ids), ("stan", search.status_ids)):
        if ids:
            parts.append(f"{label} {','.join(map(str, ids))}")
    for name, ids in search.attributes.items():
        if ids:
            parts.append(f"{name} {','.join(map(str, ids))}")
    if search.price_min is not None:
        parts.append(f"≥ {search.price_min:g}")
    if search.price_max is not None:
        parts.append(f"≤ {search.price_max:g}")
    mode = search.effective_mode
    parts.append(f"tryb: {MODE_LABELS[mode]}")
    threshold = search.deal_threshold or default_threshold
    if threshold and mode != "all":
        parts.append(f"próg {threshold * 100:.0f}%" + ("" if search.deal_threshold else " (domyślny)"))
    return " · ".join(parts)


class SearchService:
    def __init__(self, storage: Storage, yaml_searches: list[SearchConfig], max_searches: int):
        self.storage = storage
        self.yaml_searches = yaml_searches
        self.max_searches = max_searches

    def discord_searches(self) -> list[StoredSearch]:
        return self.storage.list_searches()

    def active(self) -> list[SearchConfig]:
        """Wyszukiwania do sprawdzenia w tym cyklu: wszystkie z YAML + włączone z Discorda."""
        return self.yaml_searches + [s.search for s in self.discord_searches() if s.enabled]

    def _find(self, name: str) -> StoredSearch:
        if any(s.name == name for s in self.yaml_searches):
            raise SearchError(f"„{name}” pochodzi z config.yaml – zmień je w pliku")
        for s in self.discord_searches():
            if s.search.name == name:
                return s
        raise SearchError(f"Nie ma wyszukiwania „{name}”")

    def validate_new(self, search: SearchConfig) -> None:
        if not NAME_RE.fullmatch(search.name):
            raise SearchError("Nazwa: 1–50 znaków, litery, cyfry, spacje i . , + & # ' ( ) -")
        taken = {s.name.casefold() for s in self.yaml_searches}
        taken |= {s.search.name.casefold() for s in self.discord_searches()}
        if search.name.casefold() in taken:
            raise SearchError(f"Nazwa „{search.name}” jest już zajęta")
        if len(self.discord_searches()) >= self.max_searches:
            raise SearchError(f"Osiągnięto limit {self.max_searches} wyszukiwań z Discorda "
                              "(discord.max_searches w config.yaml). Usuń któreś albo podnieś limit.")
        if not (search.query or search.catalog_ids or search.brand_ids):
            raise SearchError("Podaj frazę, kategorię albo markę – inaczej wyszukiwanie obejmie cały Vinted")
        if search.mode is not None and search.mode not in MODES:
            raise SearchError(f"Nieznany tryb {search.mode!r}")
        if search.price_min is not None and search.price_max is not None and search.price_min > search.price_max:
            raise SearchError("cena_min jest większa niż cena_max")

    def add(self, search: SearchConfig, created_by: str | None = None) -> None:
        search.source = "discord"
        self.validate_new(search)
        self.storage.add_search(search, created_by)

    def remove(self, name: str) -> SearchConfig:
        found = self._find(name)
        self.storage.delete_search(name)
        return found.search

    def set_enabled(self, name: str, enabled: bool) -> SearchConfig:
        found = self._find(name)
        if found.enabled == enabled:
            raise SearchError(f"„{name}” jest już {'włączone' if enabled else 'wstrzymane'}")
        self.storage.set_search_enabled(name, enabled)
        if enabled:
            # Po wznowieniu nie zasypuj kanału ofertami z czasu pauzy.
            self.storage.reset_initialized(name)
        return found.search
