"""Extract the ESC deposits to import as warehouses, and their stock per variant.

Only runs SELECT queries: the ESC database is never modified.

Usage (needs `pip install pymssql`):
    python tools/esc_import/extract_stock.py tools/esc_import/esc_stock.json.gz
"""
import gzip
import json
import sys

from extract_products import clean, connect, fetch, jsonable

# ESC deposit ID -> (Odoo warehouse name, short code). Names follow production
# ("PV - <shop>" for a shop, "MAG - <shop>" for the stock room behind it).
DEPOSITS = {
    1: ("ING - CONTESSE", "ICONT"),  # production code, di_stock_arrival's default
    3: ("MAG - CENTRALE PISTUNINA", "MCEN"),
    4: ("MAG - GAZZI", "MGAZ"),
    5: ("MERCE NON TROVATA", "MNT"),
    2: ("PV - VIA DEI MILLE", "PVMIL"),
    13: ("PV - GIORDANO BRUNO", "PVGBR"),
    74: ("PV - MD LECCE", "PVLEC"),
    81: ("MAG - LECCE", "MLEC"),
    14: ("PV - MD SANTO STEFANO DI ROGLIANO", "PVSSR"),
    15: ("PV - MONTESILVANO", "PVMON"),
    73: ("MAG - MONTESILVANO", "MMON"),
    12: ("PV - NICOLA FABRIZI", "PVNFA"),
    67: ("MAG - NICOLA FABRIZI", "MNFA"),
    75: ("PV - SILVI", "PVSIL"),
    78: ("MAG - SILVI", "MSIL"),
    11: ("PV - TORRE FARO", "PVTFA"),
    66: ("MAG - TORRE FARO", "MTFA"),
    8: ("PV - TORREGROTTA", "PVTGR"),
    80: ("MAG - TORREGROTTA", "MTGR"),
    10: ("PV - TREMESTIERI", "PVTRE"),
    65: ("MAG - TREMESTIERI", "MTRE"),
    60: ("Z BEST SELLER - TREMESTIERI", "BSTRE"),
    62: ("Z BEST SELLER - NICOLA FABRIZI", "BSNFA"),
}


def main(out_path):
    conn = connect()
    cur = conn.cursor()
    colori = {r["ID"]: clean(r["D"]) for r in fetch(cur, "SELECT ID, DESCRIZIONE D FROM TAB_VARIANTI")}
    ids = ",".join(str(i) for i in DEPOSITS)
    stock, negative = [], 0
    # ART_PROGRESSIVI: one row per article / size / colour / deposit
    for r in fetch(cur, f"""SELECT CODICE, RTRIM(DS_TAGLIA) SIZE, ID_COLORE, DEPOSITO, SUM(ESISTENZA) QTY
                            FROM ART_PROGRESSIVI WHERE DEPOSITO IN ({ids})
                            GROUP BY CODICE, RTRIM(DS_TAGLIA), ID_COLORE, DEPOSITO
                            HAVING SUM(ESISTENZA) <> 0"""):
        if r["QTY"] < 0:
            negative += 1
            continue
        stock.append([int(r["CODICE"]), clean(r["SIZE"]) or "UNICA",
                      colori.get(r["ID_COLORE"]) or "N/D", int(r["DEPOSITO"]), r["QTY"]])
    conn.close()

    deposits = [{"esc_id": esc_id, "name": name, "code": code, "sequence": seq}
                for seq, (esc_id, (name, code)) in enumerate(DEPOSITS.items(), start=1)]
    with gzip.open(out_path, "wt", encoding="utf-8") as f:
        json.dump({"deposits": deposits, "stock": stock}, f, default=jsonable)
    print(f"{len(deposits)} deposits, {len(stock)} stock lines "
          f"({sum(line[4] for line in stock):.0f} pieces), {negative} negative lines skipped -> {out_path}")


if __name__ == "__main__":
    main(sys.argv[1])
