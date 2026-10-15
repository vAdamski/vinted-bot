Chcę zbudować bota w Pythonie, który monitoruje Vinted i wysyła mi powiadomienia na Telegram, gdy znajdzie okazję.

Wymagania:
1. Vinted nie ma publicznego API, więc bot ma korzystać z wewnętrznego API strony (/api/v2/catalog/items), z sesją i ciasteczkami jak w przeglądarce (httpx lub curl_cffi).
2. Co 30–60 s (losowy odstęp) sprawdza najnowsze oferty dla zadanych fraz wyszukiwania i filtrów (cena maksymalna, kategoria, rozmiar).
3. SQLite przechowuje ID już widzianych ofert, żeby nie wysyłać duplikatów.
4. Wycena rynkowa: dla każdej frazy pobiera 50–100 ofert, liczy medianę ceny (z odrzuceniem wartości skrajnych). Oferta to okazja, jeśli cena ≤ 75% mediany (próg ma być w configu).
5. Weryfikacja sprzedającego: pobiera ocenę i liczbę opinii z profilu sprzedającego. Odrzuca ofertę, jeśli ocena < 4.5 lub opinii < 5 (progi w configu).
6. Powiadomienie na Telegram (Bot API) ze zdjęciem, tytułem, ceną, medianą rynkową, % taniej, oceną sprzedającego i linkiem do oferty.
7. Obsługa błędów 403/429 (backoff, ponowienie), logowanie, konfiguracja w pliku YAML/.env.

Chcę działać za darmo na własnym komputerze / apliakcja docker / linux server (proxmox).

Napisz kompletny, działający kod z podziałem na moduły (scraper, wycena, filtr sprzedającego, notifier, main), instrukcją instalacji i przykładowym configiem. Zaznacz miejsca, które mogą się zepsuć, jeśli Vinted zmieni API.

