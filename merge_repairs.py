"""Apply the page-verified repairs of a repaired tracking file to a CURRENT tracking file, by listing id.

  python merge_repairs.py repaired.csv tracking/smeg_tumbler.csv

Why not just replace the file: the repaired copy is a snapshot; the tracker has collected new rows since.
This only ever ADDS knowledge, and never deletes or downgrades anything:
  * a row with no Vinted category yet takes brand, size, colour, category and category_path read on the item page;
  * a row still 'disappeared' takes the verified outcome (sold / removed) from the repaired file;
  * a posting time is filled only when missing;
  * rows only in the current file are untouched; rows only in the repaired file are not added.
The current file is first copied to <file>.before_merge.csv.
"""
import shutil
import sys

import track_sales as ts

PAGE_FIELDS = ("brand", "size", "color", "category", "category_path")


def merge(cur: dict, rep: dict) -> dict:
    n = dict.fromkeys(("page_fields", "sold", "removed", "posting_time", "only_in_repaired"), 0)
    n["only_in_repaired"] = len(set(rep) - set(cur))
    for i, row in cur.items():
        r = rep.get(i)
        if not r:
            continue
        if r.get("category") and not row.get("category"):
            for k in PAGE_FIELDS:
                row[k] = r.get(k, "")
            row["variant"] = ts.build_variant(row.get("title", ""), row.get("brand", ""), row.get("color", ""), row.get("size", ""))
            n["page_fields"] += 1
        if row.get("status") == "disappeared" and r.get("status") in ("sold", "removed"):
            row["status"], row["sale_confirmed"] = r["status"], r["status"] == "sold"
            n[r["status"]] += 1
        if r.get("created_at_ts") and not row.get("created_at_ts"):
            row["created_at_ts"] = r["created_at_ts"]
            n["posting_time"] += 1
    return n


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    rep_path, cur_path = sys.argv[1:]
    cur, rep = ts.load_tracking(cur_path), ts.load_tracking(rep_path)
    if not cur or not rep:
        sys.exit("One of the two files is empty or missing: nothing was changed.")
    shutil.copyfile(cur_path, cur_path.rsplit(".", 1)[0] + ".before_merge.csv")
    n = merge(cur, rep)
    ts.save_tracking(cur_path, cur)
    print(f"{cur_path}: {len(cur)} rows kept. page details added to {n['page_fields']}, "
          f"confirmed sold {n['sold']}, removed {n['removed']}, posting time {n['posting_time']}; "
          f"{n['only_in_repaired']} rows only in the repaired file were ignored.")
