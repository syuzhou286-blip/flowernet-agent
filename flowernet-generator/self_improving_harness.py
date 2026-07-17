"""Self-improving harness loop for FlowerNet.

This module turns failed or weak generation traces into bounded, auditable
harness-improvement proposals. It does not edit code, prompts, thresholds, or
policies by itself. A proposal is retained only when validation evidence shows
held-in improvement, held-out improvement, and no-harm compliance.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, Iterable, List, Mapping, Sequence


INTERNAL_METRICS = (
    "quality_score",
    "source_alignment_score",
    "claim_support_rate",
    "novelty_confidence",
)
EXTERNAL_METRICS = ("rouge1", "rouge2", "rougeL", "bertscore_f1")


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _stable_id(*parts: Any, prefix: str = "fh") -> str:
    raw = "\n".join(str(part or "") for part in parts)
    return f"{prefix}_{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:12]}"


def _nested(mapping: Mapping[str, Any], path: Sequence[str], default: Any = None) -> Any:
    cur: Any = mapping
    for key in path:
        if not isinstance(cur, Mapping):
            return default
        cur = cur.get(key)
    return default if cur is None else cur


def _claim_graph(trace: Mapping[str, Any]) -> Dict[str, Any]:
    direct = trace.get("claim_evidence_graph")
    if isinstance(direct, dict):
        return direct
    ri = trace.get("research_intelligence")
    if isinstance(ri, dict) and isinstance(ri.get("claim_evidence_graph"), dict):
        return ri["claim_evidence_graph"]
    verification = trace.get("verification")
    if isinstance(verification, dict) and isinstance(verification.get("claim_evidence_graph"), dict):
        return verification["claim_evidence_graph"]
    return {}


def _novelty(trace: Mapping[str, Any]) -> Dict[str, Any]:
    direct = trace.get("research_novelty")
    if isinstance(direct, dict):
        return direct
    ri = trace.get("research_intelligence")
    if isinstance(ri, dict) and isinstance(ri.get("novelty"), dict):
        return ri["novelty"]
    verification = trace.get("verification")
    if isinstance(verification, dict) and isinstance(verification.get("research_novelty"), dict):
        return verification["research_novelty"]
    return {}


def _verification(trace: Mapping[str, Any]) -> Dict[str, Any]:
    value = trace.get("verification")
    return value if isinstance(value, dict) else {}


def _controller_events(trace: Mapping[str, Any]) -> List[Dict[str, Any]]:
    bandit = trace.get("bandit")
    if isinstance(bandit, dict) and isinstance(bandit.get("events"), list):
        return [event for event in bandit["events"] if isinstance(event, dict)]
    verification = _verification(trace)
    outcome = verification.get("controller_realized_outcome")
    if isinstance(outcome, dict):
        return [{"controller_realized_outcome": outcome, **outcome}]
    return []


def _source_check_failed(verification: Mapping[str, Any]) -> bool:
    source_check = verification.get("source_check")
    if isinstance(source_check, dict):
        reason = str(source_check.get("reason") or "").lower()
        return (
            source_check.get("passed") is False
            or "unsupported" in reason
            or "citation" in reason
            or "low_semantic" in reason
            or "invalid_source" in reason
        )
    feedback = str(verification.get("feedback") or "").lower()
    return "citation" in feedback or "unsupported" in feedback or "source" in feedback


def _generic_claim_ratio(graph: Mapping[str, Any]) -> float:
    claims = [claim for claim in graph.get("claims", []) if isinstance(claim, dict)]
    if not claims:
        return 0.0
    generic_terms = {
        "important", "significant", "various", "different", "several",
        "many", "better", "improve", "system", "approach", "method",
        "research", "paper", "study",
    }
    generic = 0
    for claim in claims:
        text = str(claim.get("claim") or "").lower()
        tokens = re.findall(r"[a-z][a-z0-9_-]{2,}", text)
        content_tokens = [tok for tok in tokens if tok not in generic_terms]
        weak_support = str(claim.get("reviewer_risk_tag") or "") in {"insufficient-evidence", "needs-caveat"}
        if len(set(content_tokens)) < 5 or weak_support:
            generic += 1
    return generic / max(1, len(claims))


def mine_weaknesses(trace: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Identify weakness patterns from a single subsection/document trace."""
    system = str(trace.get("system") or trace.get("document_id") or "flowernet")
    topic_id = str(trace.get("topic_id") or trace.get("subsection_id") or trace.get("document_id") or "")
    verification = _verification(trace)
    novelty = _novelty(trace)
    graph = _claim_graph(trace)
    weaknesses: List[Dict[str, Any]] = []

    def add(kind: str, severity: float, evidence: Dict[str, Any], component: str) -> None:
        weaknesses.append({
            "weakness_id": _stable_id(system, topic_id, kind, evidence, prefix="weak"),
            "kind": kind,
            "severity": round(max(0.0, min(1.0, float(severity))), 4),
            "component": component,
            "evidence": evidence,
        })

    novelty_conf = _as_float(novelty.get("novelty_confidence"), 1.0)
    reviewer_risk = str(novelty.get("reviewer_risk") or "").lower()
    if novelty and (novelty_conf < 0.45 or reviewer_risk == "high"):
        add(
            "novelty_weak",
            max(0.0, 0.55 - novelty_conf) + (0.25 if reviewer_risk == "high" else 0.0),
            {
                "novelty_confidence": novelty_conf,
                "reviewer_risk": reviewer_risk,
                "literature_gap_count": len(novelty.get("literature_gaps", []) or []),
            },
            "novelty_miner",
        )

    claim_support = _as_float(graph.get("claim_support_rate"), 1.0)
    if graph and claim_support < 0.62:
        add(
            "claim_evidence_weak",
            0.62 - claim_support,
            {
                "claim_support_rate": claim_support,
                "claim_count": len(graph.get("claims", []) or []),
            },
            "claim_evidence_graph",
        )

    generic_ratio = _generic_claim_ratio(graph)
    if graph and generic_ratio >= 0.40:
        add(
            "claim_generic",
            generic_ratio,
            {
                "generic_claim_ratio": round(generic_ratio, 4),
                "claim_count": len(graph.get("claims", []) or []),
            },
            "claim_evidence_graph",
        )

    if _source_check_failed(verification):
        source_check = verification.get("source_check") if isinstance(verification.get("source_check"), dict) else {}
        add(
            "citation_unsupported",
            0.75,
            {
                "source_reason": str(source_check.get("reason") or verification.get("feedback") or "")[:240],
                "reference_count": int(source_check.get("reference_count", 0) or 0),
            },
            "verifier",
        )

    external = trace.get("external_metrics")
    baseline = trace.get("baseline_external_metrics")
    if isinstance(external, dict) and isinstance(baseline, dict):
        losses: Dict[str, float] = {}
        for metric in EXTERNAL_METRICS:
            gap = _as_float(baseline.get(metric)) - _as_float(external.get(metric))
            if gap > 0.002:
                losses[metric] = round(gap, 6)
        if losses:
            add(
                "external_metrics_low",
                min(1.0, sum(losses.values()) * 8.0),
                {"metric_losses": losses},
                "external_evaluator",
            )

    harmful_events = []
    for event in _controller_events(trace):
        reward = _as_float(event.get("realized_reward", event.get("reward")), 0.0)
        regressed = bool(event.get("realized_regressed") or event.get("regressed"))
        effective = bool(event.get("realized_effective", event.get("effective", False)))
        if regressed or reward <= 0.0 or not effective:
            harmful_events.append({
                "selected_arm": event.get("selected_arm", event.get("arm", "")),
                "realized_reward": reward,
                "realized_regressed": regressed,
                "realized_effective": effective,
            })
    if harmful_events:
        add(
            "controller_harmful",
            min(1.0, 0.35 + 0.18 * len(harmful_events)),
            {"harmful_events": harmful_events[:4]},
            "controller",
        )

    status = str(trace.get("status") or "").lower()
    success = trace.get("success")
    if success is False or status in {"failed", "invalid_incomplete_components", "error"}:
        add(
            "run_failed",
            0.90,
            {"status": status, "error": str(trace.get("error") or trace.get("reason") or "")[:240]},
            "orchestrator",
        )

    return sorted(weaknesses, key=lambda item: item["severity"], reverse=True)


_PROPOSAL_BY_WEAKNESS: Dict[str, Dict[str, Any]] = {
    "novelty_weak": {
        "target_component": "novelty_miner",
        "minimal_change": "tighten literature-gap extraction and add reviewer-risk-specific novelty prompts",
        "proposal_type": "prompt_and_scoring_patch",
        "validation_focus": ["novelty_confidence", "claim_support_rate", "bertscore_f1"],
    },
    "claim_evidence_weak": {
        "target_component": "claim_evidence_graph",
        "minimal_change": "require each planned claim to retain at least one high-support RAG source and caveat",
        "proposal_type": "graph_planning_patch",
        "validation_focus": ["claim_support_rate", "quality_score", "source_alignment_score"],
    },
    "claim_generic": {
        "target_component": "claim_evidence_graph",
        "minimal_change": "replace generic claim templates with source-specific mechanism, benchmark, limitation, or contribution claims",
        "proposal_type": "claim_specificity_patch",
        "validation_focus": ["claim_support_rate", "novelty_confidence", "rouge2"],
    },
    "citation_unsupported": {
        "target_component": "verifier",
        "minimal_change": "raise source-faithfulness gate and force citation repair before accepting the subsection",
        "proposal_type": "verifier_gate_patch",
        "validation_focus": ["source_alignment_score", "claim_support_rate", "quality_score"],
    },
    "external_metrics_low": {
        "target_component": "controller",
        "minimal_change": "add external-metric proxy no-harm constraints to pass-candidate selection",
        "proposal_type": "controller_reward_patch",
        "validation_focus": ["rouge1", "rouge2", "rougeL", "bertscore_f1"],
    },
    "controller_harmful": {
        "target_component": "controller",
        "minimal_change": "cool down regressing arms and require realized targeted gain before retaining a repair",
        "proposal_type": "controller_policy_patch",
        "validation_focus": ["quality_score", "claim_support_rate", "source_alignment_score"],
    },
    "run_failed": {
        "target_component": "orchestrator",
        "minimal_change": "record failure trace and use bounded retry/fallback diagnostics without marking failed runs as success",
        "proposal_type": "telemetry_and_guard_patch",
        "validation_focus": ["quality_score", "claim_support_rate"],
    },
}


def propose_harness_updates(
    weaknesses: Sequence[Mapping[str, Any]],
    *,
    max_proposals: int = 3,
) -> List[Dict[str, Any]]:
    """Convert weakness records into bounded harness proposals."""
    proposals: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for weakness in sorted(weaknesses, key=lambda item: _as_float(item.get("severity")), reverse=True):
        kind = str(weakness.get("kind") or "")
        template = _PROPOSAL_BY_WEAKNESS.get(kind)
        if not template:
            continue
        target = str(template["target_component"])
        proposal_key = f"{kind}:{target}"
        if proposal_key in seen:
            continue
        seen.add(proposal_key)
        focus = list(template["validation_focus"])
        proposal = {
            "proposal_id": _stable_id(kind, target, weakness.get("evidence"), prefix="prop"),
            "source_weakness_id": weakness.get("weakness_id", ""),
            "weakness_kind": kind,
            "target_component": target,
            "proposal_type": template["proposal_type"],
            "minimal_change": template["minimal_change"],
            "bounded": True,
            "must_not_change": [
                "held_out_references",
                "raw_experiment_outputs",
                "benchmark_topic_selection",
                "RAG source truthfulness",
            ],
            "no_harm_metrics": sorted(set(focus + ["citation_faithfulness", "readability"])),
            "validation_plan": {
                "held_in": "rerun affected development topics before/after the proposal",
                "held_out": "rerun frozen held-out topics without changing references or topics",
                "acceptance_rule": "retain only if internal metrics, external metrics, and no-harm gates improve or remain safe on both splits",
            },
            "status": "proposed_unvalidated",
        }
        proposals.append(proposal)
        if len(proposals) >= max(1, int(max_proposals or 1)):
            break
    return proposals


def _mean_delta(before: Mapping[str, Any], after: Mapping[str, Any], metrics: Iterable[str]) -> float:
    deltas = [_as_float(after.get(metric)) - _as_float(before.get(metric)) for metric in metrics if metric in before or metric in after]
    return sum(deltas) / max(1, len(deltas))


def _metric_regressions(before: Mapping[str, Any], after: Mapping[str, Any], metrics: Iterable[str], tolerance: float) -> Dict[str, float]:
    regressions: Dict[str, float] = {}
    for metric in metrics:
        if metric not in before and metric not in after:
            continue
        delta = _as_float(after.get(metric)) - _as_float(before.get(metric))
        if delta < -abs(tolerance):
            regressions[metric] = round(delta, 6)
    return regressions


def validate_harness_proposal(
    proposal: Mapping[str, Any],
    *,
    held_in_before: Mapping[str, Any],
    held_in_after: Mapping[str, Any],
    held_out_before: Mapping[str, Any],
    held_out_after: Mapping[str, Any],
    min_internal_gain: float = 0.0,
    min_external_gain: float = 0.0,
    no_harm_tolerance: float = 0.001,
) -> Dict[str, Any]:
    """Validate a harness proposal with held-in and held-out evidence."""
    held_in_internal = _mean_delta(held_in_before, held_in_after, INTERNAL_METRICS)
    held_out_internal = _mean_delta(held_out_before, held_out_after, INTERNAL_METRICS)
    held_in_external = _mean_delta(held_in_before, held_in_after, EXTERNAL_METRICS)
    held_out_external = _mean_delta(held_out_before, held_out_after, EXTERNAL_METRICS)
    all_metrics = sorted(set(INTERNAL_METRICS + EXTERNAL_METRICS))
    regressions = {
        "held_in": _metric_regressions(held_in_before, held_in_after, all_metrics, no_harm_tolerance),
        "held_out": _metric_regressions(held_out_before, held_out_after, all_metrics, no_harm_tolerance),
    }
    no_harm_passed = not regressions["held_in"] and not regressions["held_out"]
    held_in_passed = held_in_internal >= min_internal_gain and held_in_external >= min_external_gain
    held_out_passed = held_out_internal >= min_internal_gain and held_out_external >= min_external_gain
    retained = bool(held_in_passed and held_out_passed and no_harm_passed)
    return {
        "proposal_id": proposal.get("proposal_id", ""),
        "status": "validated_retain" if retained else "validated_reject",
        "retained": retained,
        "held_in_internal_delta": round(held_in_internal, 6),
        "held_out_internal_delta": round(held_out_internal, 6),
        "held_in_external_delta": round(held_in_external, 6),
        "held_out_external_delta": round(held_out_external, 6),
        "held_in_passed": bool(held_in_passed),
        "held_out_passed": bool(held_out_passed),
        "no_harm_passed": bool(no_harm_passed),
        "regressions": regressions,
        "decision_rule": "retain iff held-in, held-out, and no-harm gates all pass",
    }


def build_self_improving_harness_record(
    trace: Mapping[str, Any],
    *,
    max_proposals: int = 3,
) -> Dict[str, Any]:
    """Build a weakness-mining and proposal record for one trace."""
    weaknesses = mine_weaknesses(trace)
    proposals = propose_harness_updates(weaknesses, max_proposals=max_proposals)
    return {
        "enabled": True,
        "weaknesses": weaknesses,
        "proposals": proposals,
        "weakness_count": len(weaknesses),
        "proposal_count": len(proposals),
        "status": "proposal_ready" if proposals else ("no_weakness_detected" if not weaknesses else "no_bounded_proposal"),
    }

