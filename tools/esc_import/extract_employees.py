"""Extract the ESC staff (salespeople, cashiers, software users) for load_employees.py.

Only runs SELECT queries, and never reads password or PIN columns.

Usage (needs `pip install pymssql`):
    python tools/esc_import/extract_employees.py tools/esc_import/esc_employees.json.gz
"""
import gzip
import json
import re
import sys
from collections import OrderedDict

from extract_products import clean, connect, fetch


def person_key(name):
    name = re.sub(r"\((SOSPESO|DISATTIVATO)\)", "", name or "", flags=re.I)
    return re.sub(r"\s+", " ", name).strip().upper()


def shop_ids(text):
    return sorted({int(i) for i in re.findall(r"\d+", text or "")})


def main(out_path):
    conn = connect()
    cur = conn.cursor()
    people = OrderedDict()

    def person(name):
        key = person_key(name)
        return people.setdefault(key, {
            "key": key, "active": [], "salesperson": False, "badge": "", "salesperson_shops": [],
            "cashier": False, "cashier_shops": [], "user": False})

    for r in fetch(cur, "SELECT DESCRIZIONE, BARCODE, SOSPESO, FILIALI FROM COMMESSI"):
        if not person_key(r["DESCRIZIONE"]):
            continue
        p = person(r["DESCRIZIONE"])
        p.update(salesperson=True, badge=clean(r["BARCODE"]), salesperson_shops=shop_ids(r["FILIALI"]))
        p["active"].append(not r["SOSPESO"])
    for r in fetch(cur, "SELECT CASSIERE, SOSPESO, FILIALI_CONSULTA FROM CASSIERI"):
        if not person_key(r["CASSIERE"]):
            continue
        p = person(r["CASSIERE"])
        p.update(cashier=True, cashier_shops=shop_ids(r["FILIALI_CONSULTA"]))
        p["active"].append(not r["SOSPESO"] and "SOSPESO" not in (r["CASSIERE"] or "").upper())
    for r in fetch(cur, "SELECT NOMINATIVO FROM UTENTI"):
        if not person_key(r["NOMINATIVO"]):
            continue
        p = person(r["NOMINATIVO"])
        p["user"] = True
        p["active"].append("DISATTIVATO" not in (r["NOMINATIVO"] or "").upper())
    conn.close()

    for p in people.values():
        p["active"] = any(p["active"])
    with gzip.open(out_path, "wt", encoding="utf-8") as f:
        json.dump(list(people.values()), f)
    print(f"{len(people)} people ({sum(p['active'] for p in people.values())} active) -> {out_path}")


if __name__ == "__main__":
    main(sys.argv[1])
