"""Shared Fable + Codex swarm harness for holdings pipeline stages.

Extracted from ``scripts/grade_holdings_sheet.py`` so sheet grades and
``scripts/swarm_validate_pipeline.py`` share one judge runner + board merge.

Mechanical checks FAIL hard. LLM judges are optional per ``--judges``.
Board FAIL does not hard-block Dagster (v1 = offline/CLI gate).
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SCHEMA_PATH = (
    _ROOT / ".cursor" / "skills" / "holdings-sheet-swarm-grade" / "grade_schema.json"
)
PIPELINE_SCHEMA_PATH = (
    _ROOT / ".cursor" / "skills" / "pipeline-swarm-validate" / "grade_schema.json"
)

# Stages we fully wire in v1; others are stubs that emit info-only mechanical.
WIRED_STAGES = frozenset({"broker_pdf_parse", "overlay_merge", "composition_export"})
STUB_STAGES = frozenset(
    {
        "catalog_parse",
        "hk_annual_parse",
        "sec_13g_parse",
        "sheet_export",
    }
)
ALL_STAGES = WIRED_STAGES | STUB_STAGES

JUDGE_EXTRA_FIELDS = (
    "recommended_fixes",
    "root_cause_class",
    "avoid_next_time",
)


def load_dotenv(root: Path | None = None) -> None:
    env_path = (root or _ROOT) / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_schema(path: Path | None = None) -> dict:
    p = path or (
        PIPELINE_SCHEMA_PATH if PIPELINE_SCHEMA_PATH.is_file() else DEFAULT_SCHEMA_PATH
    )
    return json.loads(p.read_text(encoding="utf-8"))


def judge_prompt_suffix() -> str:
    """Required FAIL fields for every pipeline/sheet swarm judge."""
    return (
        "\n\nOn FAIL or NEEDS_WORK you MUST also include:\n"
        "- recommended_fixes: 1–3 concrete code/data actions "
        "(file/function when possible)\n"
        "- root_cause_class: e.g. unit_mixup | parent_child_column_confusion | "
        "wrong_pdf_window | shallow_13g_scan | invent_marks | duplicate_period_ticker\n"
        "- avoid_next_time: one mechanical assert that would have blocked ship\n"
        "On PASS leave those as empty array / null / empty string as schema allows.\n"
    )


def _judge_prompt(packet_text: str, judge_name: str) -> str:
    return (
        f"You are judge={judge_name} verifying a holdings pipeline stage packet.\n"
        "Apply the rubric in the packet strictly. Output JSON only (no markdown).\n"
        + judge_prompt_suffix()
        + "\n"
        + packet_text
        + "\n"
    )


def parse_json_response(text: str, *, judge: str) -> dict:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:].lstrip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end < 0:
        return {
            "judge": judge,
            "verdict": "needs_work",
            "score": 0,
            "blocking_issues": [
                {
                    "id": "unparseable",
                    "severity": "Judge did not return JSON",
                    "evidence": text[:2000],
                }
            ],
            "minor_issues": [],
            "what_looks_good": [],
            "checks": {},
            "summary": "Unparseable judge output",
            "recommended_fixes": [],
            "root_cause_class": "judge_unparseable",
            "avoid_next_time": "Require schema-valid JSON from judge CLI",
        }
    data = json.loads(raw[start : end + 1])
    data["judge"] = judge
    for k in JUDGE_EXTRA_FIELDS:
        data.setdefault(k, [] if k == "recommended_fixes" else None)
    return data


def _unavailable(
    judge: str, *, issue_id: str, severity: str, evidence: str, summary: str
) -> dict:
    return {
        "judge": judge,
        "verdict": "needs_work",
        "score": 0,
        "blocking_issues": [
            {"id": issue_id, "severity": severity, "evidence": evidence[:2000]}
        ],
        "minor_issues": [],
        "what_looks_good": [],
        "checks": {},
        "summary": summary,
        "recommended_fixes": [],
        "root_cause_class": None,
        "avoid_next_time": None,
    }


def run_fable(
    packet_text: str,
    schema: dict,
    *,
    timeout: int = 600,
    wrap_prompt: bool = True,
) -> dict:
    prompt = _judge_prompt(packet_text, "fable") if wrap_prompt else packet_text
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    cmd = [
        "claude",
        "-p",
        "--model",
        "claude-fable-5",
        "--tools",
        "",
        "--output-format",
        "text",
        prompt
        + "\n\nRespond with a single JSON object matching this schema:\n"
        + json.dumps(schema),
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError:
        return _unavailable(
            "fable",
            issue_id="fable_cli_missing",
            severity="claude CLI not found",
            evidence="",
            summary="Fable judge unavailable (cli)",
        )
    except subprocess.TimeoutExpired:
        return _unavailable(
            "fable",
            issue_id="fable_timeout",
            severity="Fable timed out",
            evidence="",
            summary="Fable judge timed out",
        )
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    combined = out or err
    if any(
        x in combined.lower()
        for x in ("401", "api key is invalid", "authenticate", "oauth session expired")
    ):
        return _unavailable(
            "fable",
            issue_id="fable_auth",
            severity="Claude Fable auth failed — run `claude login`",
            evidence=combined,
            summary="Fable judge unavailable (auth)",
        )
    if proc.returncode != 0 and not out:
        return _unavailable(
            "fable",
            issue_id="fable_exec_failed",
            severity=f"claude fable exited {proc.returncode}",
            evidence=err or out,
            summary="Fable judge failed to run",
        )
    return parse_json_response(out, judge="fable")


def run_codex(
    packet_text: str,
    schema_path: Path,
    *,
    timeout: int = 600,
    out_message: Path | None = None,
    wrap_prompt: bool = True,
) -> dict:
    prompt = _judge_prompt(packet_text, "codex") if wrap_prompt else packet_text
    out_msg = out_message or (_ROOT / "exports" / "_codex_last_message.txt")
    out_msg.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tf:
        tf.write(prompt)
        prompt_path = tf.name
    try:
        cmd = [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "-s",
            "read-only",
            "--output-schema",
            str(schema_path),
            "-o",
            str(out_msg),
            prompt,
        ]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(_ROOT),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError:
            return _unavailable(
                "codex",
                issue_id="codex_cli_missing",
                severity="codex CLI not found",
                evidence="",
                summary="Codex judge unavailable (cli)",
            )
        except subprocess.TimeoutExpired:
            return _unavailable(
                "codex",
                issue_id="codex_timeout",
                severity="Codex timed out",
                evidence="",
                summary="Codex judge timed out",
            )
        out = out_msg.read_text(encoding="utf-8") if out_msg.is_file() else (proc.stdout or "")
        if proc.returncode != 0 and not out.strip():
            return _unavailable(
                "codex",
                issue_id="codex_exec_failed",
                severity=f"codex exited {proc.returncode}",
                evidence=(proc.stderr or proc.stdout or ""),
                summary="Codex judge failed to run",
            )
        return parse_json_response(out, judge="codex")
    finally:
        try:
            os.unlink(prompt_path)
        except OSError:
            pass


def stage_report_to_mechanical(report: Any, *, stage: str) -> dict:
    """Convert broker_sotp_validate.StageReport → judge-shaped mechanical dict."""
    findings = list(getattr(report, "findings", []) or [])
    ok = bool(getattr(report, "ok", True))
    blocking = []
    minor = []
    good = []
    checks: dict[str, str] = {}
    for f in findings:
        sev = getattr(f, "severity", "info")
        cid = getattr(f, "check_id", "check")
        msg = getattr(f, "message", "")
        ev = getattr(f, "evidence", {}) or {}
        checks[cid] = "fail" if sev == "fail" else "pass" if sev == "info" else "unknown"
        item = {
            "id": cid,
            "severity": msg,
            "evidence": json.dumps(ev) if isinstance(ev, dict) else str(ev),
        }
        if sev == "fail":
            blocking.append(item)
        elif sev == "warn":
            minor.append(item)
        else:
            good.append(msg)
    root = None
    fixes: list[str] = []
    avoid = None
    if not ok:
        ids = {b["id"] for b in blocking}
        if "parent_vs_child_confusion_risk" in ids or "child_equals_parent" in ids:
            root = "parent_child_column_confusion"
            fixes.append(
                "composition.hk_composition_frame: join broker_sotp $ into "
                "child_market_value_usd; keep parent_aggregate_market_value_usd"
            )
            avoid = (
                "mechanical: PDD child_market_value_usd null while "
                "parent_aggregate > $50B → FAIL"
            )
        elif "pdd_looks_like_parent_agg" in ids or "child_looks_like_parent_agg" in ids:
            root = "parent_child_column_confusion"
            fixes.append("Do not copy Note 22 aggregate into child_market_value_usd")
            avoid = "mechanical: child_market_value_usd > $60B for PDD → FAIL"
        elif any("unit" in i or "hkd" in i for i in ids):
            root = "unit_mixup"
            fixes.append("broker_sotp: assert value_to_parent is HK$mn before FX")
            avoid = "mechanical: PDD value_to_parent_hkd_mn outside 1e3–1e6 → FAIL"
        else:
            root = "pipeline_stage_fail"
            fixes.append(f"Inspect stage={stage} findings and fix the class")
            avoid = f"mechanical stage={stage} FAIL must block ship"
    return {
        "judge": "mechanical",
        "verdict": "fail" if not ok else "pass",
        "score": 0 if not ok else 100,
        "blocking_issues": blocking,
        "minor_issues": minor,
        "what_looks_good": good[:8],
        "checks": checks,
        "summary": (
            f"Mechanical {stage} FAILED" if not ok else f"Mechanical {stage} passed"
        ),
        "stage": stage,
        "recommended_fixes": fixes,
        "root_cause_class": root,
        "avoid_next_time": avoid,
    }


def merge_board(mechanical: dict, judges: list[dict], *, stage: str) -> dict:
    """Merge mechanical + LLM judges into one board dict."""
    results = [mechanical] + list(judges)
    verdicts = {str(r.get("verdict") or "").lower() for r in results}
    if "fail" in verdicts or str(mechanical.get("verdict")) == "fail":
        board = "FAIL"
    elif "needs_work" in verdicts or len(verdicts - {""}) > 1:
        board = "NEEDS_WORK"
    else:
        board = "PASS"
    # Prefer first non-null root_cause / fixes from fail judges
    root = mechanical.get("root_cause_class")
    fixes = list(mechanical.get("recommended_fixes") or [])
    avoid = mechanical.get("avoid_next_time")
    for r in judges:
        if str(r.get("verdict")).lower() in {"fail", "needs_work"}:
            root = root or r.get("root_cause_class")
            for fx in r.get("recommended_fixes") or []:
                if fx and fx not in fixes:
                    fixes.append(fx)
            avoid = avoid or r.get("avoid_next_time")
    return {
        "stage": stage,
        "board": board,
        "ok": board == "PASS",
        "mechanical": mechanical,
        "judges": judges,
        "recommended_fixes": fixes,
        "root_cause_class": root,
        "avoid_next_time": avoid,
        "results": results,
    }


def write_stage_board_md(
    *,
    parent: str,
    stage: str,
    board: dict,
    out_path: Path,
) -> Path:
    lines = [
        f"# Pipeline swarm board — {parent} / {stage}",
        "",
        f"**BOARD: {board.get('board')}**",
        "",
        "| Judge | Verdict | Score |",
        "|---|---|---|",
    ]
    for r in board.get("results") or []:
        lines.append(
            f"| {r.get('judge')} | **{r.get('verdict')}** | {r.get('score')} |"
        )
    lines.append("")
    if board.get("root_cause_class"):
        lines.append(f"- root_cause_class: `{board['root_cause_class']}`")
    if board.get("avoid_next_time"):
        lines.append(f"- avoid_next_time: {board['avoid_next_time']}")
    if board.get("recommended_fixes"):
        lines.append("- recommended_fixes:")
        for fx in board["recommended_fixes"]:
            lines.append(f"  - {fx}")
    lines.append("")
    for r in board.get("results") or []:
        lines.append(f"## {r.get('judge')}")
        lines.append("")
        lines.append(r.get("summary") or "")
        lines.append("")
        if r.get("blocking_issues"):
            lines.append("### Blocking")
            for issue in r["blocking_issues"]:
                lines.append(
                    f"- **{issue.get('id')}**: {issue.get('severity')} — "
                    f"{issue.get('evidence')}"
                )
            lines.append("")
        if r.get("minor_issues"):
            lines.append("### Minor")
            for issue in r["minor_issues"]:
                lines.append(
                    f"- **{issue.get('id')}**: {issue.get('severity')} — "
                    f"{issue.get('evidence')}"
                )
            lines.append("")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


def run_parallel_judges(
    packet_text: str,
    *,
    judges: list[str],
    schema: dict,
    schema_path: Path,
) -> list[dict]:
    llm = [j.strip().lower() for j in judges if j.strip().lower() in {"fable", "codex"}]
    if not llm:
        return []

    def _run(name: str) -> dict:
        if name == "fable":
            return run_fable(packet_text, schema)
        return run_codex(packet_text, schema_path)

    out: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, len(llm))) as pool:
        futs = {pool.submit(_run, j): j for j in llm}
        for fut in as_completed(futs):
            name = futs[fut]
            try:
                out.append(fut.result())
            except Exception as e:
                out.append(
                    _unavailable(
                        name,
                        issue_id="exception",
                        severity=str(e),
                        evidence=repr(e),
                        summary=f"{name} raised",
                    )
                )
    order = {n: i for i, n in enumerate(llm)}
    out.sort(key=lambda r: order.get(str(r.get("judge")), 99))
    return out


# ---------------------------------------------------------------------------
# Stage packet builders + mechanical runners
# ---------------------------------------------------------------------------


def _snippet(text: str, max_chars: int = 4000) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n… truncated ({len(text)} chars)\n"


def build_broker_pdf_packet(*, parent: str) -> tuple[str, Any]:
    """Return (packet_md, StageReport mechanical)."""
    from hidden_stock.quirks.holdings.broker_sotp import (
        catalog_for_parent,
        default_cache_dir,
        download_pdf,
        extract_pdf_text,
        rows_from_entry,
    )
    from hidden_stock.quirks.holdings.broker_sotp_validate import (
        validate_parse_rows,
    )

    entries = catalog_for_parent(parent)
    if not entries:
        from hidden_stock.quirks.holdings.broker_sotp_validate import Finding, StageReport

        rep = StageReport(stage="parse", ok=False)
        rep.add(Finding("parse", "fail", "empty_catalog", "No broker catalog entries"))
        packet = (
            f"# Stage broker_pdf_parse — {parent}\n\nNo catalog entries.\n"
            + judge_prompt_suffix()
        )
        return packet, rep

    e = entries[-1]
    path = download_pdf(str(e["url"]), default_cache_dir())
    text = extract_pdf_text(path)
    low = text.lower()
    idx = low.find("strategic investment")
    snippet = text[max(0, idx) : max(0, idx) + 5000] if idx >= 0 else text[:5000]
    parsed = rows_from_entry(e, text=snippet if len(snippet) > 500 else text)
    if not parsed:
        parsed = rows_from_entry(e)
    rep = validate_parse_rows(parsed, report_id=str(e.get("id")), min_rows=8)
    sample = [
        {
            "investee_ticker": r.get("investee_ticker"),
            "investee_name": r.get("investee_name"),
            "ownership_pct": r.get("ownership_pct"),
            "value_to_parent_hkd_mn": r.get("value_to_parent_hkd_mn"),
            "mkt_cap_usd_mn": r.get("mkt_cap_usd_mn"),
        }
        for r in parsed[:12]
    ]
    packet = "\n".join(
        [
            f"# Stage broker_pdf_parse — {parent}",
            "",
            f"- report_id: `{e.get('id')}`",
            f"- citation: {e.get('citation') or ''}",
            f"- as_of: {e.get('as_of')}",
            "",
            "## Rubric",
            "",
            "1. Snippet must be the strategic-investments / value-to-parent table "
            "(not PDF cover/disclaimer).",
            "2. `value_to_parent_hkd_mn` is **HK$ millions** (not USD, not billions).",
            "3. Spot-check PDD / Meituan stake % vs snippet.",
            "4. Do not treat value-to-parent as Note 22 parent aggregate (~$90B).",
            "",
            "## Parsed-backed table snippet",
            "",
            "```",
            _snippet(snippet, 4500),
            "```",
            "",
            "## Parsed rows (sample)",
            "",
            "```json",
            json.dumps(sample, indent=2),
            "```",
            "",
        ]
    )
    return packet, rep


def build_overlay_merge_packet(*, parent: str, exports_dir: Path) -> tuple[str, Any]:
    from hidden_stock.quirks.holdings.broker_sotp_validate import (
        Finding,
        StageReport,
        validate_overlay_history,
        validate_overlay_live,
    )

    live_path = exports_dir / f"{parent.lower()}_equity_holdings.csv"
    hist_path = exports_dir / f"{parent.lower()}_equity_holdings_history.csv"
    reports: list[Any] = []
    live_sample: list[dict] = []
    hist_broker: list[dict] = []

    import pandas as pd

    if live_path.is_file():
        live = pd.read_csv(live_path).to_dict(orient="records")
        reports.append(validate_overlay_live(live))
        live_sample = [
            {
                k: r.get(k)
                for k in (
                    "investee_ticker",
                    "investee_name",
                    "ownership_pct",
                    "shares_held",
                    "market_value_usd",
                    "note",
                )
            }
            for r in live
            if str(r.get("investee_ticker") or "").upper() == "PDD"
            or "value_source=broker_sotp" in str(r.get("note") or "")
        ][:8]
    else:
        r = StageReport(stage="overlay_live", ok=False)
        r.add(
            Finding(
                "overlay_live",
                "fail",
                "no_live_csv",
                f"Missing {live_path}",
            )
        )
        reports.append(r)

    if hist_path.is_file():
        hist = pd.read_csv(hist_path).to_dict(orient="records")
        reports.append(validate_overlay_history(hist))
        hist_broker = [
            {
                k: r.get(k)
                for k in (
                    "period_end",
                    "investee_ticker",
                    "market_value_usd",
                    "note",
                )
            }
            for r in hist
            if "value_source=broker_sotp" in str(r.get("note") or "")
            and str(r.get("investee_ticker") or "").upper() == "PDD"
        ][-6:]
    else:
        r = StageReport(stage="overlay_history", ok=False)
        r.add(
            Finding(
                "overlay_history",
                "fail",
                "no_hist_csv",
                f"Missing {hist_path}",
            )
        )
        reports.append(r)

    # Merge reports into one StageReport-like for mechanical
    merged = StageReport(stage="overlay_history", ok=all(x.ok for x in reports))
    for x in reports:
        for f in x.findings:
            merged.add(f)

    packet = "\n".join(
        [
            f"# Stage overlay_merge — {parent}",
            "",
            "## Rubric",
            "",
            "1. Broker fills null `$` with `value_source=broker_sotp`; 13G keeps %/shares.",
            "2. PDD broker `$` should be ~$10–40B (value-to-Tencent), never ~$90B Note 22.",
            "3. Broker-named rows with listed Note 22 present stamp `excluded_from_portfolio_mv`.",
            "4. Provenance: `report_id=` / `cite=` present on broker stamps.",
            "",
            "## Live PDD / broker sample",
            "",
            "```json",
            json.dumps(live_sample, indent=2, default=str),
            "```",
            "",
            "## History broker-stamped PDD (recent)",
            "",
            "```json",
            json.dumps(hist_broker, indent=2, default=str),
            "```",
            "",
        ]
    )
    return packet, merged


def build_composition_export_packet(
    *, parent: str, exports_dir: Path
) -> tuple[str, Any]:
    from hidden_stock.quirks.holdings.broker_sotp_validate import (
        Finding,
        StageReport,
        validate_composition_ux,
    )
    from hidden_stock.quirks.holdings.composition import hk_composition_frame

    import pandas as pd

    hist_path = exports_dir / f"{parent.lower()}_equity_holdings_history.csv"
    legacy_comp = exports_dir / f"{parent.lower()}_hk_composition.csv"
    if hist_path.is_file():
        hist = pd.read_csv(hist_path).to_dict(orient="records")
        rep = validate_composition_ux(hist)
        frame = hk_composition_frame(hist)
        pdd = [r for r in frame if str(r.get("child_ticker") or "").upper() == "PDD"]
        source = f"`{hist_path.name}` (composition_* on QoQ)"
    elif legacy_comp.is_file():
        comp = pd.read_csv(legacy_comp).to_dict(orient="records")
        rep = validate_composition_ux(comp)
        pdd = [r for r in comp if str(r.get("child_ticker") or "").upper() == "PDD"]
        source = f"`{legacy_comp.name}` (legacy)"
    else:
        r = StageReport(stage="composition_ux", ok=False)
        r.add(
            Finding(
                "composition_ux",
                "fail",
                "no_hist_csv",
                f"Missing {hist_path} (composition lives on QoQ)",
            )
        )
        packet = (
            f"# Stage composition_export — {parent}\n\n"
            "Missing QoQ history CSV (composition columns).\n"
        )
        return packet, r

    packet = "\n".join(
        [
            f"# Stage composition_export — {parent}",
            "",
            f"Source: {source}",
            "",
            "## Rubric",
            "",
            "1. `parent_aggregate_market_value_usd` = Note 22 listed bucket (~$80–100B).",
            "2. Child `$` = name-level `market_value_usd` / broker join; PDD ≈ $10–40B.",
            "3. Never treat parent aggregate as PDD's value-to-Tencent.",
            "4. `child_value_source` / note provenance when child $ set.",
            "5. Composition overlay lives on **positions_qoq** — no separate hk_composition tab.",
            "6. Inventing child $ via shares×EOD into history is FAIL.",
            "",
            "## PDD composition rows",
            "",
            "```json",
            json.dumps(pdd[-5:], indent=2, default=str),
            "```",
            "",
        ]
    )
    return packet, rep


def build_stub_packet(*, parent: str, stage: str) -> tuple[str, Any]:
    from hidden_stock.quirks.holdings.broker_sotp_validate import Finding, StageReport

    r = StageReport(stage="catalog", ok=True)
    r.add(
        Finding(
            "catalog",
            "info",
            "stage_stub",
            f"Stage `{stage}` is stubbed in v1 — no mechanical/swarm packet yet",
            {"parent": parent},
        )
    )
    packet = (
        f"# Stage {stage} — {parent}\n\n"
        "Stub stage (not wired). Do not FAIL solely for stub; "
        "return pass with note that stage is unimplemented.\n"
    )
    return packet, r


def build_stage_packet(
    stage: str, *, parent: str, exports_dir: Path
) -> tuple[str, Any]:
    stage = stage.strip().lower()
    if stage == "broker_pdf_parse":
        return build_broker_pdf_packet(parent=parent)
    if stage == "overlay_merge":
        return build_overlay_merge_packet(parent=parent, exports_dir=exports_dir)
    if stage == "composition_export":
        return build_composition_export_packet(parent=parent, exports_dir=exports_dir)
    if stage in STUB_STAGES:
        return build_stub_packet(parent=parent, stage=stage)
    raise ValueError(f"Unknown stage {stage!r}; known={sorted(ALL_STAGES)}")


def validate_stage(
    stage: str,
    *,
    parent: str,
    exports_dir: Path,
    judges: list[str],
    schema: dict | None = None,
    schema_path: Path | None = None,
) -> dict:
    """Run mechanical + optional LLM judges for one stage; return board dict."""
    schema_path = schema_path or (
        PIPELINE_SCHEMA_PATH if PIPELINE_SCHEMA_PATH.is_file() else DEFAULT_SCHEMA_PATH
    )
    schema = schema or load_schema(schema_path)
    packet, mech_report = build_stage_packet(
        stage, parent=parent, exports_dir=exports_dir
    )
    mechanical = stage_report_to_mechanical(mech_report, stage=stage)
    # Mechanical FAIL hard — still run judges for recommendations when requested
    llm_results = run_parallel_judges(
        packet,
        judges=judges,
        schema=schema,
        schema_path=schema_path,
    )
    return merge_board(mechanical, llm_results, stage=stage)
