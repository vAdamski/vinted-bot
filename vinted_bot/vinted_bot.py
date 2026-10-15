"""Vinted deal bot – monitoruje Vinted i wysyła okazje na Discorda (wszystko w jednym pliku).

Uruchamianie:
    python vinted_bot.py                   # praca ciągła z Discordem
    python vinted_bot.py --dry-run         # bez Discorda, okazje w logu
    python vinted_bot.py --once            # jeden cykl
    python vinted_bot.py --test-notify     # wiadomość testowa na kanał
    python vinted_bot.py --retail-check "jbl flip 7" [--shops ceneo,xkom] [--save]   # diagnostyka sklepów

Wymaga: curl_cffi, discord.py, pyyaml, python-dotenv (opcjonalnie beautifulsoup4).
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import os
import random
import re
import signal
import sqlite3
import statistics
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol
from urllib.parse import parse_qs, quote_plus, urlparse

import discord
import yaml
from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException
from discord import app_commands
from dotenv import load_dotenv

__version__ = "0.1.0"

log = logging.getLogger("vinted_bot")
stop_event = threading.Event()


# ============================================================================
# config
# ============================================================================
class ConfigError(Exception):
    pass


# Tryby oceny ofert w wyszukiwaniu:
#   deals    – okazja, gdy cena ≤ próg × mediana dla frazy wyszukiwania
#   all      – powiadomienie o każdej nowej ofercie z filtrów (np. cała kategoria)
#   per_item – mediana liczona osobno dla każdej oferty, z wyszukiwania po jej tytule
MODES = ("deals", "all", "per_item")


@dataclass
class VintedConfig:
    domain: str = "www.vinted.pl"
    currency: str = "PLN"
    api_host: str | None = None  # None = automatycznie (api.vinted.pl dla www.vinted.pl)
    impersonate: str = "chrome"
    request_timeout: float = 20
    max_retries: int = 5
    backoff_base: float = 5
    backoff_max: float = 300
    proxy: str | None = None


@dataclass
class PollingConfig:
    min_interval: float = 30
    max_interval: float = 60
    delay_between_searches: tuple[float, float] = (2, 5)
    per_page: int = 30


@dataclass
class PricingConfig:
    deal_threshold: float = 0.75
    sample_size: int = 96
    min_sample: int = 15
    iqr_k: float = 1.5
    cache_ttl_minutes: float = 60
    order: str = "newest_first"
    per_item_sample: int = 96        # tryb per_item: ile ofert pobrać dla tytułu (większość odpada w filtrze trafności)
    per_item_min_sample: int = 8     # tryb per_item: mniej ofert => brak wyceny, brak powiadomienia
    per_item_max_per_cycle: int = 10 # tryb per_item: limit wycen na wyszukiwanie w jednym cyklu


@dataclass
class SellerConfig:
    min_rating: float = 4.5
    min_reviews: int = 5
    cache_ttl_hours: float = 12


@dataclass
class StorageConfig:
    db_path: str = "data/vinted.db"
    retention_days: int = 30


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str | None = "data/bot.log"


@dataclass
class DiscordConfig:
    max_searches: int = 25           # limit wyszukiwań dodanych z Discorda (z config.yaml)
    bot_token: str = ""              # poniższe z .env
    guild_id: int | None = None
    channel_id: int | None = None    # kanał domyślny dla powiadomień


@dataclass
class SearchConfig:
    name: str
    query: str = ""                # pusta = cała kategoria/marka z filtrów
    price_max: float | None = None
    price_min: float | None = None
    catalog_ids: list[int] = field(default_factory=list)
    size_ids: list[int] = field(default_factory=list)
    brand_ids: list[int] = field(default_factory=list)
    status_ids: list[int] = field(default_factory=list)
    # Inne filtry z URL-a Vinted, np. {"color": [1, 12], "material": [44]}
    attributes: dict[str, list[int]] = field(default_factory=dict)
    deal_threshold: float | None = None  # None = użyj pricing.deal_threshold
    mode: str | None = None        # deals | all | per_item; None = deals z frazą, all bez frazy
    channel_id: int | None = None  # None = kanał domyślny (DISCORD_CHANNEL_ID)
    source: str = "yaml"           # "yaml" | "discord" – ustawiane w kodzie

    @property
    def effective_mode(self) -> str:
        return self.mode or ("deals" if self.query else "all")


@dataclass
class Config:
    vinted: VintedConfig
    polling: PollingConfig
    pricing: PricingConfig
    seller: SellerConfig
    storage: StorageConfig
    logging: LoggingConfig
    discord: DiscordConfig
    searches: list[SearchConfig]
    notify_on_first_run: bool = False


def _build(cls, data: dict[str, Any] | None):
    """Tworzy dataclass z dicta, zgłaszając nieznane klucze (literówki w YAML)."""
    data = data or {}
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"Nieznane klucze w sekcji {cls.__name__}: {', '.join(sorted(unknown))}")
    return cls(**data)


def _env_int(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    if not value.isdigit():
        raise ConfigError(f"{name} musi być liczbą (ID z Discorda), jest: {value!r}")
    return int(value)


def load_config(path: str | Path = "config.yaml", env_file: str | Path = ".env") -> Config:
    load_dotenv(env_file)
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Brak pliku konfiguracyjnego: {path} (skopiuj config.example.yaml)")

    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    vinted = _build(VintedConfig, raw.get("vinted"))
    vinted.proxy = os.getenv("VINTED_PROXY") or vinted.proxy

    polling = _build(PollingConfig, raw.get("polling"))
    polling.delay_between_searches = tuple(polling.delay_between_searches)  # type: ignore[assignment]
    if polling.min_interval > polling.max_interval:
        raise ConfigError("polling.min_interval > polling.max_interval")

    pricing = _build(PricingConfig, raw.get("pricing"))
    if not 0 < pricing.deal_threshold <= 1:
        raise ConfigError("pricing.deal_threshold musi być w przedziale (0, 1]")

    discord_cfg = _build(DiscordConfig, raw.get("discord"))
    discord_cfg.bot_token = os.getenv("DISCORD_BOT_TOKEN", "")
    discord_cfg.guild_id = _env_int("DISCORD_GUILD_ID")
    discord_cfg.channel_id = _env_int("DISCORD_CHANNEL_ID")

    logging_cfg = _build(LoggingConfig, raw.get("logging"))
    logging_cfg.level = os.getenv("LOG_LEVEL", logging_cfg.level).upper()

    # Pusta lista jest OK – wyszukiwania można dodać komendami na Discordzie.
    searches = [_build(SearchConfig, s) for s in raw.get("searches") or []]
    for s in searches:
        s.source = "yaml"
        if s.mode is not None and s.mode not in MODES:
            raise ConfigError(f"[{s.name}] mode musi być jednym z: {', '.join(MODES)}")
    names = [s.name for s in searches]
    if len(names) != len(set(names)):
        raise ConfigError("Nazwy w 'searches' muszą być unikalne")

    return Config(
        vinted=vinted,
        polling=polling,
        pricing=pricing,
        seller=_build(SellerConfig, raw.get("seller")),
        storage=_build(StorageConfig, raw.get("storage")),
        logging=logging_cfg,
        discord=discord_cfg,
        searches=searches,
        notify_on_first_run=bool(raw.get("notify_on_first_run", False)),
    )


# ============================================================================
# scraper – klient wewnętrznego (nieoficjalnego!) API Vinted
# ============================================================================
# ⚠️ VINTED-API: ścieżki endpointów. Stary /api/v2/catalog/items zwraca 404 od 2026 r. –
# wyszukiwarka przeszła na „API gateway” pod https://api.<domena>/svc-catalogue/items.
# Profil użytkownika nadal jest pod https://www.<domena>/api/v2/users/{id}.
CATALOG_PATH = "/svc-catalogue/items"
USER_PATH = "/api/v2/users/{user_id}"
# ⚠️ VINTED-API: anonimowy token, który Vinted ustawia w ciasteczku przy wejściu
# na stronę główną. Bez niego /api/v2/* zwraca 401.
SESSION_COOKIE = "access_token_web"

MAX_PER_PAGE = 96  # ⚠️ VINTED-API: większe wartości są obcinane / odrzucane


class VintedError(Exception):
    pass


@dataclass
class Item:
    id: int
    title: str
    price: float
    currency: str
    total_price: float | None  # cena z opłatą za Ochronę Kupujących
    url: str
    photo_url: str | None
    seller_id: int
    seller_login: str
    brand: str | None
    details: str | None  # np. "42 · Bardzo dobry" (rozmiar · stan)


@dataclass
class Seller:
    id: int
    login: str
    rating: float  # 0–5
    reviews: int


def _parse_money(value: Any, default_currency: str) -> tuple[float | None, str]:
    """⚠️ VINTED-API: cena bywała stringiem "45.0" albo obiektem
    {"amount": "45.0", "currency_code": "PLN"} – obsługujemy oba warianty."""
    if value is None:
        return None, default_currency
    if isinstance(value, dict):
        amount = value.get("amount")
        currency = value.get("currency_code") or default_currency
    else:
        amount, currency = value, default_currency
    try:
        return float(str(amount).replace(",", ".")), currency
    except (TypeError, ValueError):
        return None, currency


class VintedClient:
    def __init__(self, cfg: VintedConfig):
        self.cfg = cfg
        self.base_url = f"https://{cfg.domain}"
        self.api_url = f"https://{cfg.api_host or self._default_api_host(cfg.domain)}"
        self._session: requests.Session | None = None

    @staticmethod
    def _default_api_host(domain: str) -> str:
        """⚠️ VINTED-API: logika z frontendu Vinted – www.vinted.pl -> api.vinted.pl,
        a dla USA (www.vinted.com) -> api.www.vinted.com."""
        if domain == "www.vinted.com":
            return "api.www.vinted.com"
        return "api." + domain.removeprefix("www.")

    # ------------------------------------------------------------------ sesja
    def _new_session(self) -> requests.Session:
        """Otwiera stronę główną jak przeglądarka, żeby dostać ciasteczka sesyjne."""
        proxies = {"http": self.cfg.proxy, "https": self.cfg.proxy} if self.cfg.proxy else None
        s = requests.Session(impersonate=self.cfg.impersonate, proxies=proxies, timeout=self.cfg.request_timeout)
        s.headers.update({"Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7"})
        r = s.get(self.base_url + "/")
        if r.status_code >= 400:
            raise VintedError(f"Strona główna zwróciła HTTP {r.status_code} (blokada IP / Cloudflare?)")
        if not s.cookies.get(SESSION_COOKIE):
            # ⚠️ VINTED-API: jeśli Vinted zmieni nazwę ciasteczka, zapytania API mogą dostawać 401.
            log.warning("Brak ciasteczka %s po wejściu na stronę główną – API może odrzucać zapytania", SESSION_COOKIE)
        log.info("Nowa sesja Vinted (%s), ciasteczka: %s", self.cfg.domain, ", ".join(s.cookies.keys()))
        return s

    @property
    def session(self) -> requests.Session:
        if self._session is None:
            self._session = self._new_session()
        return self._session

    def reset_session(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            except Exception:  # noqa: BLE001
                pass
        self._session = None

    # ----------------------------------------------------------- HTTP + retry
    def _backoff(self, attempt: int, retry_after: str | None = None) -> None:
        delay = None
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                pass
        if delay is None:
            delay = self.cfg.backoff_base * (2 ** attempt)
        delay = min(delay, self.cfg.backoff_max) + random.uniform(0, 3)
        log.warning("Czekam %.1f s przed ponowieniem (próba %d/%d)", delay, attempt + 1, self.cfg.max_retries)
        time.sleep(delay)

    def _headers(self, gateway: bool) -> dict[str, str]:
        headers = {"Accept": "application/json, text/plain, */*", "Referer": self.base_url + "/catalog"}
        if gateway:
            # ⚠️ VINTED-API: nagłówki dodawane przez frontend do zapytań do API gateway.
            headers.update({
                "Origin": self.base_url,
                "Platform": "web",
                "X-Anon-Id": self.session.cookies.get("anon_id") or "",
                "Locale": "pl-PL",
            })
        return headers

    def _get_json(self, path: str, params: dict[str, Any] | None = None, gateway: bool = False) -> dict[str, Any]:
        url = (self.api_url if gateway else self.base_url) + path
        last_error = "nieznany błąd"
        for attempt in range(self.cfg.max_retries):
            try:
                r = self.session.get(url, params=params, headers=self._headers(gateway))
            except (RequestException, VintedError) as e:
                last_error = f"błąd sieci: {e}"
                log.warning("%s %s – %s", path, params or "", last_error)
                self.reset_session()
                self._backoff(attempt)
                continue

            status = r.status_code
            if status == 200:
                try:
                    return r.json()
                except ValueError:
                    # ⚠️ VINTED-API: HTML zamiast JSON zwykle oznacza stronę z captchą/Cloudflare.
                    last_error = "odpowiedź nie jest JSON-em (captcha?)"
                    self.reset_session()
                    self._backoff(attempt)
                    continue

            last_error = f"HTTP {status}"
            if status in (401, 403):
                # 401 = wygasła sesja/token; 403 = Cloudflare/datadome lub ban IP.
                log.warning("%s dla %s – odświeżam sesję", last_error, path)
                self.reset_session()
                self._backoff(attempt)
            elif status == 429:
                log.warning("HTTP 429 (rate limit) dla %s", path)
                self._backoff(attempt + 1, r.headers.get("Retry-After"))
            elif status >= 500:
                log.warning("%s dla %s", last_error, path)
                self._backoff(attempt)
            elif status == 404:
                raise VintedError(f"404 dla {path}")
            else:
                raise VintedError(f"{last_error} dla {path}: {r.text[:200]}")

        raise VintedError(f"Nie udało się pobrać {path} po {self.cfg.max_retries} próbach ({last_error})")

    # --------------------------------------------------------------- katalog
    def _search_params(self, search: SearchConfig, page: int, per_page: int, order: str,
                       with_price_filter: bool) -> dict[str, Any]:
        # ⚠️ VINTED-API: nazwy parametrów. Filtry kategorii/rozmiaru/marki/stanu idą jako
        # zagnieżdżony obiekt attribute_ids[<typ>]=id1,id2 (stare catalog_ids/size_ids są ignorowane).
        params: dict[str, Any] = {
            "search_text": search.query,
            "page": page,
            "per_page": per_page,
            "order": order,
            "currency": self.cfg.currency,
        }
        attributes = {
            "catalog": search.catalog_ids,
            "size": search.size_ids,
            "brand": search.brand_ids,
            "status": search.status_ids,
            **search.attributes,
        }
        for name, ids in attributes.items():
            if ids:
                params[f"attribute_ids[{name}]"] = ",".join(map(str, ids))
        if with_price_filter:
            if search.price_max is not None:
                params["price_to"] = search.price_max
            if search.price_min is not None:
                params["price_from"] = search.price_min
        return params

    def search(self, search: SearchConfig, limit: int = 30, order: str = "newest_first",
               with_price_filter: bool = True) -> list[Item]:
        """Zwraca do `limit` ofert. Dla limit > 96 pobiera kolejne strony."""
        items: list[Item] = []
        seen: set[int] = set()
        page = 1
        while len(items) < limit:
            per_page = min(MAX_PER_PAGE, limit - len(items)) if page == 1 else MAX_PER_PAGE
            per_page = max(per_page, 1)
            data = self._get_json(CATALOG_PATH, self._search_params(search, page, per_page, order, with_price_filter),
                                  gateway=True)
            raw_items = data.get("items")
            if raw_items is None:
                # ⚠️ VINTED-API: zmiana struktury odpowiedzi.
                raise VintedError(f"Brak klucza 'items' w odpowiedzi katalogu (klucze: {list(data)[:10]})")
            if not raw_items:
                break
            for raw in raw_items:
                item = self._parse_item(raw)
                if item and item.id not in seen:
                    seen.add(item.id)
                    items.append(item)
            # ⚠️ VINTED-API: pagination.total_pages
            total_pages = (data.get("pagination") or {}).get("total_pages") or page
            if page >= total_pages or len(raw_items) < per_page:
                break
            page += 1
            time.sleep(random.uniform(1, 2.5))
        return items[:limit]

    def _parse_item(self, raw: dict[str, Any]) -> Item | None:
        # ⚠️ VINTED-API: nazwy pól oferty.
        try:
            price, currency = _parse_money(raw.get("price"), raw.get("currency") or self.cfg.currency)
            if price is None:
                return None
            total, _ = _parse_money(raw.get("total_item_price"), currency)
            # Kiedyś pojedyncze "photo", teraz lista "photos".
            photo = raw.get("photo") or next(iter(raw.get("photos") or []), None) or {}
            user = raw.get("user") or {}
            box = raw.get("item_box") or {}
            item_id = int(raw["id"])
            url = raw.get("url") or f"/items/{item_id}"
            if url.startswith("/"):
                url = self.base_url + url
            details = " · ".join(x for x in (raw.get("size_title"), raw.get("status")) if x) or box.get("second_line")
            return Item(
                id=item_id,
                title=raw.get("title") or "",
                price=price,
                currency=currency,
                total_price=total,
                url=url,
                photo_url=photo.get("url") or photo.get("full_size_url"),
                seller_id=int(user.get("id") or raw.get("user_id") or 0),
                seller_login=user.get("login") or "",
                brand=raw.get("brand_title") or box.get("first_line") or None,
                details=details or None,
            )
        except (KeyError, TypeError, ValueError) as e:
            log.debug("Pominięto ofertę o nieoczekiwanym formacie: %s (%s)", raw.get("id"), e)
            return None

    # ----------------------------------------------------------- sprzedający
    def get_seller(self, user_id: int) -> Seller:
        data = self._get_json(USER_PATH.format(user_id=user_id))
        user = data.get("user")
        if not user:
            raise VintedError(f"Brak klucza 'user' w odpowiedzi profilu {user_id}")
        return Seller(
            id=user_id,
            login=user.get("login") or "",
            rating=self._rating(user),
            reviews=int(user.get("feedback_count") or 0),
        )

    @staticmethod
    def _rating(user: dict[str, Any]) -> float:
        """⚠️ VINTED-API: `feedback_reputation` to ułamek 0–1 (gwiazdki = ×5).
        Jeśli pole zniknie, przybliżamy ocenę z liczby opinii pozytywnych/neutralnych/negatywnych."""
        rep = user.get("feedback_reputation")
        if rep is not None:
            rep = float(rep)
            return round(rep * 5 if rep <= 1 else rep, 2)
        pos = int(user.get("positive_feedback_count") or 0)
        neu = int(user.get("neutral_feedback_count") or 0)
        neg = int(user.get("negative_feedback_count") or 0)
        total = pos + neu + neg
        return round((pos * 5 + neu * 3 + neg * 1) / total, 2) if total else 0.0


# ============================================================================
# storage – SQLite
# ============================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_items (
    item_id     INTEGER PRIMARY KEY,
    search_name TEXT NOT NULL,
    price       REAL,
    seen_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_seen_at ON seen_items(seen_at);

CREATE TABLE IF NOT EXISTS search_state (
    search_name    TEXT PRIMARY KEY,
    initialized_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS searches (
    name       TEXT PRIMARY KEY,
    data       TEXT NOT NULL,          -- JSON z polami SearchConfig (bez name/channel_id/source)
    channel_id INTEGER,
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_by TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sellers (
    user_id    INTEGER PRIMARY KEY,
    login      TEXT,
    rating     REAL NOT NULL,
    reviews    INTEGER NOT NULL,
    fetched_at REAL NOT NULL
);
"""


@dataclass
class StoredSearch:
    search: SearchConfig
    enabled: bool
    created_by: str | None
    created_at: float


_NOT_IN_JSON = {"name", "channel_id", "source"}


class Storage:
    """Jedna instancja na wątek (monitor i bot Discord mają osobne połączenia)."""

    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: obiekt bywa tworzony w innym wątku niż ten, który go używa.
        self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---------------------------------------------------------- oferty
    def filter_unseen(self, item_ids: Iterable[int]) -> set[int]:
        ids = list(item_ids)
        if not ids:
            return set()
        placeholders = ",".join("?" * len(ids))
        rows = self.conn.execute(f"SELECT item_id FROM seen_items WHERE item_id IN ({placeholders})", ids)
        return set(ids) - {r[0] for r in rows}

    def mark_seen(self, search_name: str, items: Iterable[tuple[int, float]]) -> None:
        now = time.time()
        self.conn.executemany(
            "INSERT OR IGNORE INTO seen_items(item_id, search_name, price, seen_at) VALUES (?, ?, ?, ?)",
            [(item_id, search_name, price, now) for item_id, price in items],
        )
        self.conn.commit()

    # ------------------------------------------------------ stan fraz
    def is_initialized(self, search_name: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM search_state WHERE search_name = ?", (search_name,)).fetchone()
        return row is not None

    def set_initialized(self, search_name: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO search_state(search_name, initialized_at) VALUES (?, ?)",
            (search_name, time.time()),
        )
        self.conn.commit()

    def reset_initialized(self, search_name: str) -> None:
        """Następny cykl frazy znów tylko zapisze obecne oferty, bez powiadomień."""
        self.conn.execute("DELETE FROM search_state WHERE search_name = ?", (search_name,))
        self.conn.commit()

    # ---------------------------------------------- wyszukiwania z Discorda
    def add_search(self, search: SearchConfig, created_by: str | None = None) -> None:
        data = {k: v for k, v in dataclasses.asdict(search).items() if k not in _NOT_IN_JSON}
        self.conn.execute(
            "INSERT INTO searches(name, data, channel_id, enabled, created_by, created_at) VALUES (?, ?, ?, 1, ?, ?)",
            (search.name, json.dumps(data, ensure_ascii=False), search.channel_id, created_by, time.time()),
        )
        self.reset_initialized(search.name)  # commit w środku

    def list_searches(self) -> list[StoredSearch]:
        rows = self.conn.execute(
            "SELECT name, data, channel_id, enabled, created_by, created_at FROM searches ORDER BY created_at"
        ).fetchall()
        known = {f.name for f in dataclasses.fields(SearchConfig)} - _NOT_IN_JSON
        result = []
        for name, data, channel_id, enabled, created_by, created_at in rows:
            kwargs = {k: v for k, v in json.loads(data).items() if k in known}
            search = SearchConfig(name=name, channel_id=channel_id, source="discord", **kwargs)
            result.append(StoredSearch(search, bool(enabled), created_by, created_at))
        return result

    def delete_search(self, name: str) -> bool:
        cur = self.conn.execute("DELETE FROM searches WHERE name = ?", (name,))
        self.reset_initialized(name)
        return cur.rowcount > 0

    def set_search_enabled(self, name: str, enabled: bool) -> bool:
        cur = self.conn.execute("UPDATE searches SET enabled = ? WHERE name = ?", (int(enabled), name))
        self.conn.commit()
        return cur.rowcount > 0

    # ---------------------------------------------------- sprzedający
    def get_seller(self, user_id: int, max_age_s: float) -> Seller | None:
        row = self.conn.execute(
            "SELECT login, rating, reviews FROM sellers WHERE user_id = ? AND fetched_at >= ?",
            (user_id, time.time() - max_age_s),
        ).fetchone()
        if row is None:
            return None
        return Seller(id=user_id, login=row[0] or "", rating=row[1], reviews=row[2])

    def save_seller(self, seller: Seller) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO sellers(user_id, login, rating, reviews, fetched_at) VALUES (?, ?, ?, ?, ?)",
            (seller.id, seller.login, seller.rating, seller.reviews, time.time()),
        )
        self.conn.commit()

    # --------------------------------------------------------- sprzątanie
    def cleanup(self, retention_days: int) -> int:
        cutoff = time.time() - retention_days * 86400
        cur = self.conn.execute("DELETE FROM seen_items WHERE seen_at < ?", (cutoff,))
        self.conn.execute("DELETE FROM sellers WHERE fetched_at < ?", (cutoff,))
        self.conn.commit()
        return cur.rowcount


# ============================================================================
# pricing – wycena rynkowa
# ============================================================================
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
    query: str = ""        # dla basis="title"/"retail": zapytanie zbudowane z tytułu


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
        """retail: opcjonalny RetailPriceProvider – zapasowe źródło cen nowych produktów.
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
        """True = wycena tej oferty nie wymaga nowych zapytań (limit per_item_max_per_cycle)."""
        query = title_query(item.title)
        if not query:
            return True  # nie ma czego wyceniać, zapytanie nie poleci
        if self.retail_only:
            key = "retail:" + query
            return key in self._cache and time.time() - self._cache_ts[key] < RETAIL_TTL_HOURS * 3600
        _, fresh, cached = self._cached(item_search(search, query))
        return fresh

    def get_item_stats(self, search: SearchConfig, item: Item) -> MarketStats | None:
        """Najpierw mediana podobnych ofert z Vinted; gdy ich brakuje – cena nowego produktu
        ze źródła sklepowego (jeśli skonfigurowane). None = nie da się wycenić (oferta pomijana)."""
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


# ============================================================================
# shops – ceny nowych produktów ze stron sklepów
# ============================================================================
# ⚠️ NIEZWERYFIKOWANE NA ŻYWYCH STRONACH: adresy wyszukiwarek i sposób wyciągania cen to
# najlepsze przybliżenie. Po wdrożeniu uruchom:
#     python vinted_bot.py --retail-check "jbl flip 7" --save
# Jeśli sklep zwraca 0 ofert, ustaw dla niego selektory CSS (item_sel / title_sel / price_sel).
@dataclass(frozen=True)
class Shop:
    name: str
    search_url: str            # {q} = zapytanie (zakodowane w URL)
    item_sel: str | None = None   # selektor karty produktu na liście wyników
    title_sel: str | None = None  # selektor tytułu wewnątrz karty
    price_sel: str | None = None  # selektor ceny wewnątrz karty


SHOPS: dict[str, Shop] = {s.name: s for s in (
    Shop("ceneo", "https://www.ceneo.pl/;szukaj-{q}"),
    Shop("xkom", "https://www.x-kom.pl/szukaj?q={q}"),
    Shop("morele", "https://www.morele.net/wyszukiwarka/?q={q}"),
    Shop("mediaexpert", "https://www.mediaexpert.pl/search?query[querystring]={q}"),
    Shop("euro", "https://www.euro.com.pl/search.bhtml?keyword={q}"),
)}
DEFAULT_SHOPS = ("ceneo", "xkom", "morele")

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
    """(tytuł, cena) ze strony wyników: selektory (jeśli ustawione), w razie braku wyników – JSON-LD."""
    return extract_selectors(html, shop) or extract_jsonld(html)


class ShopFetcher:
    """Osobna sesja curl_cffi (odcisk Chrome) do stron sklepów – niezależna od sesji Vinted."""

    def __init__(self, timeout: float = 20):
        self.timeout = timeout
        self._session = None

    def _get_session(self):
        if self._session is None:
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


# ============================================================================
# retail_pricing – źródła cen nowych produktów
# ============================================================================
class RetailPriceProvider(Protocol):
    def lookup(self, query: str) -> list[float]: ...


def _tokens(text: str) -> set[str]:
    text = unicodedata.normalize("NFKD", text.lower().replace("ł", "l"))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return set(re.findall(r"[a-z0-9]+", text))


class JsonFileProvider:
    """Ceny z pliku JSON: {"jbl flip 7": [449, 459, 499], "sony wh-1000xm5": [1099, 1199]}.

    Zapytanie z tytułu oferty pasuje do wpisu, gdy zawiera wszystkie jego słowa.
    Przy kilku trafieniach wygrywa najdłuższy wpis. Plik jest czytany przy każdym lookup().
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
    """Ceny nowego produktu ze stron wyników kilku sklepów (awaria jednego nie przerywa reszty)."""

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

      RETAIL_SHOPS=ceneo,xkom,morele   – sklepy do porównania ("off" wyłącza)
                                         dostępne: ceneo, xkom, morele, mediaexpert, euro
      RETAIL_PRICES_FILE=data/retail_prices.json – ręczne ceny, sprawdzane PRZED sklepami (opcjonalne)
      RETAIL_FACTOR=1.0                – mnożnik ceny sklepowej
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


# ============================================================================
# retail_check – diagnostyka cen sklepowych (--retail-check)
# ============================================================================
def retail_check(query_arg: str, shops_arg: str, save: bool) -> int:
    query = title_query(query_arg) or query_arg
    print(f"Zapytanie: „{query}”\n")
    fetcher, all_prices = ShopFetcher(), []
    for name in (n.strip() for n in shops_arg.split(",") if n.strip()):
        shop = SHOPS.get(name)
        if shop is None:
            print(f"[{name}] nieznany sklep (dostępne: {', '.join(SHOPS)})")
            continue
        try:
            status, html = fetcher.get(shop, query)
        except Exception as e:  # noqa: BLE001
            print(f"[{name}] błąd połączenia: {e}")
            continue
        if save:
            out = Path("data") / f"retail_debug_{name}.html"
            out.parent.mkdir(exist_ok=True)
            out.write_text(html, encoding="utf-8")
        items = extract_items(html, shop) if status < 400 else []
        matched = [(t, p) for t, p in items if title_matches(query, t)]
        print(f"[{name}] HTTP {status}, {len(html)//1024} KB, ofert znalezionych: {len(items)}, pasujących: {len(matched)}")
        for t, p in matched[:5]:
            print(f"      {p:>9.2f}  {t[:70]}")
        if status < 400 and not items:
            print("      → 0 ofert: strona bez JSON-LD albo zablokowana. Ustaw selektory CSS w SHOPS "
                  "(uruchom z --save i obejrzyj zapisany HTML).")
        all_prices += [p for _, p in matched[:5]]

    result = robust_median(all_prices)
    print()
    if result:
        print(f"Mediana z {len(all_prices)} cen: {result[0]:.2f} (po odrzuceniu skrajnych: {len(result[1])})")
    else:
        print("Brak cen do wyceny.")
    return 0


# ============================================================================
# seller_filter
# ============================================================================
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


# ============================================================================
# search_service – wyszukiwania: config.yaml (tylko odczyt) + SQLite (z Discorda)
# ============================================================================
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


# ============================================================================
# vinted_url – zamiana linku vinted.pl na parametry SearchConfig
# ============================================================================
HOST_RE = re.compile(r"(?:www\.)?vinted\.[a-z]{2,3}(?:\.[a-z]{2})?")
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
        # pozostałe (ignorowane i nieznane) pomijamy

    for k in ("catalog_ids", "size_ids", "brand_ids", "status_ids"):
        result[k] = sorted(set(result[k]))
    result["attributes"] = {k: sorted(set(v)) for k, v in result["attributes"].items()}

    has_filter = result["query"] or result["catalog_ids"] or result["brand_ids"]
    if not has_filter:
        raise VintedUrlError("Link nie zawiera frazy, kategorii ani marki – za szerokie wyszukiwanie")
    return result


# ============================================================================
# notifier – powiadomienia na Discorda (lub do logu w --dry-run)
# ============================================================================
DISCORD_COLOR = 0x09B1BA      # turkus Vinted
DISCORD_COLOR_NEW = 0x5865F2  # tryb "all" – zwykła nowa oferta, nie okazja


class NotifierError(Exception):
    pass


def _md(text: str) -> str:
    """Escapuje znaki markdownu Discorda w tekstach z Vinted (login, szczegóły)."""
    return re.sub(r"([\\*_~`|>\[\]()])", r"\\\1", text)


def _median_label(stats: MarketStats) -> str:
    if stats.basis == "retail":
        return f"🏬 Cena nowego (sklepy, n={stats.used_size})"
    if stats.basis == "title":
        return f"📊 Mediana podobnych (≈, n={stats.used_size})"
    return "📊 Mediana"


def build_discord_embed(item: Item, stats: MarketStats | None, seller: Seller, search_name: str) -> dict:
    """stats=None – tryb "all": nowa oferta bez wyceny."""
    price = f"**{item.price:.2f} {item.currency}**"
    if item.total_price:
        price += f" (z ochroną: {item.total_price:.2f})"
    embed_fields = [{"name": "💰 Cena", "value": price, "inline": True}]
    if stats is not None:
        pct = discount(item.price, stats.median) * 100
        embed_fields += [
            {"name": _median_label(stats), "value": f"{stats.median:.2f} {item.currency}", "inline": True},
            {"name": "📉 Taniej", "value": f"**{pct:.0f}%**", "inline": True},
        ]
    embed_fields.append({"name": "⭐ Sprzedający",
                         "value": f"{_md(seller.login or str(seller.id))} — {seller.rating:.2f}/5 ({seller.reviews} opinii)",
                         "inline": False})
    details = " · ".join(x for x in (item.brand, item.details) if x)
    if details:
        embed_fields.append({"name": "🏷 Szczegóły", "value": _md(details)[:1024], "inline": False})
    footer = f"🔎 {search_name}"
    if stats is not None and stats.basis in ("title", "retail"):
        footer += f" · wycena {'ze sklepów' if stats.basis == 'retail' else 'z tytułu'}: „{stats.query}”"
    embed = {
        "title": f"{'🔥' if stats is not None else '🆕'} {item.title}"[:256],
        "url": item.url,
        "color": DISCORD_COLOR if stats is not None else DISCORD_COLOR_NEW,
        "fields": embed_fields,
        "footer": {"text": footer[:2048]},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if item.photo_url:
        embed["image"] = {"url": item.photo_url}
    return embed


def format_deal_text(item: Item, stats: MarketStats | None, seller: Seller, search_name: str) -> str:
    """Zwykły tekst okazji – do logu w trybie --dry-run."""
    details = " · ".join(x for x in (item.brand, item.details) if x)
    if stats is not None:
        pct = discount(item.price, stats.median) * 100
        basis = ""
        if stats.basis in ("title", "retail"):
            basis = f" ({'sklepy' if stats.basis == 'retail' else 'z tytułu'} „{stats.query}”, n={stats.used_size})"
        price_line = (f"💰 {item.price:.2f} {item.currency}  📊 mediana {stats.median:.2f}{basis}"
                      f"  📉 {pct:.0f}% taniej")
    else:
        price_line = f"💰 {item.price:.2f} {item.currency}  (nowa oferta, bez wyceny)"
    return "\n".join(x for x in (
        f"{'🔥' if stats is not None else '🆕'} {item.title}",
        price_line,
        f"⭐ {seller.login or seller.id} — {seller.rating:.2f}/5 ({seller.reviews} opinii)",
        f"🏷 {details}" if details else "",
        f"🔎 {search_name}  📷 {item.photo_url or '-'}",
        f"👉 {item.url}",
    ) if x)


class LogNotifier:
    """--dry-run: zamiast wysyłać, wypisuje okazje do logu."""

    def send_text(self, text: str) -> None:
        log.info("[DRY-RUN] wiadomość:\n%s", text)

    def send_deal(self, item: Item, stats: MarketStats | None, seller: Seller, search: SearchConfig) -> None:
        log.info("[DRY-RUN] oferta:\n%s", format_deal_text(item, stats, seller, search.name))


class BotNotifier:
    """Wysyła przez połączonego bota. Metody synchroniczne są dla wątku monitora –
    zlecają wysyłkę do pętli asyncio discord.py i czekają na wynik."""

    def __init__(self, client: discord.Client, loop: asyncio.AbstractEventLoop, default_channel_id: int,
                 timeout: float = 60):
        self.client = client
        self.loop = loop
        self.default_channel_id = default_channel_id
        self.timeout = timeout

    async def _channel(self, channel_id: int) -> discord.abc.Messageable:
        channel = self.client.get_channel(channel_id) or await self.client.fetch_channel(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            raise NotifierError(f"Kanał {channel_id} nie przyjmuje wiadomości")
        return channel

    async def send_async(self, channel_id: int | None, **kwargs) -> None:
        target = channel_id or self.default_channel_id
        try:
            await (await self._channel(target)).send(**kwargs)
            return
        except (discord.NotFound, discord.Forbidden) as e:
            if target == self.default_channel_id:
                raise NotifierError(f"Brak dostępu do kanału domyślnego {target}: {e}") from e
            log.warning("Kanał %s niedostępny (%s) – wysyłam na kanał domyślny", target, e)
        except discord.HTTPException as e:
            raise NotifierError(f"Discord: {e}") from e
        try:
            await (await self._channel(self.default_channel_id)).send(**kwargs)
        except discord.HTTPException as e:
            raise NotifierError(f"Discord (kanał domyślny): {e}") from e

    def _run(self, coro) -> None:
        try:
            asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=self.timeout)
        except NotifierError:
            raise
        except Exception as e:  # noqa: BLE001 – timeout, zamknięty klient itp.
            raise NotifierError(f"Discord: {e!r}") from e

    def send_text(self, text: str, channel_id: int | None = None) -> None:
        self._run(self.send_async(channel_id, content=text[:2000]))

    def send_deal(self, item: Item, stats: MarketStats | None, seller: Seller, search: SearchConfig) -> None:
        embed = discord.Embed.from_dict(build_discord_embed(item, stats, seller, search.name))
        self._run(self.send_async(search.channel_id, embed=embed))


# ============================================================================
# discord_bot – slash-komendy /szukaj … + wysyłka okazji
# ============================================================================
# Bot nie korzysta z kanałów głosowych – wyłącz ostrzeżenia o brakujących bibliotekach audio.
discord.VoiceClient.warn_nacl = False
discord.VoiceClient.warn_dave = False

COLOR_OK = 0x2ECC71
COLOR_ERR = 0xE74C3C
COLOR_INFO = 0x09B1BA

MODE_CHOICES = [app_commands.Choice(name=label, value=mode) for mode, label in MODE_LABELS.items()]
TRYB_HELP = "Ocena ofert; domyślnie: okazje z frazą, wszystkie bez frazy (cała kategoria)"


@dataclass
class Preview:
    count: int
    stats: MarketStats | None


class VintedDiscordBot(discord.Client):
    """mode: "run" – praca ciągła, "once" – jeden cykl i koniec, "test" – wiadomość testowa i koniec."""

    def __init__(self, cfg: Config, monitor_factory: Callable[[BotNotifier], "Bot"], mode: str = "run",
                 stop_event: threading.Event | None = None):
        intents = discord.Intents.none()
        intents.guilds = True  # wystarczy do slash-komend i wysyłania; bez uprzywilejowanych intentów
        super().__init__(intents=intents)
        self.cfg = cfg
        self.mode = mode
        self.monitor_factory = monitor_factory
        self.stop_event = stop_event or threading.Event()
        self.tree = app_commands.CommandTree(self)
        self.storage = Storage(cfg.storage.db_path)  # połączenie dla wątku asyncio
        self.service = SearchService(self.storage, cfg.searches, cfg.discord.max_searches)
        # Osobny klient Vinted do podglądu w komendach (sesja curl_cffi nie jest współdzielona z monitorem).
        self._preview_client = VintedClient(cfg.vinted)
        self._preview_pricer = MarketPricer(self._preview_client, cfg.pricing)
        self._preview_lock = threading.Lock()
        self.notifier: BotNotifier | None = None
        self.monitor: Bot | None = None
        self.monitor_thread: threading.Thread | None = None
        self.exit_code = 0

    # ------------------------------------------------------------ cykl życia
    async def setup_hook(self) -> None:
        self.tree.add_command(SearchCommands(self))
        if self.cfg.discord.guild_id:
            guild = discord.Object(id=self.cfg.discord.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            # Usuń ewentualne globalne kopie, żeby komendy nie pojawiały się podwójnie.
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
        else:
            log.warning("Brak DISCORD_GUILD_ID – komendy rejestrowane globalnie, pojawienie się może potrwać do godziny")
            await self.tree.sync()

    async def on_ready(self) -> None:
        if self.notifier is not None:  # on_ready przychodzi też po każdym ponownym połączeniu
            return
        log.info("Połączono z Discordem jako %s", self.user)
        self.notifier = BotNotifier(self, asyncio.get_running_loop(), self.cfg.discord.channel_id)

        if self.mode == "test":
            try:
                await self.notifier.send_async(None, content="✅ Vinted bot: test powiadomień działa.")
                log.info("Wysłano wiadomość testową")
            except Exception as e:  # noqa: BLE001
                log.error("Nie wysłano wiadomości testowej: %s", e)
                self.exit_code = 1
            await self.close()
            return

        self.monitor = self.monitor_factory(self.notifier)
        self.monitor_thread = threading.Thread(target=self._run_monitor, name="monitor", daemon=True)
        self.monitor_thread.start()

    def _run_monitor(self) -> None:
        assert self.monitor is not None
        try:
            if self.mode == "once":
                self.monitor.run_cycle()
            else:
                self.monitor.run_forever()
        except Exception:  # noqa: BLE001
            log.exception("Wątek monitora zakończył się błędem")
            self.exit_code = 1
        finally:
            # Koniec monitora = koniec procesu (w Dockerze restart: unless-stopped podniesie go z powrotem).
            if not self.is_closed():
                asyncio.run_coroutine_threadsafe(self.close(), self.loop)

    async def close(self) -> None:
        self.stop_event.set()
        await super().close()

    def shutdown(self) -> None:
        """Zamyka wątek monitora i zasoby po zakończeniu pętli asyncio."""
        self.stop_event.set()
        if self.monitor_thread is not None:
            self.monitor_thread.join(timeout=30)
        if self.monitor is not None:
            self.monitor.close()
        self._preview_client.reset_session()
        self.storage.close()

    # ---------------------------------------------------------------- podgląd
    def preview(self, search: SearchConfig) -> Preview:
        """Próbne wyszukiwanie (blokujące – wołać przez asyncio.to_thread)."""
        with self._preview_lock:
            items = self._preview_client.search(search, limit=self.cfg.polling.per_page, order="newest_first")
            # Mediana frazy ma sens tylko w trybie "deals" – w pozostałych oszczędzamy zapytanie.
            stats = self._preview_pricer.get_stats(search) if items and search.effective_mode == "deals" else None
            return Preview(count=len(items), stats=stats)


def _embed(title: str, description: str = "", color: int = COLOR_INFO) -> discord.Embed:
    return discord.Embed(title=title[:256], description=description[:4096], color=color)


async def _error(interaction: discord.Interaction, message: str) -> None:
    embed = _embed("❌ Nie udało się", message, COLOR_ERR)
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


def _check_channel(channel: discord.abc.GuildChannel | None, me: discord.Member | None) -> str | None:
    if channel is None or me is None:
        return None
    perms = channel.permissions_for(me)
    missing = [n for n, ok in (("Wyświetlanie kanału", perms.view_channel), ("Wysyłanie wiadomości", perms.send_messages),
                               ("Osadzanie linków", perms.embed_links)) if not ok]
    if missing:
        return f"Bot nie ma na kanale {channel.mention} uprawnień: {', '.join(missing)}"
    return None


class SearchCommands(app_commands.Group, name="szukaj", description="Wyszukiwania okazji na Vinted",
                     guild_only=True, default_permissions=discord.Permissions(manage_guild=True)):
    def __init__(self, bot: VintedDiscordBot):
        super().__init__()
        self.bot = bot

    # --------------------------------------------------------- dodawanie
    async def _add(self, interaction: discord.Interaction, search: SearchConfig,
                   channel: discord.TextChannel | None) -> None:
        target = channel or interaction.channel
        search.channel_id = target.id if target else None
        if problem := _check_channel(target, interaction.guild.me if interaction.guild else None):
            await _error(interaction, problem)
            return
        try:
            search.source = "discord"
            self.bot.service.validate_new(search)
        except SearchError as e:
            await _error(interaction, str(e))
            return

        await interaction.response.defer(thinking=True)
        try:
            preview = await asyncio.to_thread(self.bot.preview, search)
        except VintedError as e:
            await _error(interaction, f"Vinted nie odpowiedział poprawnie: {e}\nSpróbuj ponownie za chwilę.")
            return
        if preview.count == 0:
            await _error(interaction, f"To wyszukiwanie nie zwraca teraz żadnych ofert – nie zapisano.\n"
                                      f"Filtry: {describe(search)}")
            return
        try:
            self.bot.service.add(search, created_by=str(interaction.user))
        except SearchError as e:  # np. ktoś równolegle dodał tę samą nazwę
            await _error(interaction, str(e))
            return

        threshold = search.deal_threshold or self.bot.cfg.pricing.deal_threshold
        mode = search.effective_mode
        lines = [
            f"**Filtry:** {describe(search, self.bot.cfg.pricing.deal_threshold)}",
            f"**Kanał:** {target.mention if target else 'domyślny'}",
            f"**Teraz:** {preview.count} najnowszych ofert w filtrach",
        ]
        if mode == "all":
            lines.append("🆕 Powiadomienie o **każdej** nowej ofercie z tych filtrów (po weryfikacji sprzedającego).")
        elif mode == "per_item":
            lines.append(f"🔍 Każda nowa oferta jest wyceniana osobno – wyszukiwanie po jej tytule w tej samej "
                         f"kategorii; okazja przy cenie ≤ {threshold * 100:.0f}% mediany podobnych ofert. "
                         f"Wycena przybliżona.")
        elif preview.stats:
            lines.append(f"**Mediana rynkowa:** {preview.stats.median:.2f} {self.bot.cfg.vinted.currency} "
                         f"→ okazja poniżej **{preview.stats.median * threshold:.2f}**")
        else:
            lines.append("⚠️ Za mało ofert do wyceny – okazje nie będą wykrywane, dopóki próbka nie urośnie. "
                         "Rozważ szerszą frazę.")
        if mode == "deals" and not search.query:
            lines.append("⚠️ Tryb „okazje” bez frazy liczy medianę z całej kategorii – przy różnorodnych "
                         "przedmiotach (np. elektronika) lepiej użyć trybu „wszystkie” albo „z tytułu oferty”.")
        lines.append("Obecne oferty zostaną oznaczone jako widziane – powiadomienia przyjdą dla nowych.")
        await interaction.followup.send(embed=_embed(f"✅ Dodano: {search.name}", "\n".join(lines), COLOR_OK))
        log.info("Discord: %s dodał wyszukiwanie %s (%s)", interaction.user, search.name, describe(search))

    @app_commands.command(name="dodaj", description="Dodaj wyszukiwanie ręcznie")
    @app_commands.describe(
        nazwa="Unikalna nazwa, np. 'Air Max 90 r.42'",
        fraza="Tekst wyszukiwania, np. 'nike air max 90' (puste = cała kategoria/marka)",
        tryb=TRYB_HELP,
        cena_max="Maksymalna cena monitorowanych ofert",
        cena_min="Minimalna cena monitorowanych ofert",
        prog="Okazja, gdy cena ≤ tyle % mediany (domyślnie z configu)",
        kategorie="ID kategorii z URL Vinted (catalog[]), np. 1242 lub 1242,1243",
        rozmiary="ID rozmiarów z URL (size_ids[]), np. 784",
        marki="ID marek z URL (brand_ids[]), np. 53",
        stany="ID stanów z URL (status_ids[]): 6 nowy z metką, 1 nowy, 2 bardzo dobry, 3 dobry",
        kanal="Kanał na powiadomienia (domyślnie bieżący)",
    )
    @app_commands.choices(tryb=MODE_CHOICES)
    async def add(self, interaction: discord.Interaction, nazwa: str, fraza: str | None = None,
                  tryb: app_commands.Choice[str] | None = None,
                  cena_max: app_commands.Range[float, 0] | None = None,
                  cena_min: app_commands.Range[float, 0] | None = None,
                  prog: app_commands.Range[int, 1, 100] | None = None,
                  kategorie: str | None = None, rozmiary: str | None = None,
                  marki: str | None = None, stany: str | None = None,
                  kanal: discord.TextChannel | None = None) -> None:
        try:
            search = SearchConfig(
                name=nazwa.strip(), query=(fraza or "").strip(), price_max=cena_max, price_min=cena_min,
                catalog_ids=parse_ids(kategorie, "kategorie"), size_ids=parse_ids(rozmiary, "rozmiary"),
                brand_ids=parse_ids(marki, "marki"), status_ids=parse_ids(stany, "stany"),
                deal_threshold=prog / 100 if prog else None, mode=tryb.value if tryb else None,
            )
        except SearchError as e:
            await _error(interaction, str(e))
            return
        await self._add(interaction, search, kanal)

    @app_commands.command(name="z-linku", description="Dodaj wyszukiwanie z linku vinted.pl (z ustawionymi filtrami)")
    @app_commands.describe(
        url="Adres strony z wynikami wyszukiwania na Vinted",
        nazwa="Unikalna nazwa wyszukiwania",
        tryb=TRYB_HELP,
        prog="Okazja, gdy cena ≤ tyle % mediany (domyślnie z configu)",
        kanal="Kanał na powiadomienia (domyślnie bieżący)",
    )
    @app_commands.choices(tryb=MODE_CHOICES)
    async def from_url(self, interaction: discord.Interaction, url: str, nazwa: str,
                       tryb: app_commands.Choice[str] | None = None,
                       prog: app_commands.Range[int, 1, 100] | None = None,
                       kanal: discord.TextChannel | None = None) -> None:
        try:
            parsed = parse_catalog_url(url)
        except VintedUrlError as e:
            await _error(interaction, str(e))
            return
        search = SearchConfig(name=nazwa.strip(), deal_threshold=prog / 100 if prog else None,
                              mode=tryb.value if tryb else None, **parsed)
        await self._add(interaction, search, kanal)

    # ------------------------------------------------------------- lista
    @app_commands.command(name="lista", description="Pokaż wszystkie wyszukiwania")
    async def list_(self, interaction: discord.Interaction) -> None:
        default = self.bot.cfg.pricing.deal_threshold
        default_ch = f"<#{self.bot.cfg.discord.channel_id}>"
        lines = []
        if self.bot.service.yaml_searches:
            lines.append("**🔒 Z config.yaml** (zmiana tylko w pliku)")
            for s in self.bot.service.yaml_searches:
                ch = f"<#{s.channel_id}>" if s.channel_id else default_ch
                lines.append(f"▶️ **{s.name}** → {ch}\n  {describe(s, default)}")
        stored = self.bot.service.discord_searches()
        lines.append(f"\n**💬 Z Discorda** ({len(stored)}/{self.bot.cfg.discord.max_searches})")
        if not stored:
            lines.append("_brak – dodaj przez /szukaj z-linku_")
        for st in stored:
            s = st.search
            ch = f"<#{s.channel_id}>" if s.channel_id else default_ch
            icon = "▶️" if st.enabled else "⏸️"
            lines.append(f"{icon} **{s.name}** → {ch}\n  {describe(s, default)}")
        active = len(self.bot.service.active())
        lines.append(f"\nAktywne: **{active}** → {active} zapytań do Vinted na cykl "
                     f"(co {self.bot.cfg.polling.min_interval:g}–{self.bot.cfg.polling.max_interval:g} s)")
        text = "\n".join(lines)
        if len(text) > 4000:
            text = text[:3990] + "\n…"
        await interaction.response.send_message(embed=_embed("🔎 Wyszukiwania", text), ephemeral=True)

    # ------------------------------------------------ usuń / pauza / wznów
    async def _names(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        current = current.casefold()
        names = [s.search.name for s in self.bot.service.discord_searches() if current in s.search.name.casefold()]
        return [app_commands.Choice(name=n, value=n) for n in names[:25]]

    @app_commands.command(name="usun", description="Usuń wyszukiwanie dodane z Discorda")
    @app_commands.describe(nazwa="Nazwa wyszukiwania")
    async def remove(self, interaction: discord.Interaction, nazwa: str) -> None:
        try:
            self.bot.service.remove(nazwa)
        except SearchError as e:
            await _error(interaction, str(e))
            return
        await interaction.response.send_message(embed=_embed(f"🗑️ Usunięto: {nazwa}", color=COLOR_OK))
        log.info("Discord: %s usunął wyszukiwanie %s", interaction.user, nazwa)

    @app_commands.command(name="pauza", description="Wstrzymaj wyszukiwanie bez usuwania")
    @app_commands.describe(nazwa="Nazwa wyszukiwania")
    async def pause(self, interaction: discord.Interaction, nazwa: str) -> None:
        await self._toggle(interaction, nazwa, False)

    @app_commands.command(name="wznow", description="Wznów wstrzymane wyszukiwanie")
    @app_commands.describe(nazwa="Nazwa wyszukiwania")
    async def resume(self, interaction: discord.Interaction, nazwa: str) -> None:
        await self._toggle(interaction, nazwa, True)

    async def _toggle(self, interaction: discord.Interaction, name: str, enabled: bool) -> None:
        try:
            self.bot.service.set_enabled(name, enabled)
        except SearchError as e:
            await _error(interaction, str(e))
            return
        title = f"▶️ Wznowiono: {name}" if enabled else f"⏸️ Wstrzymano: {name}"
        note = "Oferty z czasu pauzy nie zostaną wysłane." if enabled else ""
        await interaction.response.send_message(embed=_embed(title, note, COLOR_OK))
        log.info("Discord: %s %s wyszukiwanie %s", interaction.user, "wznowił" if enabled else "wstrzymał", name)

    remove.autocomplete("nazwa")(_names)
    pause.autocomplete("nazwa")(_names)
    resume.autocomplete("nazwa")(_names)


# ============================================================================
# main – pętla bota: wyszukiwanie → nowe oferty → wycena → sprzedający → Discord
# ============================================================================
def setup_logging(level: str, file: str | None) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(level)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)
    if file:
        Path(file).parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(file, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    # discord.py na INFO loguje szczegóły połączenia z gatewayem – wystarczą ostrzeżenia.
    logging.getLogger("discord").setLevel(logging.WARNING)


class Bot:
    """Pętla monitora. Działa synchronicznie – w trybie Discord w osobnym wątku."""

    def __init__(self, cfg: Config, notifier: BotNotifier | LogNotifier):
        self.cfg = cfg
        self.client = VintedClient(cfg.vinted)
        self.storage = Storage(cfg.storage.db_path)
        self.searches = SearchService(self.storage, cfg.searches, cfg.discord.max_searches)
        retail, retail_factor = build_provider_from_env()
        self.pricer = MarketPricer(self.client, cfg.pricing, retail=retail, retail_factor=retail_factor,
                                   retail_only=True)
        self.sellers = SellerFilter(self.client, self.storage, cfg.seller)
        self.notifier = notifier

    def close(self) -> None:
        self.storage.close()
        self.client.reset_session()

    def process_search(self, search: SearchConfig) -> int:
        """Zwraca liczbę wysłanych powiadomień."""
        items = self.client.search(search, limit=self.cfg.polling.per_page, order="newest_first")
        unseen_ids = self.storage.filter_unseen(i.id for i in items)
        new_items = [i for i in items if i.id in unseen_ids]

        if not self.storage.is_initialized(search.name):
            self.storage.set_initialized(search.name)
            if not self.cfg.notify_on_first_run:
                self.storage.mark_seen(search.name, [(i.id, i.price) for i in new_items])
                log.info("[%s] pierwsze uruchomienie – zapisano %d ofert jako widziane", search.name, len(new_items))
                return 0

        if not new_items:
            log.debug("[%s] brak nowych ofert", search.name)
            return 0
        log.info("[%s] nowe oferty: %d", search.name, len(new_items))

        # Jeden tryb: każda nowa oferta z kategorii jest porównywana z ceną nowego produktu ze sklepów.
        threshold = search.deal_threshold or self.cfg.pricing.deal_threshold
        limit = self.cfg.pricing.per_item_max_per_cycle  # limit nowych wycen (zapytań do sklepów) na cykl
        priced = 0
        sent = 0
        # Od najstarszej, żeby powiadomienia szły w kolejności wystawienia.
        for item in reversed(new_items):
            deferred = False
            try:
                if not self.pricer.is_item_cached(search, item):
                    if priced >= limit:
                        # Nie oznaczamy jako widzianej – oferta wróci w następnym cyklu, gdy będzie limit.
                        log.info("[%s] limit %d wycen na cykl – odkładam %s", search.name, limit, item.url)
                        deferred = True
                        continue
                    priced += 1
                    if priced > 1:
                        stop_event.wait(random.uniform(1, 2.5))  # nie zasypuj sklepów serią zapytań
                stats = self.pricer.get_item_stats(search, item)
                if stats is None:
                    log.debug("[%s] %s – brak ceny rynkowej, pomijam", search.name, item.url)
                    continue
                if item.price > stats.median * threshold:
                    log.debug("[%s] %s – %.2f nie jest okazją (próg %.2f)",
                              search.name, item.id, item.price, stats.median * threshold)
                    continue
                verdict = self.sellers.check(item.seller_id)
                if not verdict.ok:
                    log.info("[%s] oferta %s odrzucona: %s", search.name, item.url, verdict.reason)
                    continue
                log.info("[%s] OKAZJA: %s %.2f %s (rynek %.2f, -%.0f%%) %s", search.name, item.title, item.price,
                         item.currency, stats.median, discount(item.price, stats.median) * 100, item.url)
                self.notifier.send_deal(item, stats, verdict.seller, search)
                sent += 1
            except NotifierError as e:
                log.error("[%s] nie wysłano powiadomienia dla %s: %s", search.name, item.url, e)
            except VintedError as e:
                log.warning("[%s] nie udało się sprawdzić %s: %s", search.name, item.url, e)
            finally:
                if not deferred:
                    # Oznacz jako widzianą, żeby nie przetwarzać jej w kółko.
                    self.storage.mark_seen(search.name, [(item.id, item.price)])
        return sent

    def run_cycle(self) -> None:
        # Lista co cykl od nowa – zmiany z komend Discorda działają bez restartu.
        searches = self.searches.active()
        if not searches:
            log.info("Brak aktywnych wyszukiwań – dodaj je w config.yaml albo komendą /szukaj na Discordzie")
        for idx, search in enumerate(searches):
            if stop_event.is_set():
                return
            try:
                self.process_search(search)
            except VintedError as e:
                log.error("[%s] błąd Vinted: %s", search.name, e)
            except Exception:  # noqa: BLE001 – jedna fraza nie może zatrzymać bota
                log.exception("[%s] nieoczekiwany błąd", search.name)
            if idx < len(searches) - 1:
                stop_event.wait(random.uniform(*self.cfg.polling.delay_between_searches))

    def run_forever(self) -> None:
        log.info("Start monitora: %d aktywnych wyszukiwań, odstęp %s–%s s", len(self.searches.active()),
                 self.cfg.polling.min_interval, self.cfg.polling.max_interval)
        cycles = 0
        while not stop_event.is_set():
            self.run_cycle()
            cycles += 1
            if cycles % 500 == 0:
                removed = self.storage.cleanup(self.cfg.storage.retention_days)
                log.info("Sprzątanie bazy: usunięto %d starych wpisów", removed)
            delay = random.uniform(self.cfg.polling.min_interval, self.cfg.polling.max_interval)
            log.debug("Następny cykl za %.0f s", delay)
            stop_event.wait(delay)
        log.info("Zatrzymano")


def run_dry(cfg: Config, once: bool) -> int:
    """--dry-run: bez Discorda, okazje tylko w logu."""
    bot = Bot(cfg, LogNotifier())
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop_event.set())
    try:
        if once:
            bot.run_cycle()
        else:
            bot.run_forever()
    finally:
        bot.close()
    return 0


def run_discord(cfg: Config, mode: str) -> int:
    missing = [n for n, v in (("DISCORD_BOT_TOKEN", cfg.discord.bot_token),
                              ("DISCORD_CHANNEL_ID", cfg.discord.channel_id)) if not v]
    if missing:
        log.error("Brak w .env: %s (linia nie może zaczynać się od '#'; w Dockerze po zmianie .env "
                  "uruchom: docker compose up -d). Bez Discorda możesz testować z --dry-run.", ", ".join(missing))
        return 2

    client = VintedDiscordBot(cfg, monitor_factory=lambda notifier: Bot(cfg, notifier), mode=mode,
                              stop_event=stop_event)

    async def runner() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda: asyncio.ensure_future(client.close()))
            except NotImplementedError:  # Windows
                pass
        async with client:
            await client.start(cfg.discord.bot_token)

    try:
        asyncio.run(runner())
    except discord.LoginFailure:
        log.error("Discord odrzucił DISCORD_BOT_TOKEN – skopiuj token ponownie (Developer Portal → Bot → Reset Token)")
        return 2
    except discord.PrivilegedIntentsRequired:
        log.error("Discord wymaga uprzywilejowanych intentów – nie powinno się zdarzyć, zgłoś błąd")
        return 2
    except KeyboardInterrupt:
        pass
    finally:
        client.shutdown()
    return client.exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vinted_bot", description="Monitor okazji na Vinted z botem Discord")
    parser.add_argument("-c", "--config", default="config.yaml", help="ścieżka do config.yaml")
    parser.add_argument("--env", default=".env", help="ścieżka do pliku .env")
    parser.add_argument("--once", action="store_true", help="wykonaj jeden cykl i zakończ")
    parser.add_argument("--dry-run", action="store_true",
                        help="bez łączenia z Discordem – okazje tylko w logu (nie wymaga tokenu)")
    parser.add_argument("--test-notify", action="store_true",
                        help="wyślij wiadomość testową na DISCORD_CHANNEL_ID i zakończ")
    parser.add_argument("--retail-check", metavar="ZAPYTANIE",
                        help="diagnostyka cen sklepowych dla zapytania (np. 'jbl flip 7') i zakończ")
    parser.add_argument("--shops", default=",".join(DEFAULT_SHOPS),
                        help=f"sklepy dla --retail-check; dostępne: {', '.join(SHOPS)}")
    parser.add_argument("--save", action="store_true",
                        help="z --retail-check: zapisz HTML do data/retail_debug_<sklep>.html")
    args = parser.parse_args(argv)

    if args.retail_check:
        return retail_check(args.retail_check, args.shops, args.save)

    try:
        cfg = load_config(args.config, args.env)
    except ConfigError as e:
        print(f"Błąd konfiguracji: {e}", file=sys.stderr)
        return 2
    setup_logging(cfg.logging.level, cfg.logging.file)

    if args.dry_run:
        if args.test_notify:
            log.error("--test-notify wymaga połączenia z Discordem – uruchom bez --dry-run")
            return 2
        return run_dry(cfg, args.once)
    return run_discord(cfg, "test" if args.test_notify else "once" if args.once else "run")


if __name__ == "__main__":
    sys.exit(main())
