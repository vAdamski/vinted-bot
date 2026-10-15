"""Klient wewnętrznego API Vinted (nieoficjalnego!).

Miejsca oznaczone `⚠️ VINTED-API` to założenia o nieudokumentowanym API,
które mogą przestać działać po zmianach po stronie Vinted.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Any

from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException

from .config import SearchConfig, VintedConfig

log = logging.getLogger(__name__)

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
