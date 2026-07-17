"""Harness optimizer for FlowerNet research-writing workflows.

The optimizer proposes bounded workflow changes. It never edits prompts,
thresholds, source pools, or benchmark outputs by itself. A proposal can only be
retained after held-in and held-out validation pass no-harm gates.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _weakness_kinds(trace: Mapping[str, Any]) -> List[str]:
    out: List[str] = []
    for item in trace.get("weaknesses", []) if isinstance(trace.get("weaknesses"), list) else []:
        if isinstance(item, Mapping):
            kind = str(item.get("kind") or "")
            if kind and kind not in out:
                out.append(kind)
    if trace.get("status") == "failed" and "run_failed" not in out:
        out.append("run_failed")
    reviewer = trace.get("reviewer_assessment") if isinstance(trace.get("reviewer_assessment"), Mapping) else {}
    external = reviewer.get("external_alignment") if isinstance(reviewer.get("external_alignment"), Mapping) else {}
    if external.get("available") and external.get("aligned_with_external_metrics") is False:
        out.append("external_metrics_low")
    dims = reviewer.get("reviewer_dimensions") if isinstance(reviewer.get("reviewer_dimensions"), Mapping) else {}
    if isinstance(dims.get("claim_support"), Mapping) and _f(dims["claim_support"].get("score"), 1.0) < 0.55:
        out.append("claim_evidence_weak")
    return list(dict.fromkeys(out))


def recommended_flowernet_full_architecture() -> List[Dict[str, str]]:
    return [
        {
            "name": "Research Scout",
            "role": "retrieve papers, surveys, benchmarks, datasets, methods, and known failure cases",
            "paper_value": "turns FlowerNet from a generic writer into a literature-aware research system",
        },
        {
            "name": "Literature Map Builder",
            "role": "maps mainstream routes, open gaps, controversies, and limitations",
            "paper_value": "makes novelty claims auditable against the literature space",
        },
        {
            "name": "Novelty Miner",
            "role": "generates literature-anchored contribution candidates with reviewer risk",
            "paper_value": "separates real contribution from surface-level wording novelty",
        },
        {
            "name": "Claim-Evidence Graph Builder",
            "role": "decomposes contributions into claim-evidence-citation graph nodes",
            "paper_value": "enables paragraph-level auditability and citation faithfulness",
        },
        {
            "name": "Research Paper Writer",
            "role": "writes introduction, related work, method/system, experiment, results, discussion, and conclusion",
            "paper_value": "covers the complete research-paper lifecycle rather than only long-form summaries",
        },
        {
            "name": "Evolvable Reviewer / Verifier",
            "role": "checks claim support, citation faithfulness, novelty, logic, reproducibility, and external metric proxies",
            "paper_value": "aligns internal assessment with external objective signals",
        },
        {
            "name": "Controller + Bandit Repair",
            "role": "selects targeted repair arms and learns from realized success/failure under no-harm gates",
            "paper_value": "demonstrates adaptive, component-level contribution rather than static prompt stacking",
        },
        {
            "name": "Self-Improving Harness Optimizer",
            "role": "proposes minimal workflow changes and validates them on held-out topics",
            "paper_value": "moves FlowerNet toward a self-improving research-writing harness without test-set leakage",
        },
    ]


def propose_harness_optimization(*, trace: Mapping[str, Any], memories: Mapping[str, Any]) -> Dict[str, Any]:
    kinds = _weakness_kinds(trace)
    topic_strategy = memories.get("topic_strategy") if isinstance(memories.get("topic_strategy"), Mapping) else {}
    repair_hints = topic_strategy.get("repair_policy_hints") if isinstance(topic_strategy.get("repair_policy_hints"), Mapping) else {}

    flow = ["research_scout", "literature_map", "novelty_mining", "claim_graph", "writer", "reviewer", "controller", "self_harness"]
    rationale = []
    if "claim_evidence_weak" in kinds or "citation_unsupported" in kinds:
        flow = ["research_scout", "claim_graph", "literature_map", "novelty_mining", "writer", "reviewer", "controller", "self_harness"]
        rationale.append("claim/evidence weakness requires graph-first planning before novelty expansion")
    if "external_metrics_low" in kinds:
        rationale.append("external metric mismatch requires source-selection and reviewer-alignment validation")
    if "novelty_weak" in kinds and "claim_evidence_weak" not in kinds:
        flow = ["research_scout", "literature_map", "novelty_mining", "claim_graph", "writer", "reviewer", "controller", "self_harness"]
        rationale.append("novelty weakness favors literature-map before writing")
    if not rationale:
        rationale.append("no severe workflow defect detected; keep default 8-layer order")

    threshold_action = "keep_current_thresholds"
    if "run_failed" in kinds or "controller_harmful" in kinds:
        threshold_action = "audit_thresholds_with_no_harm_gate"

    return {
        "enabled": True,
        "optimizer_version": "harness_optimizer_v1",
        "bounded": True,
        "topic": str(trace.get("topic") or topic_strategy.get("topic") or ""),
        "detected_weaknesses": kinds,
        "recommended_flow_order": flow,
        "controller_trigger_policy": (
            "trigger_only_on_reviewer_or_external_metric_defect"
            if "external_metrics_low" in kinds
            else "trigger_on_multidim_or_memory_confirmed_defect"
        ),
        "verifier_threshold_policy": threshold_action,
        "reviewer_prompt_policy": "prefer reviewer prompts that improve held-out external alignment and no-harm pass rate",
        "source_selection_policy": (
            "prefer sources that support claim-evidence graph and external metric alignment"
            if "external_metrics_low" in kinds
            else "preserve domain-diverse high-salience sources"
        ),
        "memory_policy_hints": {
            "prefer_arms": list(repair_hints.get("prefer_arms", [])),
            "cooldown_arms": list(repair_hints.get("cooldown_arms", [])),
        },
        "rationale": rationale,
        "held_out_required": True,
        "acceptance_metrics": [
            "external_mean",
            "reviewer_score",
            "no_harm_pass_rate",
            "citation_faithfulness",
            "claim_support",
        ],
        "must_not_change": [
            "held-out topics",
            "raw experiment outputs",
            "retrieved source truthfulness",
            "benchmark topic selection",
            "evaluation references",
        ],
        "status": "proposed_unvalidated",
    }


def validate_harness_optimization(
    proposal: Mapping[str, Any],
    *,
    held_in_before: Mapping[str, Any],
    held_in_after: Mapping[str, Any],
    held_out_before: Mapping[str, Any],
    held_out_after: Mapping[str, Any],
    tolerance: float = 0.001,
) -> Dict[str, Any]:
    required = ["external_mean", "reviewer_score", "no_harm_pass_rate"]

    def split_ok(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
        return all(_f(after.get(k), _f(before.get(k))) >= _f(before.get(k)) - abs(tolerance) for k in required)

    regressions: List[Dict[str, Any]] = []
    for split, before, after in (
        ("held_in", held_in_before, held_in_after),
        ("held_out", held_out_before, held_out_after),
    ):
        for key in set(before.keys()) | set(after.keys()):
            if key not in before or key not in after:
                continue
            delta = round(_f(after[key]) - _f(before[key]), 6)
            if delta < -abs(tolerance):
                regressions.append({"split": split, "metric": key, "delta": delta})

    held_in_ok = split_ok(held_in_before, held_in_after)
    held_out_ok = split_ok(held_out_before, held_out_after)
    no_harm = held_in_ok and held_out_ok and not regressions
    return {
        "proposal_id": proposal.get("optimizer_version", "harness_optimizer_v1"),
        "status": "validated_retain" if no_harm else "validated_reject",
        "validation": {
            "held_in_ok": bool(held_in_ok),
            "held_out_ok": bool(held_out_ok),
            "no_harm_gate": bool(no_harm),
            "regressions": regressions,
        },
        "held_out_required": True,
    }


def build_harness_optimizer_record(*, trace: Mapping[str, Any], memory_record: Mapping[str, Any]) -> Dict[str, Any]:
    memories = memory_record.get("topic_strategy") if isinstance(memory_record.get("topic_strategy"), Mapping) else {}
    proposal = propose_harness_optimization(trace=trace, memories={"topic_strategy": memories})
    return {
        "enabled": True,
        "memory_used": bool(memories),
        "proposal": proposal,
        "held_out_required": True,
        "status": proposal.get("status", "proposed_unvalidated"),
    }
