# Vinted deal bot

Bot w Pythonie, który co 30–60 s sprawdza najnowsze oferty z wybranej kategorii Vinted (np. Elektronika,
stan „nowe z metką”), dla każdej oferty ustala cenę **nowego produktu w popularnych sklepach**, sprawdza
sprzedającego i wysyła okazje na Discorda (zdjęcie, tytuł, cena, cena rynkowa, % taniej, ocena sprzedającego,
link). Wyszukiwania można dodawać, wstrzymywać i usuwać slash-komendami na Discordzie.

```
vinted_bot/
├── config.py          # config.yaml + .env → dataclasses
├── scraper.py         # klient wewnętrznego API Vinted (curl_cffi, sesja, retry/backoff)
├── pricing.py         # wycena oferty (cena rynkowa, IQR, cache), dopasowanie tytułów
├── retail_pricing.py  # źródła cen nowych produktów: sklepy, ręczny plik JSON
├── shops.py           # pobieranie i parsowanie stron wyników sklepów (Ceneo, x-kom, Morele…)
├── retail_check.py    # diagnostyka: co zwraca każdy sklep dla danego zapytania
├── seller_filter.py   # ocena i liczba opinii sprzedającego (cache w SQLite)
├── storage.py         # SQLite: widziane oferty, stan fraz, cache sprzedających, wyszukiwania z Discorda
├── search_service.py  # wyszukiwania z config.yaml + z bazy, walidacja (bez zależności od Discorda)
├── vinted_url.py      # link wyszukiwania z vinted.pl → filtry
├── discord_bot.py     # bot Discord: komendy /szukaj, start wątku monitora
├── notifier.py        # wysyłka okazji przez bota (embed ze zdjęciem) / do logu przy --dry-run
└── main.py            # pętla monitora, CLI
```

## Jak to działa

1. **Sesja** – `curl_cffi` podszywa się pod Chrome (odcisk TLS/HTTP2), wchodzi na stronę główną
   i dostaje ciasteczka (`access_token_web`, `anon_id`, `__cf_bm`...). Przy 401/403 sesja jest odnawiana.
2. **Monitorowanie** – co `min_interval`–`max_interval` sekund (losowo) pobiera `per_page` najnowszych ofert
   dla każdego wyszukiwania (z filtrami: kategoria, stan, cena, marka, rozmiar).
3. **Deduplikacja** – ID widzianych ofert są w SQLite. Przy pierwszym uruchomieniu wyszukiwania obecne
   oferty są tylko zapisywane (bez powiadomień), chyba że `notify_on_first_run: true`.
4. **Wycena ze sklepów** – dla każdej nowej oferty bot buduje zapytanie z jej tytułu (bez kolorów, słów
   „nowy”, „idealny stan” itp.), szuka go w sklepach, zostawia tylko pasujące wyniki (zgodne oznaczenie modelu,
   bez akcesoriów), odrzuca wartości spoza `[Q1 − 1.5·IQR, Q3 + 1.5·IQR]` i liczy medianę z kilku sklepów.
   Wynik jest cache’owany na 24 h (także brak wyniku). Oferta jest okazją, gdy
   `cena ≤ deal_threshold × cena rynkowa`. Brak ceny rynkowej = oferta pomijana (bot niczego nie zgaduje).
5. **Sprzedający** – pobierany profil (`/api/v2/users/{id}`); odrzucenie gdy ocena < `min_rating` albo
   opinii < `min_reviews`. Profil cache’owany w SQLite (`cache_ttl_hours`).
6. **Discord** – bot wysyła embed z tytułem-linkiem, zdjęciem, ceną, ceną rynkową (🏬 Cena nowego, sklepy),
   % taniej i sprzedającym na kanał przypisany do wyszukiwania (albo domyślny). Komendy `/szukaj` działają
   w tym samym procesie; zmiany obowiązują od następnego cyklu, bez restartu.
7. **Limit wycen na cykl** – `pricing.per_item_max_per_cycle` ogranicza liczbę nowych wycen (zapytań do
   sklepów) w jednym cyklu. Oferty ponad limit **nie są oznaczane jako widziane** – wrócą w następnym cyklu.
8. **Błędy** – 401/403: odnowienie sesji + backoff; 429: `Retry-After` lub backoff wykładniczy;
   5xx/sieć: backoff. Błąd jednego wyszukiwania lub jednego sklepu nie zatrzymuje bota.

## Konfiguracja

```
cp config.example.yaml config.yaml
cp .env.example .env
```

### Bot Discord

1. [Discord Developer Portal](https://discord.com/developers/applications) → *New Application* → nazwa, np. „Vinted bot”.
2. Zakładka *Bot* → *Reset Token* → skopiuj do `DISCORD_BOT_TOKEN` w `.env`.
   Uprzywilejowanych intentów (*Privileged Gateway Intents*) **nie trzeba** włączać.
3. Zakładka *OAuth2* → *URL Generator*: zaznacz zakresy `bot` i `applications.commands`,
   uprawnienia *View Channels*, *Send Messages*, *Embed Links*. Otwórz wygenerowany link
   i dodaj bota do swojego serwera.
4. W Discordzie: *Ustawienia użytkownika* → *Zaawansowane* → włącz *Tryb dewelopera*. Potem:
   - prawy klik na serwer → *Kopiuj ID serwera* → `DISCORD_GUILD_ID` (komendy pojawią się od razu; bez tego Discord rejestruje je nawet do godziny),
   - prawy klik na kanał → *Kopiuj ID kanału* → `DISCORD_CHANNEL_ID` (kanał domyślny na okazje).
5. Test: `python -m vinted_bot --test-notify`.

Token bota to sekret – kto go zna, może przejąć bota.

### Ceny rynkowe (sklepy)

Źródła cen ustawiasz w `.env`:

| Zmienna | Znaczenie | Domyślnie |
| --- | --- | --- |
| `RETAIL_SHOPS` | sklepy do porównania, po przecinku: `ceneo`, `xkom`, `morele`, `mediaexpert`, `euro`; `off` wyłącza sklepy | `ceneo,xkom,morele` |
| `RETAIL_PRICES_FILE` | ręczna lista cen (JSON), sprawdzana **przed** sklepami; zob. `retail_prices.example.json` | – |
| `RETAIL_FACTOR` | mnożnik ceny rynkowej (np. `0.7` przy porównywaniu z używanymi) | `1.0` |
| `RETAIL_PROXY` | proxy dla zapytań do sklepów | – |

Format pliku z ręcznymi cenami: `{"jbl flip 7": [449, 459, 499]}`. Wpis pasuje do oferty, gdy jej
zapytanie zawiera wszystkie słowa z nazwy wpisu. Plik jest czytany przy każdej wycenie, więc zmiany działają
bez restartu.

**Sprawdź sklepy przed pierwszym uruchomieniem:**

```
python -m vinted_bot.retail_check "jbl flip 7"
python -m vinted_bot.retail_check "sony wh-1000xm5" --shops ceneo,xkom --save
```

Skrypt pokazuje dla każdego sklepu status HTTP, liczbę znalezionych ofert, pasujące ceny i medianę.
`--save` zapisuje pobrany HTML do `data/retail_debug_<sklep>.html`.

Ceny są wyciągane ze stron wyników na dwa sposoby: z danych strukturalnych JSON-LD (bez konfiguracji)
albo z selektorów CSS ustawionych dla sklepu w `shops.py` (`item_sel`, `title_sel`, `price_sel`; wymaga
`pip install beautifulsoup4`). Jeśli `retail_check` pokazuje „0 ofert” dla sklepu, ustaw dla niego selektory.

> ⚠️ Adresy wyszukiwarek sklepów w `shops.py` i sposób wyciągania cen nie były sprawdzone na żywych
> stronach. Sklepy zmieniają HTML i blokują boty – traktuj `retail_check` jako obowiązkowy test.

### Komendy

Dostępne tylko dla osób z uprawnieniem *Zarządzanie serwerem* (zmienisz to w *Ustawienia serwera → Integracje → Vinted bot*).

| Komenda | Opis |
| --- | --- |
| `/szukaj z-linku url nazwa [prog] [kanal]` | **Najprościej:** ustaw filtry na vinted.pl, skopiuj adres strony z wynikami i wklej. |
| `/szukaj dodaj nazwa [cena_max] [cena_min] [prog] [kategorie] [rozmiary] [marki] [stany] [kanal]` | Ręcznie; ID podawaj po przecinku, `prog` w % ceny rynkowej (np. 70). |
| `/szukaj lista` | Wszystkie wyszukiwania (🔒 z config.yaml, ▶️/⏸️ z Discorda) i liczba zapytań na cykl. |
| `/szukaj pauza nazwa` / `/szukaj wznow nazwa` | Wstrzymanie bez usuwania. Po wznowieniu oferty z czasu pauzy nie są wysyłane. |
| `/szukaj usun nazwa` | Usunięcie (tylko wyszukiwań dodanych z Discorda). |

Pole `tryb`, jeśli jeszcze widzisz je w komendach, jest **ignorowane** – bot ma jeden tryb wyceny (ze sklepów).
Próbne wyszukiwanie przy dodawaniu może nadal pokazywać medianę z ofert Vinted (to starszy kod w `discord_bot.py`);
do wyceny okazji nie jest używana.

Przykład – cała Elektronika, nowe z metką, okazje od 30% taniej niż w sklepach:

```
/szukaj z-linku url:https://www.vinted.pl/catalog?catalog[]=2994&status_ids[]=6 nazwa:Elektronika nowe prog:70
```

Chcesz pominąć drobiazgi? Dopisz do linku dolną cenę: `...&status_ids[]=6&price_from=200`.

Powiadomienia trafiają na kanał, na którym wpisano komendę (albo wskazany w `kanal`). Limit wyszukiwań
z Discorda: `discord.max_searches` (25). Każde aktywne wyszukiwanie to 1 zapytanie do Vinted na cykl,
więc dużo wyszukiwań = większe ryzyko 403.

### Wyszukiwania w config.yaml

Opcjonalne (`searches: []` jest OK). Ustaw filtry na vinted.pl w przeglądarce i przepisz ID z URL-a:

```
https://www.vinted.pl/catalog?catalog[]=2994&status_ids[]=6
→ catalog_ids: [2994], status_ids: [6]
```

Inne filtry z URL-a (`color_ids[]`, `material_ids[]`...) → `attributes: {color: [1], material: [44]}`.
Inny kanał niż domyślny: `channel_id: <ID kanału>`. Własny próg wyszukiwania: `deal_threshold: 0.7`.

**Ważne ustawienia wyceny** (sekcja `pricing`): `deal_threshold` (próg, np. 0.7 = co najmniej 30% taniej)
oraz `per_item_max_per_cycle` (limit nowych wycen na cykl). Pozostałe ustawienia próbkowania z Vinted
(`sample_size`, `min_sample`, `per_item_sample`, `per_item_min_sample`, `order`) nie są już używane do wyceny okazji.

**Wskazówki:**

- Ustawiaj kategorię i stan „nowe z metką” – ceny sklepowe dotyczą nowych produktów, więc tylko wtedy porównanie jest uczciwe.
  Dla używanych ustaw `RETAIL_FACTOR` poniżej 1.
- Bot rozpoznaje produkt po tytule oferty. Tytuły ogólnikowe lub w obcym języku często nie dają się wycenić
  i są pomijane – to zamierzone.
- Vinted nie udostępnia cen sprzedaży, a ceny rynkowe to ceny **ofertowe** sklepów, nie faktycznych transakcji.
- Cena na Vinted bywa podana bez kosztów wysyłki i opłaty za ochronę kupujących – brutto różnica może być mniejsza.

## Uruchomienie

### Lokalnie (macOS / Linux / Windows, Python 3.11+)

```
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install beautifulsoup4           # opcjonalnie: selektory CSS dla sklepów

python -m vinted_bot.retail_check "jbl flip 7"  # sprawdź, czy sklepy oddają ceny
python -m vinted_bot --test-notify    # wiadomość testowa na kanał domyślny
python -m vinted_bot --once --dry-run # jeden cykl bez Discorda, okazje tylko w logu
python -m vinted_bot                  # bot + monitor, praca ciągła (Ctrl+C kończy)
```

Opcje: `-c/--config` (ścieżka do configu), `--env` (ścieżka do .env), `--once` (jeden cykl
i koniec), `--dry-run` (bez łączenia z Discordem, nie wymaga tokenu), `--test-notify`.

### Docker / docker compose

```
docker compose up -d --build
docker compose logs -f
```

`config.yaml` jest montowany tylko do odczytu, baza i log trafiają do `./data`.
Po zmianie configu: `docker compose restart`. Po zmianie `.env`: `docker compose up -d`.

### Serwer Linux / Proxmox

**Opcja A – kontener LXC z Dockerem** (np. Debian 12 LXC z włączonym `nesting=1`):
zainstaluj Dockera i postępuj jak wyżej.

**Opcja B – LXC/VM bez Dockera, jako usługa systemd:**

```
apt install -y python3 python3-venv git
useradd -r -m -d /opt/vinted-bot vinted
# skopiuj projekt do /opt/vinted-bot (git clone / scp), potem:
cd /opt/vinted-bot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml && cp .env.example .env   # uzupełnij
chown -R vinted:vinted /opt/vinted-bot

cp deploy/vinted-bot.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now vinted-bot
journalctl -u vinted-bot -f
```

Wystarczy najmniejszy kontener: 1 vCPU, 256 MB RAM.

### Testy

```
pip install -r requirements-dev.txt
pytest
```

## ⚠️ Co może się zepsuć

### Gdy Vinted zmieni API

W kodzie te miejsca są oznaczone komentarzem `⚠️ VINTED-API` (`grep -rn "VINTED-API" vinted_bot`).

| Miejsce | Założenie | Objaw awarii |
| --- | --- | --- |
| `scraper.CATALOG_PATH` + `_default_api_host` | wyszukiwarka pod `https://api.vinted.pl/svc-catalogue/items` | `404 dla /svc-catalogue/items` |
| `scraper.USER_PATH` | profil pod `https://www.vinted.pl/api/v2/users/{id}` | wszystkie okazje odrzucone: „nie udało się pobrać profilu” |
| `scraper._headers` | nagłówki gateway: `Platform: web`, `X-Anon-Id` (z ciasteczka `anon_id`), `Origin` | 401/403 mimo odnowienia sesji |
| `scraper.SESSION_COOKIE` | anonimowy token w ciasteczku `access_token_web` po wejściu na stronę | ostrzeżenie w logu + 401 |
| `scraper._search_params` | filtry jako `attribute_ids[catalog\|size\|brand\|status]=id1,id2` | puste lub niepoprawne wyniki |
| `scraper._parse_item` / `_parse_money` | pola `id`, `title`, `price{amount,currency_code}`, `photos[]`, `user{id,login}`, `url`, `item_box` | „Brak klucza 'items'” lub puste listy ofert |
| `scraper._rating` | `feedback_reputation` 0–1 (×5 = gwiazdki), `feedback_count` | błędne oceny → wszyscy odrzuceni / przepuszczeni |
| `notifier.build_discord_embed` | zdjęcia z CDN `images1.vinted.net` dostępne bez ciasteczek | powiadomienia bez zdjęć |
| `vinted_url.parse_catalog_url` | linki stron: `/catalog?search_text=…&catalog[]=…&size_ids[]=…`, `/catalog/<id>-…`, `/brand/<id>-…` | `/szukaj z-linku` odrzuca linki albo gubi filtry |
| ochrona anty-bot (Cloudflare/DataDome) | `curl_cffi` z `impersonate: chrome` wystarcza | ciągłe 403, HTML zamiast JSON |

**Jak naprawić po zmianie:** otwórz vinted.pl w Chrome → DevTools → Network → filtr `Fetch/XHR` →
wyszukaj coś i znajdź zapytanie zwracające listę ofert. Porównaj URL, parametry, nagłówki
i strukturę JSON z miejscami z tabeli. `python -m vinted_bot --once --dry-run` z `LOG_LEVEL=DEBUG`
szybko pokaże, czy poprawka działa.

**Przy ciągłych 403:** zwiększ odstępy, zmniejsz liczbę wyszukiwań, zaktualizuj `curl_cffi`
(`pip install -U curl_cffi`) albo ustaw `VINTED_PROXY`. Z serwerowego IP (VPS/datacenter) blokady
zdarzają się częściej niż z domowego łącza.

### Gdy sklep zmieni stronę albo zablokuje bota

| Objaw | Co robić |
| --- | --- |
| Log: `[sklep] nie udało się pobrać cen … HTTP 403` | sklep blokuje IP/bota: zmniejsz liczbę sklepów w `RETAIL_SHOPS`, ustaw `RETAIL_PROXY`, zaktualizuj `curl_cffi` |
| `retail_check`: „0 ofert” przy statusie 200 | strona bez JSON-LD: ustaw selektory CSS dla sklepu w `shops.py` (obejrzyj HTML z `--save`) |
| `retail_check`: ofert dużo, „pasujących” 0 | tytuły w sklepie nie zawierają oznaczenia modelu z zapytania; sprawdź `title_query` / `title_matches` w `pricing.py` |
| Bot nic nie wysyła, w logu „brak ceny rynkowej” | żaden sklep nie zwraca cen dla tych produktów; dodaj ręczne ceny w `RETAIL_PRICES_FILE` |
| Adres wyszukiwarki sklepu przestał działać | popraw `search_url` w `shops.py` (wpisz coś w wyszukiwarce sklepu i skopiuj URL) |

## Uwagi

- Bot używa nieoficjalnego API Vinted i pobiera strony sklepów (scraping); korzystanie może naruszać
  regulaminy tych serwisów, a IP może zostać zablokowane. Używaj rozsądnych odstępów i do celów prywatnych.
- Koszt na cykl: 1 zapytanie na wyszukiwanie + po 1 zapytaniu na nowego sprzedającego okazji
  + zapytania do sklepów dla nowych, jeszcze niewycenionych produktów (maks. `per_item_max_per_cycle`
  wycen na cykl; każda wycena to 1 zapytanie na sklep z `RETAIL_SHOPS`, wynik w cache 24 h).
