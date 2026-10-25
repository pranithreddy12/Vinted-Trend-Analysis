"""Regression checks for the bugs found 2026-10-22. Synthetic data only (no client file is read).
  python test_fixes.py        -> prints 'test_fixes OK' or fails with the broken case."""
import csv, os, subprocess, sys, tempfile

import lookup_product as lp
import merge_repairs as mr
import product_groups as pg
import track_sales as ts

D = tempfile.mkdtemp()


def row(i, title="Adidas Samba", color="Blanc", size="37", status="sold", seen="2026-09-26 10:00:00", **kw):
    r = {k: "" for k in ts.TRACK_FIELDS}
    r.update(id=str(i), title=title, brand="Adidas", color=color, size=size, status=status, price="20",
             category="Baskets", category_path="Femmes > Chaussures > Baskets", first_seen="2026-09-25 10:00:00",
             last_seen=seen, sale_confirmed=status == "sold")
    r.update(kw)
    return r


def write(name, rows):
    p = os.path.join(D, name)
    ts.save_tracking(p, {r["id"]: r for r in rows})
    return p


# lookup: colour in English or French and a size are matched on the variant; singular finds plural
f = write("w.csv", [row(1), row(2, color="Noir", size="38"), row(3, title="Tasses Tupperware", status="sold",
                                                                   brand="Tupperware", category="Tasses", category_path="Maison > Tasses")])
res, unid, span, now = lp.lookup("adidas samba white", [f])
assert len(res) == 1 and len(res[0]["sold"]) == 1, res
assert len(lp.lookup("adidas samba blanc 37", [f])[0][0]["sold"]) == 1
assert lp.lookup("adidas samba white 99", [f])[0] == []
assert len(lp.lookup("adidas samba", [f])[0][0]["sold"]) == 2            # no colour given: both colours, one product
assert lp.lookup("tasse", [f])[0] != [] and lp.lookup("tasses", [f])[0] != []
assert lp.lookup("de la", [f]) == []                                      # stop words only: refused, not 'everything'
assert lp.lookup("adidas", [os.path.join(D, "nope.csv")]) is None         # missing file: no crash
out = subprocess.run([sys.executable, "lookup_product.py", "adidas", "--files", os.path.join(D, "nope.csv")],
                     capture_output=True, text=True)
assert out.returncode != 0 and "Traceback" not in out.stderr, out.stderr

# brand aliases and the monthly estimate (file covers 14 days: 10 sales = 21.4 a month, not 10)
assert pg._brand_of({"brand": "Nike Air"}) == "nike" and pg._brand_of({"brand": "Adidas Stan Smith"}) == "adidas"
rows = [row(i, seen=f"2026-10-0{1 + i % 9} 10:00:00", first_seen="2026-09-25 10:00:00") for i in range(10, 20)]
p = pg.rank_products(rows, min_sold=3)[0][0]
assert abs(p["est_sales_per_month"] - 10 * 30 / 14) < 0.2, p   # file covers 14 days: scaled up, not left at 10

# a confirmed sale re-opened for repair whose page does not load keeps its flag (verify_disappearances)
g = write("g.csv", [row(50)])
tr = ts.load_tracking(g)
ts._verify_sold_worker = lambda iid: (iid, "disappeared")
ts.verify_disappearances(tr, g, ["50"])
assert tr["50"]["status"] == "sold" and str(tr["50"]["sale_confirmed"]) == "True", tr["50"]

# merge: page details and verified outcomes are added, nothing is lost, new rows survive
cur = {r["id"]: r for r in [row(1, status="disappeared", category="", category_path="", color="", size="", sale_confirmed=""),
                            row(2, status="sold"), row(9, status="active")]}
rep = {r["id"]: r for r in [row(1, status="sold"), row(2, status="removed"), row(3)]}
n = mr.merge(cur, rep)
assert set(cur) == {"1", "2", "9"} and n["only_in_repaired"] == 1
assert cur["1"]["status"] == "sold" and cur["1"]["category"] == "Baskets" and cur["1"]["size"] == "37"
assert cur["2"]["status"] == "sold"            # a sale is never downgraded
assert cur["9"]["status"] == "active"          # a row only the tracker knows is untouched

# check_models: a limited-edition collaboration is another product, even with a space or a typo in its name
import check_model_sales as cms
s = write("s.csv", [row(60 + i, title=t, brand="Stanley", color="Rose", size="", category="Gourdes", seen="2099-01-01 10:00:00")
                    for i, t in enumerate(["Stanley Quencher H2.0 40oz", "Stanley x LoveShack Fancy Quencher 40oz",
                                           "Stanley Quencher LoveShakeFancy 40oz"])])
assert cms.check(s, "stanley", line="Quencher", hours=1e6)["sold"] == 3
assert cms.check(s, "stanley", line="Quencher", hours=1e6, exclude="loveshack, loveshake")["sold"] == 1
# model names learned from a brand's own titles; a listing without one is not counted (client 2026-10-24)
jr = [row(100 + i, title=t, brand="Jellycat", color="Marron", size="", category="Peluches", category_path="Enfants > Jouets")
      for i, t in enumerate(["Jellycat Amuseable Croissant"] * 12 + ["Jellycat Bashful Bunny"] * 12 + ["Jellycat peluche"] * 16)]
nm = pg.model_namer(jr)
assert nm(jr[0]) == "Amuseable Croissant" and nm(jr[12]) == "Bashful Bunny" and nm(jr[30]) == "", (nm(jr[0]), nm(jr[12]), nm(jr[30]))
assert nm(row(1, brand="Zara")) is None and pg.model_namer(jr[:5]).active is False   # small brand / small file: old grouping
jt = {r["id"]: r for r in jr}
for r in jt.values():
    r.update(size="Prématuré, jusqu'à 44cm", first_seen="2026-09-25 10:00:00")
vs, _ = ts.variant_analysis(jt, slug="jellycat_test")
assert {v["variant"] for v in vs} == {"amuseable croissant brown", "bashful bunny brown"}, [v["variant"] for v in vs]
assert vs[0]["product"] in ("Jellycat Amuseable Croissant Brown", "Jellycat Bashful Bunny Brown"), vs[0]["product"]

# photo AI: fills a missing brand / model only, never overrides the listing, never low confidence or accessories
import json
cd = os.path.join(D, "cache"); os.makedirs(cd)
json.dump({"1": {"brand": "Adidas", "product_line": "Samba OG", "confidence": "medium"},
           "2": {"brand": "Adidas", "product_line": "Samba OG", "confidence": "low"},
           "3": {"brand": "Adidas", "product_line": "Samba OG", "confidence": "high", "is_accessory": True}},
          open(os.path.join(cd, "vision_t.json"), "w"))
ai = [row(i, title="Baskets blanches", brand="Pas de marque", color="Blanc", size="38") for i in (1, 2, 3, 4)]
assert pg.attach_ai(ai, cd) == 1 and ai[0]["ai_brand"] == "Adidas" and "ai_brand" not in ai[1]
assert pg.identified_key(ai[0]) == ("adidas", "shoes", "samba og"), pg.identified_key(ai[0])
assert pg.identified_key(ai[1]) is None and pg.identified_key(ai[2]) is None and pg.identified_key(ai[3]) is None
own = row(5, title="Nike Pegasus 40 blanc", brand="Nike", color="Blanc", size="38"); own.update(ai_brand="Adidas", ai_model="Samba")
assert pg.identified_key(own)[0] == "nike" and "pegasus" in pg.identified_key(own)[2]   # the listing's own data wins
print("test_fixes OK")
