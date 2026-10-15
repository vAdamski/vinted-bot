"""Główna pętla bota: wyszukiwanie → nowe oferty → wycena → sprzedający → Discord."""
from __future__ import annotations

import argparse
import asyncio
import logging
import random
import signal
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import Config, ConfigError, SearchConfig, load_config
from .notifier import BotNotifier, LogNotifier, NotifierError
from .pricing import MarketPricer, discount
from .scraper import VintedClient, VintedError
from .search_service import SearchService
from .seller_filter import SellerFilter
from .storage import Storage

log = logging.getLogger("vinted_bot")
stop_event = threading.Event()


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
        self.pricer = MarketPricer(self.client, cfg.pricing)
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

        mode = search.effective_mode
        threshold = search.deal_threshold or self.cfg.pricing.deal_threshold
        stats = self.pricer.get_stats(search) if mode == "deals" else None
        priced = 0  # tryb per_item: ile nowych wycen (zapytań) w tym cyklu
        sent = 0
        # Od najstarszej, żeby powiadomienia szły w kolejności wystawienia.
        for item in reversed(new_items):
            try:
                if mode == "deals" and stats is None:
                    continue
                if mode == "per_item":
                    if not self.pricer.is_item_cached(search, item):
                        if priced >= self.cfg.pricing.per_item_max_per_cycle:
                            log.warning("[%s] limit %d wycen na cykl – pomijam %s (pricing.per_item_max_per_cycle)",
                                        search.name, self.cfg.pricing.per_item_max_per_cycle, item.url)
                            continue
                        priced += 1
                        if priced > 1:
                            stop_event.wait(random.uniform(1, 2.5))  # nie zasypuj Vinted serią zapytań
                    stats = self.pricer.get_item_stats(search, item)
                    if stats is None:
                        log.debug("[%s] %s – za mało podobnych ofert do wyceny", search.name, item.url)
                        continue
                if stats is not None and item.price > stats.median * threshold:
                    log.debug("[%s] %s – %.2f nie jest okazją (próg %.2f)",
                              search.name, item.id, item.price, stats.median * threshold)
                    continue
                verdict = self.sellers.check(item.seller_id)
                if not verdict.ok:
                    log.info("[%s] oferta %s odrzucona: %s", search.name, item.url, verdict.reason)
                    continue
                if stats is not None:
                    log.info("[%s] OKAZJA: %s %.2f %s (mediana %.2f, -%.0f%%) %s", search.name, item.title, item.price,
                             item.currency, stats.median, discount(item.price, stats.median) * 100, item.url)
                else:
                    log.info("[%s] NOWA: %s %.2f %s %s", search.name, item.title, item.price, item.currency, item.url)
                self.notifier.send_deal(item, stats, verdict.seller, search)
                sent += 1
            except NotifierError as e:
                log.error("[%s] nie wysłano powiadomienia dla %s: %s", search.name, item.url, e)
            except VintedError as e:
                log.warning("[%s] nie udało się wycenić %s: %s", search.name, item.url, e)
            finally:
                # Zawsze oznacz jako widzianą, żeby nie przetwarzać jej w kółko.
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
    import discord

    from .discord_bot import VintedDiscordBot

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
    args = parser.parse_args(argv)

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
