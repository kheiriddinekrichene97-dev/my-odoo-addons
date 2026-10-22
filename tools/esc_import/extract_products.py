"""Extract the ESC product catalogue into a JSON snapshot for load_products.py.

Only runs SELECT queries: the ESC database is never modified.
Connection settings are read from the repository's .env file
(DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD).

Usage (needs `pip install pymssql`):
    python tools/esc_import/extract_products.py tools/esc_import/esc_products.json.gz
"""
import gzip
import json
import re
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

import pymssql

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

# ESC price lists (TAB_LISTINI)
LISTINO_VENDITA = 2   # retail sale price, VAT included
LISTINO_ACQUISTO = 4  # purchase price


def load_env(path):
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def connect():
    env = load_env(ENV_FILE)
    return pymssql.connect(
        server=env["DB_HOST"], port=int(env["DB_PORT"]), database=env["DB_NAME"],
        user=env["DB_USER"], password=env["DB_PASSWORD"],
        login_timeout=20, timeout=600, charset="UTF-8",
    )


def clean(value):
    return re.sub(r"\s+", " ", (value or "").strip())


def jsonable(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def fetch(cur, sql):
    cur.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def extract(cur):
    def lookup(table):
        return {r["ID"]: clean(r["D"]) for r in fetch(cur, f"SELECT ID, DESCRIZIONE D FROM {table}")}

    reparti, famiglie, gruppi = lookup("TAB_REPARTI"), lookup("TAB_FAMIGLIE"), lookup("TAB_GRUPPO")
    colori = lookup("TAB_VARIANTI")

    prices = defaultdict(dict)
    for r in fetch(cur, f"""SELECT CODICE, ID_LISTINO, PREZZO FROM ART_LISTINI
                            WHERE ID_LISTINO IN ({LISTINO_VENDITA}, {LISTINO_ACQUISTO})"""):
        prices[int(r["CODICE"])][r["ID_LISTINO"]] = r["PREZZO"] or 0.0

    articles = {}
    for r in fetch(cur, """SELECT ID, CODART, DESCRIZIONE, MARCA, ID_REPARTO, ID_FAMIGLIA, ID_GRUPPO,
                                  ACQUISTO_PRZ, BENE_SERVIZIO FROM ART_ANAGRAFICA"""):
        art_id = int(r["ID"])
        price = prices.get(art_id, {})
        articles[art_id] = {
            "id": art_id,
            "code": clean(r["CODART"]),
            "name": clean(r["DESCRIZIONE"]) or clean(r["CODART"]),
            "brand": clean(r["MARCA"]),
            "reparto": reparti.get(r["ID_REPARTO"], ""),
            "famiglia": famiglie.get(r["ID_FAMIGLIA"], ""),
            "gruppo": gruppi.get(r["ID_GRUPPO"], ""),
            "service": r["BENE_SERVIZIO"] == 1,
            "list_price": price.get(LISTINO_VENDITA, 0.0),
            "cost": price.get(LISTINO_ACQUISTO) or r["ACQUISTO_PRZ"] or 0.0,
            "variants": {},
        }

    def variant_key(size, colour_id):
        return f"{clean(size) or 'UNICA'}|{colori.get(colour_id) or 'N/D'}"

    # ART_PROGRESSIVI has one row per deposit: group them into one variant per size/colour.
    for r in fetch(cur, """SELECT CODICE, RTRIM(DS_TAGLIA) SIZE, MIN(ORD_TAGL) ORD, ID_COLORE
                           FROM ART_PROGRESSIVI GROUP BY CODICE, RTRIM(DS_TAGLIA), ID_COLORE"""):
        article = articles.get(int(r["CODICE"]))
        if article is not None:
            key = variant_key(r["SIZE"], r["ID_COLORE"])
            size, colour = key.split("|")
            article["variants"].setdefault(key, {
                "size": size, "size_order": r["ORD"] or 0, "colour": colour, "barcodes": [],
            })

    for r in fetch(cur, """SELECT p.CODICE, RTRIM(p.DS_TAGLIA) SIZE, p.ID_COLORE,
                                  RTRIM(b.BARCODE) BARCODE, b.PREDEFINITO
                           FROM ART_BARCODE b JOIN ART_PROGRESSIVI p ON p.ID = b.ID_PRO"""):
        article = articles.get(int(r["CODICE"]))
        barcode = clean(r["BARCODE"])
        variant = article and article["variants"].get(variant_key(r["SIZE"], r["ID_COLORE"]))
        if variant and barcode and barcode not in variant["barcodes"]:
            # The default barcode (PREDEFINITO) goes first
            if r["PREDEFINITO"] == 1:
                variant["barcodes"].insert(0, barcode)
            else:
                variant["barcodes"].append(barcode)

    for article in articles.values():
        article["variants"] = list(article["variants"].values()) or [
            {"size": "UNICA", "size_order": 0, "colour": "N/D", "barcodes": []}]
    return list(articles.values())


def main(out_path):
    conn = connect()
    try:
        articles = extract(conn.cursor())
    finally:
        conn.close()
    n_variants = sum(len(a["variants"]) for a in articles)
    n_barcodes = sum(1 for a in articles for v in a["variants"] if v["barcodes"])
    with gzip.open(out_path, "wt", encoding="utf-8") as f:
        json.dump({"articles": articles}, f, default=jsonable)
    print(f"{len(articles)} articles, {n_variants} variants ({n_barcodes} with barcode) -> {out_path}")


if __name__ == "__main__":
    main(sys.argv[1])
