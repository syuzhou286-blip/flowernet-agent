"""Evolvable reviewer utilities for FlowerNet.

This module turns verifier outputs into an auditable mini-review. It is
deterministic by default: it uses only the draft, task, FlowerNet verifier
signals, research-intelligence records, and optional external metrics. It does
not read evaluation references or fabricate evidence.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Mapping, Optional


_REVIEWER_DIMENSIONS = (
    "citation_faithfulness",
    "claim_support",
    "novelty_strength",
    "logical_coherence",
    "experimental_completeness",
    "reviewer_concern_prediction",
    "reproducibility_risk",
)

_EXTERNAL_KEYS = ("rouge1", "rouge2", "rougeL", "bertscore_f1")


def _clip01(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(default)
    return max(0.0, min(1.0, number))


def _text(text: Any) -> str:
    return str(text or "")


def _word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z][A-Za-z0-9_-]*|[\u4e00-\u9fff]", text or ""))


def _citation_count(text: str) -> int:
    numeric = len(re.findall(r"\[[0-9,\s-]+\]", text or ""))
    doi_like = len(re.findall(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+\b", text or ""))
    url_like = len(re.findall(r"https?://\S+", text or ""))
    return numeric + doi_like + url_like


def _source_check_score(source_check: Mapping[str, Any]) -> float:
    if not source_check:
        return 0.0
    base = 0.78 if source_check.get("passed") else 0.25
    matched = (
        source_check.get("matched_source_count")
        or source_check.get("matched_count")
        or source_check.get("reference_count")
        or 0
    )
    base += min(0.18, 0.06 * max(0, int(matched or 0)))
    low_semantic = len(source_check.get("low_semantic_urls") or [])
    blacklist = len(source_check.get("blacklist_matches") or [])
    base -= min(0.30, 0.10 * low_semantic + 0.15 * blacklist)
    return _clip01(base)


def _external_mean(external_metrics: Optional[Mapping[str, Any]]) -> Optional[float]:
    if not external_metrics:
        return None
    vals = []
    for key in _EXTERNAL_KEYS:
        if key in external_metrics and external_metrics.get(key) is not None:
            vals.append(_clip01(external_metrics.get(key)))
    if not vals:
        return None
    return sum(vals) / len(vals)


def _dimension(name: str, score: float, evidence: Dict[str, Any], concern: str = "") -> Dict[str, Any]:
    bounded_score = round(_clip01(score), 4)
    if bounded_score >= 0.75:
        risk = "low"
    elif bounded_score >= 0.55:
        risk = "medium"
    else:
        risk = "high"
    return {
        "score": bounded_score,
        "risk": risk,
        "evidence": evidence,
        "concern": concern,
        "calibratable": True,
        "dimension": name,
    }


def build_reviewer_assessment(
    *,
    draft: str,
    outline: str,
    verification: Mapping[str, Any],
    research_intelligence: Optional[Mapping[str, Any]] = None,
    external_metrics: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a source-bound mini-review from existing FlowerNet signals."""
    draft_text = _text(draft)
    outline_text = _text(outline)
    verification = verification or {}
    research_intelligence = research_intelligence or {}

    quality_dims = verification.get("quality_dimensions") or {}
    uncertainty = verification.get("quality_dimensions_uncertainty") or {}
    source_check = verification.get("source_check") or {}
    evidence_diag = verification.get("evidence_diagnostics") or {}
    novelty_diag = verification.get("novelty_diagnostics") or {}

    novelty = research_intelligence.get("novelty") or verification.get("research_novelty") or {}
    graph = research_intelligence.get("claim_evidence_graph") or verification.get("claim_evidence_graph") or {}
    claims = graph.get("claims") or []
    claim_support_rate = _clip01(graph.get("claim_support_rate"), default=0.0)
    if not claims and "claim_support_rate" not in graph:
        claim_support_rate = _clip01(quality_dims.get("evidence_grounding"), default=0.0)

    citation_score = _source_check_score(source_check)
    if _citation_count(draft_text) == 0 and source_check:
        citation_score = min(citation_score, 0.52)

    novelty_conf = _clip01(
        novelty.get("novelty_confidence", novelty_diag.get("information_gain", quality_dims.get("novelty", 0.0)))
    )
    novelty_risk = str(novelty.get("reviewer_risk") or "").lower()
    if novelty_risk == "high":
        novelty_conf = min(novelty_conf, 0.48)
    elif novelty_risk == "medium":
        novelty_conf = min(0.82, novelty_conf)

    logical = _clip01(quality_dims.get("logical_coherence", verification.get("quality_score", 0.0)))
    coherence_unc = _clip01(uncertainty.get("logical_coherence"), default=0.0)
    if coherence_unc > 0.30:
        logical -= min(0.08, coherence_unc * 0.12)

    lower_all = f"{draft_text}\n{outline_text}".lower()
    experiment_terms = [
        "experiment", "experimental", "benchmark", "dataset", "metric", "result",
        "ablation", "baseline", "evaluation", "reproducible", "code", "protocol",
    ]
    experiment_hits = sum(1 for term in experiment_terms if term in lower_all)
    experimental = _clip01(
        0.35 * _clip01(quality_dims.get("coverage_completeness"), default=0.0)
        + 0.35 * min(1.0, experiment_hits / 5.0)
        + 0.30 * _clip01(evidence_diag.get("source_topic_coverage"), default=0.0)
    )

    reproducibility_terms = ["code", "dataset", "protocol", "parameter", "implementation", "reproduce", "replicate"]
    reproducibility_hits = sum(1 for term in reproducibility_terms if term in lower_all)
    reproducibility_score = _clip01(
        0.44 * citation_score
        + 0.26 * min(1.0, reproducibility_hits / 4.0)
        + 0.30 * experimental
    )

    ext_mean = _external_mean(external_metrics)
    internal_mean = sum(
        [
            citation_score,
            claim_support_rate,
            novelty_conf,
            logical,
            experimental,
            reproducibility_score,
        ]
    ) / 6.0
    if ext_mean is None:
        aligned = None
        alignment_gap = None
        concern_score = internal_mean
    else:
        alignment_gap = round(float(internal_mean - ext_mean), 4)
        aligned = abs(alignment_gap) <= 0.12 or ext_mean >= 0.78
        concern_score = _clip01(internal_mean - max(0.0, abs(alignment_gap) - 0.08) * 0.80)

    generic_penalty = 0.0
    if _word_count(draft_text) < 80:
        generic_penalty += 0.08
    if len(claims) == 0:
        generic_penalty += 0.08
    concern_score = _clip01(concern_score - generic_penalty)

    dims = {
        "citation_faithfulness": _dimension(
            "citation_faithfulness",
            citation_score,
            {"source_check_passed": bool(source_check.get("passed")), "citation_count": _citation_count(draft_text)},
            "" if citation_score >= 0.55 else "Citations are missing, unsupported, or weakly matched.",
        ),
        "claim_support": _dimension(
            "claim_support",
            claim_support_rate,
            {"claim_count": len(claims), "claim_support_rate": round(claim_support_rate, 4)},
            "" if claim_support_rate >= 0.55 else "Claims are not sufficiently bound to evidence.",
        ),
        "novelty_strength": _dimension(
            "novelty_strength",
            novelty_conf,
            {"novelty_confidence": round(novelty_conf, 4), "reviewer_risk": novelty_risk or "unknown"},
            "" if novelty_conf >= 0.55 else "Novelty is weak or likely to be challenged by reviewers.",
        ),
        "logical_coherence": _dimension(
            "logical_coherence",
            logical,
            {"logical_coherence": round(_clip01(quality_dims.get("logical_coherence")), 4), "uncertainty": coherence_unc},
            "" if logical >= 0.55 else "Argument structure is not coherent enough.",
        ),
        "experimental_completeness": _dimension(
            "experimental_completeness",
            experimental,
            {"experiment_term_hits": experiment_hits},
            "" if experimental >= 0.55 else "Experiment, benchmark, dataset, or evaluation details are incomplete.",
        ),
        "reviewer_concern_prediction": _dimension(
            "reviewer_concern_prediction",
            concern_score,
            {"internal_mean": round(internal_mean, 4), "external_mean": None if ext_mean is None else round(ext_mean, 4)},
            "" if concern_score >= 0.55 else "Internal signals and external metrics suggest likely reviewer concerns.",
        ),
        "reproducibility_risk": _dimension(
            "reproducibility_risk",
            reproducibility_score,
            {"reproducibility_term_hits": reproducibility_hits},
            "" if reproducibility_score >= 0.55 else "The draft lacks reproducibility anchors such as data, code, protocol, or parameters.",
        ),
    }

    score = sum(item["score"] for item in dims.values()) / len(dims)
    concerns = [
        item["concern"]
        for item in dims.values()
        if item.get("concern")
    ]
    if ext_mean is not None and aligned is False:
        concerns.append("External metrics are not aligned with the internal reviewer assessment.")

    if score >= 0.74 and not any(item["risk"] == "high" for item in dims.values()):
        decision = "accept_with_minor_revision"
    elif score >= 0.58:
        decision = "borderline_revision"
    else:
        decision = "major_revision"

    return {
        "enabled": True,
        "reviewer_version": "evolvable_reviewer_v1",
        "reviewer_dimensions": dims,
        "overall_reviewer_score": round(score, 4),
        "reviewer_decision": decision,
        "reviewer_concerns": concerns,
        "external_alignment": {
            "available": ext_mean is not None,
            "external_mean": None if ext_mean is None else round(ext_mean, 4),
            "internal_mean": round(internal_mean, 4),
            "alignment_gap": alignment_gap,
            "aligned_with_external_metrics": aligned,
            "metrics": dict(external_metrics or {}),
        },
        "auditability": {
            "source_bound": True,
            "uses_evaluation_references": False,
            "uses_claim_evidence_graph": bool(claims),
            "uses_external_metrics": ext_mean is not None,
        },
    }


def propose_reviewer_calibration(
    *,
    reviewer_record: Mapping[str, Any],
    before: Mapping[str, Any],
    after_held_in: Mapping[str, Any],
    after_held_out: Mapping[str, Any],
    tolerance: float = 0.001,
) -> Dict[str, Any]:
    """Validate a bounded reviewer calibration proposal against no-harm gates."""
    keys = sorted(set(before.keys()) | set(after_held_in.keys()) | set(after_held_out.keys()))
    regressions = []
    improvements = []
    for split_name, after in (("held_in", after_held_in), ("held_out", after_held_out)):
        for key in keys:
            if key not in before or key not in after:
                continue
            delta = round(float(after[key]) - float(before[key]), 6)
            if delta < -abs(tolerance):
                regressions.append({"split": split_name, "metric": key, "delta": delta})
            elif delta > abs(tolerance):
                improvements.append({"split": split_name, "metric": key, "delta": delta})

    required = ("reviewer_score", "external_mean")
    held_in_ok = all(float(after_held_in.get(key, before.get(key, 0.0))) >= float(before.get(key, 0.0)) - abs(tolerance) for key in required)
    held_out_ok = all(float(after_held_out.get(key, before.get(key, 0.0))) >= float(before.get(key, 0.0)) - abs(tolerance) for key in required)
    no_harm = not regressions and held_in_ok and held_out_ok

    weak_dims = [
        name
        for name, data in (reviewer_record.get("reviewer_dimensions") or {}).items()
        if isinstance(data, Mapping) and _clip01(data.get("score"), default=1.0) < 0.55
    ]
    if reviewer_record.get("external_alignment", {}).get("aligned_with_external_metrics") is False:
        weak_dims.append("external_alignment")

    return {
        "proposal_id": "reviewer_calibration_v1",
        "target_component": "evolvable_verifier_reviewer",
        "proposal_type": "calibration_update",
        "minimal_change": "adjust reviewer dimension weights, prompts, or thresholds only for diagnosed weak dimensions",
        "bounded": True,
        "weak_dimensions": list(dict.fromkeys(weak_dims)),
        "must_not_change": [
            "raw experiment outputs",
            "held-out references",
            "benchmark topic selection",
            "retrieved source truthfulness",
        ],
        "validation": {
            "held_in_ok": bool(held_in_ok),
            "held_out_ok": bool(held_out_ok),
            "no_harm_gate": bool(no_harm),
            "regressions": regressions,
            "improvements": improvements,
        },
        "status": "validated_retain" if no_harm else "validated_reject",
    }
