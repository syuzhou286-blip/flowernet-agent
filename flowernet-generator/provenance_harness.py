"""Provenance-gated validation and bounded repair for research reports.

This module is deliberately deterministic.  It defines the contract that a
learned trace verifier can later score, while making unsupported quantitative
claims and experiment/report inconsistencies impossible to silently average
away in a generic quality score.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, Iterable, List, Mapping, Sequence


_NUMBER = re.compile(r"(?<![A-Za-z0-9])[-+]?\d+(?:\.\d+)?%?")
_CITATION = re.compile(r"\[(\d+)\]")
_SPLITS = ("train", "training", "validation", "valid", "dev", "test", "testing")


def _id(*parts: Any, prefix: str) -> str:
    raw = "\n".join(str(part or "") for part in parts)
    return f"{prefix}_{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:12]}"


def _float(value: Any) -> float | None:
    try:
        return float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None


def _numbers(text: Any) -> List[float]:
    values: List[float] = []
    for match in _NUMBER.findall(str(text or "")):
        value = _float(match)
        if value is not None:
            values.append(value)
    return values


def _split_mentions(text: Any) -> set[str]:
    lower = str(text or "").lower()
    normalized: set[str] = set()
    for split in _SPLITS:
        if re.search(rf"\b{re.escape(split)}\b", lower):
            normalized.add(
                "train" if split in {"train", "training"}
                else "validation" if split in {"validation", "valid", "dev"}
                else "test"
            )
    return normalized


def _claims(trace: Mapping[str, Any]) -> List[Dict[str, Any]]:
    direct = trace.get("claims")
    if isinstance(direct, list):
        return [dict(item) for item in direct if isinstance(item, Mapping)]
    graph = trace.get("claim_evidence_graph")
    if isinstance(graph, Mapping) and isinstance(graph.get("claims"), list):
        return [dict(item) for item in graph["claims"] if isinstance(item, Mapping)]
    return []


def _artifacts(trace: Mapping[str, Any]) -> List[Dict[str, Any]]:
    for key in ("artifacts", "experiment_artifacts", "runs"):
        value = trace.get(key)
        if isinstance(value, list):
            return [dict(item) for item in value if isinstance(item, Mapping)]
    return []


def _source_spans(claim: Mapping[str, Any]) -> List[Dict[str, Any]]:
    spans = claim.get("source_spans")
    if isinstance(spans, list):
        return [dict(item) for item in spans if isinstance(item, Mapping)]
    evidence = claim.get("supporting_evidence")
    if isinstance(evidence, list):
        return [dict(item) for item in evidence if isinstance(item, Mapping)]
    return []


def _artifact_refs(claim: Mapping[str, Any]) -> List[Dict[str, Any]]:
    refs = claim.get("artifact_refs")
    return [dict(item) for item in refs if isinstance(item, Mapping)] if isinstance(refs, list) else []


def _artifact_index(artifacts: Sequence[Mapping[str, Any]]) -> Dict[str, Mapping[str, Any]]:
    return {
        str(item.get("run_id")): item
        for item in artifacts
        if str(item.get("run_id") or "").strip()
    }


def _defect(claim_id: str, kind: str, severity: str, evidence: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "defect_id": _id(claim_id, kind, evidence, prefix="defect"),
        "claim_id": claim_id,
        "kind": kind,
        "severity": severity,
        "evidence": dict(evidence),
    }


def validate_research_provenance(trace: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate claim→source/artifact edges without an LLM judge.

    The accepted schema is intentionally permissive so existing FlowerNet
    claim graphs can be audited before migration.  Strong validation requires
    explicit ``source_spans`` and ``artifact_refs``; legacy source IDs count as
    references but never as entailment proof.
    """
    claims = _claims(trace)
    artifacts = _artifacts(trace)
    artifact_by_run = _artifact_index(artifacts)
    defects: List[Dict[str, Any]] = []
    supported = 0
    quantitative = 0

    for index, claim in enumerate(claims, 1):
        claim_id = str(claim.get("claim_id") or f"C{index}")
        text = str(claim.get("text") or claim.get("claim") or "")
        numbers = _numbers(text)
        spans = _source_spans(claim)
        refs = _artifact_refs(claim)
        cited_ids = {int(value) for value in _CITATION.findall(text)}
        declared_ids = {
            int(value) for value in claim.get("citation_source", [])
            if str(value).isdigit()
        } if isinstance(claim.get("citation_source"), list) else set()
        valid_span = any(
            str(span.get("quote") or span.get("text") or "").strip()
            and str(span.get("source_id") or span.get("url") or "").strip()
            and str(span.get("relation") or "supports") in {"supports", "entails"}
            for span in spans
        )

        if cited_ids or declared_ids or spans:
            if not valid_span:
                defects.append(_defect(claim_id, "citation_without_support_span", "high", {
                    "citation_ids": sorted(cited_ids | declared_ids),
                    "span_count": len(spans),
                }))
        elif not numbers:
            defects.append(_defect(claim_id, "unsupported_claim", "high", {"text": text[:240]}))

        if numbers:
            quantitative += 1
            if not refs:
                defects.append(_defect(claim_id, "quantitative_claim_without_artifact", "critical", {
                    "reported_values": numbers,
                }))
            for ref in refs:
                run_id = str(ref.get("run_id") or "")
                artifact = artifact_by_run.get(run_id)
                if artifact is None:
                    defects.append(_defect(claim_id, "missing_run_artifact", "critical", {"run_id": run_id}))
                    continue
                artifact_values = _numbers([
                    artifact.get("value"), artifact.get("metrics"), artifact.get("result")
                ])
                if numbers and artifact_values and not any(
                    abs(reported - observed) <= max(1e-6, abs(observed) * 1e-4)
                    for reported in numbers for observed in artifact_values
                ):
                    defects.append(_defect(claim_id, "numeric_trace_mismatch", "critical", {
                        "run_id": run_id,
                        "reported_values": numbers,
                        "artifact_values": artifact_values,
                    }))
                claim_splits = _split_mentions(text) | _split_mentions(ref.get("split"))
                artifact_splits = _split_mentions(artifact.get("split"))
                if claim_splits and artifact_splits and claim_splits.isdisjoint(artifact_splits):
                    defects.append(_defect(claim_id, "dataset_split_mismatch", "critical", {
                        "run_id": run_id,
                        "reported_splits": sorted(claim_splits),
                        "artifact_splits": sorted(artifact_splits),
                    }))

        claim_defects = [item for item in defects if item["claim_id"] == claim_id]
        if not claim_defects:
            supported += 1

    report = str(trace.get("report") or trace.get("draft") or "").lower()
    undisclosed_negative_runs = []
    for artifact in artifacts:
        status = str(artifact.get("status") or artifact.get("outcome") or "").lower()
        run_id = str(artifact.get("run_id") or "")
        is_negative = status in {"failed", "negative", "null", "regressed"} or bool(artifact.get("regressed"))
        if is_negative and run_id and run_id.lower() not in report:
            undisclosed_negative_runs.append(run_id)
    if undisclosed_negative_runs:
        defects.append(_defect("REPORT", "negative_result_omission", "high", {
            "run_ids": undisclosed_negative_runs,
        }))

    critical = [item for item in defects if item["severity"] == "critical"]
    return {
        "version": "trace2claim_v1",
        "valid": not defects,
        "hard_gate_passed": not critical,
        "claim_count": len(claims),
        "quantitative_claim_count": quantitative,
        "supported_claim_rate": round(supported / max(1, len(claims)), 4),
        "defect_count": len(defects),
        "critical_defect_count": len(critical),
        "defects": defects,
        "undisclosed_negative_runs": undisclosed_negative_runs,
    }


_ACTION_BY_DEFECT = {
    "citation_without_support_span": "retrieve_support",
    "unsupported_claim": "mark_uncertain",
    "quantitative_claim_without_artifact": "bind_run_artifact",
    "missing_run_artifact": "bind_run_artifact",
    "numeric_trace_mismatch": "correct_numeric_claim",
    "dataset_split_mismatch": "correct_numeric_claim",
    "negative_result_omission": "restore_negative_result",
}


def propose_selective_repairs(audit: Mapping[str, Any], *, max_repairs: int = 8) -> List[Dict[str, Any]]:
    """Turn typed defects into edge-local repair proposals, never a full rewrite."""
    proposals: List[Dict[str, Any]] = []
    for defect in audit.get("defects", []) if isinstance(audit.get("defects"), list) else []:
        if not isinstance(defect, Mapping):
            continue
        kind = str(defect.get("kind") or "")
        action = _ACTION_BY_DEFECT.get(kind, "mark_uncertain")
        proposals.append({
            "repair_id": _id(defect.get("defect_id"), action, prefix="repair"),
            "defect_id": defect.get("defect_id", ""),
            "claim_id": defect.get("claim_id", ""),
            "action": action,
            "scope": "claim_edge" if defect.get("claim_id") != "REPORT" else "report_disclosure",
            "full_rewrite_allowed": False,
            "requires_provenance": action not in {"mark_uncertain", "restore_negative_result"},
            "status": "proposed",
        })
        if len(proposals) >= max(1, int(max_repairs or 1)):
            break
    return proposals


def accept_repair_candidate(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    before_no_harm: Mapping[str, Any] | None = None,
    after_no_harm: Mapping[str, Any] | None = None,
    tolerance: float = 0.001,
) -> Dict[str, Any]:
    """Apply a hard provenance improvement and external no-harm gate."""
    provenance_improved = (
        int(after.get("critical_defect_count", 0)) < int(before.get("critical_defect_count", 0))
        or (
            int(after.get("critical_defect_count", 0)) == int(before.get("critical_defect_count", 0))
            and int(after.get("defect_count", 0)) < int(before.get("defect_count", 0))
        )
    )
    regressions: Dict[str, float] = {}
    before_no_harm = before_no_harm or {}
    after_no_harm = after_no_harm or {}
    for metric in ("artifact_success", "question_coverage", "long_document_quality"):
        if metric not in before_no_harm:
            continue
        delta = float(after_no_harm.get(metric, before_no_harm[metric])) - float(before_no_harm[metric])
        if delta < -abs(tolerance):
            regressions[metric] = round(delta, 6)
    accepted = provenance_improved and not regressions
    return {
        "accepted": accepted,
        "decision": "accept" if accepted else "rollback",
        "provenance_improved": provenance_improved,
        "no_harm_passed": not regressions,
        "regressions": regressions,
        "rule": "accept iff provenance improves and every external no-harm metric is non-regressing",
    }
