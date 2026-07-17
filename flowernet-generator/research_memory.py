"""Long-term research, reviewer, and failure memory for FlowerNet.

The memory is intentionally evidence-bound. It stores topic/domain patterns,
reviewer standards, and realized FlowerNet failures/repairs. It does not read
evaluation references or invent paper results.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Mapping, Optional


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _tokens(text: Any) -> List[str]:
    stop = {
        "the", "and", "for", "with", "from", "that", "this", "these",
        "those", "paper", "section", "subsection", "research", "study",
        "write", "writing", "method", "system", "approach",
    }
    return [
        tok.lower()
        for tok in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}|[\u4e00-\u9fff]{2,}", str(text or ""))
        if tok.lower() not in stop
    ]


def _unique(items: Iterable[Any], limit: int = 20) -> List[str]:
    out: List[str] = []
    for item in items:
        value = _norm(item)
        if value and value not in out:
            out.append(value)
        if len(out) >= limit:
            break
    return out


def infer_domain(topic: str, source_results: Optional[List[Dict[str, Any]]] = None) -> str:
    text = " ".join([topic or ""] + [
        " ".join(str(src.get(k) or "") for k in ("title", "snippet", "abstract", "body"))
        for src in (source_results or [])
        if isinstance(src, dict)
    ]).lower()
    rules = [
        ("education", ("education", "k-12", "student", "teacher", "learning analytics", "classroom", "school")),
        ("agentic_research", ("research agent", "paperbench", "re-bench", "scientist", "claim-evidence", "research-writing")),
        ("large_language_models", ("llm", "large language model", "foundation model", "rag", "retrieval augmented")),
        ("robotics", ("robot", "robotics", "control", "manipulation", "navigation")),
        ("healthcare", ("clinical", "patient", "healthcare", "biomedical", "medical")),
    ]
    for domain, needles in rules:
        if any(needle in text for needle in needles):
            return domain
    toks = _tokens(text)
    return toks[0] if toks else "general"


class ResearchReviewerFailureMemory:
    """In-memory + optional JSONL-backed memory store."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or os.getenv("FLOWERNET_RESEARCH_MEMORY_PATH", "").strip()
        self.research_records: List[Dict[str, Any]] = []
        self.reviewer_records: List[Dict[str, Any]] = []
        self.failure_records: List[Dict[str, Any]] = []
        if self.path and os.path.exists(self.path):
            self._load_jsonl(self.path)

    def _load_jsonl(self, path: str) -> None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    kind = row.get("memory_type")
                    if kind == "research":
                        self.research_records.append(row)
                    elif kind == "reviewer":
                        self.reviewer_records.append(row)
                    elif kind == "failure":
                        self.failure_records.append(row)
        except Exception:
            return

    def _append(self, row: Dict[str, Any]) -> None:
        if not self.path:
            return
        try:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            return

    def record_research(
        self,
        *,
        topic: str,
        domain: str = "",
        methods: Optional[List[str]] = None,
        benchmarks: Optional[List[str]] = None,
        datasets: Optional[List[str]] = None,
        literature_patterns: Optional[List[str]] = None,
        source_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        row = {
            "memory_type": "research",
            "timestamp": time.time(),
            "topic": _norm(topic),
            "domain": _norm(domain) or infer_domain(topic),
            "methods": _unique(methods or []),
            "benchmarks": _unique(benchmarks or []),
            "datasets": _unique(datasets or []),
            "literature_patterns": _unique(literature_patterns or []),
            "source_ids": _unique(source_ids or []),
        }
        self.research_records.append(row)
        self._append(row)
        return row

    def record_reviewer(
        self,
        *,
        domain: str,
        standards: Optional[List[str]] = None,
        rejection_reasons: Optional[List[str]] = None,
        venue_profile: str = "high-impact ML/Nature-style reviewer",
    ) -> Dict[str, Any]:
        row = {
            "memory_type": "reviewer",
            "timestamp": time.time(),
            "domain": _norm(domain) or "general",
            "standards": _unique(standards or []),
            "rejection_reasons": _unique(rejection_reasons or []),
            "venue_profile": _norm(venue_profile),
        }
        self.reviewer_records.append(row)
        self._append(row)
        return row

    def record_failure(
        self,
        *,
        topic: str,
        failure_kind: str,
        repair_arm: str,
        effective: bool,
        harmful: bool,
        metrics_delta: Optional[Mapping[str, Any]] = None,
        domain: str = "",
    ) -> Dict[str, Any]:
        row = {
            "memory_type": "failure",
            "timestamp": time.time(),
            "topic": _norm(topic),
            "domain": _norm(domain) or infer_domain(topic),
            "failure_kind": _norm(failure_kind),
            "repair_arm": _norm(repair_arm),
            "effective": bool(effective),
            "harmful": bool(harmful),
            "metrics_delta": dict(metrics_delta or {}),
        }
        self.failure_records.append(row)
        self._append(row)
        return row

    def ingest_sources(self, *, topic: str, source_results: List[Dict[str, Any]]) -> Dict[str, Any]:
        blob = " ".join(
            " ".join(str(src.get(k) or "") for k in ("title", "snippet", "abstract", "body"))
            for src in (source_results or [])
            if isinstance(src, dict)
        )
        methods = [tok for tok in _unique(re.findall(r"\b(?:graph|retrieval|benchmark|audit|controller|verifier|dataset|protocol|self-[A-Za-z-]+)\b", blob, flags=re.I), 12)]
        benchmarks = _unique(re.findall(r"\b(?:PaperBench|RE-Bench|MLE-bench|ScienceAgentBench|CORE-Bench|ASSISTments|EdNet)\b", blob, flags=re.I), 12)
        datasets = _unique(re.findall(r"\b(?:dataset|corpus|benchmark suite|interaction logs|replication tasks)\b", blob, flags=re.I), 12)
        patterns = _unique(re.findall(r"\b(?:survey|benchmark|ablation|case study|replication|deployment|failure analysis)\b", blob, flags=re.I), 12)
        source_ids = _unique([
            src.get("url") or src.get("href") or src.get("title") or f"source_{idx}"
            for idx, src in enumerate(source_results or [], 1)
            if isinstance(src, dict)
        ], 20)
        return self.record_research(
            topic=topic,
            domain=infer_domain(topic, source_results),
            methods=methods,
            benchmarks=benchmarks,
            datasets=datasets,
            literature_patterns=patterns,
            source_ids=source_ids,
        )

    def topic_strategy(self, topic: str, domain: str = "") -> Dict[str, Any]:
        resolved_domain = _norm(domain) or infer_domain(topic)
        domain_research = [r for r in self.research_records if r.get("domain") == resolved_domain]
        if not domain_research:
            domain_research = self.research_records[-8:]
        domain_reviewers = [r for r in self.reviewer_records if r.get("domain") == resolved_domain]
        if not domain_reviewers:
            domain_reviewers = self.reviewer_records[-8:]
        domain_failures = [r for r in self.failure_records if r.get("domain") == resolved_domain or r.get("topic") in topic]

        method_counter = Counter(m for r in domain_research for m in r.get("methods", []))
        benchmark_counter = Counter(b for r in domain_research for b in r.get("benchmarks", []))
        dataset_counter = Counter(d for r in domain_research for d in r.get("datasets", []))
        pattern_counter = Counter(p for r in domain_research for p in r.get("literature_patterns", []))
        standards = _unique(s for r in domain_reviewers for s in r.get("standards", []))
        risks = _unique(s for r in domain_reviewers for s in r.get("rejection_reasons", []))

        repair_stats: Dict[str, Dict[str, int]] = defaultdict(lambda: {"effective": 0, "harmful": 0})
        for row in domain_failures:
            arm = row.get("repair_arm", "")
            if not arm:
                continue
            if row.get("effective"):
                repair_stats[arm]["effective"] += 1
            if row.get("harmful"):
                repair_stats[arm]["harmful"] += 1
        prefer = [
            arm for arm, stats in sorted(
                repair_stats.items(),
                key=lambda item: (item[1]["effective"] - item[1]["harmful"], item[1]["effective"]),
                reverse=True,
            )
            if stats["effective"] > stats["harmful"]
        ][:4]
        cooldown = [arm for arm, stats in repair_stats.items() if stats["harmful"] > 0 and stats["harmful"] >= stats["effective"]]

        return {
            "enabled": True,
            "domain": resolved_domain,
            "topic": _norm(topic),
            "recommended_methods": [x for x, _ in method_counter.most_common(8)],
            "recommended_benchmarks": [x for x, _ in benchmark_counter.most_common(8)],
            "recommended_datasets": [x for x, _ in dataset_counter.most_common(8)],
            "literature_structure": [x for x, _ in pattern_counter.most_common(8)],
            "reviewer_standards": standards[:8],
            "reviewer_risks": risks[:8],
            "repair_policy_hints": {
                "prefer_arms": prefer,
                "cooldown_arms": cooldown,
                "evidence_source": "realized_failure_memory",
            },
            "novelty_strategy": [
                "anchor novelty to unresolved literature gaps, not surface wording changes",
                "bind each proposed contribution to claim-evidence-citation graph nodes",
                "pre-empt reviewer risks with limitations and reproducibility anchors",
            ],
        }


def build_memory_record(
    *,
    topic: str,
    source_results: Optional[List[Dict[str, Any]]] = None,
    trace: Optional[Mapping[str, Any]] = None,
    path: Optional[str] = None,
) -> Dict[str, Any]:
    store = ResearchReviewerFailureMemory(path=path)
    source_results = source_results or []
    if source_results:
        store.ingest_sources(topic=topic, source_results=source_results)
    domain = infer_domain(topic, source_results)
    store.record_reviewer(
        domain=domain,
        standards=[
            "claims require explicit source support",
            "novelty must be literature-anchored and non-incremental",
            "evaluation and reproducibility risks must be stated",
        ],
        rejection_reasons=[
            "unsupported contribution claim",
            "weak novelty against prior literature",
            "missing benchmark or reproducibility detail",
        ],
    )
    trace = trace or {}
    for weakness in trace.get("weaknesses", []) if isinstance(trace.get("weaknesses"), list) else []:
        if not isinstance(weakness, Mapping):
            continue
        store.record_failure(
            topic=topic,
            domain=domain,
            failure_kind=str(weakness.get("kind") or "unknown"),
            repair_arm=str(weakness.get("target_component") or weakness.get("repair_arm") or "unresolved"),
            effective=False,
            harmful=bool(weakness.get("severity", 0.0) and float(weakness.get("severity", 0.0)) > 0.5),
        )
    return {
        "enabled": True,
        "source_bound": True,
        "topic_strategy": store.topic_strategy(topic, domain=domain),
        "record_counts": {
            "research": len(store.research_records),
            "reviewer": len(store.reviewer_records),
            "failure": len(store.failure_records),
        },
    }
