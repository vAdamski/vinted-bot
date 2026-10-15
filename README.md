# Vinted deal bot

Bot w Pythonie, który co 30–60 s sprawdza najnowsze oferty na Vinted dla zadanych fraz,
porównuje ich cenę z medianą rynkową, sprawdza sprzedającego i wysyła okazje na Discorda
(zdjęcie, tytuł, cena, mediana, % taniej, ocena sprzedającego, link). Wyszukiwania można
dodawać, wstrzymywać i usuwać slash-komendami na Discordzie.

```
vinted_bot/
├── config.py         # config.yaml + .env → dataclasses
├── scraper.py        # klient wewnętrznego API Vinted (curl_cffi, sesja, retry/backoff)
├── pricing.py        # mediana z odrzuceniem wartości skrajnych (IQR), cache
├── seller_filter.py  # ocena i liczba opinii sprzedającego (cache w SQLite)
├── storage.py        # SQLite: widziane oferty, stan fraz, cache sprzedających, wyszukiwania z Discorda
├── search_service.py # wyszukiwania z config.yaml + z bazy, walidacja (bez zależności od Discorda)
├── vinted_url.py     # link wyszukiwania z vinted.pl → filtry
├── discord_bot.py    # bot Discord: komendy /szukaj, start wątku monitora
├── notifier.py       # wysyłka okazji przez bota (embed ze zdjęciem) / do logu przy --dry-run
└── main.py           # pętla monitora, CLI
```

## Jak to działa

1. **Sesja** – `curl_cffi` podszywa się pod Chrome (odcisk TLS/HTTP2), wchodzi na stronę główną
   i dostaje ciasteczka (`access_token_web`, `anon_id`, `__cf_bm`...). Przy 401/403 sesja jest odnawiana.
2. **Monitorowanie** – co `min_interval`–`max_interval` sekund (losowo) pobiera `per_page`
   najnowszych ofert dla każdej frazy (z filtrami: cena, kategoria, rozmiar, marka, stan).
3. **Deduplikacja** – ID widzianych ofert są w SQLite. Przy pierwszym uruchomieniu frazy obecne
   oferty są tylko zapisywane (bez powiadomień), chyba że `notify_on_first_run: true`.
4. **Wycena** – dla frazy pobierana jest próbka `sample_size` ofert **bez filtra ceny**
   (inaczej mediana byłaby zaniżona), odrzucane są wartości spoza `[Q1 − 1.5·IQR, Q3 + 1.5·IQR]`,
   liczona mediana. Wynik jest cache’owany przez `cache_ttl_minutes`.
   Oferta jest okazją, gdy `cena ≤ deal_threshold × mediana`.
5. **Sprzedający** – pobierany profil (`/api/v2/users/{id}`); odrzucenie gdy ocena < `min_rating`
   albo opinii < `min_reviews`. Profil cache’owany w SQLite (`cache_ttl_hours`).
6. **Discord** – bot wysyła embed z tytułem-linkiem, zdjęciem, ceną, medianą, % taniej
   i sprzedającym na kanał przypisany do wyszukiwania (albo domyślny). Komendy `/szukaj`
   działają w tym samym procesie; zmiany obowiązują od następnego cyklu, bez restartu.
7. **Błędy** – 401/403: odnowienie sesji + backoff; 429: `Retry-After` lub backoff wykładniczy;
   5xx/sieć: backoff. Błąd jednej frazy nie zatrzymuje bota.

## Konfiguracja

```bash
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
   - prawy klik na serwer → *Kopiuj ID serwera* → `DISCORD_GUILD_ID`
     (komendy pojawią się od razu; bez tego Discord rejestruje je nawet do godziny),
   - prawy klik na kanał → *Kopiuj ID kanału* → `DISCORD_CHANNEL_ID` (kanał domyślny na okazje).
5. Test: `python -m vinted_bot --test-notify`.

Token bota to sekret – kto go zna, może przejąć bota.

### Komendy

Dostępne tylko dla osób z uprawnieniem *Zarządzanie serwerem* (zmienisz to w
*Ustawienia serwera → Integracje → Vinted bot*).

| Komenda | Opis |
|---|---|
| `/szukaj z-linku url nazwa [prog] [kanal]` | **Najprościej:** ustaw filtry na vinted.pl, skopiuj adres strony z wynikami i wklej. |
| `/szukaj dodaj nazwa fraza [cena_max] [cena_min] [prog] [kategorie] [rozmiary] [marki] [stany] [kanal]` | Ręcznie; ID podawaj po przecinku, `prog` w % mediany (np. 70). |
| `/szukaj lista` | Wszystkie wyszukiwania (🔒 z config.yaml, ▶️/⏸️ z Discorda) i liczba zapytań na cykl. |
| `/szukaj pauza nazwa` / `/szukaj wznow nazwa` | Wstrzymanie bez usuwania. Po wznowieniu oferty z czasu pauzy nie są wysyłane. |
| `/szukaj usun nazwa` | Usunięcie (tylko wyszukiwań dodanych z Discorda). |

Przy dodawaniu bot robi próbne wyszukiwanie i pokazuje liczbę ofert oraz medianę. Jeśli nic
nie znajdzie, nie zapisze wyszukiwania. Powiadomienia trafiają na kanał, na którym wpisano
komendę (albo wskazany w `kanal`). Limit wyszukiwań z Discorda: `discord.max_searches` (25).
Każde aktywne wyszukiwanie to 1 zapytanie do Vinted na cykl, więc dużo wyszukiwań = większe ryzyko 403.

### Wyszukiwania w config.yaml

Opcjonalne (`searches: []` jest OK). Ustaw filtry na vinted.pl w przeglądarce i przepisz ID z URL-a:

```
https://www.vinted.pl/catalog?search_text=air%20max%2090&catalog[]=1242&size_ids[]=784&status_ids[]=2
→ query: "air max 90", catalog_ids: [1242], size_ids: [784], status_ids: [2]
```

Inne filtry z URL-a (`color_ids[]`, `material_ids[]`...) → `attributes: {color: [1], material: [44]}`.
Inny kanał niż domyślny: `channel_id: <ID kanału>`.

**Wskazówki do wyceny:**
- Im bardziej konkretna fraza i filtry, tym sensowniejsza mediana. „lego technic” miesza luźne
  klocki z dużymi zestawami; „lego technic 42115” z kategorią daje dużo lepszą bazę.
- Ustawiaj kategorię i rozmiar – inaczej buty dziecięce zaniżą medianę dla dorosłych.
- `price_max` filtruje tylko monitorowane oferty, nie próbkę do wyceny.
- Vinted nie udostępnia cen sprzedaży – mediana liczona jest z **cen wystawienia**.
- Próbka do wyceny domyślnie bierze najnowsze oferty (`order: newest_first`). Sortowanie
  `relevance` w testach dawało nawet 2× wyższą medianę (promowane/droższe oferty na górze).

## Uruchomienie

### Lokalnie (macOS / Linux / Windows, Python 3.11+)

```bash
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python -m vinted_bot --test-notify    # wiadomość testowa na kanał domyślny
python -m vinted_bot --once --dry-run # jeden cykl bez Discorda, okazje tylko w logu
python -m vinted_bot                  # bot + monitor, praca ciągła (Ctrl+C kończy)
```

Opcje: `-c/--config` (ścieżka do configu), `--env` (ścieżka do .env), `--once` (jeden cykl
i koniec), `--dry-run` (bez łączenia z Discordem, nie wymaga tokenu), `--test-notify`.

### Docker / docker compose

```bash
docker compose up -d --build
docker compose logs -f
```

`config.yaml` jest montowany tylko do odczytu, baza i log trafiają do `./data`.
Po zmianie configu: `docker compose restart`.

### Serwer Linux / Proxmox

**Opcja A – kontener LXC z Dockerem** (np. Debian 12 LXC z włączonym `nesting=1`):
zainstaluj Dockera i postępuj jak wyżej.

**Opcja B – LXC/VM bez Dockera, jako usługa systemd:**

```bash
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

```bash
pip install -r requirements-dev.txt
pytest
```

## ⚠️ Co może się zepsuć, gdy Vinted zmieni API

W kodzie te miejsca są oznaczone komentarzem `⚠️ VINTED-API` (`grep -rn "VINTED-API" vinted_bot`).

| Miejsce | Założenie | Objaw awarii |
|---|---|---|
| `scraper.CATALOG_PATH` + `_default_api_host` | wyszukiwarka pod `https://api.vinted.pl/svc-catalogue/items` | `404 dla /svc-catalogue/items` |
| `scraper.USER_PATH` | profil pod `https://www.vinted.pl/api/v2/users/{id}` | wszystkie okazje odrzucone: „nie udało się pobrać profilu” |
| `scraper._headers` | nagłówki gateway: `Platform: web`, `X-Anon-Id` (z ciasteczka `anon_id`), `Origin` | 401/403 mimo odnowienia sesji |
| `scraper.SESSION_COOKIE` | anonimowy token w ciasteczku `access_token_web` po wejściu na stronę | ostrzeżenie w logu + 401 |
| `scraper._search_params` | filtry jako `attribute_ids[catalog|size|brand|status]=1,2`; `price_to`, `order=newest_first` | filtry ignorowane (oferty spoza kategorii/rozmiaru) |
| `scraper._parse_item` / `_parse_money` | pola `id`, `title`, `price{amount,currency_code}`, `photos[]`, `user{id,login}`, `url`, `item_box` | „Brak klucza 'items'” lub puste listy ofert |
| `scraper._rating` | `feedback_reputation` 0–1 (×5 = gwiazdki), `feedback_count` | błędne oceny → wszyscy odrzuceni / przepuszczeni |
| `notifier.build_discord_embed` | zdjęcia z CDN `images1.vinted.net` dostępne bez ciasteczek | powiadomienia bez zdjęć |
| `vinted_url.parse_catalog_url` | linki stron: `/catalog?search_text=…&catalog[]=…&size_ids[]=…`, `/catalog/<id>-…`, `/brand/<id>-…` | `/szukaj z-linku` odrzuca linki albo gubi filtry |
| ochrona anty-bot (Cloudflare/DataDome) | `curl_cffi` z `impersonate: chrome` wystarcza | ciągłe 403, HTML zamiast JSON |

To nie jest teoria: przy pisaniu bota (10.2026) stary endpoint `/api/v2/catalog/items` zwracał już 404,
a wyszukiwarka działała pod nowym adresem z nowym formatem filtrów i ofert.

**Jak naprawić po zmianie:** otwórz vinted.pl w Chrome → DevTools → Network → filtr `Fetch/XHR` →
wyszukaj coś i znajdź zapytanie zwracające listę ofert. Porównaj URL, parametry, nagłówki
i strukturę JSON z miejscami z tabeli. Jeśli `/szukaj z-linku` przestanie rozumieć linki,
sprawdź format adresu w `vinted_url.py`. `python -m vinted_bot --once --dry-run` z `LOG_LEVEL=DEBUG`
szybko pokaże, czy poprawka działa.

**Przy ciągłych 403:** zwiększ odstępy, zmniejsz liczbę fraz, zaktualizuj `curl_cffi`
(`pip install -U curl_cffi`; nowsze profile przeglądarek) albo ustaw `VINTED_PROXY`.
Z serwerowego IP (VPS/datacenter) blokady zdarzają się częściej niż z domowego łącza.

## Uwagi

- Bot używa nieoficjalnego API; korzystanie może naruszać regulamin Vinted. Używaj rozsądnych
  odstępów i do celów prywatnych.
- Koszt na cykl: 1 zapytanie na frazę + (co `cache_ttl_minutes`) 1–2 zapytania wyceny
  + po 1 zapytaniu na nowego sprzedającego okazji.
