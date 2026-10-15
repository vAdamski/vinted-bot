# Tryby oceny ofert

Tryb decyduje tylko o jednym: **kiedy nowa oferta zasługuje na powiadomienie**. Pobieranie ofert, filtry, sprawdzanie sprzedającego i wysyłka działają tak samo we wszystkich trybach.

## Wspólny przebieg cyklu (co 30–60 s, dla każdego aktywnego wyszukiwania)

1. **Pobranie:** bot bierze 30 najnowszych ofert spełniających wszystkie filtry wyszukiwania: frazę, kategorię, rozmiar, markę, stan i ceny (`cena_min`/`cena_max`).
2. **Nowe oferty:** odrzuca te, które już widział. Przy pierwszym uruchomieniu wyszukiwania (także po dodaniu i po `/szukaj wznow`) tylko zapisuje obecne oferty, bez powiadomień.
3. **Ocena według trybu:** opisana niżej.
4. **Sprzedający:** jeśli oferta przeszła ocenę, bot sprawdza ocenę sprzedającego (min. 4,5) i liczbę opinii (min. 5). Profil trzyma w pamięci przez 12 h.
5. **Wysyłka:** powiadomienie idzie na kanał wyszukiwania.
6. **Oznaczenie:** każda nowa oferta zostaje oznaczona jako widziana, niezależnie od wyniku. Bot nie wraca do niej w kolejnych cyklach.

Ważne: `cena_min`/`cena_max` zawężają tylko **monitorowane** oferty. Nigdy nie wpływają na próbkę, z której liczona jest mediana.

## Wybór trybu w komendach

`/szukaj dodaj` i `/szukaj z-linku` mają opcję `tryb` z trzema wartościami:

| Etykieta w Discordzie | W YAML (`mode:`) |
|---|---|
| okazje (mediana frazy) | `deals` |
| wszystkie nowe oferty | `all` |
| okazje (mediana z tytułu oferty) | `per_item` |

**Jeśli nie wybierzesz trybu:** z frazą bot użyje `deals`, bez frazy `all`. W `/szukaj z-linku` frazą jest `search_text` z linku. Link z samą kategorią daje więc tryb „wszystkie”.

**`prog`** (np. 70 = 70%) działa w trybach `deals` i `per_item`. W trybie `all` jest ignorowany. Bez `prog` bot bierze `deal_threshold` z configu (75%).

**Trybu nie da się zmienić** w istniejącym wyszukiwaniu. Trzeba zrobić `/szukaj usun` i dodać je ponownie.

## Tryb `deals`: okazje według mediany frazy

**Jak liczy medianę:**
- bierze 96 najnowszych ofert dla tej samej frazy i filtrów, ale bez limitów ceny;
- odrzuca wartości skrajne (metoda IQR);
- liczy medianę;
- przelicza ją raz na 60 minut, a nie co cykl.

**Kiedy jest powiadomienie:** gdy cena oferty ≤ `prog` × mediana. Na przykład przy medianie 400 zł i progu 75% okazją jest wszystko do 300 zł. Porównywana jest cena bez opłaty za Ochronę Kupujących.

**Gdy próbka jest za mała** (mniej niż 15 ofert po odrzuceniu skrajnych): bot nie ocenia żadnej oferty z tego wyszukiwania do następnego przeliczenia.

**Przy dodawaniu** bot pokazuje medianę i cenę graniczną, np. „okazja poniżej 300 zł”. Ostrzega też, jeśli wybrano `deals` bez frazy.

**Powiadomienie:** 🔥, cena, mediana, „% taniej”.

**Do czego:** konkretny produkt („lego technic 42115”, „air max 90” + rozmiar). Do całej kategorii się nie nadaje, bo mediana miesza różne przedmioty.

## Tryb `all`: wszystkie nowe oferty

**Jak działa:** bot niczego nie wycenia. Każda nowa oferta, która spełnia filtry i ma zaufanego sprzedającego, trafia na kanał.

**Koszt:** zero dodatkowych zapytań do Vinted, poza profilem sprzedającego.

**Przy dodawaniu** bot pokazuje tylko, ile ofert pasuje teraz do filtrów.

**Powiadomienie:** 🆕, inny kolor, bez mediany i bez „% taniej”.

**Do czego:** „chcę widzieć wszystko z Elektroniki od 200 zł, nowe z metką”. Selekcję robisz sam, patrząc na kanał. Liczbę powiadomień regulujesz filtrami, np. wyższą `cena_min` albo węższą podkategorią.

## Tryb `per_item`: okazje według mediany z tytułu oferty

Tu bot wycenia każdą nową ofertę osobno, krok po kroku:

1. **Zapytanie z tytułu.** Tytuł zamienia na zapytanie: małe litery, bez słów typu „nowy/new/nou/sprzedam/z metką”, pierwsze 5 słów. Przykład: „Głośnik JBL Flip 7 NOWY!” → „głośnik jbl flip 7”.
2. **Wyszukiwanie.** Szuka tego zapytania w kategorii, marce i stanie z wyszukiwania, bez rozmiaru i bez limitów ceny. Pobiera 96 ofert.
3. **Filtr trafności.** Zostają tylko oferty z pasującym tytułem:
   - muszą się zgadzać wszystkie oznaczenia modelu z cyframi (`s9`, `xm5`; „flip 7” pasuje też do „Flip7”);
   - musi się zgadzać co najmniej 70% pozostałych słów;
   - akcesoria (etui, ładowarka, kabel, głowica, pudełko…) wypadają, chyba że samo zapytanie ich dotyczy;
   - pomijana jest też sama oceniana oferta.
4. **Mediana.** Z pasujących ofert bot liczy medianę z odrzuceniem skrajnych wartości. Potrzeba **co najmniej 8** pasujących ofert, inaczej oferta jest pomijana bez powiadomienia, a nie zgadywana.
5. **Ocena.** Okazja, gdy cena ≤ `prog` × ta mediana.

**Limity i koszty:**
- Każda nowa wycena to jedno zapytanie do Vinted, z pauzą 1–2,5 s między nimi.
- Wyniki dla tego samego zapytania z tytułu są pamiętane przez 60 min.
- Maksymalnie **10 nowych wycen na wyszukiwanie w jednym cyklu** (`per_item_max_per_cycle`). Oferty ponad limit są pomijane i oznaczane jako widziane, więc **nie wrócą** w kolejnym cyklu.

**Przy dodawaniu** bot nie pokazuje mediany, tylko opis zasady, bo każda oferta ma własną.

**Powiadomienie:** 🔥, pole „Mediana podobnych (≈, n=11)” z liczbą ofert, z których ją policzono, i w stopce zapytanie, np. „wycena z tytułu: glosnik jbl flip 7”.

**Do czego:** kategoria, w której chcesz tylko okazje. W praktyce działa dla popularnych modeli. W teście na Elektronice wycenę dostała 1 oferta z 10. Reszta to rzadkie modele albo tytuły po fińsku czy rumuńsku.

## Porównanie

| | `deals` | `all` | `per_item` |
|---|---|---|---|
| Mediana | jedna dla wyszukiwania | brak | osobna dla każdej oferty |
| Działa `prog` | tak | nie | tak |
| Dodatkowe zapytania | ~1–2 na godzinę | 0 | do 10 na cykl |
| Liczba powiadomień | mało, tylko okazje | dużo, wszystko z filtrów | bardzo mało, tylko pewne okazje |
| Ryzyko | zła mediana przy szerokiej frazie | spam przy szerokich filtrach | pomija nieznane modele i oferty ponad limit |

## Przykład: Elektronika, od 200 zł, nowe z metką

- `all`, jeśli chcesz widzieć wszystko i sam wybierać;
- `per_item`, jeśli wolisz rzadkie, ale wiarygodne okazje i zgadzasz się, że większość ofert nie zostanie oceniona.

Można też dodać obie wersje jako dwa osobne wyszukiwania na różnych kanałach:

```
/szukaj z-linku url:https://www.vinted.pl/catalog?catalog[]=2994&price_from=200&status_ids[]=6 nazwa:Elektronika – wszystkie tryb:wszystkie nowe oferty kanal:#elektronika-wszystko
/szukaj z-linku url:https://www.vinted.pl/catalog?catalog[]=2994&price_from=200&status_ids[]=6 nazwa:Elektronika – okazje tryb:okazje (mediana z tytułu oferty) kanal:#elektronika-okazje
```

Nazwy kanałów są przykładowe – wybierz własne kanały w opcji `kanal`.
