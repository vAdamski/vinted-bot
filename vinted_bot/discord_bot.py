"""Bot Discord: slash-komendy /szukaj … + wysyłka okazji.

Pętla monitora (synchroniczna, curl_cffi) działa w osobnym wątku; komendy w pętli asyncio discord.py.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

import discord
from discord import app_commands

from .config import Config, SearchConfig
from .notifier import BotNotifier
from .pricing import MarketPricer, MarketStats
from .scraper import VintedClient, VintedError
from .search_service import SearchError, SearchService, describe, parse_ids
from .storage import Storage
from .vinted_url import VintedUrlError, parse_catalog_url

if TYPE_CHECKING:
    from .main import Bot

log = logging.getLogger(__name__)

# Bot nie korzysta z kanałów głosowych – wyłącz ostrzeżenia o brakujących bibliotekach audio.
discord.VoiceClient.warn_nacl = False
discord.VoiceClient.warn_dave = False

COLOR_OK = 0x2ECC71
COLOR_ERR = 0xE74C3C
COLOR_INFO = 0x09B1BA


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
            stats = self._preview_pricer.get_stats(search) if items else None
            return Preview(count=len(items), stats=stats)


# ---------------------------------------------------------------------- komendy
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
        lines = [
            f"**Filtry:** {describe(search, self.bot.cfg.pricing.deal_threshold)}",
            f"**Kanał:** {target.mention if target else 'domyślny'}",
            f"**Teraz:** {preview.count} najnowszych ofert w filtrach",
        ]
        if preview.stats:
            lines.append(f"**Mediana rynkowa:** {preview.stats.median:.2f} {self.bot.cfg.vinted.currency} "
                         f"→ okazja poniżej **{preview.stats.median * threshold:.2f}**")
        else:
            lines.append("⚠️ Za mało ofert do wyceny – okazje nie będą wykrywane, dopóki próbka nie urośnie. "
                         "Rozważ szerszą frazę.")
        lines.append("Obecne oferty zostaną oznaczone jako widziane – powiadomienia przyjdą dla nowych.")
        await interaction.followup.send(embed=_embed(f"✅ Dodano: {search.name}", "\n".join(lines), COLOR_OK))
        log.info("Discord: %s dodał wyszukiwanie %s (%s)", interaction.user, search.name, describe(search))

    @app_commands.command(name="dodaj", description="Dodaj wyszukiwanie ręcznie")
    @app_commands.describe(
        nazwa="Unikalna nazwa, np. 'Air Max 90 r.42'",
        fraza="Tekst wyszukiwania, np. 'nike air max 90'",
        cena_max="Maksymalna cena monitorowanych ofert",
        cena_min="Minimalna cena monitorowanych ofert",
        prog="Okazja, gdy cena ≤ tyle % mediany (domyślnie z configu)",
        kategorie="ID kategorii z URL Vinted (catalog[]), np. 1242 lub 1242,1243",
        rozmiary="ID rozmiarów z URL (size_ids[]), np. 784",
        marki="ID marek z URL (brand_ids[]), np. 53",
        stany="ID stanów z URL (status_ids[]): 6 nowy z metką, 1 nowy, 2 bardzo dobry, 3 dobry",
        kanal="Kanał na powiadomienia (domyślnie bieżący)",
    )
    async def add(self, interaction: discord.Interaction, nazwa: str, fraza: str,
                  cena_max: app_commands.Range[float, 0] | None = None,
                  cena_min: app_commands.Range[float, 0] | None = None,
                  prog: app_commands.Range[int, 1, 100] | None = None,
                  kategorie: str | None = None, rozmiary: str | None = None,
                  marki: str | None = None, stany: str | None = None,
                  kanal: discord.TextChannel | None = None) -> None:
        try:
            search = SearchConfig(
                name=nazwa.strip(), query=fraza.strip(), price_max=cena_max, price_min=cena_min,
                catalog_ids=parse_ids(kategorie, "kategorie"), size_ids=parse_ids(rozmiary, "rozmiary"),
                brand_ids=parse_ids(marki, "marki"), status_ids=parse_ids(stany, "stany"),
                deal_threshold=prog / 100 if prog else None,
            )
        except SearchError as e:
            await _error(interaction, str(e))
            return
        await self._add(interaction, search, kanal)

    @app_commands.command(name="z-linku", description="Dodaj wyszukiwanie z linku vinted.pl (z ustawionymi filtrami)")
    @app_commands.describe(
        url="Adres strony z wynikami wyszukiwania na Vinted",
        nazwa="Unikalna nazwa wyszukiwania",
        prog="Okazja, gdy cena ≤ tyle % mediany (domyślnie z configu)",
        kanal="Kanał na powiadomienia (domyślnie bieżący)",
    )
    async def from_url(self, interaction: discord.Interaction, url: str, nazwa: str,
                       prog: app_commands.Range[int, 1, 100] | None = None,
                       kanal: discord.TextChannel | None = None) -> None:
        try:
            fields = parse_catalog_url(url)
        except VintedUrlError as e:
            await _error(interaction, str(e))
            return
        search = SearchConfig(name=nazwa.strip(), deal_threshold=prog / 100 if prog else None, **fields)
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
