"""Diagnostyka cen sklepowych: co zwraca każdy sklep dla danego zapytania.

    python -m vinted_bot.retail_check "jbl flip 7"
    python -m vinted_bot.retail_check "sony wh-1000xm5" --shops ceneo,xkom --save

--save zapisuje pobrany HTML do data/retail_debug_<sklep>.html (do podejrzenia selektorów CSS).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .pricing import robust_median, title_matches, title_query
from .shops import DEFAULT_SHOPS, SHOPS, ShopFetcher, extract_items


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vinted_bot.retail_check", description=__doc__.splitlines()[0])
    parser.add_argument("query", help="np. 'jbl flip 7' albo pełny tytuł oferty z Vinted")
    parser.add_argument("--shops", default=",".join(DEFAULT_SHOPS), help=f"dostępne: {', '.join(SHOPS)}")
    parser.add_argument("--save", action="store_true", help="zapisz HTML do data/retail_debug_<sklep>.html")
    args = parser.parse_args(argv)

    query = title_query(args.query) or args.query
    print(f"Zapytanie: „{query}”\n")
    fetcher, all_prices = ShopFetcher(), []
    for name in (n.strip() for n in args.shops.split(",") if n.strip()):
        shop = SHOPS.get(name)
        if shop is None:
            print(f"[{name}] nieznany sklep (dostępne: {', '.join(SHOPS)})")
            continue
        try:
            status, html = fetcher.get(shop, query)
        except Exception as e:  # noqa: BLE001
            print(f"[{name}] błąd połączenia: {e}")
            continue
        if args.save:
            out = Path("data") / f"retail_debug_{name}.html"
            out.parent.mkdir(exist_ok=True)
            out.write_text(html, encoding="utf-8")
        items = extract_items(html, shop) if status < 400 else []
        matched = [(t, p) for t, p in items if title_matches(query, t)]
        print(f"[{name}] HTTP {status}, {len(html)//1024} KB, ofert znalezionych: {len(items)}, pasujących: {len(matched)}")
        for t, p in matched[:5]:
            print(f"      {p:>9.2f}  {t[:70]}")
        if status < 400 and not items:
            print("      → 0 ofert: strona bez JSON-LD albo zablokowana. Ustaw selektory CSS w shops.py "
                  "(uruchom z --save i obejrzyj zapisany HTML).")
        all_prices += [p for _, p in matched[:5]]

    result = robust_median(all_prices)
    print()
    if result:
        print(f"Mediana z {len(all_prices)} cen: {result[0]:.2f} (po odrzuceniu skrajnych: {len(result[1])})")
    else:
        print("Brak cen do wyceny.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
