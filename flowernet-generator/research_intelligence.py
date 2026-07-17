"""Research-intelligence utilities for FlowerNet.

The functions in this module are intentionally deterministic and source-bound:
they only use the task, outline, generated draft, and FlowerNet RAG sources.
They do not read evaluation references and do not invent citations.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Dict, Iterable, List, Sequence, Tuple


_STOPWORDS = {
    "the", "and", "for", "with", "from", "into", "that", "this", "these",
    "those", "were", "have", "has", "are", "was", "their", "which", "about",
    "using", "used", "based", "between", "within", "without", "section",
    "source", "paper", "study", "studies", "research", "article", "analysis",
    "method", "methods", "system", "systems", "approach", "approaches",
    "write", "academic", "report", "long", "form", "current", "include",
    "including", "overview", "discuss", "explain", "compare", "comparison",
}

_AXIS_PATTERNS: List[Tuple[str, Sequence[str]]] = [
    ("definition_boundary", ("definition", "taxonomy", "scope", "concept", "boundary", "framework")),
    ("mechanism_method", ("method", "architecture", "mechanism", "algorithm", "pipeline", "workflow", "model")),
    ("evidence_benchmark", ("benchmark", "evaluation", "metric", "dataset", "experiment", "result", "measurement")),
    ("application_deployment", ("application", "deployment", "production", "product", "case", "real-world", "workflow")),
    ("risk_limitation", ("risk", "limitation", "challenge", "failure", "bias", "uncertainty", "cost", "privacy")),
    ("future_gap", ("future", "open", "gap", "unresolved", "direction", "opportunity", "novelty")),
]

_NOVELTY_TYPE_BY_AXIS = {
    "definition_boundary": "conceptual reframing",
    "mechanism_method": "methodological synthesis",
    "evidence_benchmark": "evaluation contribution",
    "application_deployment": "application-grounded contribution",
    "risk_limitation": "risk-aware limitation analysis",
    "future_gap": "literature-gap contribution",
}


def _normalize_space(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _tokens(text: str) -> List[str]:
    return [
        tok.lower().strip("-_/.,;:!?()[]{}")
        for tok in re.findall(r"[A-Za-z][A-Za-z0-9+./_-]{2,}|[\u4e00-\u9fff]{2,12}", str(text or ""))
        if tok.lower() not in _STOPWORDS and not tok.isdigit()
    ]


def _phrases(text: str, *, limit: int = 12) -> List[str]:
    raw = _normalize_space(text).lower()
    candidates: List[str] = []
    for segment in re.split(r"[,;:()]|\b(?:and|or|versus|vs\.?|including)\b", raw):
        toks = [tok for tok in _tokens(segment) if len(tok) > 2]
        if 2 <= len(toks) <= 5:
            phrase = " ".join(toks)
            if phrase not in candidates:
                candidates.append(phrase)
    all_toks = _tokens(raw)
    for size in (4, 3, 2):
        for idx in range(max(0, len(all_toks) - size + 1)):
            phrase = " ".join(all_toks[idx : idx + size])
            if phrase not in candidates:
                candidates.append(phrase)
            if len(candidates) >= limit:
                return candidates[:limit]
    return candidates[:limit]


def _source_text(source: Dict[str, Any]) -> str:
    return _normalize_space(
        " ".join(
            str(source.get(key) or "")
            for key in ("title", "snippet", "summary", "description", "body", "abstract")
        )
    )


def _source_label(source: Dict[str, Any], idx: int) -> str:
    title = _normalize_space(source.get("title") or source.get("source_name") or f"Source {idx}")
    href = _normalize_space(source.get("href") or source.get("url") or source.get("link") or "")
    return f"{title} | {href}".strip(" |")


def _axis_hits(text: str) -> Dict[str, float]:
    lower = str(text or "").lower()
    scores: Dict[str, float] = {}
    for axis, terms in _AXIS_PATTERNS:
        hits = sum(1 for term in terms if term in lower)
        scores[axis] = min(1.0, hits / max(1, min(3, len(terms))))
    return scores


def _best_sources_for_terms(terms: Iterable[str], sources: Sequence[Dict[str, Any]], limit: int = 3) -> List[Dict[str, Any]]:
    wanted = [term.lower() for term in terms if str(term or "").strip()]
    scored: List[Tuple[float, int, Dict[str, Any]]] = []
    for idx, source in enumerate(sources, 1):
        text = _source_text(source).lower()
        if not text:
            continue
        hit = sum(1 for term in wanted if term in text)
        quality = float(source.get("quality_score", source.get("rerank_score", source.get("sbert_score", 0.0))) or 0.0)
        score = hit + min(1.0, max(0.0, quality))
        if score > 0:
            scored.append((score, idx, source))
    scored.sort(key=lambda row: row[0], reverse=True)
    out = []
    for score, idx, source in scored[:limit]:
        out.append({
            "source_id": idx,
            "title": _normalize_space(source.get("title") or f"Source {idx}")[:180],
            "url": _normalize_space(source.get("href") or source.get("url") or source.get("link") or ""),
            "support_score": round(float(score), 4),
        })
    return out


def analyze_literature_anchored_novelty(
    *,
    topic: str,
    section_title: str = "",
    subsection_title: str = "",
    outline: str,
    sources: Sequence[Dict[str, Any]],
    max_gaps: int = 4,
) -> Dict[str, Any]:
    """Return source-bound novelty diagnostics for one subsection."""
    task_text = _normalize_space(f"{topic} {section_title} {subsection_title} {outline}")
    source_blob = " ".join(_source_text(source) for source in sources if isinstance(source, dict))
    task_terms = list(dict.fromkeys(_tokens(task_text)))[:40]
    source_terms = set(_tokens(source_blob))
    task_phrases = _phrases(task_text, limit=16)
    source_phrases = set(_phrases(source_blob, limit=80))
    axis_need = _axis_hits(task_text)
    axis_source = _axis_hits(source_blob)
    resolved_terms = [term for term in task_terms if term in source_terms][:18]
    weak_terms = [term for term in task_terms if term not in source_terms][:18]
    phrase_coverage = (
        sum(1 for phrase in task_phrases if phrase in source_phrases) / max(1, min(10, len(task_phrases)))
        if task_phrases
        else 0.0
    )
    source_count = len([s for s in sources if isinstance(s, dict)])
    gaps: List[Dict[str, Any]] = []
    for axis, need in sorted(axis_need.items(), key=lambda row: row[1], reverse=True):
        if need <= 0 and len(gaps) >= 2:
            continue
        support = axis_source.get(axis, 0.0)
        axis_terms = [
            term for term in task_terms
            if any(anchor in term or term in anchor for anchor in dict(_AXIS_PATTERNS)[axis])
        ]
        if not axis_terms:
            axis_terms = task_terms[:8]
        contribution = _NOVELTY_TYPE_BY_AXIS.get(axis, "research synthesis contribution")
        risk_level = "high" if source_count < 2 or support < 0.20 else ("medium" if support < 0.50 else "low")
        novelty_confidence = max(0.05, min(0.95, 0.34 * need + 0.34 * support + 0.18 * phrase_coverage + 0.14 * min(1.0, source_count / 4.0)))
        evidence = _best_sources_for_terms(axis_terms + task_terms[:6], sources, limit=3)
        gaps.append({
            "axis": axis,
            "literature_gap": (
                f"The subsection needs a {axis.replace('_', ' ')} angle, but the retrieved literature "
                f"only partially covers it." if support < 0.55 else
                f"The retrieved literature covers {axis.replace('_', ' ')}, leaving room for a clearer synthesized contribution."
            ),
            "already_solved": resolved_terms[:8],
            "unresolved_challenge": weak_terms[:8] if weak_terms else [
                f"translate source-level findings into a coherent {axis.replace('_', ' ')} argument"
            ],
            "possible_contribution": contribution,
            "evidence_support": evidence,
            "reviewer_risk": risk_level,
            "novelty_confidence": round(novelty_confidence, 4),
        })
        if len(gaps) >= max(1, int(max_gaps or 1)):
            break
    aggregate_confidence = round(
        sum(float(gap["novelty_confidence"]) for gap in gaps) / max(1, len(gaps)),
        4,
    )
    return {
        "enabled": True,
        "source_bound": True,
        "topic_terms": task_terms[:20],
        "covered_terms": resolved_terms,
        "weakly_supported_terms": weak_terms,
        "source_count": source_count,
        "phrase_coverage": round(phrase_coverage, 4),
        "literature_gaps": gaps,
        "novelty_confidence": aggregate_confidence,
        "reviewer_risk": "high" if any(g["reviewer_risk"] == "high" for g in gaps) else ("medium" if any(g["reviewer_risk"] == "medium" for g in gaps) else "low"),
    }


def build_claim_evidence_graph(
    *,
    topic: str,
    section_title: str = "",
    subsection_title: str = "",
    outline: str,
    sources: Sequence[Dict[str, Any]],
    novelty: Dict[str, Any] | None = None,
    max_claims: int = 5,
) -> Dict[str, Any]:
    """Build a planned claim-evidence graph before drafting."""
    task_text = _normalize_space(f"{topic} {section_title} {subsection_title} {outline}")
    source_list = [source for source in sources if isinstance(source, dict)]
    phrases = _phrases(f"{subsection_title}. {outline}", limit=max_claims + 2)
    if not phrases:
        phrases = _phrases(task_text, limit=max_claims + 2)
    novelty_gaps = (novelty or {}).get("literature_gaps", [])
    claims: List[Dict[str, Any]] = []
    for idx, phrase in enumerate(phrases[: max(1, int(max_claims or 1))], 1):
        claim_terms = _tokens(phrase)
        support = _best_sources_for_terms(claim_terms + _tokens(task_text)[:8], source_list, limit=3)
        axis_scores = _axis_hits(phrase)
        axis = max(axis_scores, key=lambda key: axis_scores[key]) if axis_scores else "mechanism_method"
        if axis_scores and axis_scores.get(axis, 0.0) <= 0:
            axis = (novelty_gaps[idx - 1].get("axis") if idx - 1 < len(novelty_gaps) else "mechanism_method")
        risk_tag = "insufficient-evidence" if len(support) < 1 else ("needs-caveat" if len(support) < 2 else "low-risk")
        counter = (
            "Evidence is limited in the retrieved sources; frame this as a bounded synthesis rather than a definitive claim."
            if risk_tag != "low-risk"
            else "Scope remains bounded by retrieved literature and should acknowledge deployment or evaluation limitations."
        )
        claims.append({
            "claim_id": f"C{idx}",
            "claim": f"The subsection should make a source-grounded claim about {phrase}.",
            "supporting_evidence": support,
            "citation_source": [item["source_id"] for item in support],
            "counter_evidence_limitation": counter,
            "novelty_type": _NOVELTY_TYPE_BY_AXIS.get(axis, "research synthesis contribution"),
            "verifier_status": "planned_unverified",
            "reviewer_risk_tag": risk_tag,
            "expected_paragraph_role": "definition" if idx == 1 else ("limitation" if "risk" in axis or "limitation" in axis else "analysis"),
        })
    return {
        "enabled": True,
        "source_bound": True,
        "topic": topic,
        "section_title": section_title,
        "subsection_title": subsection_title,
        "claims": claims,
        "source_index": [
            {
                "source_id": idx,
                "label": _source_label(source, idx),
                "title": _normalize_space(source.get("title") or f"Source {idx}")[:180],
                "url": _normalize_space(source.get("href") or source.get("url") or source.get("link") or ""),
            }
            for idx, source in enumerate(source_list, 1)
        ],
        "graph_status": "planned",
    }


def bind_draft_to_claim_evidence_graph(draft: str, graph: Dict[str, Any]) -> Dict[str, Any]:
    """Attach generated paragraphs to planned graph claims and mark support status."""
    result = dict(graph or {})
    claims = [dict(claim) for claim in result.get("claims", []) if isinstance(claim, dict)]
    paragraphs = [
        para.strip()
        for para in re.split(r"\n\s*\n+", str(draft or ""))
        if para.strip() and not re.match(r"^\s*#{1,6}\s+", para.strip())
    ]
    bindings: List[Dict[str, Any]] = []
    supported_count = 0
    for claim in claims:
        claim_terms = set(_tokens(str(claim.get("claim") or "")))
        source_ids = [int(x) for x in claim.get("citation_source", []) if str(x).isdigit()]
        best_score = 0.0
        best_idx = -1
        best_para = ""
        for idx, para in enumerate(paragraphs, 1):
            para_lower = para.lower()
            para_terms = set(_tokens(para))
            lexical = len(claim_terms & para_terms) / max(1, min(12, len(claim_terms)))
            citation = 1.0 if any(re.search(rf"\[{sid}\]", para_lower) for sid in source_ids) else 0.0
            score = 0.72 * lexical + 0.28 * citation
            if score > best_score:
                best_score, best_idx, best_para = score, idx, para
        status = "verified_supported" if best_score >= 0.24 else "weak_or_missing"
        if status == "verified_supported":
            supported_count += 1
        claim["verifier_status"] = status
        bindings.append({
            "claim_id": claim.get("claim_id", ""),
            "paragraph_index": best_idx if best_idx > 0 else None,
            "binding_score": round(float(best_score), 4),
            "has_required_citation": bool(
                best_para and any(re.search(rf"\[{sid}\]", best_para) for sid in source_ids)
            ),
        })
    result["claims"] = claims
    result["paragraph_bindings"] = bindings
    result["claim_support_rate"] = round(supported_count / max(1, len(claims)), 4)
    result["graph_status"] = "draft_bound"
    return result


def format_research_intelligence_prompt(novelty: Dict[str, Any], graph: Dict[str, Any], *, max_claims: int = 4) -> str:
    """Compact prompt block for generation."""
    if not novelty and not graph:
        return ""
    lines = [
        "[Literature-anchored novelty and claim-evidence plan]",
        "Use this plan as a source-bound research scaffold. Do not invent sources or unsupported claims.",
    ]
    gaps = novelty.get("literature_gaps", []) if isinstance(novelty, dict) else []
    if gaps:
        lines.append("Novelty targets:")
        for gap in gaps[:3]:
            evidence_ids = [
                str(item.get("source_id"))
                for item in gap.get("evidence_support", [])
                if isinstance(item, dict) and item.get("source_id")
            ]
            lines.append(
                f"- Gap: {gap.get('literature_gap', '')} | Contribution: {gap.get('possible_contribution', '')} "
                f"| Risk: {gap.get('reviewer_risk', '')} | Evidence: {', '.join(evidence_ids) or 'needs cautious framing'}"
            )
    claims = graph.get("claims", []) if isinstance(graph, dict) else []
    if claims:
        lines.append("Claim-evidence graph to realize in paragraphs:")
        for claim in claims[: max(1, int(max_claims or 1))]:
            srcs = ", ".join(f"[{sid}]" for sid in claim.get("citation_source", []) if str(sid).isdigit())
            lines.append(
                f"- {claim.get('claim_id')}: {claim.get('claim')} Evidence: {srcs or 'no strong source; phrase cautiously'}. "
                f"Novelty type: {claim.get('novelty_type')}. Reviewer risk: {claim.get('reviewer_risk_tag')}."
            )
        lines.append("Each developed paragraph should map to at least one claim above and cite the listed source when making the claim.")
    return "\n".join(lines).strip()

