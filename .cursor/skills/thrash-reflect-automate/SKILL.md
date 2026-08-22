---
name: thrash-reflect-automate
description: >-
  On holdings swarm FAIL / NEEDS_WORK, test thrash, or /thrash: auto-run short
  /reflect, fix the class (not only the named ticker), codify the invariant,
  short /automate-me catch, then re-grade. Collapse phrase for “reflect and
  automate after every failure” — do not wait for the user to re-prompt.
---

# Thrash → reflect + automate (no re-prompt)

When a **swarm grade**, **test thrash**, or **FAIL / NEEDS_WORK board** happens
in this repo — or the user says **`/thrash`** — do **not** wait for the long
“after every failure /reflect and /automate-me” phrase.

Companion always-on rule: `.cursor/rules/thrash-reflect-automate.mdc`.

## Auto sequence (every FAIL turn)

1. **/reflect** (short): why it shipped, class of bug, why we missed it.
2. **Fix** the class (assert / identity / coalesce / provenance) — not only the
   named ticker. Prefer `principle-assert-invariants-not-last-bug`.
3. **Codify** the invariant in the relevant skill when holdings-related
   (`holdings-sheet-swarm-grade`, `equity-holdings-sheets`, principles).
4. **/automate-me** (short): one concrete automation that would catch this next
   time (CI grade gate, mechanical check in `grade_holdings_sheet.py`, Cursor
   Automation draft). Do not open the Automations editor unless the user asks.
5. Re-export / re-grade if that was the active loop; do not claim PASS without
   a new board.

## Collapse phrase

| Shortcut | Means |
|---|---|
| `/thrash` | Run the auto sequence above |
| swarm FAIL / NEEDS_WORK | Same — no extra prompt needed |

## Do not

- Skip reflect on FAIL because mechanical passed.
- Ask “should I reflect?” — just do it.
- Commit or open Automations editor unless asked.
- Paraphrase away a FAIL on the judgment board.
