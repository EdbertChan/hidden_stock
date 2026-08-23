---
name: principle-assert-invariants-not-last-bug
description: >-
  After a holdings/data bug ships past visual QA or a PASS grade: assert the
  class of failure (no invent, uniqueness, provenance, exits) instead of
  patching only the named ticker. Use on /reflect, SERV/AUR/DIDIY/BILI-class
  misses, or when a sheet "looked fine" but a filing says otherwise.
---

# Assert invariants, not the last bug name

## Why this exists

We fixed “one AUR” and still shipped SERV as exit+new in the same period.
We fixed history `$` and still shipped invent marks on live. We filled DIDIY
at $1.9B and still labeled the note `source=13g` only. We banned Neuton
`%`-as-shares on **live** and still invented `shares_proxy=presence=1` on
**history** (BILI SC 13G/A exit). Each time we patched the symptom judges
pointed at, not the rule — and invent without `$` stayed invisible to grade.

## Principles (canonical)

### P1 — Never invent

Do not fabricate quantity, identity, or dollars to keep QoQ “continuous.”

| Field | Allowed | Forbidden |
|---|---|---|
| `shares_held` | Parsed 13F/13G count; `0` on exit; **null** if unknown | `ownership_%`; `1` presence; any stuffed proxy |
| `ownership_pct` | Parsed % | Writing % into `shares_held` |
| `investee_ticker` | Real exchange ticker; **`PRIV_<slug>`** + `ticker=private_note` | First-word fakes (`ANT`, `CHINA`, `MANGO`) |
| `market_value_usd` | 13F `$`; Investments-table FV; **published broker value-to-parent** with `value_source=broker_sotp` when filing `$` is null; **null** | OTC×shares into this column; silent mcap×%; 13G-as-`$`; EOD filing estimate written here |
| `cost_basis_est_usd` / `mark_at_filing_est_usd` | EOD×shares at first_seen / each filing with `value_estimate=eod_at_filing; excluded_from_portfolio_mv` | Treating estimate as portfolio SoT or GAAP cost |

Empty parse ⇒ **drop the row** or mark **exit**. Continuity for %-only uses
`ownership_pct` + `qoq_continuity=ownership_pct` — never a fake share count.

Broker / sell-side SOTP (curated PDF ingest) may fill named-row `$` / `%` gaps and
must stamp `value_source=broker_sotp; cite=…`. When Note 22 listed aggregates are
present, broker-named `$` also gets `excluded_from_portfolio_mv` so portfolio
totals stay on company aggregates — never double-count.

### P2 — Assert the class, not the ticker

After a miss: one-line invariant → code refuse-to-ship → skill assert language
→ re-export + swarm grade. Never “we checked BILI.”

### P3 — Grade invent even when `$` is null

Judges and mechanical checks must fail invent on **identity/shares**, not only
dollar marks. Null chart `$` does not excuse `presence=1`, `%`-as-shares, or
fake tickers. Spot-open cited SC 13G/A when note claims ownership.

### P4 — Exits are exits

`ownership_pct==0` / Aggregate Amount `0` / Item 4 “no longer owns” / a
**checked** Item 5 box ⇒ `13g_exit=1`, pop 13G running map, suppress stale
20-F/note overlays, block older 13Gs from re-adding on live. Exit rows cite
the **period-grid** accession (disappearance), not the prior hold — except
13G multi-issuer same-day buckets, which keep the **prior-hold** accession
for that issuer (sibling-filing steal). Overlay `filing_date` matches source
`as_of`, not the 13F grid date. Exit rows set **`ownership_pct=0`** — never
retain the prior hold’s %.

**Not an exit:** positive Aggregate Amount **and** positive % — even when the
HTML includes Item 5 *form instructions* (“ceased to be the beneficial
owner… check the following”). Matching that boilerplate alone is forbidden
(DIDIY/NU class).

### P7 — Lookback wall is not inception

Display window (`lookback_years`, default 5) is for chart/portfolio only. Names
already held at the cut get sparse pre-window 13F walkback until shares hit 0.
`first_seen` / avg-cost lots use true inception. If walk cannot prove a prior
zero → `lookback_truncated=1` and **no priced buy lot** at the window edge.

### P8 — ADS ratio restatement ≠ disposal

Exact integer share consolidation (R≥2) plus issuer-CUSIP change (this period
or next) ⇒ `action=ratio_adj`, stamp `ads_ratio=R:1; restatement_not_disposal`,
**scale** open lots (shares÷R, cost_px×R), **zero** external flow, **no**
avg/FIFO realized. Do not book reverse-splits as sells.

### P5 — Provenance stamps travel with the value

Non-null `$` ⇒ note cites `13f` | `investments_table` | `value_source=` |
`hk_annual_note22`. Non-null `ownership_pct` on a merged 13F row should not
pretend the % came from 13F alone when it came from 13G.

### P9 — One parser module per form type

| Form | Module |
|---|---|
| SEC 13F | `parse_13f` / `sec_api_13f` |
| SC 13G/D | `sec_13g` |
| 10-K/10-Q/20-F notes | `parse_notes` |
| HKEX annual report | `parse_hk_annual` |

Shared identity (CUSIP / clean name / ticker / `holding_key`) lives in
`identity.py` — not a form parser. Never hardcode filing-table numbers in
orchestration (`tencent.py`).

### P10 — HK aggregate `$` ≠ named-child FV

Tencent Note 22 discloses **aggregates only** (no per-name FV table). Source of
truth for portfolio `$` = `PRIV_HK_*` (`value_source=hk_annual_note22`), shown as
`parent_aggregate_market_value_usd` on **positions_qoq** composition columns.
Source of truth for named children = SEC 13G/D (identity/%/shares). First-class
`composition_parent` / `composition_as_of` on QoQ rows (+ note mirror) is allowed;
inventing child `market_value_usd` via shares×EOD is not. Joining stamped broker
SOTP `$` (+ `child_value_source=broker_sotp`) is allowed — never copy the Note 22
aggregate into the child cell. Residuals (`PRIV_HK_*_RESIDUAL`) live on history
with `excluded_from_portfolio_mv` — **not** a separate `hk_composition` sheet.
`current_holdings` is the open-positions slice of QoQ (same SoT).

**Allowed estimate (not SoT):** `cost_basis_est_usd` / `mark_at_filing_est_usd`
from EOD×shares at filing (`value_estimate=eod_at_filing; estimate_role=…;
excluded_from_portfolio_mv`). Best-guess only — never write into
`market_value_usd`, portfolio, or Dietz.

### P6 — No silent escape hatches

If a code path exists “for continuity,” it must be named in the skill **and**
banned or tightly typed. `shares_proxy=presence` was an escape hatch that
looked rubric-compliant — never again.

## Required move after a miss

1. Name the **invariant** in one line.
2. Encode it in **code** (assert / coalesce / refuse-to-ship) for unseen tickers.
3. Encode it in **skills** in assert language — not “remember BILI/AUR.”
4. Re-export `--live --history` + `/holdings-sheet-swarm-grade` before claiming fixed.

**Steps 2 and 3 land together, same commit.** A principle line added to this
skill without a matching code assert/behavior change is a documented invariant
the code doesn't enforce — invisible drift until a grade run happens to catch
it. P5's `ownership_pct` provenance line shipped in `ed4495c` without the
`merge_raw_holdings` fix to match; a swarm grade run found the gap for real
(`ownership_pct_provenance_not_preserved`, BABA board) before the code caught
up. Never add an invariant here as a TODO for “the code should eventually do
this” — either fix the code in the same change, or don't add the line yet.

## Holdings invariants (closed world)

```text
history.groupby(period_end, investee_ticker).size() == 1
$ ∈ {13F, investments_table FV, null}
non-null $ ⇒ note cites 13f | investments_table | value_source=
shares_held ∈ {parsed count, 0 on exit, null}   # never % / presence
ticker ∈ {exchange symbol, PRIV_<slug>, null-stamped-then-PRIV}
never invent shares_proxy=presence OR exchange-like first-word tickers
0% / Aggregate 0 / cessation ⇒ pop 13G map + suppress note overlays + block live re-add
exit accession = period grid filing
exit ⇒ ownership_pct=0 (never retain prior hold %)
13G/note filing_date = source as_of (not 13F grid date)
13F-sourced exit ⇒ market_value_usd forced 0.0 (real ground truth: 0 shares = $0)
13G/note-overlay exit with no filed $ ⇒ market_value_usd stays null (never invent 0.0)
never treat lookback_start as inception
edge-held at window cut ⇒ sparse 13F walkback until shares=0 or lookback_truncated=1
chart/portfolio windowed; lots/first_seen inception-correct
lookback_truncated=1 ⇒ no priced buy lot at window edge
ADS ratio / reverse-split (exact integer share consol + issuer CUSIP change)
  ⇒ action=ratio_adj; scale lots; never book disposal realized
never mix shares_held with ownership_pct in one QoQ compare
  (%-only amendment after share count ⇒ action on %; shares_delta=null)
13G multi-issuer same-day exit ⇒ prior hold accession (not sibling issuer acc)
blank public issuers ⇒ stamp ticker via alias / CUSIP / hints (not leave blank)
export always --live --history together
chart series order = latest period $ rank
grade invent on null-$ rows too
```

## Anti-patterns

- “We checked AUR is unique” (should be every ticker).
- “History CSV looks good” (ignore live/current).
- “Dollar matches 10-Q” (ignore note provenance).
- “Shares column has a number” (could be % / presence invent).
- “Rubric allowed shares_proxy=” (escape hatch ≠ correct).
- “Null `$` so grade doesn’t care” (BILI 1-share class).
- “Parse got only issuer_name → invent 1 share” (drop or exit).
- “Share count dropped → always sell” (ADS ratio restatement class).
- “Every exit forces $0” (only a 13F-sourced exit does — a 13G/note-overlay exit with no filed $ stays null, or you've invented a dollar figure the filing never disclosed; FOO/test_price_history_rows class, 2026-08-22).
