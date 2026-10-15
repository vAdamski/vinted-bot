"""Powiadomienia na Discorda przez bota (lub do logu w trybie --dry-run)."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

import discord

from .config import SearchConfig
from .pricing import MarketStats, discount
from .scraper import Item, Seller

log = logging.getLogger(__name__)

DISCORD_COLOR = 0x09B1BA  # turkus Vinted


class NotifierError(Exception):
    pass


def _md(text: str) -> str:
    """Escapuje znaki markdownu Discorda w tekstach z Vinted (login, szczegóły)."""
    return re.sub(r"([\\*_~`|>\[\]()])", r"\\\1", text)


def build_discord_embed(item: Item, stats: MarketStats, seller: Seller, search_name: str) -> dict:
    pct = discount(item.price, stats.median) * 100
    price = f"**{item.price:.2f} {item.currency}**"
    if item.total_price:
        price += f" (z ochroną: {item.total_price:.2f})"
    fields = [
        {"name": "💰 Cena", "value": price, "inline": True},
        {"name": "📊 Mediana", "value": f"{stats.median:.2f} {item.currency}", "inline": True},
        {"name": "📉 Taniej", "value": f"**{pct:.0f}%**", "inline": True},
        {"name": "⭐ Sprzedający",
         "value": f"{_md(seller.login or str(seller.id))} — {seller.rating:.2f}/5 ({seller.reviews} opinii)",
         "inline": False},
    ]
    details = " · ".join(x for x in (item.brand, item.details) if x)
    if details:
        fields.append({"name": "🏷 Szczegóły", "value": _md(details)[:1024], "inline": False})
    embed = {
        "title": f"🔥 {item.title}"[:256],
        "url": item.url,
        "color": DISCORD_COLOR,
        "fields": fields,
        "footer": {"text": f"🔎 {search_name}"[:2048]},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if item.photo_url:
        embed["image"] = {"url": item.photo_url}
    return embed


def format_deal_text(item: Item, stats: MarketStats, seller: Seller, search_name: str) -> str:
    """Zwykły tekst okazji – do logu w trybie --dry-run."""
    pct = discount(item.price, stats.median) * 100
    details = " · ".join(x for x in (item.brand, item.details) if x)
    return "\n".join(x for x in (
        f"🔥 {item.title}",
        f"💰 {item.price:.2f} {item.currency}  📊 mediana {stats.median:.2f}  📉 {pct:.0f}% taniej",
        f"⭐ {seller.login or seller.id} — {seller.rating:.2f}/5 ({seller.reviews} opinii)",
        f"🏷 {details}" if details else "",
        f"🔎 {search_name}  📷 {item.photo_url or '-'}",
        f"👉 {item.url}",
    ) if x)


class LogNotifier:
    """--dry-run: zamiast wysyłać, wypisuje okazje do logu."""

    def send_text(self, text: str) -> None:
        log.info("[DRY-RUN] wiadomość:\n%s", text)

    def send_deal(self, item: Item, stats: MarketStats, seller: Seller, search: SearchConfig) -> None:
        log.info("[DRY-RUN] okazja:\n%s", format_deal_text(item, stats, seller, search.name))


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

    def send_deal(self, item: Item, stats: MarketStats, seller: Seller, search: SearchConfig) -> None:
        embed = discord.Embed.from_dict(build_discord_embed(item, stats, seller, search.name))
        self._run(self.send_async(search.channel_id, embed=embed))
