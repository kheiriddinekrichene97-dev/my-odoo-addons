"""Extract the ESC sale price lists used by the di_* modules' pricelists.

Only runs SELECT queries: the ESC database is never modified.

    ESC list (TAB_LISTINI)        Odoo pricelist
    2  VENDITA                    Dettaglio (= the product's Sales Price)
    3  VENDITA INGROSSO           Ingrosso
    1  VENDITA SCONTATO           Promo

Usage (needs `pip install pymssql`):
    python tools/esc_import/extract_pricelists.py tools/esc_import/esc_pricelists.json.gz
"""
import gzip
import json
import sys
from collections import defaultdict

from extract_products import connect, fetch, jsonable

LISTS = {1: "promo", 2: "dettaglio", 3: "ingrosso"}


def main(out_path):
    conn = connect()
    prices = defaultdict(dict)
    for r in fetch(conn.cursor(), f"""SELECT CODICE, ID_LISTINO, PREZZO FROM ART_LISTINI
                                      WHERE ID_LISTINO IN ({','.join(map(str, LISTS))}) AND PREZZO > 0"""):
        prices[int(r["CODICE"])][LISTS[r["ID_LISTINO"]]] = r["PREZZO"]
    conn.close()
    with gzip.open(out_path, "wt", encoding="utf-8") as f:
        json.dump({str(k): v for k, v in prices.items()}, f, default=jsonable)
    counts = {name: sum(1 for p in prices.values() if name in p) for name in LISTS.values()}
    print(f"{len(prices)} articles with prices {counts} -> {out_path}")


if __name__ == "__main__":
    main(sys.argv[1])
