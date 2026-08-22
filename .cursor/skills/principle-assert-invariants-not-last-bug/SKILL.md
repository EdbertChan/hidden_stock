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
| `market_value_usd` | 13F `$`; Investments-table FV; **null** | OTC×shares; mcap×%; 13G-as-`$` |

Empty parse ⇒ **drop the row** or mark **exit**. Continuity for %-only uses
`ownership_pct` + `qoq_continuity=ownership_pct` — never a fake share count.

### P2 — Assert the class, not the ticker

After a miss: one-line invariant → code refuse-to-ship → skill assert language
→ re-export + swarm grade. Never “we checked BILI.”

### P3 — Grade invent even when `$` is null

Judges and mechanical checks must fail invent on **identity/shares**, not only
dollar marks. Null chart `$` does not excuse `presence=1`, `%`-as-shares, or
fake tickers. Spot-open cited SC 13G/A when note claims ownership.

### P4 — Exits are exits

`ownership_pct==0` / Aggregate Amount `0` / Item 5 cessation / “no longer owns”
⇒ `13g_exit=1`, pop 13G running map, suppress stale 20-F/note overlays, block
older 13Gs from re-adding on live. Exit rows cite the **period-grid** accession
(disappearance), not the prior hold. Overlay `filing_date` matches source
`as_of`, not the 13F grid date. Exit rows set **`ownership_pct=0`** — never
retain the prior hold’s %.

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

Non-null `$` ⇒ note cites `13f` | `investments_table` | `value_source=`.
Non-null `ownership_pct` on a merged 13F row should not pretend the % came from
13F alone when it came from 13G.

### P6 — No silent escape hatches

If a code path exists “for continuity,” it must be named in the skill **and**
banned or tightly typed. `shares_proxy=presence` was an escape hatch that
looked rubric-compliant — never again.

## Required move after a miss

1. Name the **invariant** in one line.
2. Encode it in **code** (assert / coalesce / refuse-to-ship) for unseen tickers.
3. Encode it in **skills** in assert language — not “remember BILI/AUR.”
4. Re-export `--live --history` + `/holdings-sheet-swarm-grade` before claiming fixed.

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
never treat lookback_start as inception
edge-held at window cut ⇒ sparse 13F walkback until shares=0 or lookback_truncated=1
chart/portfolio windowed; lots/first_seen inception-correct
lookback_truncated=1 ⇒ no priced buy lot at window edge
ADS ratio / reverse-split (exact integer share consol + issuer CUSIP change)
  ⇒ action=ratio_adj; scale lots; never book disposal realized
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
