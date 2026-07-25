#!/usr/bin/env python3
"""Revalidate and summarize a completed TTS speech-plan scratch run."""

from __future__ import annotations

import argparse
import collections
import json
import statistics
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from run_experiment import (
    build_report,
    compile_preview,
    expectation_metrics,
    extract_json_object,
    validate_plan,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results_dir", type=Path)
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def mode_metrics(rows: list[dict[str, Any]], mode: str) -> dict[str, Any]:
    selected = [row for row in rows if row["mode"] == mode]
    latencies = [float(row["latency_seconds"]) for row in selected]
    decisions = [
        decision
        for row in selected
        for decision in (row.get("parsed") or {}).get("decisions", [])
        if isinstance(decision, dict)
    ]
    expected = [
        detail
        for row in selected
        for detail in row["expectation_metrics"]["expected_pronunciation_terms"]
    ]
    prompt_tokens = sum(
        int(row.get("usage", {}).get("prompt_tokens") or 0) for row in selected
    )
    completion_tokens = sum(
        int(row.get("usage", {}).get("completion_tokens") or 0)
        for row in selected
    )
    contextual = mode == "contextual"
    return {
        "calls": len(selected),
        "parse_ok": sum(row["validation"]["parse_ok"] for row in selected),
        "plan_ok": sum(row["validation"]["schema_ok"] for row in selected),
        "placeholder_ok": (
            sum(
                row["validation"]["placeholder_integrity"] is True
                for row in selected
            )
            if contextual
            else None
        ),
        "latency_mean": statistics.mean(latencies) if latencies else 0.0,
        "latency_median": statistics.median(latencies) if latencies else 0.0,
        "latency_max": max(latencies) if latencies else 0.0,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "length_truncated": sum(
            row.get("finish_reason") == "length" for row in selected
        ),
        "reasoning_characters": sum(
            len(str(row.get("reasoning_content") or "")) for row in selected
        ),
        "actions": collections.Counter(
            str(decision.get("action")) for decision in decisions
        ),
        "discoveries": sum(
            len((row.get("parsed") or {}).get("discoveries", []))
            for row in selected
        ),
        "expected_count": len(expected),
        "expected_pronounced": sum(
            detail.get("model_action") == "pronounce" for detail in expected
        ),
    }


def build_analysis(
    prepared: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    modes: list[str],
) -> str:
    prepared_by_id = {case["case_id"]: case for case in prepared}
    metrics = {mode: mode_metrics(rows, mode) for mode in modes}
    lines = [
        "# Revalidated TTS speech-plan analysis",
        "",
        "## Aggregate results",
        "",
        "| Metric | "
        + " | ".join(mode.capitalize() for mode in modes)
        + " |",
        "|---|" + "---:|" * len(modes),
        "| Calls | "
        + " | ".join(str(metrics[mode]["calls"]) for mode in modes)
        + " |",
        "| Parseable JSON | "
        + " | ".join(
            f"{metrics[mode]['parse_ok']}/{metrics[mode]['calls']}"
            for mode in modes
        )
        + " |",
        "| Fully plan-valid | "
        + " | ".join(
            f"{metrics[mode]['plan_ok']}/{metrics[mode]['calls']}"
            for mode in modes
        )
        + " |",
        "| Mean latency | "
        + " | ".join(f"{metrics[mode]['latency_mean']:.2f} s" for mode in modes)
        + " |",
        "| Median latency | "
        + " | ".join(
            f"{metrics[mode]['latency_median']:.2f} s" for mode in modes
        )
        + " |",
        "| Maximum latency | "
        + " | ".join(f"{metrics[mode]['latency_max']:.2f} s" for mode in modes)
        + " |",
        "| Prompt tokens | "
        + " | ".join(f"{metrics[mode]['prompt_tokens']:,}" for mode in modes)
        + " |",
        "| Completion tokens | "
        + " | ".join(f"{metrics[mode]['completion_tokens']:,}" for mode in modes)
        + " |",
        "| Length-truncated calls | "
        + " | ".join(
            f"{metrics[mode]['length_truncated']}/{metrics[mode]['calls']}"
            for mode in modes
        )
        + " |",
        "| Captured reasoning characters | "
        + " | ".join(
            f"{metrics[mode]['reasoning_characters']:,}" for mode in modes
        )
        + " |",
        "| Expected terms pronounced | "
        + " | ".join(
            f"{metrics[mode]['expected_pronounced']}/{metrics[mode]['expected_count']}"
            for mode in modes
        )
        + " |",
        "| Extra discoveries | "
        + " | ".join(str(metrics[mode]["discoveries"]) for mode in modes)
        + " |",
        "",
    ]
    if "contextual" in metrics:
        contextual = metrics["contextual"]
        lines.extend(
            [
                f"Contextual placeholder integrity: "
                f"{contextual['placeholder_ok']}/{contextual['calls']}.",
                "",
            ]
        )

    contextual_changes = []
    for row in rows:
        if row["mode"] != "contextual" or not isinstance(row.get("parsed"), dict):
            continue
        speech_template = row["parsed"].get("speech_template")
        base_template = prepared_by_id[row["case_id"]]["base_template"]
        if speech_template != base_template:
            contextual_changes.append(row["case_id"])
    lines.extend(
        [
            f"Contextual templates with any whole-sentence change: "
            f"{len(contextual_changes)}/{metrics.get('contextual', {}).get('calls', 0)}.",
            "",
            "Action distribution:",
            "",
        ]
    )
    for mode in modes:
        distribution = ", ".join(
            f"{action}={count}"
            for action, count in sorted(metrics[mode]["actions"].items())
        )
        lines.append(f"- {mode}: {distribution}")

    result_by_key: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for row in rows:
        for decision in (row.get("parsed") or {}).get("decisions", []):
            if not isinstance(decision, dict):
                continue
            key = (row["case_id"], str(decision.get("span_id") or ""))
            result_by_key.setdefault(key, {})[row["mode"]] = decision

    action_agreement = 0
    compared = 0
    pronunciation_agreement = 0
    pronunciation_compared = 0
    disagreements: list[str] = []
    if {"guarded", "contextual"}.issubset(modes):
        for (case_id, span_id), decisions in result_by_key.items():
            if not {"guarded", "contextual"}.issubset(decisions):
                continue
            compared += 1
            guarded = decisions["guarded"]
            contextual = decisions["contextual"]
            if guarded.get("action") == contextual.get("action"):
                action_agreement += 1
            if guarded.get("action") == contextual.get("action") == "pronounce":
                pronunciation_compared += 1
                if guarded.get("spoken") == contextual.get("spoken"):
                    pronunciation_agreement += 1
            if (
                guarded.get("action"),
                guarded.get("spoken"),
            ) != (
                contextual.get("action"),
                contextual.get("spoken"),
            ):
                candidate = next(
                    (
                        item
                        for item in prepared_by_id[case_id]["candidates"]
                        if item["id"] == span_id
                    ),
                    {"text": span_id},
                )
                disagreements.append(
                    f"- `{case_id}` / `{candidate['text']}`: guarded "
                    f"`{guarded.get('action')} → {guarded.get('spoken')}`; "
                    f"contextual `{contextual.get('action')} → "
                    f"{contextual.get('spoken')}`."
                )
        lines.extend(
            [
                "",
                "## Cross-mode consistency",
                "",
                f"- Action agreement: {action_agreement}/{compared}.",
                f"- Exact respelling agreement when both pronounced: "
                f"{pronunciation_agreement}/{pronunciation_compared}.",
                "",
            ]
        )

    invalid = [row for row in rows if not row["validation"]["schema_ok"]]
    warned = [row for row in rows if row["validation"]["warnings"]]
    lines.extend(
        [
            "## Validation findings",
            "",
            f"- Invalid plans: {len(invalid)}.",
            f"- Plans with warnings: {len(warned)}.",
            "",
        ]
    )
    for row in invalid:
        lines.append(
            f"- `{row['case_id']}` / `{row['mode']}`: "
            + "; ".join(row["validation"]["errors"])
        )
    for row in warned:
        lines.append(
            f"- `{row['case_id']}` / `{row['mode']}` warning: "
            + "; ".join(row["validation"]["warnings"])
        )

    if disagreements:
        lines.extend(["", "## Differing decisions", "", *disagreements])
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    args = parse_args()
    results_dir = args.results_dir.resolve()
    prepared = json.loads(
        (results_dir / "prepared_cases.json").read_text(encoding="utf-8")
    )
    rows = read_jsonl(results_dir / "results.jsonl")
    prepared_by_id = {case["case_id"]: case for case in prepared}

    revalidated: list[dict[str, Any]] = []
    for row in rows:
        parsed = row.get("parsed")
        parse_note = None
        if not isinstance(parsed, dict):
            parsed, parse_note = extract_json_object(row.get("raw_content", ""))
        case = prepared_by_id[row["case_id"]]
        validation = validate_plan(
            parsed,
            mode=row["mode"],
            prepared=case,
            parse_note=parse_note,
        )
        compiled = compile_preview(
            parsed,
            mode=row["mode"],
            prepared=case,
        )
        revised = {
            **row,
            "parsed": parsed,
            "validation": validation,
            "compiled_preview": compiled,
            "expectation_metrics": expectation_metrics(case, parsed, compiled),
        }
        revalidated.append(revised)

    config = json.loads((results_dir / "run_config.json").read_text(encoding="utf-8"))
    modes = list(config.get("modes") or sorted({row["mode"] for row in rows}))
    report_args = SimpleNamespace(
        model=config.get("model", "unknown"),
        endpoint=config.get("endpoint", "unknown"),
        modes=modes,
        nemo=config.get("nemo", "unknown"),
        hunspell_dictionary=config.get("hunspell_dictionary", "unknown"),
    )
    write_jsonl(results_dir / "revalidated_results.jsonl", revalidated)
    (results_dir / "revalidated_report.md").write_text(
        build_report(prepared, revalidated, args=report_args),
        encoding="utf-8",
    )
    (results_dir / "analysis.md").write_text(
        build_analysis(prepared, revalidated, modes),
        encoding="utf-8",
    )
    print(results_dir / "analysis.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
