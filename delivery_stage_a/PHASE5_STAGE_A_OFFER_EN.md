# Offer — Phase 5, Stage A: AI Product Identification

**Date:** 3 August 2026
**From:** [Your name]
**To:** leslie570
**Reference:** Vinted Market-Intelligence Tool — Phase 5, Stage A

---

## Scope delivered

Vision-AI product identification, integrated into the existing sales-tracking engine:

- Identifies the exact product from a listing **photo** and generates a complete, specific title.
- **Measured accuracy: 96%** correct product-line identification at **96% coverage** (photo-only,
  title hidden), versus ~73% for the previous local prototype.
- Colour taken from the listing's mandatory colour field, and clothing size from the listing's
  mandatory size field — not guessed from the photo.
- Results written to `product_identities_<product>.csv` and an `ai_product` column in the variant
  report; optional visual HTML reports for review.
- Cost controls: per-listing cache (each product paid for once), demand-first ordering, per-run
  cap, and your own account spend cap.
- Full documentation (English & French) included.

## Price

| Item | Amount |
|---|---|
| Phase 5 — Stage A (fixed price, as agreed) | **$450** |

## Ongoing costs (separate from the build price)

- **AI usage:** billed directly to your own Anthropic API key, under the spend cap you set
  (~$20 to start). Small and capped; scales with new distinct products, not total listings.
- **Continuous running / hosting:** handled as a separate monthly running cost (to be confirmed
  when we set up continuous data collection).

## Not included in Stage A (available separately)

- **Stage B** — generic/no-brand product identification with external-source lookup and full
  multi-brand handling.
- **Automatic Google-verification** of titles (needs a search API key).

## Payment terms

- Amount: **$450**, due on delivery of Stage A.
- Method: [to be completed — e.g. Fiverr / bank transfer].

---

*This offer covers Phase 5, Stage A only. Phase 6 (Autonomous Discovery) will be scoped and
quoted separately once a reliable data history has been collected.*
