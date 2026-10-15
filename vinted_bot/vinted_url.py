"""Zamiana linku wyszukiwania z vinted.pl na parametry SearchConfig.

⚠️ VINTED-API: format URL-i strony (nie API). Przykład:
  https://www.vinted.pl/catalog?search_text=air%20max&catalog[]=1242&size_ids[]=784&price_to=250
  https://www.vinted.pl/catalog/1242-mens-sneakers?brand_ids[]=53
  https://www.vinted.pl/brand/53-nike
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlparse

HOST_RE = re.compile(r"(?:www\.)?vinted\.[a-z]{2,3}(?:\.[a-z]{2})?")
# Parametry z URL-a, które nie są filtrami – ignorujemy je.
IGNORED = {"search_id", "time", "page", "order", "currency", "disabled_personalization", "search_by_image_uuid"}
# *_ids mapowane na dedykowane pola SearchConfig; reszta trafia do `attributes`.
DEDICATED = {"size": "size_ids", "brand": "brand_ids", "status": "status_ids"}


class VintedUrlError(ValueError):
    pass


def _ints(values: list[str], key: str) -> list[int]:
    out = []
    for v in values:
        for part in v.split(","):
            part = part.strip()
            if not part:
                continue
            if not part.isdigit():
                raise VintedUrlError(f"Nieprawidłowe ID w parametrze {key}: {part!r}")
            out.append(int(part))
    return out


def _price(values: list[str] | None) -> float | None:
    if not values or not values[0].strip():
        return None
    try:
        return float(values[0].replace(",", "."))
    except ValueError as e:
        raise VintedUrlError(f"Nieprawidłowa cena: {values[0]!r}") from e


def parse_catalog_url(url: str) -> dict[str, Any]:
    """Zwraca kwargs dla SearchConfig (bez `name`). Rzuca VintedUrlError."""
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not HOST_RE.fullmatch(host):
        raise VintedUrlError("To nie jest link do Vinted (oczekiwano np. https://www.vinted.pl/catalog?...)")

    result: dict[str, Any] = {
        "query": "", "catalog_ids": [], "size_ids": [], "brand_ids": [], "status_ids": [], "attributes": {},
        "price_min": None, "price_max": None,
    }

    if m := re.match(r"^/catalog/(\d+)", parsed.path):
        result["catalog_ids"].append(int(m.group(1)))
    elif m := re.match(r"^/brand/(\d+)", parsed.path):
        result["brand_ids"].append(int(m.group(1)))
    elif not parsed.path.startswith("/catalog"):
        raise VintedUrlError("Link musi prowadzić do wyszukiwania (/catalog...) – skopiuj adres z listy wyników")

    for key, values in parse_qs(parsed.query).items():
        name = key.removesuffix("[]")
        if name == "search_text":
            result["query"] = values[0].strip()
        elif name == "price_from":
            result["price_min"] = _price(values)
        elif name == "price_to":
            result["price_max"] = _price(values)
        elif name in ("catalog", "catalog_ids"):
            result["catalog_ids"] += _ints(values, key)
        elif name.endswith("_ids"):
            base = name.removesuffix("_ids")
            ids = _ints(values, key)
            if base in DEDICATED:
                result[DEDICATED[base]] += ids
            else:
                result["attributes"].setdefault(base, []).extend(ids)
        # pozostałe (IGNORED i nieznane) pomijamy

    for k in ("catalog_ids", "size_ids", "brand_ids", "status_ids"):
        result[k] = sorted(set(result[k]))
    result["attributes"] = {k: sorted(set(v)) for k, v in result["attributes"].items()}

    has_filter = result["query"] or result["catalog_ids"] or result["brand_ids"]
    if not has_filter:
        raise VintedUrlError("Link nie zawiera frazy, kategorii ani marki – za szerokie wyszukiwanie")
    return result
