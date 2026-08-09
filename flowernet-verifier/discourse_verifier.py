"""Hierarchical, model-free discourse diagnostics for long documents.

The verifier deliberately exposes measurements instead of hiding them behind a
single LLM-judge score.  It can therefore run in tests and degraded deployments
without downloading a model, and its failure evidence can be consumed by the
controller.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Dict, Iterable, List, Sequence, Set


_STOP = {
    "the", "and", "for", "that", "with", "from", "this", "are", "was", "were",
    "into", "also", "have", "has", "not", "but", "its", "their", "which",
    "因此", "然而", "此外", "同时", "本文", "本节", "一个", "以及", "进行",
}
_BRIDGES = re.compile(
    r"\b(?:however|therefore|moreover|consequently|in contrast|building on|"
    r"the previous|this (?:result|finding|section))\b|然而|因此|此外|相比之下|"
    r"承接|上一节|前述|基于此",
    re.I,
)
_ANAPHORA = re.compile(r"^(?:this|these|those|such|it|they|其|这|该|上述|前述)", re.I)


def _segments(text: str) -> List[str]:
    blocks = [re.sub(r"\s+", " ", part).strip() for part in re.split(r"\n\s*\n+", text or "")]
    blocks = [part for part in blocks if part]
    if len(blocks) <= 1:
        blocks = [part.strip() for part in re.split(r"(?<=[。！？.!?])\s+", text or "") if part.strip()]
    return blocks


def _tokens(text: str) -> List[str]:
    latin = re.findall(r"[a-z][a-z0-9-]{2,}", (text or "").lower())
    cjk = []
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", text or ""):
        cjk.extend(chunk[i:i + 2] for i in range(len(chunk) - 1))
    return [token for token in latin + cjk if token not in _STOP]


def _set(text: str) -> Set[str]:
    return set(_tokens(text))


def _jaccard(left: Set[str], right: Set[str]) -> float:
    return len(left & right) / max(1, len(left | right))


def _mean(values: Iterable[float], default: float = 0.0) -> float:
    values = list(values)
    return sum(values) / len(values) if values else default


def analyze_discourse(draft: str, outline: str = "", history: Sequence[str] | None = None) -> Dict[str, Any]:
    """Measure continuity and information gain at local and document boundaries.

    ``discourse_delta`` is the sharp summary: useful continuation is high only
    when a segment is connected to its predecessor *and* contributes new
    information.  Diagnostics retain the two factors so a controller knows
    whether to add a bridge or remove duplicated claims.
    """
    paragraphs = _segments(draft)
    if not paragraphs:
        return {
            "version": "discourse_delta_v1",
            "paragraph_count": 0,
            "discourse_delta": 0.0,
            "continuity": 0.0,
            "boundary_continuity": 0.0,
            "bridge_coverage": 0.0,
            "information_gain": 0.0,
            "internal_redundancy": 0.0,
            "document_redundancy": 0.0,
            "topic_coverage": 0.0,
            "adjacent_overlap": [],
            "duplicate_pairs": [],
            "dangling_anaphora_paragraphs": [],
            "dominant_terms": [],
            "repair_targets": ["generate_section_content"],
        }
    paragraph_terms = [_set(part) for part in paragraphs]
    adjacent_overlap = [
        _jaccard(paragraph_terms[i - 1], paragraph_terms[i])
        for i in range(1, len(paragraph_terms))
    ]
    continuity = _mean([min(1.0, value / 0.16) for value in adjacent_overlap], default=1.0)
    explicit_bridges = [bool(_BRIDGES.search(part[:180])) for part in paragraphs[1:]]
    bridge_coverage = _mean([1.0 if value else 0.0 for value in explicit_bridges], default=1.0)

    pairwise = []
    duplicate_pairs = []
    for i in range(len(paragraph_terms)):
        for j in range(i):
            overlap = _jaccard(paragraph_terms[i], paragraph_terms[j])
            pairwise.append(overlap)
            if overlap >= 0.52:
                duplicate_pairs.append({"left": j, "right": i, "overlap": round(overlap, 4)})
    internal_redundancy = max(pairwise, default=0.0)

    history_terms = [_set(item) for item in history or [] if str(item).strip()]
    boundary_overlap = [
        _jaccard(paragraph_terms[0], terms) for terms in history_terms
    ] if paragraph_terms else []
    document_redundancy = max(
        (_jaccard(terms, old) for terms in paragraph_terms for old in history_terms),
        default=0.0,
    )
    boundary_continuity = min(1.0, max(boundary_overlap, default=0.16) / 0.16)

    novelty_steps = [1.0 - value for value in adjacent_overlap]
    information_gain = _mean(novelty_steps, default=1.0) * (1.0 - document_redundancy)
    local_balance = _mean([
        min(1.0, overlap / 0.12) * (1.0 - overlap)
        for overlap in adjacent_overlap
    ], default=1.0)
    discourse_delta = max(0.0, min(1.0, 0.45 * local_balance + 0.25 * information_gain + 0.20 * boundary_continuity + 0.10 * bridge_coverage))

    dangling = []
    for index, part in enumerate(paragraphs):
        if _ANAPHORA.search(part.strip()) and index == 0 and not history_terms:
            dangling.append(index)

    outline_terms = _set(outline)
    topic_coverage = _mean([
        len(terms & outline_terms) / max(1, len(outline_terms)) for terms in paragraph_terms
    ]) if outline_terms else 1.0
    term_counts = Counter(token for terms in paragraph_terms for token in terms)

    return {
        "version": "discourse_delta_v1",
        "paragraph_count": len(paragraphs),
        "discourse_delta": round(discourse_delta, 4),
        "continuity": round(continuity, 4),
        "boundary_continuity": round(boundary_continuity, 4),
        "bridge_coverage": round(bridge_coverage, 4),
        "information_gain": round(information_gain, 4),
        "internal_redundancy": round(internal_redundancy, 4),
        "document_redundancy": round(document_redundancy, 4),
        "topic_coverage": round(topic_coverage, 4),
        "adjacent_overlap": [round(value, 4) for value in adjacent_overlap],
        "duplicate_pairs": duplicate_pairs[:12],
        "dangling_anaphora_paragraphs": dangling,
        "dominant_terms": [term for term, _ in term_counts.most_common(12)],
        "repair_targets": [
            target for condition, target in (
                (continuity < 0.45, "add_predecessor_bridge"),
                (internal_redundancy > 0.52, "merge_or_delete_duplicate_paragraph"),
                (document_redundancy > 0.48, "replace_repeated_history_claim_with_new_evidence"),
                (topic_coverage < 0.18, "restore_section_contract"),
                (bool(dangling), "resolve_dangling_anaphora"),
            ) if condition
        ],
    }
