"""Wczytywanie konfiguracji z config.yaml + .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


class ConfigError(Exception):
    pass


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
    query: str
    price_max: float | None = None
    price_min: float | None = None
    catalog_ids: list[int] = field(default_factory=list)
    size_ids: list[int] = field(default_factory=list)
    brand_ids: list[int] = field(default_factory=list)
    status_ids: list[int] = field(default_factory=list)
    # Inne filtry z URL-a Vinted, np. {"color": [1, 12], "material": [44]}
    attributes: dict[str, list[int]] = field(default_factory=dict)
    deal_threshold: float | None = None  # None = użyj pricing.deal_threshold
    channel_id: int | None = None  # None = kanał domyślny (DISCORD_CHANNEL_ID)
    source: str = "yaml"           # "yaml" | "discord" – ustawiane w kodzie


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

    discord = _build(DiscordConfig, raw.get("discord"))
    discord.bot_token = os.getenv("DISCORD_BOT_TOKEN", "")
    discord.guild_id = _env_int("DISCORD_GUILD_ID")
    discord.channel_id = _env_int("DISCORD_CHANNEL_ID")

    logging_cfg = _build(LoggingConfig, raw.get("logging"))
    logging_cfg.level = os.getenv("LOG_LEVEL", logging_cfg.level).upper()

    # Pusta lista jest OK – wyszukiwania można dodać komendami na Discordzie.
    searches = [_build(SearchConfig, s) for s in raw.get("searches") or []]
    for s in searches:
        s.source = "yaml"
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
        discord=discord,
        searches=searches,
        notify_on_first_run=bool(raw.get("notify_on_first_run", False)),
    )
