"""
Cost model for the vision-AI layer — combines the measured funnel with per-call pricing.

Answers: at N listings/day, how many actually reach the paid AI, and what does that cost?

The per-image cost is built from tokens, not guessed: one listing photo + a short prompt +
a small structured JSON reply. Image token count depends on the resolution we send — we
control that, and Vinted thumbnails are small, so we send modest images deliberately.

Pricing below is per million tokens (published list price). The Batch API is ~50% cheaper
and identification is a background job, not interactive — so batch is the right mode.

NOTE: these are list-price estimates with stated assumptions. Once the client's key is live
we replace the token estimate with a measured count and this becomes exact.
"""

import json
import argparse

# $ per 1M tokens (input, output)
PRICING = {
    "haiku-4.5":  (1.00, 5.00),
    "sonnet-5":   (3.00, 15.00),
    "opus-4.8":   (5.00, 25.00),
}

# Tokens per identification call.
IMAGE_TOKENS = 1_100   # a modest-resolution listing photo (we choose the size we send)
PROMPT_TOKENS = 220    # the identification instruction
OUTPUT_TOKENS = 120    # the small structured JSON identity


def cost_per_call(model: str, batch: bool = True) -> float:
    inp, out = PRICING[model]
    c = (IMAGE_TOKENS + PROMPT_TOKENS) / 1_000_000 * inp + OUTPUT_TOKENS / 1_000_000 * out
    return c * (0.5 if batch else 1.0)


def project(report: dict, listings_per_day: int, model: str, batch: bool = True) -> dict:
    """Apply the measured funnel rates to a daily volume."""
    stage1 = report.get("stage1_pct", 0) / 100
    local = report.get("stage2_local_rate", 0.0)
    distinct = report.get("distinct_ratio", 1.0)

    after_title = listings_per_day * (1 - stage1)
    after_local = after_title * (1 - local)
    paid_calls = after_local * distinct          # dedup: pay per distinct product
    per_call = cost_per_call(model, batch)
    return {
        "listings_per_day": listings_per_day,
        "after_title": round(after_title),
        "after_local_model": round(after_local),
        "paid_ai_calls_per_day": round(paid_calls),
        "per_call_usd": round(per_call, 6),
        "cost_per_day_usd": round(paid_calls * per_call, 2),
        "cost_per_month_usd": round(paid_calls * per_call * 30, 2),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", default="funnel_report.json")
    ap.add_argument("--per-day", type=int, nargs="*", default=[10_000, 100_000])
    args = ap.parse_args()

    with open(args.report, encoding="utf-8") as f:
        report = json.load(f)

    print("=" * 74)
    print("COST PROJECTION — measured funnel × list pricing (Batch API, 50% off)")
    print("=" * 74)
    print(f"  measured on {report['total_listings']} real listings")
    print(f"    title layer resolves      {report.get('stage1_pct', 0)}%")
    if "stage2_local_rate" in report:
        print(f"    local model resolves      {round(100*report['stage2_local_rate'])}% of the rest")
        print(f"    distinct products        {round(100*report['distinct_ratio'])}% of the residual "
              f"(rest are repeats → cache hits)")
    print()
    for n in args.per_day:
        print(f"  ── {n:,} listings/day ──")
        for model in ("haiku-4.5", "sonnet-5", "opus-4.8"):
            p = project(report, n, model)
            print(f"     {model:10}  {p['paid_ai_calls_per_day']:>7,} paid calls/day  "
                  f"${p['per_call_usd']:.5f}/call  →  ${p['cost_per_day_usd']:>8,.2f}/day   "
                  f"${p['cost_per_month_usd']:>9,.2f}/mo")
        print()
    print("  Assumptions: ~1,100 image tokens (modest resolution we control), ~220 prompt,")
    print("  ~120 output; Batch API pricing; steady state (catalog already largely cached).")
    print("  Day-one backfill is a one-off multiple of this, then it drops to the trickle above.")


if __name__ == "__main__":
    main()
