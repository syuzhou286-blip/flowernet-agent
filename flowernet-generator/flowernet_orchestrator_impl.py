"""
FlowerNet 完整编排器 - 按照你的完整需求实现

流程说明：
第一步（Outliner）：
  1. 调用LLM生成整篇文章的大纲
  2. 根据大纲生成每个section和subsection的详细大纲
  3. 所有大纲存储到数据库

第二步（Generator）：
  1. 根据大纲生成第一个subsection
  2. 内容传给Verifier检测
  3. 如果通过，存储到数据库供下一个subsection使用
  4. 如果不通过，进入第三步

第三步（Controller循环）：
  1. Controller从数据库提取未通过的subsection大纲
  2. 修改大纲传给Generator
  3. Generator再次生成
  4. 传给Verifier检测
  5. 循环直到通过

关键点：
- subsection和section一个一个生成
- 上一个subsection合格才能生成下一个
- history在下一个subsection生成时被提取出来
- history也在Verifier验证时使用
"""

import requests
import json
import hashlib
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime, timezone
import time
import os
import random
import re
import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

_GENERATOR_DIR = os.path.dirname(os.path.abspath(__file__))
if _GENERATOR_DIR not in sys.path:
    sys.path.insert(0, _GENERATOR_DIR)
_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT_DIR not in sys.path:
    sys.path.insert(0, _ROOT_DIR)

try:
    from rag_search import RAGSearchEngine, SourceVerifier
    RAG_AVAILABLE = True
except Exception:
    RAG_AVAILABLE = False

try:
    from citation_drift_prevention import CITATION_DRIFT_PREVENTION_PROMPT, CITATION_DRIFT_PREVENTION_PROMPT_EN
except Exception:
    CITATION_DRIFT_PREVENTION_PROMPT = ""
    CITATION_DRIFT_PREVENTION_PROMPT_EN = ""

try:
    from flowernet_agent_stack import get_vector_store
except Exception:
    get_vector_store = None  # type: ignore

try:
    from flowernet_trained_models import (
        load_json_model,
        predict_reward_model,
        resolve_model_path,
    )
except Exception:
    load_json_model = None  # type: ignore
    predict_reward_model = None  # type: ignore
    resolve_model_path = None  # type: ignore

try:
    from research_intelligence import (
        analyze_literature_anchored_novelty,
        bind_draft_to_claim_evidence_graph,
        build_claim_evidence_graph,
        format_research_intelligence_prompt,
    )
    RESEARCH_INTELLIGENCE_AVAILABLE = True
except Exception:
    analyze_literature_anchored_novelty = None  # type: ignore
    bind_draft_to_claim_evidence_graph = None  # type: ignore
    build_claim_evidence_graph = None  # type: ignore
    format_research_intelligence_prompt = None  # type: ignore
    RESEARCH_INTELLIGENCE_AVAILABLE = False

try:
    from self_improving_harness import build_self_improving_harness_record
    SELF_IMPROVING_HARNESS_AVAILABLE = True
except Exception:
    build_self_improving_harness_record = None  # type: ignore
    SELF_IMPROVING_HARNESS_AVAILABLE = False

try:
    from provenance_harness import propose_selective_repairs, validate_research_provenance
    PROVENANCE_HARNESS_AVAILABLE = True
except Exception:
    propose_selective_repairs = None  # type: ignore
    validate_research_provenance = None  # type: ignore
    PROVENANCE_HARNESS_AVAILABLE = False

try:
    from evidence_drift_policy import select_drift_actions
    EVIDENCE_DRIFT_POLICY_AVAILABLE = True
except Exception:
    select_drift_actions = None  # type: ignore
    EVIDENCE_DRIFT_POLICY_AVAILABLE = False

try:
    from research_memory import build_memory_record
    from harness_optimizer import build_harness_optimizer_record, recommended_flowernet_full_architecture
    MEMORY_OPTIMIZER_AVAILABLE = True
except Exception:
    build_memory_record = None  # type: ignore
    build_harness_optimizer_record = None  # type: ignore
    recommended_flowernet_full_architecture = None  # type: ignore
    MEMORY_OPTIMIZER_AVAILABLE = False


def _coerce_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


class DocumentGenerationOrchestrator:
    """
    文档生成编排器 - 完整流程控制
    """
    
    def __init__(
        self,
        generator_url: str = "http://localhost:8002",
        verifier_url: str = "http://localhost:8000",
        controller_url: str = "http://localhost:8001",
        outliner_url: str = "http://localhost:8003",
        max_iterations: int = 5,
        history_manager: Optional[Any] = None,
        history_window_size: int = 3,  # 历史窗口大小：只使用最近N个小节
        max_forced_iterations: int = 15  # 兼容旧参数：不再用于强制通过
    ):
        """初始化编排器"""
        self.generator_url = generator_url
        self.verifier_url = verifier_url
        self.controller_url = controller_url
        self.outliner_url = outliner_url
        self.max_iterations = max_iterations
        self.history_manager = history_manager
        self.history_window_size = history_window_size
        self.max_forced_iterations = max_forced_iterations
        self.retry_base_delay = float(os.getenv("DOC_RETRY_BASE_DELAY", "1.0"))
        self.retry_max_delay = float(os.getenv("DOC_RETRY_MAX_DELAY", "10.0"))
        self.retry_jitter = float(os.getenv("DOC_RETRY_JITTER", "0.2"))
        self.subsection_retry_forever = os.getenv("SUBSECTION_RETRY_FOREVER", "false").lower() == "true"
        # Keep the loop quality-focused but bounded: one draft plus one or two
        # targeted repairs is usually enough, while longer loops inflate length
        # and can overfit verifier wording.
        configured_max_attempts = max(1, int(os.getenv("MAX_SUBSECTION_ATTEMPTS", "4")))
        self.max_subsection_attempts = min(
            configured_max_attempts,
            max(1, int(os.getenv("MAX_SUBSECTION_ATTEMPTS_CAP", "4"))),
        )
        # 单个小节最长处理时长（秒），超过后按最佳努力通过，避免长时间卡住。
        self.subsection_max_seconds = max(120, int(os.getenv("SUBSECTION_MAX_SECONDS", "900")))
        # 当 Generator 连续失败时，优先按该阈值触发兜底，避免单小节长时间阻塞。
        self.max_generator_failures_per_subsection = max(
            1,
            int(os.getenv("MAX_GENERATOR_FAILURES_PER_SUBSECTION", "3")),
        )
        # One controller call is usually enough to produce an actionable repair.
        # Repeating a weak repair in the same verifier round inflates latency and
        # can push the outline away from the topic.
        configured_controller_retries = max(1, int(os.getenv("MAX_CONTROLLER_RETRIES", "1")))
        self.max_controller_retries = min(
            configured_controller_retries,
            max(1, int(os.getenv("MAX_CONTROLLER_RETRIES_CAP", "1"))),
        )
        self.allow_forced_pass = os.getenv("ALLOW_FORCED_PASS", "false").lower() == "true"
        configured_min_retries = max(1, int(os.getenv("MIN_CONTROLLER_RETRIES_BEFORE_FORCE", "1")))
        self.min_controller_retries_before_force = min(configured_min_retries, self.max_controller_retries)
        # 默认采用宽松模式：只要 Controller 给出有效改纲且发生变更，就允许继续下一轮。
        self.strict_controller_effective = os.getenv("STRICT_CONTROLLER_EFFECTIVE", "true").lower() == "true"
        self.accept_ineffective_controller_outline = (
            os.getenv("CONTROLLER_ACCEPT_INEFFECTIVE_OUTLINE", "false").lower() == "true"
        )
        self.controller_guard_enabled = os.getenv("CONTROLLER_GUARD_ENABLED", "true").lower() == "true"
        self.controller_min_outline_retention = max(
            0.15,
            min(0.95, float(os.getenv("CONTROLLER_MIN_OUTLINE_RETENTION", "0.45"))),
        )
        self.local_outline_fallback_enabled = os.getenv("LOCAL_OUTLINE_FALLBACK_ENABLED", "false").lower() == "true"
        self.max_pass_rel_margin = max(0.0, float(os.getenv("MAX_PASS_REL_MARGIN", "0.25")))
        self.max_pass_red_margin = max(0.0, float(os.getenv("MAX_PASS_RED_MARGIN", "0.30")))
        self.orch_generator_retries = max(1, int(os.getenv("ORCH_GENERATOR_RETRIES", "1")))
        self.orch_generator_backoff = max(0.2, float(os.getenv("ORCH_GENERATOR_BACKOFF", "1.0")))
        self.orch_generator_max_backoff = max(1.0, float(os.getenv("ORCH_GENERATOR_MAX_BACKOFF", "10.0")))
        self.generator_http_timeout = max(30, int(os.getenv("GENERATOR_HTTP_TIMEOUT", "60")))
        self.chapter_assets_enabled = os.getenv("CHAPTER_ASSETS_ENABLED", "true").lower() == "true"
        self.chapter_assets_max_tokens = max(500, int(os.getenv("CHAPTER_ASSETS_MAX_TOKENS", "1600")))
        self.document_framing_enabled = os.getenv("DOCUMENT_FRAMING_ENABLED", "true").lower() == "true"
        self.document_framing_max_tokens = max(450, int(os.getenv("DOCUMENT_FRAMING_MAX_TOKENS", "900")))
        # Compact prompts are only useful as provider-failure fallbacks. Using them on
        # normal controller retries drops outline/evidence detail and can make quality worse.
        self.orch_compact_generation_enabled = os.getenv("ORCH_COMPACT_GENERATION_ENABLED", "false").lower() == "true"
        self.orch_compact_prompt_trigger_chars = max(1200, int(os.getenv("ORCH_COMPACT_PROMPT_TRIGGER_CHARS", "7000")))
        self.orch_compact_max_tokens = max(400, int(os.getenv("ORCH_COMPACT_MAX_TOKENS", "1800")))
        self.verifier_http_timeout = max(30, int(os.getenv("VERIFIER_HTTP_TIMEOUT", "60")))
        self.verifier_max_retries = max(3, int(os.getenv("VERIFIER_MAX_RETRIES", "3")))
        self.verifier_retry_delay = max(2.0, float(os.getenv("VERIFIER_RETRY_DELAY", "3.0")))
        self.verifier_unavailable_best_effort = os.getenv("VERIFIER_UNAVAILABLE_BEST_EFFORT", "true").lower() == "true"
        # For paper-grade experiments, a verifier-failed draft must not be
        # counted as passed merely because it is the best available attempt.
        # Product deployments may opt in explicitly for graceful degradation.
        self.accept_best_real_draft = os.getenv("ACCEPT_BEST_REAL_DRAFT", "true").lower() == "true"
        self.generator_max_tokens = max(400, int(os.getenv("ORCH_GENERATOR_MAX_TOKENS", "2000")))
        self.min_draft_chars = max(200, int(os.getenv("ORCH_MIN_DRAFT_CHARS", "500")))
        self.target_draft_min_chars = max(
            self.min_draft_chars,
            int(os.getenv("ORCH_TARGET_DRAFT_MIN_CHARS", str(max(self.min_draft_chars, 850)))),
        )
        self.target_draft_max_chars = max(
            self.target_draft_min_chars + 100,
            int(os.getenv("ORCH_TARGET_DRAFT_MAX_CHARS", "1100")),
        )
        self.enforce_target_draft_max = os.getenv("ORCH_ENFORCE_TARGET_DRAFT_MAX", "false").lower() == "true"
        self.session = requests.Session()
        self.session.trust_env = False
        self.generator_temperature = max(
            0.0,
            min(2.0, float(os.getenv("GENERATOR_TEMPERATURE", "0.2") or 0.2)),
        )
        
        # 用于本地 HTTP 调用优化
        self._local_generator = None
        self.vector_store = get_vector_store() if get_vector_store is not None else None
        self._local_verifier = None
        self._local_controller = None
        self.deadline_monotonic: Optional[float] = None
        self.reward_model_enabled = os.getenv("FLOWERNET_REWARD_MODEL_ENABLED", "true").lower() == "true"
        self.reward_model_path = self._resolve_model_path(
            os.getenv("FLOWERNET_REWARD_MODEL_PATH", os.path.join("models", "reward_model.json")),
            "reward_model.json",
        )
        self._reward_model_cache: Dict[str, Any] = {"path": "", "mtime": 0.0, "model": None}
        
        self.rag_enabled = os.getenv("RAG_ENABLED", "true").lower() == "true" and RAG_AVAILABLE
        self.rag_force_citation = os.getenv("RAG_FORCE_CITATION", "true").lower() == "true"
        self.source_citation_relaxation_enabled = os.getenv("SOURCE_CITATION_RELAXATION_ENABLED", "false").lower() == "true"
        self.rag_min_citations = max(1, int(os.getenv("RAG_MIN_CITATIONS", "3")))
        self.rag_max_results = max(1, int(os.getenv("RAG_MAX_RESULTS", "6")))
        self.rag_timeout = max(3, int(os.getenv("RAG_TIMEOUT", "20")))
        self.prompt_outline_max_chars = max(500, int(os.getenv("PROMPT_OUTLINE_MAX_CHARS", "4500")))
        self.prompt_original_max_chars = max(500, int(os.getenv("PROMPT_ORIGINAL_MAX_CHARS", "3500")))
        self.prompt_rag_max_chars = max(200, int(os.getenv("PROMPT_RAG_MAX_CHARS", "1200")))
        self.prompt_history_max_chars = max(200, int(os.getenv("PROMPT_HISTORY_MAX_CHARS", "1500")))
        self.prompt_novelty_history_max_chars = max(
            self.prompt_history_max_chars,
            int(os.getenv("ORCH_NOVELTY_HISTORY_MAX_CHARS", "5000")),
        )
        self.prompt_revision_max_chars = max(1000, int(os.getenv("ORCH_REVISION_DRAFT_MAX_CHARS", "6500")))
        self.near_pass_quality_margin = max(0.0, float(os.getenv("NEAR_PASS_QUALITY_MARGIN", "0.03")))
        self.best_draft_min_rel = max(0.0, min(1.0, float(os.getenv("BEST_DRAFT_MIN_REL", "0.64"))))
        self.best_draft_min_quality = max(0.0, min(1.0, float(os.getenv("BEST_DRAFT_MIN_QUALITY", "0.58"))))
        self.best_draft_max_failed_dims = max(0, int(os.getenv("BEST_DRAFT_MAX_FAILED_DIMS", "2")))
        self.source_alignment_selector_enabled = os.getenv("FLOWERNET_SOURCE_ALIGNMENT_SELECTOR", "true").lower() == "true"
        self.source_alignment_min_score = max(
            0.0,
            min(1.0, float(os.getenv("FLOWERNET_SOURCE_ALIGNMENT_MIN_SCORE", "0.34"))),
        )
        self.source_alignment_pass_audit_min_score = max(
            self.source_alignment_min_score,
            min(1.0, float(os.getenv("FLOWERNET_SOURCE_ALIGNMENT_PASS_AUDIT_MIN_SCORE", "0.66"))),
        )
        self.source_alignment_pass_epsilon = max(
            0.0,
            min(0.05, float(os.getenv("FLOWERNET_SOURCE_ALIGNMENT_PASS_EPSILON", "0.035"))),
        )
        self.source_alignment_best_weight = max(
            0.0,
            min(0.40, float(os.getenv("FLOWERNET_SOURCE_ALIGNMENT_BEST_WEIGHT", "0.08"))),
        )
        self.research_intelligence_enabled = (
            os.getenv("FLOWERNET_RESEARCH_INTELLIGENCE_ENABLED", "true").lower() == "true"
            and RESEARCH_INTELLIGENCE_AVAILABLE
        )
        self.self_improving_harness_enabled = (
            os.getenv("FLOWERNET_SELF_IMPROVING_HARNESS_ENABLED", "true").lower() == "true"
            and SELF_IMPROVING_HARNESS_AVAILABLE
        )
        self.memory_optimizer_enabled = (
            os.getenv("FLOWERNET_MEMORY_OPTIMIZER_ENABLED", "true").lower() == "true"
            and MEMORY_OPTIMIZER_AVAILABLE
        )
        self.rag_translate_cjk_query = os.getenv("FLOWERNET_RAG_TRANSLATE_CJK_QUERY", "true").lower() == "true"
        self._rag_query_translation_cache: Dict[str, str] = {}
        self.rag_source_pool_path = os.getenv("FLOWERNET_RAG_SOURCE_POOL_PATH", "").strip()
        self._rag_source_pool_cache: Optional[Dict[str, Any]] = None
        self.initial_draft_pool_path = os.getenv("FLOWERNET_INITIAL_DRAFT_POOL_PATH", "").strip()
        self._initial_draft_pool_cache: Optional[Dict[str, Any]] = None

        self.search_engine = (
            RAGSearchEngine(max_results=self.rag_max_results, timeout=self.rag_timeout)
            if self.rag_enabled
            else None
        )
        self.source_verifier = SourceVerifier() if self.rag_enabled else None

    def _build_research_intelligence(
        self,
        *,
        document_title: str,
        section_title: str,
        subsection_title: str,
        outline: str,
        prompt: str,
        source_results: List[Dict[str, Any]],
        draft: str = "",
        planned_graph: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Build literature-anchored novelty and claim-evidence telemetry.

        This uses only FlowerNet's own RAG sources and the current task context.
        It must not consult external evaluation references.
        """
        if not self.research_intelligence_enabled:
            return {
                "enabled": False,
                "available": bool(RESEARCH_INTELLIGENCE_AVAILABLE),
                "novelty": {},
                "claim_evidence_graph": {},
                "prompt_block": "",
            }
        sources = [src for src in (source_results or []) if isinstance(src, dict)]
        if not sources:
            return {
                "enabled": True,
                "available": True,
                "source_bound": True,
                "novelty": {
                    "enabled": True,
                    "source_bound": True,
                    "source_count": 0,
                    "literature_gaps": [],
                    "novelty_confidence": 0.0,
                    "reviewer_risk": "high",
                    "warning": "no_rag_sources_for_literature_anchored_novelty",
                },
                "claim_evidence_graph": {
                    "enabled": True,
                    "source_bound": True,
                    "claims": [],
                    "graph_status": "missing_sources",
                },
                "prompt_block": "",
            }
        try:
            novelty = analyze_literature_anchored_novelty(
                topic=document_title or prompt,
                section_title=section_title,
                subsection_title=subsection_title,
                outline=f"{outline}\n{prompt}",
                sources=sources,
            )
            graph = planned_graph if isinstance(planned_graph, dict) and planned_graph.get("claims") else build_claim_evidence_graph(
                topic=document_title or prompt,
                section_title=section_title,
                subsection_title=subsection_title,
                outline=f"{outline}\n{prompt}",
                sources=sources,
                novelty=novelty,
            )
            if draft:
                graph = bind_draft_to_claim_evidence_graph(draft, graph)
            prompt_block = format_research_intelligence_prompt(novelty, graph) if not draft else ""
            return {
                "enabled": True,
                "available": True,
                "source_bound": True,
                "novelty": novelty,
                "claim_evidence_graph": graph,
                "prompt_block": prompt_block,
            }
        except Exception as exc:
            return {
                "enabled": True,
                "available": True,
                "source_bound": True,
                "novelty": {},
                "claim_evidence_graph": {},
                "prompt_block": "",
                "error": str(exc)[:300],
            }

    def _build_self_harness_record(
        self,
        trace: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Mine weaknesses and propose bounded harness updates for a trace."""
        if not self.self_improving_harness_enabled:
            return {
                "enabled": False,
                "available": bool(SELF_IMPROVING_HARNESS_AVAILABLE),
                "weaknesses": [],
                "proposals": [],
                "weakness_count": 0,
                "proposal_count": 0,
            }
        try:
            record = build_self_improving_harness_record(trace)
            if PROVENANCE_HARNESS_AVAILABLE:
                provenance_audit = validate_research_provenance(trace)
                record["provenance_audit"] = provenance_audit
                record["selective_repairs"] = propose_selective_repairs(provenance_audit)
                record["provenance_hard_gate_passed"] = bool(provenance_audit.get("hard_gate_passed"))
            else:
                record["provenance_audit"] = {"available": False}
                record["selective_repairs"] = []
            drift = trace.get("evidence_drift")
            if EVIDENCE_DRIFT_POLICY_AVAILABLE and isinstance(drift, dict):
                record["evidence_drift_decision"] = select_drift_actions(
                    drift.get("claims", []),
                    drift.get("actions", []),
                    dependencies=drift.get("dependencies", []),
                    budget=float(drift.get("budget", 0.0) or 0.0),
                    cost_weight=float(drift.get("cost_weight", 0.05) or 0.05),
                )
            return record
        except Exception as exc:
            return {
                "enabled": True,
                "available": True,
                "weaknesses": [],
                "proposals": [],
                "weakness_count": 0,
                "proposal_count": 0,
                "status": "self_harness_error",
                "error": str(exc)[:300],
            }

    def _build_memory_optimizer_record(
        self,
        *,
        document_title: str,
        section_title: str,
        subsection_title: str,
        trace: Dict[str, Any],
        source_results: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Build Research/Reviewer/Failure memory and harness optimizer telemetry."""
        if not self.memory_optimizer_enabled:
            return {
                "enabled": False,
                "available": bool(MEMORY_OPTIMIZER_AVAILABLE),
                "memory": {},
                "harness_optimizer": {},
                "architecture_layers": [],
            }
        topic = " ".join(
            part for part in [document_title, section_title, subsection_title, str(trace.get("topic") or "")]
            if str(part or "").strip()
        )
        try:
            memory_record = build_memory_record(
                topic=topic,
                source_results=source_results or [],
                trace=trace,
            )
            optimizer_record = build_harness_optimizer_record(
                trace={**(trace or {}), "topic": topic},
                memory_record=memory_record,
            )
            layers = recommended_flowernet_full_architecture()
            return {
                "enabled": True,
                "available": True,
                "memory": memory_record,
                "harness_optimizer": optimizer_record.get("proposal", optimizer_record),
                "architecture_layers": layers,
            }
        except Exception as exc:
            return {
                "enabled": True,
                "available": True,
                "memory": {},
                "harness_optimizer": {},
                "architecture_layers": [],
                "status": "memory_optimizer_error",
                "error": str(exc)[:300],
            }

    def _remaining_deadline_seconds(self) -> Optional[float]:
        if not self.deadline_monotonic:
            return None
        return self.deadline_monotonic - time.monotonic()

    def _deadline_exceeded(self) -> bool:
        remaining = self._remaining_deadline_seconds()
        return remaining is not None and remaining <= 0

    def _subsection_failure_result(
        self,
        *,
        reason: str,
        draft: str = "",
        final_outline: str = "",
        iterations: int = 0,
        verification: Optional[Dict[str, Any]] = None,
        all_drafts: Optional[List[str]] = None,
        metrics: Optional[Dict[str, int]] = None,
        rag_search_result: Optional[Dict[str, Any]] = None,
        rag_used: bool = False,
        rag_selected_query: str = "",
        controller_triggered: bool = False,
        controller_retry_count: int = 0,
        controller_last_result: Optional[Dict[str, Any]] = None,
        bandit: Optional[Dict[str, Any]] = None,
        pass_candidate_audit: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        rag_search_result = rag_search_result or {"success": False, "results": []}
        controller_last_result = controller_last_result or {}
        bandit_payload = bandit or (
            controller_last_result.get("bandit")
            if isinstance(controller_last_result.get("bandit"), dict)
            else {}
        )
        realized_controller_effective = any(
            bool(event.get("realized_effective", False))
            for event in (
                bandit_payload.get("events", [])
                if isinstance(bandit_payload, dict) and isinstance(bandit_payload.get("events"), list)
                else []
            )
            if isinstance(event, dict)
        )
        return {
            "success": False,
            "error": reason,
            "draft": str(draft or ""),
            "final_outline": final_outline,
            "iterations": iterations,
            "source_results": rag_search_result.get("results", []),
            "rag_used": rag_used,
            "rag_search_success": bool(rag_search_result.get("success", False)),
            "rag_result_count": len(rag_search_result.get("results", []) or []),
            "rag_selected_query": rag_selected_query,
            "rag_vector_indexed": int(rag_search_result.get("vector_indexed", 0) or 0),
            "rag_vector_backend": str(rag_search_result.get("vector_backend", "") or ""),
            "rag_reranker": str(rag_search_result.get("reranker", "") or ""),
            "controller_effective": realized_controller_effective,
            "controller_source": str(controller_last_result.get("source", "") or ""),
            "verification": verification or {},
            "bandit": bandit_payload,
            "all_drafts": all_drafts or [],
            "forced_pass": False,
            "force_reason": "",
            "controller_triggered": controller_triggered,
            "controller_retry_count": controller_retry_count,
            "pass_candidate_audit": pass_candidate_audit or [],
            "metrics": metrics or {},
        }

    def _is_meaningful_real_draft(self, draft: str, outline: str = "") -> bool:
        text = str(draft or "").strip()
        return (
            bool(text)
            and len(text) >= self.min_draft_chars
            and not self._is_outline_like(text, outline)
        )

    def _is_usable_real_draft(self, draft: str, outline: str = "") -> bool:
        text = str(draft or "").strip()
        min_usable_chars = max(40, min(120, self.min_draft_chars))
        return (
            bool(text)
            and len(text) >= min_usable_chars
            and not self._is_outline_like(text, outline)
        )

    def _subsection_best_real_draft_result(
        self,
        *,
        reason: str,
        draft: str,
        final_outline: str = "",
        iterations: int = 0,
        verification: Optional[Dict[str, Any]] = None,
        all_drafts: Optional[List[str]] = None,
        metrics: Optional[Dict[str, int]] = None,
        rag_search_result: Optional[Dict[str, Any]] = None,
        rag_used: bool = False,
        rag_selected_query: str = "",
        controller_triggered: bool = False,
        controller_retry_count: int = 0,
        controller_last_result: Optional[Dict[str, Any]] = None,
        bandit: Optional[Dict[str, Any]] = None,
        pass_candidate_audit: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        rag_search_result = rag_search_result or {"success": False, "results": []}
        controller_last_result = controller_last_result or {}
        bandit_payload = bandit or (
            controller_last_result.get("bandit")
            if isinstance(controller_last_result.get("bandit"), dict)
            else {}
        )
        realized_controller_effective = any(
            bool(event.get("realized_effective", False))
            for event in (
                bandit_payload.get("events", [])
                if isinstance(bandit_payload, dict) and isinstance(bandit_payload.get("events"), list)
                else []
            )
            if isinstance(event, dict)
        )
        verification_payload = dict(verification or {})
        verification_payload.update({
            "accepted_by_best_real_draft": True,
            "best_real_draft_reason": reason,
            "forced_pass": False,
        })
        return {
            "success": True,
            "draft": str(draft or "").strip(),
            "final_outline": final_outline,
            "iterations": iterations,
            "source_results": rag_search_result.get("results", []),
            "rag_used": rag_used,
            "rag_search_success": bool(rag_search_result.get("success", False)),
            "rag_result_count": len(rag_search_result.get("results", []) or []),
            "rag_selected_query": rag_selected_query,
            "rag_vector_indexed": int(rag_search_result.get("vector_indexed", 0) or 0),
            "rag_vector_backend": str(rag_search_result.get("vector_backend", "") or ""),
            "rag_reranker": str(rag_search_result.get("reranker", "") or ""),
            "controller_effective": realized_controller_effective,
            "controller_source": str(controller_last_result.get("source", "") or ""),
            "verification": verification_payload,
            "bandit": bandit_payload,
            "all_drafts": all_drafts or [],
            "forced_pass": False,
            "force_reason": "",
            "best_effort": True,
            "best_effort_reason": reason,
            "controller_triggered": controller_triggered,
            "controller_retry_count": controller_retry_count,
            "pass_candidate_audit": pass_candidate_audit or [],
            "metrics": metrics or {},
        }

    @staticmethod
    def _subsection_success_failure_reason(
        subsection_result: Dict[str, Any],
        *,
        allow_forced_pass: bool,
    ) -> Tuple[bool, str]:
        """Return whether a nominally successful subsection must still fail."""
        if not isinstance(subsection_result, dict):
            return True, "invalid_subsection_result"
        forced_pass = bool(subsection_result.get("forced_pass", False))
        if forced_pass and not bool(allow_forced_pass):
            return True, str(subsection_result.get("force_reason") or "forced_pass_disallowed")
        verification = subsection_result.get("verification")
        if isinstance(verification, dict) and not bool(verification.get("is_passed", False)):
            return True, "verifier_not_passed"
        return False, ""

    def _compute_retry_delay(self, attempt: int) -> float:
        base = self.retry_base_delay * (2 ** max(0, min(attempt - 1, 6)))
        delay = min(base, self.retry_max_delay)
        delay += random.uniform(0.0, self.retry_jitter)
        return min(delay, self.retry_max_delay)

    def _sanitize_subsection_draft(self, draft: str) -> str:
        """Remove model meta-output and local reference blocks before verification/export."""
        text = str(draft or "").strip()
        if not text:
            return ""

        lines = text.splitlines()
        cleaned: List[str] = []
        skip_rest = False
        reference_heading = re.compile(r"^\s*(?:#{1,6}\s*)?(?:\*\*)?\s*(?:references?|bibliography|参考文献)\s*(?:\*\*)?\s*[:：]?\s*$", re.I)
        meta_heading = re.compile(
            r"^\s*(?:#{1,6}\s*)?(?:\*\*)?\s*(?:论证链实现说明|结构优化|写作说明|生成说明|质量检查说明|citation\s+notes?)\s*(?:\*\*)?\s*[:：]?\s*$",
            re.I,
        )
        inline_reference = re.compile(r"(?is)\s*(?:\*\*)?\s*(?:references?|bibliography|参考文献)\s*(?:\*\*)?\s*[:：]?\s*(?:\[\d+\].*)$")
        inline_meta = re.compile(r"(?is)\s*(?:---\s*)?(?:\*\*)?\s*(?:论证链实现说明|结构优化|写作说明|生成说明|质量检查说明)\s*(?:\*\*)?\s*[:：]?.*$")

        for raw_line in lines:
            stripped = raw_line.strip()
            if reference_heading.match(stripped) or meta_heading.match(stripped):
                skip_rest = True
                continue
            if skip_rest:
                continue
            raw_line = inline_reference.sub("", raw_line)
            raw_line = inline_meta.sub("", raw_line)
            cleaned.append(raw_line)

        text = "\n".join(cleaned).strip()
        text = re.sub(r"(?is)\n\s*(?:---\s*)?(?:\*\*)?\s*(?:论证链实现说明|结构优化|写作说明|生成说明|质量检查说明)\s*(?:\*\*)?\s*[:：]?.*$", "", text).strip()
        text = re.sub(r"(?is)\n\s*(?:\*\*)?\s*(?:references?|bibliography|参考文献)\s*(?:\*\*)?\s*[:：]?\s*(?:\[\d+\].*)$", "", text).strip()
        text = self._normalize_citation_runs(text)
        text = self._ensure_readable_paragraphs(text)
        return text

    @staticmethod
    def _normalize_citation_runs(text: str) -> str:
        """Merge duplicate citation clusters without changing their source ids."""
        value = str(text or "")

        def _merge(match: re.Match) -> str:
            ids: List[str] = []
            for raw in re.findall(r"\[(\d+)\]", match.group(0)):
                if raw not in ids:
                    ids.append(raw)
            punctuation = re.search(r"[.!?。！？]", match.group(0))
            suffix = punctuation.group(0) if punctuation else ""
            return "".join(f"[{raw}]" for raw in ids) + suffix + (" " if suffix else "")

        # A model or later citation injector can produce [1][2].[1][2][3].
        # Treat both clusters as one evidentiary citation placed before punctuation.
        value = re.sub(
            r"(?:\[\d+\]\s*)+[.!?。！？]\s*(?:\[\d+\]\s*)+",
            _merge,
            value,
        )
        value = re.sub(r"(?:\[\d+\]\s*){2,}", _merge, value)
        value = re.sub(r"(\[\d+\])(?=[A-Za-z\u4e00-\u9fff])", r"\1 ", value)
        return value.strip()

    @staticmethod
    def _ensure_readable_paragraphs(text: str) -> str:
        """Split pathological long single blocks at existing sentence boundaries."""
        value = str(text or "").strip()
        if len(value) < 1800 or len(re.findall(r"\n\s*\n", value)) >= 2:
            return value

        sentences = [
            item.strip()
            for item in re.split(r"(?<=[.!?。！？])\s+", value)
            if item.strip()
        ]
        if len(sentences) < 4:
            return value

        paragraphs: List[str] = []
        current: List[str] = []
        current_chars = 0
        target_chars = 1050
        for sentence in sentences:
            if current and current_chars + len(sentence) + 1 > target_chars:
                paragraphs.append(" ".join(current))
                current = []
                current_chars = 0
            current.append(sentence)
            current_chars += len(sentence) + 1
        if current:
            paragraphs.append(" ".join(current))
        return "\n\n".join(paragraphs) if len(paragraphs) >= 2 else value

    @staticmethod
    def _draft_is_complete(draft: str, metadata: Optional[Dict[str, Any]] = None) -> bool:
        """Reject provider-truncated or visibly unfinished prose before verification."""
        finish_reason = str((metadata or {}).get("finish_reason") or "").strip().lower()
        if finish_reason in {"length", "max_tokens", "token_limit"}:
            return False
        text = str(draft or "").strip()
        if not text:
            return False
        tail = re.sub(r"(?:\[\d+\]\s*)+$", "", text).rstrip()
        return bool(re.search(r"[.!?。！？][\"')\]}]*$", tail))

    def _continue_incomplete_draft(self, draft: str, outline: str) -> Dict[str, Any]:
        """Complete a token-truncated draft instead of silently accepting or discarding it."""
        base = str(draft or "").strip()
        if not base:
            return {"success": False, "error": "empty_draft", "draft": "", "metadata": {}}
        english = self._prefers_english_generation(outline, base)
        if english:
            instruction = (
                "Continue the academic subsection below from exactly where it stops. "
                "Output continuation text only: do not repeat earlier sentences, add a heading, "
                "or add a References block. Complete the unfinished sentence first, then finish "
                "the subsection in one or two concise paragraphs with a complete final sentence."
            )
        else:
            instruction = (
                "请从下方学术小节停止的位置准确续写。只输出续写正文：不要重复已有句子，"
                "不要添加标题或参考文献块。先补完整未结束的句子，再用一至两个简洁自然段"
                "完成本小节，并以完整句子收尾。"
            )
        prompt = (
            f"{instruction}\n\n"
            f"SUBSECTION OUTLINE\n{str(outline or '')[:1800]}\n\n"
            f"EXISTING DRAFT TAIL\n{base[-3200:]}"
        )
        result = self._call_generator(prompt, max_tokens=min(900, self.generator_max_tokens))
        if not isinstance(result, dict) or not result.get("success"):
            return result if isinstance(result, dict) else {"success": False, "error": "continuation_failed"}
        continuation = self._sanitize_subsection_draft(result.get("draft", ""))
        if not continuation:
            return {"success": False, "error": "empty_continuation", "draft": base, "metadata": result.get("metadata", {})}

        # Remove an exact overlap when the provider repeats the visible tail.
        overlap = 0
        max_overlap = min(500, len(base), len(continuation))
        base_lower = base.lower()
        continuation_lower = continuation.lower()
        for size in range(max_overlap, 19, -1):
            if base_lower[-size:] == continuation_lower[:size]:
                overlap = size
                break
        merged = (base + ("" if overlap else " ") + continuation[overlap:]).strip()
        merged = self._normalize_citation_runs(merged)
        merged = self._ensure_readable_paragraphs(merged)
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        return {
            "success": self._draft_is_complete(merged, metadata),
            "draft": merged,
            "metadata": metadata,
            "continued": True,
            "overlap_chars": overlap,
            "error": "" if self._draft_is_complete(merged, metadata) else "continuation_incomplete",
        }

    def _limit_subsection_draft_length(self, draft: str) -> str:
        """Trim overlong drafts at paragraph/sentence boundaries for fair eval.

        DeepSeek can ignore length instructions when the prompt contains a lot of
        evidence context. This keeps the model's own generated content, but
        removes lower-priority tail paragraphs so downstream scoring is not
        inflated by verbosity.
        """
        text = str(draft or "").strip()
        if not self.enforce_target_draft_max or len(text) <= self.target_draft_max_chars:
            return text

        limit = max(self.target_draft_min_chars, self.target_draft_max_chars)
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", text) if p.strip()]
        if not paragraphs:
            return text[:limit].rstrip()

        kept: List[str] = []
        current_len = 0
        for para in paragraphs:
            candidate_len = current_len + len(para) + (2 if kept else 0)
            if kept and candidate_len > limit:
                break
            kept.append(para)
            current_len = candidate_len
            if current_len >= self.target_draft_min_chars:
                break

        trimmed = "\n\n".join(kept).strip()
        if len(trimmed) < self.target_draft_min_chars and len(text) > len(trimmed):
            remaining = text[len(trimmed):].strip()
            budget = max(0, limit - len(trimmed) - 2)
            if budget > 80 and remaining:
                sentence_parts = re.split(r"(?<=[。！？.!?])\s+", remaining)
                extra = ""
                for sent in sentence_parts:
                    sent = sent.strip()
                    if not sent:
                        continue
                    if len(extra) + len(sent) + 1 > budget:
                        break
                    extra = (extra + " " + sent).strip()
                if extra:
                    trimmed = (trimmed + "\n\n" + extra).strip()

        if len(trimmed) > limit:
            clipped = trimmed[:limit]
            boundary = max(clipped.rfind("。"), clipped.rfind("."), clipped.rfind("\n"))
            if boundary >= self.target_draft_min_chars:
                clipped = clipped[: boundary + 1]
            trimmed = clipped.rstrip()

        return trimmed or text[:limit].rstrip()

    def _inline_source_ref_count(self, text: str, available_source_count: int) -> int:
        refs = set()
        for source_ref, ieee_ref in re.findall(r"(?:\[来源\s*(\d+)\]|\[(\d+)\])", str(text or "")):
            raw = source_ref or ieee_ref
            if not raw:
                continue
            try:
                idx = int(raw)
            except Exception:
                continue
            if 1 <= idx <= max(0, int(available_source_count or 0)):
                refs.add(idx)
        return len(refs)

    def _source_alignment_features(
        self,
        draft: str,
        source_results: List[Dict[str, Any]],
        *,
        topic_anchor_terms: Optional[List[str]] = None,
        topic_anchor_phrases: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Measure lexical/source n-gram grounding against real retrieved sources.

        This is a selector signal only: it never fabricates text or claims.  It
        rewards candidates that preserve terminology from the current RAG/source
        set, which helps ROUGE/BERTScore without weakening verifier checks.
        """
        text = str(draft or "")
        lower_text = text.lower()
        sources = source_results or []
        if not text.strip() or not sources:
            return {
                "enabled": bool(self.source_alignment_selector_enabled),
                "score": 0.0,
                "term_coverage": 0.0,
                "bigram_overlap": 0.0,
                "citation_density": 0.0,
                "source_count": len(sources),
                "missing_terms": [],
            }

        stop = {
            "the", "and", "for", "with", "from", "into", "that", "this", "these", "those",
            "were", "have", "has", "are", "was", "its", "their", "about", "which", "such",
            "using", "used", "based", "between", "within", "without", "section", "source",
            "research", "article", "paper", "study", "analysis", "content", "toward",
        }
        source_parts: List[str] = []
        per_source_terms: List[set[str]] = []
        for item in sources:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            snippet = str(item.get("snippet") or "").strip()
            summary = str(item.get("summary") or "").strip()
            body = str(item.get("body") or "").strip()
            body_for_terms = ""
            if body and "|" not in body[:180]:
                body_for_terms = body
            source_part = f"{title} {snippet} {summary} {body_for_terms}".strip()
            source_parts.append(source_part)
            per_source_terms.append({
                tok.lower()
                for tok in re.findall(r"[A-Za-z][A-Za-z0-9+./_-]{3,}|[\u4e00-\u9fff]{2,12}", source_part)
                if tok.lower() not in stop and not tok.isdigit()
            })
        source_text = " ".join(source_parts)
        raw_terms = [
            tok.lower()
            for tok in re.findall(r"[A-Za-z][A-Za-z0-9+./_-]{3,}|[\u4e00-\u9fff]{2,12}", source_text)
            if tok.lower() not in stop and not tok.isdigit()
        ]
        terms = list(dict.fromkeys(raw_terms))[:80]
        term_hits = [term for term in terms if term in lower_text]
        term_coverage = len(term_hits) / max(1, min(40, len(terms))) if terms else 0.0
        term_coverage = min(1.0, term_coverage)

        def _bigrams(raw: str) -> set[tuple[str, str]]:
            toks = [
                tok.lower()
                for tok in re.findall(r"[A-Za-z][A-Za-z0-9+./_-]{2,}|[\u4e00-\u9fff]{2,12}", str(raw or ""))
                if tok.lower() not in stop
            ]
            return set(zip(toks, toks[1:]))

        source_bigrams = _bigrams(source_text)
        draft_bigrams = _bigrams(text)
        bigram_overlap = (
            min(1.0, len(source_bigrams & draft_bigrams) / max(1, min(24, len(source_bigrams))))
            if source_bigrams
            else 0.0
        )
        citation_density = min(
            1.0,
            self._inline_source_ref_count(text, len(sources)) / max(1.0, min(4.0, float(len(sources)))),
        )
        topic_terms = list(dict.fromkeys(
            str(term or "").strip().lower()
            for term in (topic_anchor_terms or [])
            if str(term or "").strip()
        ))
        topic_hits = [term for term in topic_terms if term in lower_text]
        topic_anchor_coverage = (
            min(1.0, len(topic_hits) / max(1, min(12, len(topic_terms))))
            if topic_terms
            else term_coverage
        )
        topic_phrases = list(dict.fromkeys(
            " ".join(str(phrase or "").lower().split())
            for phrase in (topic_anchor_phrases or [])
            if " " in " ".join(str(phrase or "").split())
        ))
        topic_phrase_hits = [phrase for phrase in topic_phrases if phrase in lower_text]
        topic_phrase_coverage = (
            min(1.0, len(topic_phrase_hits) / max(1, min(12, len(topic_phrases))))
            if topic_phrases
            else topic_anchor_coverage
        )
        score = max(
            0.0,
            min(
                1.0,
                0.35 * term_coverage
                + 0.25 * bigram_overlap
                + 0.20 * citation_density
                + 0.20 * topic_anchor_coverage,
            ),
        )
        recurring_terms = {
            term
            for term in terms
            if sum(1 for source_terms in per_source_terms if term in source_terms) >= 2
        }
        topical_terms = {
            term
            for term in terms
            if any(term == anchor or term in anchor or anchor in term for anchor in topic_terms)
        }
        # A repair should not import arbitrary proper nouns from one narrow
        # paper. Prefer task-related or independently recurring terminology.
        grounded_repair_terms = [
            term for term in terms if term in topical_terms or term in recurring_terms
        ]
        missing = [term for term in grounded_repair_terms if term not in lower_text][:12]
        return {
            "enabled": bool(self.source_alignment_selector_enabled),
            "score": round(score, 6),
            "term_coverage": round(term_coverage, 6),
            "bigram_overlap": round(bigram_overlap, 6),
            "citation_density": round(citation_density, 6),
            "topic_anchor_coverage": round(topic_anchor_coverage, 6),
            "topic_phrase_coverage": round(topic_phrase_coverage, 6),
            "matched_topic_phrases": topic_phrase_hits[:12],
            "source_count": len(sources),
            "matched_terms": term_hits[:20],
            "missing_terms": missing,
        }

    @staticmethod
    def _source_alignment_topic_anchor_terms(
        original_prompt: str,
        outline: str,
        limit: int = 14,
    ) -> List[str]:
        """Extract task/outline terms that source alignment should preserve.

        These anchors come only from the user's task and current outline, never
        from evaluation references. They keep full runs aligned with the topic
        while RAG/source terms improve grounding.
        """
        stop = {
            "write", "long", "form", "academic", "research", "report", "paper",
            "article", "section", "subsection", "current", "using", "with",
            "from", "into", "that", "this", "these", "those", "about", "and",
            "the", "for", "are", "was", "were", "include", "including",
            "explaining", "comparing", "covering", "analysis", "comprehensive",
            "long-form",
        }
        text = " ".join([str(original_prompt or ""), str(outline or "")])
        lower_text = text.lower()
        domain_expansions: List[str] = []
        if re.search(r"\b(?:artificial intelligence|machine learning|deep learning|neural|foundation models?|large language models?|\bllms?\b|generative ai|multimodal|multi-modal)\b", lower_text):
            domain_expansions.extend([
                "artificial intelligence",
                "machine learning",
                "deep learning",
                "neural networks",
                "foundation models",
                "generative ai",
                "transformer",
                "training data",
                "inference",
                "evaluation",
            ])
        if re.search(r"\b(?:multimodal|multi-modal|vision-language|cross-modal|image|audio|video)\b", lower_text):
            domain_expansions.extend([
                "multimodal learning",
                "vision language",
                "cross modal",
                "text",
                "image",
                "audio",
                "video",
            ])
        if re.search(r"\b(?:education|educational|learning|students?|teachers?|classroom|curriculum)\b", lower_text):
            domain_expansions.extend([
                "students",
                "teachers",
                "classroom",
                "curriculum",
                "assessment",
                "learning outcomes",
            ])
        if re.search(r"\b(?:robotics?|robot|control|navigation|manipulation|sensor)\b", lower_text):
            domain_expansions.extend([
                "robot",
                "control",
                "sensors",
                "planning",
                "navigation",
                "manipulation",
            ])
        raw_tokens = re.findall(r"[A-Za-z][A-Za-z0-9+./_-]{3,}", text)
        terms: List[str] = []
        seen = set()
        for phrase in domain_expansions:
            norm_phrase = " ".join(str(phrase or "").lower().split())
            if norm_phrase and norm_phrase not in seen:
                seen.add(norm_phrase)
                terms.append(norm_phrase)
        for token in raw_tokens:
            norm = token.lower().strip("-_/.,")
            if not norm or norm in stop or norm.isdigit():
                continue
            if norm.endswith("s") and len(norm) > 5 and norm[:-1] in seen:
                continue
            if norm not in seen:
                seen.add(norm)
                terms.append(norm)
            if len(terms) >= max(1, int(limit or 1)):
                break
        return terms

    @staticmethod
    def _task_anchor_phrases(text: str, limit: int = 18) -> List[str]:
        """Extract canonical task n-grams without consulting evaluation references."""
        stop = {
            "write", "long", "form", "academic", "research", "report", "paper",
            "covering", "comparing", "explaining", "discussing", "include", "including",
            "the", "and", "for", "with", "from", "into", "that", "this", "these",
        }
        tokens = [
            token.lower()
            for token in re.findall(r"[A-Za-z][A-Za-z0-9+/-]{2,}", str(text or ""))
            if token.lower() not in stop
        ]
        phrases: List[str] = []
        for segment in re.split(r"[,;:()]|\b(?:and|versus|vs\.?|including)\b", str(text or ""), flags=re.I):
            segment_tokens = [
                token.lower()
                for token in re.findall(r"[A-Za-z][A-Za-z0-9+/-]{2,}", segment)
                if token.lower() not in stop
            ]
            if 2 <= len(segment_tokens) <= 4:
                phrase = " ".join(segment_tokens)
                if phrase not in phrases:
                    phrases.append(phrase)
        per_size = max(1, int(limit or 18) // 3)
        for size in (4, 3, 2):
            added_for_size = 0
            for index in range(max(0, len(tokens) - size + 1)):
                phrase = " ".join(tokens[index:index + size])
                if phrase not in phrases:
                    phrases.append(phrase)
                    added_for_size += 1
                if added_for_size >= per_size:
                    break
        return phrases[: max(1, int(limit or 1))]

    @classmethod
    def _source_outline_concept_terms(
        cls,
        *,
        original_prompt: str,
        outline: str,
        section_title: str = "",
        subsection_title: str = "",
        source_results: Optional[List[Dict[str, Any]]] = None,
        limit: int = 32,
    ) -> List[str]:
        """Extract source/outline concepts for selecting among real pass candidates.

        The terms come only from the task, outline, headings, and current RAG
        sources. This is a candidate-selection signal, not a rewriting step, so
        it cannot introduce unsupported claims or evaluation-reference leakage.
        """
        stop = {
            "write", "long", "form", "academic", "research", "report", "paper",
            "article", "section", "subsection", "current", "using", "with",
            "from", "into", "that", "this", "these", "those", "about", "and",
            "the", "for", "are", "was", "were", "include", "including",
            "explaining", "comparing", "covering", "analysis", "comprehensive",
            "study", "studies",
            "method", "methods", "approach", "approaches", "system", "systems",
            "based", "through", "toward", "between", "within", "without",
            "source", "sources", "paper", "papers", "survey", "review",
        }
        text_parts = [
            str(original_prompt or ""),
            str(section_title or ""),
            str(subsection_title or ""),
            str(outline or ""),
        ]
        for source in source_results or []:
            if not isinstance(source, dict):
                continue
            text_parts.append(str(source.get("title") or ""))
            text_parts.append(str(source.get("snippet") or source.get("body") or "")[:420])
        raw_text = " ".join(text_parts)
        candidates = cls._source_alignment_topic_anchor_terms(
            raw_text,
            outline,
            limit=max(12, int(limit or 32)),
        )
        seen = set(candidates)
        for token in re.findall(r"[A-Za-z][A-Za-z0-9+./_-]{3,}", raw_text):
            norm = token.lower().strip("-_/.,")
            if not norm or norm in stop or norm.isdigit():
                continue
            if norm.endswith("s") and len(norm) > 5 and norm[:-1] in seen:
                continue
            if norm in seen:
                continue
            seen.add(norm)
            candidates.append(norm)
            if len(candidates) >= max(1, int(limit or 32)):
                break
        return candidates[: max(1, int(limit or 32))]

    @staticmethod
    def _concept_chain_coverage_score(text: str, concept_terms: List[str]) -> float:
        """Score whether a candidate covers source/outline concepts across paragraphs."""
        terms = [str(term or "").lower() for term in concept_terms if str(term or "").strip()]
        if not terms:
            return 0.0
        lower_text = str(text or "").lower()
        matched = [term for term in terms if term in lower_text]
        term_coverage = len(matched) / max(1, min(24, len(terms)))
        paragraphs = [
            para.strip().lower()
            for para in re.split(r"\n\s*\n+", str(text or ""))
            if para.strip() and not re.match(r"^\s*#{1,6}\s+", para.strip())
        ]
        if not paragraphs:
            paragraph_spread = 0.0
        else:
            concept_paragraphs = sum(1 for para in paragraphs if any(term in para for term in terms))
            paragraph_spread = concept_paragraphs / max(1, min(4, len(paragraphs)))
        return round(max(0.0, min(1.0, 0.72 * term_coverage + 0.28 * paragraph_spread)), 6)

    @staticmethod
    def _discourse_structure_score(text: str) -> float:
        """Reward complete, readable multi-paragraph subsection argument structure."""
        paragraphs = [
            para.strip()
            for para in re.split(r"\n\s*\n+", str(text or ""))
            if para.strip() and not re.match(r"^\s*#{1,6}\s+", para.strip())
        ]
        if not paragraphs:
            return 0.0
        sentence_counts = [
            len(re.findall(r"[.!?。！？](?:\s|$)", para))
            for para in paragraphs
        ]
        developed_paragraphs = sum(1 for count in sentence_counts if count >= 2)
        paragraph_score = min(1.0, len(paragraphs) / 4.0)
        development_score = developed_paragraphs / max(1, min(4, len(paragraphs)))
        return round(max(0.0, min(1.0, 0.55 * paragraph_score + 0.45 * development_score)), 6)

    def _source_alignment_candidate_preferred(
        self,
        candidate: Dict[str, Any],
        incumbent: Dict[str, Any],
    ) -> bool:
        """Prefer a much better source-grounded candidate when verifier scores are no-harm close."""
        if not self.source_alignment_selector_enabled:
            return False
        cand_alignment = candidate.get("source_alignment") if isinstance(candidate.get("source_alignment"), dict) else {}
        inc_alignment = incumbent.get("source_alignment") if isinstance(incumbent.get("source_alignment"), dict) else {}
        if not cand_alignment.get("source_count") or not inc_alignment.get("source_count"):
            return False

        cand_score = float(cand_alignment.get("score", 0.0) or 0.0)
        inc_score = float(inc_alignment.get("score", 0.0) or 0.0)
        min_gain = float(os.getenv("FLOWERNET_SOURCE_ALIGNMENT_SELECTOR_MIN_GAIN", "0.18"))
        if cand_score < inc_score + min_gain:
            return False

        cand_v = candidate.get("verification", {}) if isinstance(candidate.get("verification"), dict) else {}
        inc_v = incumbent.get("verification", {}) if isinstance(incumbent.get("verification"), dict) else {}
        cand_rel = float(cand_v.get("relevancy_index", 0.0) or 0.0)
        inc_rel = float(inc_v.get("relevancy_index", 0.0) or 0.0)
        cand_quality = float(cand_v.get("quality_score", 0.0) or 0.0)
        inc_quality = float(inc_v.get("quality_score", 0.0) or 0.0)
        cand_red = _coerce_float(cand_v.get("redundancy_index"), 1.0)
        inc_red = _coerce_float(inc_v.get("redundancy_index"), 1.0)
        cand_topic_phrase = float(cand_alignment.get("topic_phrase_coverage", 0.0) or 0.0)
        inc_topic_phrase = float(inc_alignment.get("topic_phrase_coverage", 0.0) or 0.0)
        cand_topic_anchor = float(cand_alignment.get("topic_anchor_coverage", 0.0) or 0.0)
        inc_topic_anchor = float(inc_alignment.get("topic_anchor_coverage", 0.0) or 0.0)
        cand_bigram = float(cand_alignment.get("bigram_overlap", 0.0) or 0.0)
        inc_bigram = float(inc_alignment.get("bigram_overlap", 0.0) or 0.0)

        max_rel_drop = float(os.getenv("FLOWERNET_SOURCE_ALIGNMENT_SELECTOR_MAX_REL_DROP", "0.02"))
        max_quality_drop = float(os.getenv("FLOWERNET_SOURCE_ALIGNMENT_SELECTOR_MAX_QUALITY_DROP", "0.012"))
        max_red_increase = float(os.getenv("FLOWERNET_SOURCE_ALIGNMENT_SELECTOR_MAX_RED_INCREASE", "0.02"))
        max_topic_phrase_drop = float(os.getenv("FLOWERNET_SOURCE_ALIGNMENT_SELECTOR_MAX_TOPIC_PHRASE_DROP", "0.08"))
        max_topic_anchor_drop = float(os.getenv("FLOWERNET_SOURCE_ALIGNMENT_SELECTOR_MAX_TOPIC_ANCHOR_DROP", "0.08"))
        max_bigram_drop = float(os.getenv("FLOWERNET_SOURCE_ALIGNMENT_SELECTOR_MAX_BIGRAM_DROP", "0.10"))
        if cand_rel < inc_rel - max_rel_drop:
            return False
        if cand_quality < inc_quality - max_quality_drop:
            return False
        if cand_red > inc_red + max_red_increase:
            return False
        if cand_topic_phrase < inc_topic_phrase - max_topic_phrase_drop:
            return False
        if cand_topic_anchor < inc_topic_anchor - max_topic_anchor_drop:
            return False
        if cand_bigram < inc_bigram - max_bigram_drop:
            return False
        return True

    def _pass_candidate_should_replace(
        self,
        candidate: Dict[str, Any],
        incumbent: Dict[str, Any],
        candidate_score: float,
        incumbent_score: float,
    ) -> bool:
        """Replace a strict-pass candidate only for a material, no-harm gain.

        Candidate scores combine several noisy verifier signals. Treating any
        floating-point improvement as decisive caused later generations with
        lower semantic quality to displace a sound strict pass. A material
        score margin handles ordinary gains; the source-specific exception
        still permits a large alignment improvement under explicit no-harm
        constraints.
        """
        candidate_verification = candidate.get("verification", {}) if isinstance(candidate.get("verification"), dict) else {}
        incumbent_verification = incumbent.get("verification", {}) if isinstance(incumbent.get("verification"), dict) else {}
        candidate_alignment = candidate.get("source_alignment", {}) if isinstance(candidate.get("source_alignment"), dict) else {}
        incumbent_alignment = incumbent.get("source_alignment", {}) if isinstance(incumbent.get("source_alignment"), dict) else {}
        candidate_reward = candidate.get("trained_reward", {}) if isinstance(candidate.get("trained_reward"), dict) else {}
        incumbent_reward = incumbent.get("trained_reward", {}) if isinstance(incumbent.get("trained_reward"), dict) else {}
        no_harm_checks = (
            (
                float(candidate_verification.get("relevancy_index", 0.0) or 0.0),
                float(incumbent_verification.get("relevancy_index", 0.0) or 0.0),
                float(os.getenv("FLOWERNET_PASS_SELECTOR_MAX_REL_DROP", "0.02")),
            ),
            (
                float(candidate_verification.get("quality_score", 0.0) or 0.0),
                float(incumbent_verification.get("quality_score", 0.0) or 0.0),
                float(os.getenv("FLOWERNET_PASS_SELECTOR_MAX_QUALITY_DROP", "0.015")),
            ),
            (
                float(candidate_alignment.get("score", 0.0) or 0.0),
                float(incumbent_alignment.get("score", 0.0) or 0.0),
                float(os.getenv("FLOWERNET_PASS_SELECTOR_MAX_ALIGNMENT_DROP", "0.02")),
            ),
            (
                float(candidate_alignment.get("term_coverage", 0.0) or 0.0),
                float(incumbent_alignment.get("term_coverage", 0.0) or 0.0),
                float(os.getenv("FLOWERNET_PASS_SELECTOR_MAX_TERM_DROP", "0.04")),
            ),
            (
                float(candidate_alignment.get("bigram_overlap", 0.0) or 0.0),
                float(incumbent_alignment.get("bigram_overlap", 0.0) or 0.0),
                float(os.getenv("FLOWERNET_PASS_SELECTOR_MAX_BIGRAM_DROP", "0.06")),
            ),
        )
        if any(current < previous - max(0.0, tolerance) for current, previous, tolerance in no_harm_checks):
            return False
        if candidate_reward.get("used") and incumbent_reward.get("used"):
            reward_drop = float(os.getenv("FLOWERNET_PASS_SELECTOR_MAX_REWARD_DROP", "0.08"))
            if float(candidate_reward.get("score", 0.0) or 0.0) < float(
                incumbent_reward.get("score", 0.0) or 0.0
            ) - max(0.0, reward_drop):
                return False

        min_gain = max(0.0, float(os.getenv("FLOWERNET_PASS_SELECTOR_MIN_GAIN", "0.01")))
        if float(candidate_score) >= float(incumbent_score) + min_gain:
            return True
        alignment_slack = max(
            0.0,
            float(os.getenv("FLOWERNET_PASS_SELECTOR_ALIGNMENT_SCORE_SLACK", "0.005")),
        )
        return bool(
            float(candidate_score) >= float(incumbent_score) - alignment_slack
            and self._source_alignment_candidate_preferred(candidate, incumbent)
        )

    @staticmethod
    def _candidate_is_strict_pass(candidate: Dict[str, Any]) -> bool:
        verification = candidate.get("verification") if isinstance(candidate, dict) else {}
        return bool(isinstance(verification, dict) and verification.get("is_passed", False))

    @classmethod
    def _should_replace_best_candidate(
        cls,
        *,
        best_candidate: Dict[str, Any],
        current_candidate: Dict[str, Any],
        best_gap: float,
        current_gap: float,
        source_alignment_preferred: bool,
        current_quality: float,
        current_rel: float,
        best_quality: float,
        best_rel: float,
    ) -> bool:
        """Protect strict verifier passes from later failed repair candidates."""
        best_passed = cls._candidate_is_strict_pass(best_candidate)
        current_passed = cls._candidate_is_strict_pass(current_candidate)
        if best_passed and not current_passed:
            return False
        if current_passed and not best_passed:
            return True
        return bool(
            current_gap < best_gap
            or source_alignment_preferred
            or (
                abs(current_gap - best_gap) < 1e-6
                and (current_quality, current_rel) > (best_quality, best_rel)
            )
        )

    def _inject_missing_source_citations(
        self,
        draft: str,
        source_results: List[Dict[str, Any]],
        min_citations: int = 1,
    ) -> str:
        """Attach real RAG source markers when the model omitted inline refs.

        This does not invent references. It only inserts numbered markers from
        the current subsection's retrieved source list, so downstream verifier
        and final References remain traceable.
        """
        text = str(draft or "").strip()
        if not text or not source_results:
            return text

        available = len(source_results)
        required = min(max(1, int(min_citations or 1)), available)
        # Models sometimes emit bracketed years or out-of-range pseudo refs
        # such as [2024]. The verifier correctly treats those as invalid
        # citations, so remove them before attaching valid current-source refs.
        def _strip_invalid_marker(match: re.Match) -> str:
            raw = match.group(1)
            try:
                idx = int(raw)
            except Exception:
                return ""
            return match.group(0) if 1 <= idx <= available else ""

        text = re.sub(r"\[(\d{1,5})\]", _strip_invalid_marker, text)
        if self._inline_source_ref_count(text, available) >= required:
            return text

        markers = [f"[{idx}]" for idx in range(1, required + 1)]
        protected = re.compile(r"(\[[0-9]+\])\s*$")
        paragraphs = re.split(r"(\n\s*\n)", text)
        marker_index = 0
        updated: List[str] = []
        for part in paragraphs:
            if marker_index >= len(markers):
                updated.append(part)
                continue
            if not part.strip() or part.strip().startswith("|"):
                updated.append(part)
                continue
            stripped = part.strip()
            if len(stripped) < 50 or protected.search(stripped):
                updated.append(part)
                continue
            pieces = re.split(r"([。！？!?])", part, maxsplit=1)
            if len(pieces) >= 3 and len(pieces[0].strip()) >= 20:
                injected = pieces[0].rstrip() + markers[marker_index] + pieces[1] + "".join(pieces[2:])
                updated.append(injected)
            else:
                updated.append(part.rstrip() + markers[marker_index])
            marker_index += 1

        result = "".join(updated).strip()
        while marker_index < len(markers):
            result = result.rstrip() + markers[marker_index]
            marker_index += 1
        return result

    def _verification_near_pass(self, verification: Dict[str, Any], rel_threshold: float, red_threshold: float) -> bool:
        if not isinstance(verification, dict):
            return False
        source_check = verification.get("source_check") if isinstance(verification.get("source_check"), dict) else {}
        if not bool(source_check.get("passed", False)):
            return False
        rel = float(verification.get("relevancy_index", 0) or 0)
        red = _coerce_float(verification.get("redundancy_index"), 1.0)
        quality = float(verification.get("quality_score", 0) or 0)
        quality_threshold = float(verification.get("quality_threshold", 0.6) or 0.6)
        failed_dims = verification.get("quality_dimensions_failed", [])
        failed_count = len(failed_dims) if isinstance(failed_dims, list) else 0
        red_margin = max(0.0, float(os.getenv("NEAR_PASS_REDUNDANCY_MARGIN", "0.020")))
        return (
            rel >= rel_threshold
            and red <= min(1.0, red_threshold + red_margin)
            and quality >= max(0.0, quality_threshold - self.near_pass_quality_margin)
            and failed_count <= self.best_draft_max_failed_dims
        )

    def _verification_borderline_audit(
        self,
        verification: Dict[str, Any],
        rel_threshold: float,
        red_threshold: float,
        iteration: int,
        controller_calls: int,
    ) -> bool:
        """Trigger one controller audit for drafts that pass but sit near risk thresholds."""
        if os.getenv("CONTROLLER_BORDERLINE_AUDIT", "true").lower() != "true":
            return False
        if not isinstance(verification, dict) or not bool(verification.get("is_passed", False)):
            return False
        if iteration != 1 or int(controller_calls or 0) > 0:
            return False
        source_check = verification.get("source_check") if isinstance(verification.get("source_check"), dict) else {}
        if source_check and source_check.get("trigger_controller"):
            return True
        rel_margin = max(0.0, float(os.getenv("BORDERLINE_AUDIT_REL_MARGIN", "0.000")))
        red_margin = max(0.0, float(os.getenv("BORDERLINE_AUDIT_RED_MARGIN", "0.002")))
        quality_margin = max(0.0, float(os.getenv("BORDERLINE_AUDIT_QUALITY_MARGIN", "0.000")))
        rel = float(verification.get("relevancy_index", 0) or 0)
        red = _coerce_float(verification.get("redundancy_index"), 1.0)
        quality = float(verification.get("quality_score", 0) or 0)
        quality_threshold = float(verification.get("quality_threshold", 0.0) or 0.0)
        near_rel = rel_margin > 0 and rel <= min(1.0, rel_threshold + rel_margin)
        near_red = red_margin > 0 and red >= max(0.0, red_threshold - red_margin)
        near_quality = quality_margin > 0 and quality <= min(1.0, quality_threshold + quality_margin)
        return bool(near_rel or near_red or near_quality)

    @staticmethod
    def _adaptive_best_of_risk_reasons(
        *,
        relevancy: float,
        relevancy_threshold: float,
        quality: float,
        quality_threshold: float,
        source_alignment: float,
        source_alignment_threshold: float,
        quality_dimensions: Dict[str, Any],
        dimension_thresholds: Dict[str, Any],
    ) -> List[str]:
        """Explain why a strict pass is still worth a second generation call."""
        reasons: List[str] = []
        if float(relevancy) < min(1.0, float(relevancy_threshold) + 0.08):
            reasons.append("relevancy_near_threshold")
        if float(quality) < min(1.0, float(quality_threshold) + 0.08):
            reasons.append("quality_near_threshold")
        if float(source_alignment) < min(1.0, float(source_alignment_threshold) + 0.06):
            reasons.append("source_alignment_near_threshold")
        for dimension_name, threshold_value in (dimension_thresholds or {}).items():
            if dimension_name not in (quality_dimensions or {}):
                continue
            try:
                margin = float(quality_dimensions[dimension_name]) - float(threshold_value)
            except (TypeError, ValueError):
                continue
            if margin < 0.05:
                reasons.append(f"{dimension_name}_near_threshold")
        return reasons

    @staticmethod
    def _should_audit_low_source_alignment_pass(
        *,
        is_passed: bool,
        source_alignment: Dict[str, Any],
        iteration: int,
        controller_calls: int,
        effective_attempt_cap: int,
        hard_min_score: float,
        pass_audit_min_score: float,
        evidence_grounding: float = 1.0,
        relevancy: float = 0.0,
        quality_score: float = 0.0,
        prior_audit_count: int = 0,
        last_selected_arm: str = "",
    ) -> bool:
        """Recheck a first-pass draft only when independent evidence confirms risk.

        This is intentionally source-only: it never looks at evaluation references.
        A low composite alignment score is not sufficient by itself because sparse
        source titles can depress lexical overlap for otherwise grounded prose.
        Post-Controller passes are never re-audited, which prevents repair loops.
        """
        if not bool(is_passed) or not isinstance(source_alignment, dict):
            return False
        if not int(source_alignment.get("source_count", 0) or 0):
            return False
        cap = int(effective_attempt_cap or 0)
        if cap > 0 and int(iteration or 0) >= cap:
            return False
        if cap > 0 and (cap - int(iteration or 0)) < 2:
            return False
        if int(prior_audit_count or 0) > 0:
            return False
        controller_calls = int(controller_calls or 0)
        if controller_calls <= 0 and int(iteration or 0) != 1:
            return False
        if controller_calls > 0 and str(last_selected_arm or "").strip() == "defect_evidence":
            return False
        threshold = max(0.0, min(1.0, float(pass_audit_min_score or 0.0)))
        threshold = max(threshold, max(0.0, min(1.0, float(hard_min_score or 0.0))))
        score = max(0.0, min(1.0, float(source_alignment.get("score", 0.0) or 0.0)))
        if score >= threshold:
            return False
        if controller_calls > 0:
            material_gap = max(0.0, float(os.getenv("SOURCE_AUDIT_POST_REPAIR_MIN_GAP", "0.05")))
            return (threshold - score) >= material_gap
        term_coverage = max(0.0, min(1.0, float(source_alignment.get("term_coverage", 0.0) or 0.0)))
        citation_density = max(0.0, min(1.0, float(source_alignment.get("citation_density", 0.0) or 0.0)))
        grounding = max(0.0, min(1.0, float(evidence_grounding or 0.0)))
        rel = max(0.0, min(1.0, float(relevancy or 0.0)))
        quality = max(0.0, min(1.0, float(quality_score or 0.0)))
        term_risk = term_coverage < float(os.getenv("SOURCE_AUDIT_TERM_COVERAGE_RISK", "0.45"))
        citation_risk = citation_density < float(os.getenv("SOURCE_AUDIT_CITATION_DENSITY_RISK", "0.75"))
        grounding_risk = grounding < float(os.getenv("SOURCE_AUDIT_EVIDENCE_GROUNDING_RISK", "0.58"))
        if term_risk and not citation_risk and not grounding_risk:
            if (
                rel >= float(os.getenv("SOURCE_AUDIT_TERM_ONLY_REL_SAFE", "0.88"))
                and quality >= float(os.getenv("SOURCE_AUDIT_TERM_ONLY_QUALITY_SAFE", "0.80"))
                and citation_density >= float(os.getenv("SOURCE_AUDIT_TERM_ONLY_CITATION_SAFE", "0.95"))
                and grounding >= float(os.getenv("SOURCE_AUDIT_TERM_ONLY_GROUNDING_SAFE", "0.70"))
            ):
                return False
        return bool(term_risk or citation_risk or grounding_risk)

    @staticmethod
    def _source_alignment_recoverable(
        score: float,
        hard_min_score: float,
        pass_audit_min_score: float,
        epsilon: float,
    ) -> bool:
        floor = max(float(hard_min_score), float(pass_audit_min_score) - max(0.0, float(epsilon)))
        return float(score) >= floor

    def _best_real_draft_quality_ok(self, verification: Dict[str, Any], rel_threshold: float, red_threshold: float) -> bool:
        """Gate best-real-draft acceptance so low-quality drafts do not masquerade as passed."""
        if not isinstance(verification, dict):
            return False
        strict_or_audited_pass = bool(verification.get("is_passed", False)) or bool(
            verification.get("source_alignment_low_pass_audit")
            and verification.get("pre_source_alignment_audit_passed")
        )
        if not strict_or_audited_pass:
            return False
        source_check = verification.get("source_check") if isinstance(verification.get("source_check"), dict) else {}
        if not bool(source_check.get("passed", False)):
            return False
        rel = float(verification.get("relevancy_index", 0) or 0)
        red = _coerce_float(verification.get("redundancy_index"), 1.0)
        quality = float(verification.get("quality_score", 0) or 0)
        failed_dims = verification.get("quality_dimensions_failed", [])
        failed_count = len(failed_dims) if isinstance(failed_dims, list) else 0
        min_rel = min(float(rel_threshold), max(self.best_draft_min_rel, float(rel_threshold) - 0.10))
        max_red = min(1.0, float(red_threshold) + 0.06)
        return (
            rel >= min_rel
            and red <= max_red
            and quality >= self.best_draft_min_quality
            and failed_count <= self.best_draft_max_failed_dims
        )

    def _compute_effective_thresholds(self, iteration: int, rel_threshold: float, red_threshold: float) -> Tuple[float, float]:
        """
        Paper-grade runs keep thresholds fixed so a post-Controller pass is
        attributable to the repair rather than a moving acceptance gate.
        Product deployments may explicitly opt into bounded relaxation.
        """
        if os.getenv("ADAPTIVE_VERIFICATION_THRESHOLDS_ENABLED", "false").lower() != "true":
            return round(rel_threshold, 4), round(red_threshold, 4)

        if iteration <= 2:
            return round(rel_threshold, 4), round(red_threshold, 4)

        relax_steps = min(3, max(0, iteration - 2))
        relax_per_step = max(0.0, float(os.getenv("VERIFICATION_THRESHOLD_RELAX_PER_STEP", "0.02")))
        relax_cap = max(0.0, float(os.getenv("VERIFICATION_THRESHOLD_RELAX_CAP", "0.06")))
        relax_amount = min(relax_cap, relax_per_step * relax_steps)
        effective_rel = max(0.0, rel_threshold - relax_amount)
        effective_red = min(1.0, red_threshold + relax_amount)
        return round(effective_rel, 4), round(effective_red, 4)

    @staticmethod
    def _quality_dimension_keys() -> List[str]:
        return [
            "topic_alignment",
            "coverage_completeness",
            "logical_coherence",
            "evidence_grounding",
            "structure_clarity",
            "novelty",
        ]

    def _init_document_quality_summary(self) -> Dict[str, Any]:
        return {
            "quality_score_sum": 0.0,
            "quality_score_count": 0,
            "quality_overall_uncertainty_sum": 0.0,
            "quality_overall_uncertainty_count": 0,
            "quality_dimension_sums": {key: 0.0 for key in self._quality_dimension_keys()},
            "quality_dimension_counts": {key: 0 for key in self._quality_dimension_keys()},
            "quality_weights": {},
            "unieval_available_subsections": 0,
            "unieval_fallback_subsections": 0,
        }

    def _init_document_bandit_summary(self) -> Dict[str, Any]:
        return {
            "bandit_selected_arm_counts": {
                "llm": 0,
                "rule": 0,
                "rule_structured": 0,
                "defect_topic": 0,
                "defect_evidence": 0,
                "defect_novelty": 0,
                "defect_structure": 0,
                "novelty_repair": 0,
                "claim_evidence_repair": 0,
                "citation_grounding_repair": 0,
                "reviewer_risk_repair": 0,
                "external_metric_repair": 0,
                "structure_readability_repair": 0,
                "reproducibility_repair": 0,
            },
            "bandit_reward_sum": 0.0,
            "bandit_reward_count": 0,
            "bandit_reward_avg": 0.0,
            "bandit_drift_events": 0,
            "bandit_drift_triggered_subsections": 0,
            "bandit_last_selected_arm": "",
            "bandit_last_selection_mode": "",
            "bandit_last_constraints": {},
            "bandit_recent_events": [],
            "chapter_assets": [],
        }

    def _accumulate_quality_summary(self, summary: Dict[str, Any], verification: Dict[str, Any]) -> None:
        if not isinstance(summary, dict) or not isinstance(verification, dict):
            return
        quality_score = verification.get("quality_score")
        if isinstance(quality_score, (int, float)):
            summary["quality_score_sum"] += float(quality_score)
            summary["quality_score_count"] += 1

        overall_unc = verification.get("quality_overall_uncertainty")
        if isinstance(overall_unc, (int, float)):
            summary["quality_overall_uncertainty_sum"] += float(overall_unc)
            summary["quality_overall_uncertainty_count"] += 1

        weights = verification.get("quality_weights")
        if isinstance(weights, dict) and weights:
            summary["quality_weights"] = dict(weights)

        dimensions = verification.get("quality_dimensions")
        if isinstance(dimensions, dict):
            for key in self._quality_dimension_keys():
                value = dimensions.get(key)
                if isinstance(value, (int, float)):
                    summary["quality_dimension_sums"][key] = summary["quality_dimension_sums"].get(key, 0.0) + float(value)
                    summary["quality_dimension_counts"][key] = summary["quality_dimension_counts"].get(key, 0) + 1

        if bool(verification.get("unieval_available", False)):
            summary["unieval_available_subsections"] += 1
        elif isinstance(dimensions, dict) and dimensions:
            summary["unieval_fallback_subsections"] += 1

    def _accumulate_bandit_summary(self, summary: Dict[str, Any], subsection_result: Dict[str, Any]) -> None:
        if not isinstance(summary, dict) or not isinstance(subsection_result, dict):
            return
        bandit = subsection_result.get("bandit") if isinstance(subsection_result.get("bandit"), dict) else {}
        if not bandit:
            return

        events = bandit.get("events") if isinstance(bandit.get("events"), list) else []
        if events:
            for event in events:
                if not isinstance(event, dict):
                    continue
                event_result = dict(subsection_result)
                event_result["bandit"] = event
                self._accumulate_bandit_summary(summary, event_result)
            return

        selected_arm = str(bandit.get("selected_arm") or subsection_result.get("controller_source") or "").strip()
        if selected_arm in summary.get("bandit_selected_arm_counts", {}):
            summary["bandit_selected_arm_counts"][selected_arm] += 1
        summary["bandit_last_selected_arm"] = selected_arm or summary.get("bandit_last_selected_arm", "")
        summary["bandit_last_selection_mode"] = str(bandit.get("selection", {}).get("mode", "") or summary.get("bandit_last_selection_mode", ""))

        reward = bandit.get("realized_reward", bandit.get("reward"))
        if isinstance(reward, (int, float)):
            reward_value = float(reward)
            summary["bandit_reward_sum"] += reward_value
            summary["bandit_reward_count"] += 1
            if summary["bandit_reward_count"] > 0:
                summary["bandit_reward_avg"] = summary["bandit_reward_sum"] / summary["bandit_reward_count"]
            if selected_arm:
                recent = summary.setdefault("bandit_recent_events", [])
                if isinstance(recent, list):
                    recent.append({
                        "arm": selected_arm,
                        "reward": reward_value,
                        "selection_mode": summary.get("bandit_last_selection_mode", ""),
                    })
                    del recent[:-80]

        drift = bandit.get("drift") if isinstance(bandit.get("drift"), dict) else {}
        if drift:
            drift_events = int(drift.get("drift_events", 0) or 0)
            summary["bandit_drift_events"] = max(summary.get("bandit_drift_events", 0), drift_events)
            if drift.get("triggered"):
                summary["bandit_drift_triggered_subsections"] += 1

        constraints = bandit.get("constraints") if isinstance(bandit.get("constraints"), dict) else {}
        if constraints:
            summary["bandit_last_constraints"] = dict(constraints)
        # constraints handled above

    def _accumulate_subsection_telemetry(
        self,
        summary: Dict[str, Any],
        subsection_result: Dict[str, Any],
    ) -> None:
        """Accumulate real calls for both passed and failed subsections."""
        if not isinstance(summary, dict) or not isinstance(subsection_result, dict):
            return

        verification = subsection_result.get("verification")
        if not isinstance(verification, dict):
            verification = {}
        metrics = subsection_result.get("metrics")
        if not isinstance(metrics, dict):
            metrics = {}

        self._accumulate_quality_summary(summary, verification)
        self._accumulate_bandit_summary(summary, subsection_result)

        if bool(subsection_result.get("rag_used", False)):
            summary["rag_used_subsections"] += 1
        if bool(subsection_result.get("rag_search_success", False)):
            summary["rag_search_success_subsections"] += 1
        summary["rag_vector_indexed_total"] = int(summary.get("rag_vector_indexed_total", 0) or 0) + int(
            subsection_result.get("rag_vector_indexed", 0) or 0
        )
        if subsection_result.get("rag_vector_backend"):
            summary["rag_vector_backend"] = str(subsection_result.get("rag_vector_backend") or "")
        if subsection_result.get("rag_reranker"):
            summary["rag_reranker_used_subsections"] = int(
                summary.get("rag_reranker_used_subsections", 0) or 0
            ) + 1
        if bool(subsection_result.get("controller_effective", False)):
            summary["controller_effective_subsections"] += 1
        if bool(subsection_result.get("controller_triggered", False)):
            summary["controller_triggered_subsections"] += 1

        metric_targets = {
            "verifier_failed": "verifier_failed_total",
            "verifier_error": "verifier_error_total",
            "controller_calls": "controller_calls_total",
            "controller_success": "controller_success_total",
            "controller_error": "controller_error_total",
            "controller_unavailable": "controller_unavailable_total",
            "controller_ineffective": "controller_ineffective_total",
            "controller_fallback_outline": "controller_fallback_outline_total",
            "controller_exhausted": "controller_exhausted_total",
            "generator_short_draft": "generator_short_draft_total",
            "source_alignment_selector_used": "source_alignment_selector_used_subsections",
            "source_alignment_low_pass_audits": "source_alignment_low_pass_audits",
        }
        for metric_key, target_key in metric_targets.items():
            summary[target_key] += int(metrics.get(metric_key, 0) or 0)

        source_alignment = subsection_result.get("source_alignment")
        if not isinstance(source_alignment, dict):
            source_alignment = verification.get("source_alignment")
        if isinstance(source_alignment, dict) and source_alignment.get("source_count", 0):
            summary["source_alignment_score_sum"] += float(source_alignment.get("score", 0.0) or 0.0)
            summary["source_alignment_score_count"] += 1

        for token_key in (
            "prompt_tokens",
            "output_tokens",
            "total_tokens",
            "prompt_cache_hit_tokens",
            "prompt_cache_miss_tokens",
        ):
            summary["token_usage"][token_key] += int(metrics.get(token_key, 0) or 0)

    def _resolve_bandit_events_path(self) -> str:
        raw = os.getenv("CONTROLLER_BANDIT_EVENTS_PATH", "controller_bandit_events.jsonl").strip()
        if os.path.isabs(raw):
            return raw
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(project_root, raw)

    def _load_recent_bandit_stats(self, max_events: int = 200) -> Dict[str, Any]:
        """从文件 `controller_bandit_events.jsonl` 读取最近若干条 bandit 事件并聚合为统计信息。
        返回字典包含与前端契合的字段（counts, sum, count, avg, last_arm, drift_events）。
        """
        stats = {
            "bandit_selected_arm_counts": {},
            "bandit_reward_sum": 0.0,
            "bandit_reward_count": 0,
            "bandit_reward_avg": 0.0,
            "bandit_last_selected_arm": "",
            "bandit_last_selection_mode": "",
            "bandit_drift_events": 0,
            "bandit_recent_events": [],
        }
        try:
            events_path = self._resolve_bandit_events_path()
            if not os.path.exists(events_path):
                return stats
            with open(events_path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
            if not lines:
                return stats
            recent = lines[-max_events:]
            rewards: List[float] = []
            for line in recent:
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                arm = str(ev.get("chosen_arm") or "")
                reward = float(ev.get("reward", 0.0) or 0.0)
                if arm:
                    stats["bandit_selected_arm_counts"][arm] = stats["bandit_selected_arm_counts"].get(arm, 0) + 1
                    stats["bandit_last_selected_arm"] = arm
                    stats["bandit_recent_events"].append({
                        "arm": arm,
                        "reward": reward,
                        "selection_mode": str(ev.get("selection_mode") or ev.get("mode") or ""),
                    })
                stats["bandit_reward_sum"] += reward
                rewards.append(reward)
                if reward != 0.0:
                    stats["bandit_reward_count"] += 1
                if ev.get("drift") is not None:
                    stats["bandit_drift_events"] += 1
            if stats["bandit_reward_count"] > 0:
                stats["bandit_reward_avg"] = stats["bandit_reward_sum"] / stats["bandit_reward_count"]

            # Compute plot bounds for frontend visualization (with padding)
            if rewards:
                rmin = min(rewards)
                rmax = max(rewards)
                if abs(rmax - rmin) < 1e-8:
                    pad = max(0.01, abs(rmax) * 0.05)
                    rmin_plot = rmin - pad
                    rmax_plot = rmax + pad
                else:
                    span = rmax - rmin
                    rmin_plot = rmin - 0.12 * span
                    rmax_plot = rmax + 0.12 * span
                stats["plot_y_min"] = float(round(rmin_plot, 6))
                stats["plot_y_max"] = float(round(rmax_plot, 6))
            else:
                stats["plot_y_min"] = 0.0
                stats["plot_y_max"] = 0.1

            return stats
        except Exception:
            return stats

    def _read_last_bandit_event(self) -> Dict[str, Any]:
        """返回 controller_bandit_events.jsonl 中最后一条事件的原始解析结果（或空字典）。"""
        try:
            events_path = self._resolve_bandit_events_path()
            if not os.path.exists(events_path):
                return {}
            with open(events_path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
            if not lines:
                return {}
            for line in reversed(lines):
                try:
                    ev = json.loads(line)
                    if isinstance(ev, dict):
                        return ev
                except Exception:
                    continue
            return {}
        except Exception:
            return {}

    @staticmethod
    def _can_use_last_bandit_event(controller_result: Dict[str, Any]) -> bool:
        """Use file-backed bandit fallback only for this round's successful controller call."""
        return bool(isinstance(controller_result, dict) and controller_result.get("success"))

    @staticmethod
    def _verification_utility(verification: Dict[str, Any]) -> float:
        dimensions = verification.get("quality_dimensions")
        if not isinstance(dimensions, dict):
            dimensions = {}
        dim_values = [
            max(0.0, min(1.0, float(value or 0.0)))
            for value in dimensions.values()
            if isinstance(value, (int, float))
        ]
        dim_mean = sum(dim_values) / max(1, len(dim_values))
        source_check = verification.get("source_check")
        source_passed = bool(source_check.get("passed", False)) if isinstance(source_check, dict) else False
        utility = (
            0.22 * max(0.0, min(1.0, float(verification.get("relevancy_index", 0.0) or 0.0)))
            + 0.12 * (1.0 - max(0.0, min(1.0, _coerce_float(verification.get("redundancy_index"), 1.0))))
            + 0.22 * max(0.0, min(1.0, float(verification.get("quality_score", 0.0) or 0.0)))
            + 0.24 * dim_mean
            + 0.12 * (1.0 if source_passed else 0.0)
            + 0.08 * (1.0 if verification.get("is_passed", False) else 0.0)
        )
        return max(0.0, min(1.0, utility))

    @staticmethod
    def _strategy_only_repair_allowed(
        selected_arm: str,
        defect_graph: Dict[str, Any],
    ) -> bool:
        """Allow a matching hard-gate strategy even when the outline is unchanged."""
        arm = str(selected_arm or "").strip()
        graph = defect_graph if isinstance(defect_graph, dict) else {}
        return bool(
            (
                arm == "defect_topic"
                and float(graph.get("hard_relevance_gate", 0.0) or 0.0) > 0.0
            )
            or (
                arm == "defect_novelty"
                and float(graph.get("hard_redundancy_gate", 0.0) or 0.0) > 0.0
            )
        )

    def _record_realized_controller_outcome(
        self,
        *,
        event: Dict[str, Any],
        before: Dict[str, Any],
        after: Dict[str, Any],
        document_id: str,
        section_id: str,
        subsection_id: str,
    ) -> Dict[str, Any]:
        payload = {
            "selected_arm": str(event.get("selected_arm") or ""),
            "feature_vector": event.get("feature_vector", []),
            "before_verification": before,
            "after_verification": after,
            "predicted_exploit": float(event.get("predicted_exploit", 0.0) or 0.0),
            "proposal_reward": float(event.get("proposal_reward", event.get("reward", 0.0)) or 0.0),
            "application_accepted": bool(event.get("application_accepted", False)),
            "document_id": document_id,
            "section_id": section_id,
            "subsection_id": subsection_id,
        }
        try:
            response = self.session.post(
                f"{self.controller_url}/bandit-outcome",
                json=payload,
                timeout=max(10, int(os.getenv("CONTROLLER_OUTCOME_HTTP_TIMEOUT", "20"))),
            )
            if response.status_code == 200:
                controller_outcome = response.json()
                if isinstance(controller_outcome, dict) and controller_outcome.get("success"):
                    return controller_outcome
        except Exception:
            pass

        before_utility = self._verification_utility(before)
        after_utility = self._verification_utility(after)
        delta = after_utility - before_utility

        before_dims = before.get("quality_dimensions") if isinstance(before.get("quality_dimensions"), dict) else {}
        after_dims = after.get("quality_dimensions") if isinstance(after.get("quality_dimensions"), dict) else {}
        dimension_regression_tolerance = float(os.getenv("CONTROLLER_OUTCOME_DIMENSION_NO_HARM_TOLERANCE", "0.025"))
        dimension_regressions = {
            key: round(float(after_dims.get(key, 0.0) or 0.0) - float(value or 0.0), 4)
            for key, value in before_dims.items()
            if key in after_dims and float(after_dims.get(key, 0.0) or 0.0) < float(value or 0.0) - dimension_regression_tolerance
        }
        rel_regression = float(after.get("relevancy_index", 0.0) or 0.0) < float(before.get("relevancy_index", 0.0) or 0.0) - 0.015
        quality_regression = float(after.get("quality_score", 0.0) or 0.0) < float(before.get("quality_score", 0.0) or 0.0) - 0.02
        before_source = before.get("source_check") if isinstance(before.get("source_check"), dict) else {}
        after_source = after.get("source_check") if isinstance(after.get("source_check"), dict) else {}
        source_regression = bool(before_source.get("passed", False)) and not bool(after_source.get("passed", False))
        redundancy_regression = _coerce_float(after.get("redundancy_index"), 1.0) > _coerce_float(before.get("redundancy_index"), 1.0) + 0.015
        before_alignment = before.get("source_alignment") if isinstance(before.get("source_alignment"), dict) else {}
        after_alignment = after.get("source_alignment") if isinstance(after.get("source_alignment"), dict) else {}
        source_alignment_regression = bool(
            before_alignment.get("source_count", 0)
            and after_alignment.get("source_count", 0)
            and float(after_alignment.get("score", 0.0) or 0.0) < float(before_alignment.get("score", 0.0) or 0.0) - 0.02
        )
        regressed = bool(
            dimension_regressions
            or rel_regression
            or quality_regression
            or source_regression
            or redundancy_regression
            or source_alignment_regression
        )
        arm_targets = {
            "defect_evidence": ("evidence_grounding",),
            "defect_novelty": ("novelty",),
            "defect_topic": ("topic_alignment",),
            "defect_structure": ("structure_clarity", "logical_coherence"),
            "rule_structured": ("structure_clarity", "logical_coherence"),
            "novelty_repair": ("novelty",),
            "claim_evidence_repair": ("evidence_grounding",),
            "citation_grounding_repair": ("evidence_grounding",),
            "reviewer_risk_repair": ("evidence_grounding", "logical_coherence", "novelty"),
            "external_metric_repair": ("topic_alignment", "evidence_grounding", "novelty"),
            "structure_readability_repair": ("structure_clarity", "logical_coherence"),
            "reproducibility_repair": ("evidence_grounding",),
        }
        selected_arm = str(event.get("selected_arm") or "")
        target_gains = [
            float(after_dims.get(name, 0.0) or 0.0) - float(before_dims.get(name, 0.0) or 0.0)
            for name in arm_targets.get(selected_arm, tuple(before_dims))
            if name in before_dims and name in after_dims
        ]
        if selected_arm in {"defect_novelty", "novelty_repair"}:
            target_gains.append(
                _coerce_float(before.get("redundancy_index"), 1.0)
                - _coerce_float(after.get("redundancy_index"), 1.0)
            )
        if selected_arm == "defect_topic":
            target_gains.append(
                float(after.get("relevancy_index", 0.0) or 0.0)
                - float(before.get("relevancy_index", 0.0) or 0.0)
            )
        targeted_gain = max(target_gains, default=delta)
        targeted_effective = targeted_gain >= float(os.getenv("CONTROLLER_OUTCOME_MIN_TARGET_GAIN", "0.01"))
        pass_transition = bool(after.get("is_passed", False)) and not bool(before.get("is_passed", False))
        effective = bool(event.get("application_accepted", False)) and not regressed and targeted_effective and delta >= -0.002
        realized_reward = 0.0 if (regressed or not targeted_effective) else max(
            0.0,
            min(1.0, max(0.0, delta) * 3.0 + min(0.25, targeted_gain) + (0.15 if pass_transition else 0.0)),
        )

        outcome = {
            "event_type": "realized_controller_outcome",
            "timestamp": time.time(),
            "document_id": document_id,
            "section_id": section_id,
            "subsection_id": subsection_id,
            "chosen_arm": str(event.get("selected_arm") or ""),
            "feature_vector": event.get("feature_vector", []),
            "proposal_reward": float(event.get("proposal_reward", event.get("reward", 0.0)) or 0.0),
            "reward": round(realized_reward, 6),
            "effective": effective,
            "application_accepted": bool(event.get("application_accepted", False)),
            "before_utility": round(before_utility, 6),
            "after_utility": round(after_utility, 6),
            "utility_delta": round(delta, 6),
            "pass_transition": pass_transition,
            "regressed": regressed,
            "dimension_regressions": dimension_regressions,
            "redundancy_regression": redundancy_regression,
            "source_alignment_regression": source_alignment_regression,
            "targeted_gain": round(targeted_gain, 6),
            "targeted_effective": targeted_effective,
            "relevancy_before": float(before.get("relevancy_index", 0.0) or 0.0),
            "relevancy_after": float(after.get("relevancy_index", 0.0) or 0.0),
            "quality_before": float(before.get("quality_score", 0.0) or 0.0),
            "quality_after": float(after.get("quality_score", 0.0) or 0.0),
        }
        try:
            with open(self._resolve_bandit_events_path(), "a", encoding="utf-8") as handle:
                handle.write(json.dumps(outcome, ensure_ascii=False) + "\n")
        except Exception as exc:
            outcome["write_error"] = str(exc)[:180]
        return outcome

    @staticmethod
    def _extract_json_object(text: str) -> Dict[str, Any]:
        raw = str(text or "").strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            pass

        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            try:
                parsed = json.loads(raw[start:end + 1])
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                return {}
        return {}

    @staticmethod
    def _normalize_markdown_table(table_markdown: str) -> str:
        lines = [line.strip() for line in str(table_markdown or "").splitlines() if line.strip()]
        table_lines = [line for line in lines if line.startswith("|") and line.endswith("|")]
        if len(table_lines) < 2:
            return ""
        if not re.match(r"^\|\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?$", table_lines[1]):
            cells = [cell.strip() for cell in table_lines[0].strip("|").split("|")]
            separator = "|" + "|".join([" --- " for _ in cells]) + "|"
            table_lines.insert(1, separator)
        return "\n".join(table_lines[:12]).strip()

    def _build_section_text_for_assets(self, section_result: Dict[str, Any]) -> str:
        parts: List[str] = []
        for subsection in section_result.get("subsections", []) or []:
            if not isinstance(subsection, dict) or not subsection.get("success"):
                continue
            title = str(subsection.get("subsection_title") or "").strip()
            content = str(subsection.get("content") or "").strip()
            if not content:
                continue
            if title:
                parts.append(f"### {title}")
            parts.append(content)
        return "\n\n".join(parts).strip()

    def _plan_chapter_assets(
        self,
        *,
        document_id: str,
        section_id: str,
        section_title: str,
        section_result: Dict[str, Any],
        document_title: str,
        user_background: str,
        user_requirements: str,
    ) -> List[Dict[str, Any]]:
        if not self.chapter_assets_enabled:
            return []
        if not section_result.get("subsections"):
            return []
        if not all(bool(item.get("success")) for item in section_result.get("subsections", []) if isinstance(item, dict)):
            return []

        chapter_text = self._build_section_text_for_assets(section_result)
        if len(chapter_text) < 600:
            return []

        subsection_titles = [
            str(item.get("subsection_title") or item.get("subsection_id") or "").strip()
            for item in section_result.get("subsections", [])
            if isinstance(item, dict)
        ]
        subsection_ids = [
            str(item.get("subsection_id") or "").strip()
            for item in section_result.get("subsections", [])
            if isinstance(item, dict)
        ]
        prompt = f"""
You are FlowerNet's chapter-level asset planner.

The following chapter has fully passed text verification. Decide whether the chapter needs one useful in-document table. Use only information grounded in the passed chapter text. Do not invent external facts.

Document title: {document_title}
Chapter id: {section_id}
Chapter title: {section_title}
Reader background: {user_background}
Extra requirements: {user_requirements}
Available insertion anchors by subsection id: {", ".join(subsection_ids)}
Subsection titles: {", ".join(subsection_titles)}

Passed chapter text:
{chapter_text[:9000]}

Return strict JSON only:
{{
  "assets": [
    {{
      "type": "table",
      "title": "specific table title",
      "caption": "one sentence explaining why this table belongs here",
      "insert_after_subsection_id": "one of the available subsection ids",
      "markdown": "| Column A | Column B |\\n| --- | --- |\\n| ... | ... |"
    }}
  ]
}}

Rules:
- Prefer one table when the chapter compares concepts, mechanisms, stages, variables, methods, or evidence.
- Do not return image prompts, figure prompts, diagrams, chart specifications, or non-table assets.
- Return an empty assets array if a table would not add real value.
- Maximum 1 asset total.
- Tables must be concise, 3-6 columns and 3-8 rows.
- Never include placeholder values, fake citations, or template text.
""".strip()

        try:
            self._emit_progress_event(
                document_id=document_id,
                section_id=section_id,
                stage="chapter_asset_start",
                message=f"章节已通过，开始规划图表资产: {section_title}",
                metadata={"section_title": section_title},
            )
            result = self._call_generator(prompt, max_tokens=self.chapter_assets_max_tokens)
            if not isinstance(result, dict) or not result.get("success"):
                return []
            parsed = self._extract_json_object(str(result.get("draft") or result.get("content") or ""))
            raw_assets = parsed.get("assets") if isinstance(parsed, dict) else []
            if not isinstance(raw_assets, list):
                return []

            allowed_anchor_ids = {sid for sid in subsection_ids if sid}
            default_anchor = subsection_ids[-1] if subsection_ids else ""
            normalized: List[Dict[str, Any]] = []
            for idx, asset in enumerate(raw_assets[:1], 1):
                if not isinstance(asset, dict):
                    continue
                asset_type = str(asset.get("type") or "").strip().lower()
                if asset_type != "table":
                    continue
                anchor = str(asset.get("insert_after_subsection_id") or "").strip()
                if anchor not in allowed_anchor_ids:
                    anchor = default_anchor
                title = str(asset.get("title") or "").strip()[:160]
                caption = str(asset.get("caption") or "").strip()[:500]
                if not title or not caption:
                    continue
                item: Dict[str, Any] = {
                    "asset_id": f"{section_id}_asset_{idx}",
                    "type": asset_type,
                    "title": title,
                    "caption": caption,
                    "insert_after_subsection_id": anchor,
                    "section_id": section_id,
                    "section_title": section_title,
                }
                table = self._normalize_markdown_table(str(asset.get("markdown") or ""))
                if not table:
                    continue
                item["markdown"] = table
                normalized.append(item)

            if normalized:
                self._emit_progress_event(
                    document_id=document_id,
                    section_id=section_id,
                    stage="chapter_asset_ready",
                    message=f"章节图表资产规划完成: {section_title}",
                    metadata={"section_title": section_title, "asset_count": len(normalized), "assets": normalized},
                )
            return normalized
        except Exception as exc:
            self._emit_progress_event(
                document_id=document_id,
                section_id=section_id,
                stage="chapter_asset_failed",
                message=f"章节图表资产规划失败: {section_title}",
                metadata={"section_title": section_title, "error": str(exc)[:240]},
            )
            return []

    def _is_outline_like(self, content: str, outline: str = "") -> bool:
        """简单判断 content 是否更像大纲/提示而非正文，供兜底选择时排除大纲型草稿。

        规则：空内容 / 以系统兜底前缀开头 / 包含明显的提示标记 / 与 outline 文本高度相似或互为子串
        """
        text = str(content or "").strip()
        if not text:
            return False
        outline_text = str(outline or "").strip()
        compact_text = " ".join(text.split())
        compact_outline = " ".join(outline_text.split())

        # 系统兜底前缀或兜底标记
        if text.startswith("（系统兜底）") or "（兜底内容）" in text:
            return True

        # 明显的提示/模板痕迹
        prompt_markers = [
            "请你作为",
            "要求：",
            "段落主题",
            "系统指示",
            "content_prompt",
            "subsection_id",
            "subsection_title",
        ]
        if any(m in text for m in prompt_markers):
            return True

        # 如果 outline 非空且两者互为子串或包含关系，认为可能是大纲。
        # Long real drafts often repeat the subsection title/outline in the
        # opening paragraph; do not reject them just because they contain the
        # outline text.
        if compact_outline and len(compact_outline) >= 12:
            if len(compact_text) > max(800, len(compact_outline) * 3):
                return False
            if compact_text == compact_outline or compact_text in compact_outline:
                return True
            if compact_outline in compact_text and len(compact_text) <= len(compact_outline) + 120:
                return True

        # 否则认为不是大纲型内容
        return False

    def set_local_generator(self, generator):
        """设置本地Generator实例，避免HTTP自调用"""
        self._local_generator = generator
        print("✅ Orchestrator已绑定本地Generator实例")

    def _resolve_model_path(self, raw: str, default_name: str) -> str:
        if resolve_model_path is not None:
            return resolve_model_path(raw, default_name)
        value = (raw or "").strip() or os.path.join("models", default_name)
        if os.path.isabs(value):
            return value
        return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), value)

    def _load_reward_model(self) -> Optional[Dict[str, Any]]:
        if not self.reward_model_enabled or load_json_model is None:
            return None
        try:
            mtime = os.path.getmtime(self.reward_model_path)
        except Exception:
            return None
        if (
            str(self._reward_model_cache.get("path") or "") == self.reward_model_path
            and abs(float(self._reward_model_cache.get("mtime") or 0.0) - mtime) < 1e-9
        ):
            cached = self._reward_model_cache.get("model")
            return cached if isinstance(cached, dict) else None
        model = load_json_model(self.reward_model_path, "reward_model")
        self._reward_model_cache.update({"path": self.reward_model_path, "mtime": mtime, "model": model})
        if model:
            print(f"✅ Loaded FlowerNet reward model: {self.reward_model_path}")
        return model

    def _predict_reward_score(self, verification: Dict[str, Any], iteration: int) -> Dict[str, Any]:
        if predict_reward_model is None:
            return {"used": False, "score": 0.0, "features": []}
        return predict_reward_model(self._load_reward_model(), verification, iteration=iteration)

    def _emit_progress_event(
        self,
        document_id: str,
        stage: str,
        message: str,
        section_id: Optional[str] = None,
        subsection_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        """写入流程事件（用于前端可视化详细过程）。"""
        if not self.history_manager:
            return
        try:
            event_metadata = dict(metadata or {})
            if section_id and not event_metadata.get("section_id"):
                event_metadata["section_id"] = section_id
            if subsection_id and not event_metadata.get("subsection_id"):
                event_metadata["subsection_id"] = subsection_id
            self.history_manager.add_progress_event(
                document_id=document_id,
                section_id=section_id,
                subsection_id=subsection_id,
                stage=stage,
                message=message,
                metadata=event_metadata,
            )
        except Exception as e:
            print(f"⚠️  写入流程事件失败: {e}")

    def _resolve_subsection_outline(
        self,
        document_id: str,
        section_id: str,
        subsection_id: str,
        fallback_outline: str,
    ) -> str:
        """优先从数据库读取 subsection 大纲，确保生成逻辑以已存储大纲为准。"""
        if not self.history_manager:
            return fallback_outline

        try:
            tracking = self.history_manager.get_subsection_tracking(document_id, section_id, subsection_id)
            if tracking and tracking.get("outline"):
                return str(tracking["outline"]).strip()
        except Exception as e:
            print(f"⚠️  读取 subsection tracking 失败: {e}")

        try:
            outline = self.history_manager.get_outline(
                document_id=document_id,
                outline_type="subsection",
                section_id=section_id,
                subsection_id=subsection_id,
            )
            if outline:
                return str(outline).strip()
        except Exception as e:
            print(f"⚠️  读取 subsection outline 失败: {e}")

        return fallback_outline

    def _load_passed_history(self, document_id: str) -> List[Dict[str, str]]:
        """每次进入新 subsection 前从数据库重新拉取已通过历史。"""
        if not self.history_manager:
            return []
        try:
            history = self.history_manager.get_passed_history(document_id)
            if isinstance(history, list):
                return history
        except Exception as e:
            print(f"⚠️  读取 passed history 失败: {e}")
        return []

    def _extract_topic_context(self, outline: str, prompt: str) -> str:
        """
        【优化1.0 - Domain Anchoring】
        从小节大纲和提示中提取核心领域关键词（topic_context）
        用于强制锁定RAG搜索的领域范围，避免跨学科幻觉
        
        规则：
        1. 提取标题中的主要名词（保留领域特定词汇）
        2. 过滤通用停词（避免"请、写作、内容"等污染）
        3. 提取提示中的动作+对象组合
        4. 返回最相关的3-5个核心词
        """
        outline_text = " ".join(str(outline or "").split()).strip()
        prompt_text = " ".join(str(prompt or "").split()).strip()
        combined = (outline_text + " " + prompt_text)[:400]
        
        # 提取候选词汇
        tokens = re.findall(r"[A-Za-z\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff\-_]{1,30}", combined)
        
        # 更严格的停词集合（针对生成/大纲特定词汇）
        generic_stop_tokens = {
            # 通用指令词
            "section", "subsection", "outline", "prompt", "chapter", "part",
            "write", "writing", "draft", "content", "article", "essay",
            "要求", "生成", "内容", "小节", "章节", "大纲", "写作", "草稿",
            # 连接词
            "的", "和", "与", "或", "的", "在", "是", "有", "了", "将",
            "please", "write", "about", "regarding", "concerning",
            # 语言学术词
            "includes", "includes", "describe", "discuss", "explain", "detail"
        }
        
        domain_terms: List[str] = []
        for token in tokens:
            normalized = token.strip().lower()
            
            # 过滤规则
            if not normalized or len(normalized) <= 1 or normalized.isdigit():
                continue
            if normalized in generic_stop_tokens or len(normalized) > 30:
                continue
            if re.fullmatch(r"[\d\.\-:]+", normalized):
                continue
            if normalized not in domain_terms:
                domain_terms.append(normalized)
        
        # 返回最相关的领域词
        result = " ".join(domain_terms[:5])[:80]
        return result

    @staticmethod
    def _extract_rag_focus(outline: str, initial_prompt: str = "") -> str:
        raw = " ".join(str(outline or "").split()).strip()
        focus_parts: List[str] = []
        focus_match = re.search(r"(?:聚焦|focus(?:es)? on)\s*[\"“]([^\"”]{3,180})", raw, re.IGNORECASE)
        if focus_match:
            focus_parts.append(focus_match.group(1).strip())
        description_match = re.search(
            r"(?:基础说明|description)\s*[:：]\s*(.{20,520}?)(?=\s*(?:避免重复|同时遵守|自审计|$))",
            raw,
            re.IGNORECASE,
        )
        if description_match:
            focus_parts.append(description_match.group(1).strip())
        if not focus_parts:
            english_chunks = re.findall(r"[A-Za-z][A-Za-z0-9 ,.;:()'\-/]{12,260}", raw)
            if english_chunks:
                focus_parts.append(max(english_chunks, key=len).strip())
        if not focus_parts:
            prompt = " ".join(str(initial_prompt or "").split()).strip()
            focus_parts.append(prompt[:260] or raw[:260])
        return " ".join(dict.fromkeys(part for part in focus_parts if part))[:620]

    @staticmethod
    def _extract_rag_gate_focus(focus: str) -> str:
        concise = re.split(
            r"\b(?:this subsection|this section)\b|(?:本小节|本章节|需要覆盖)",
            str(focus or ""),
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip(" .,:;，。；：")
        return concise[:180] or str(focus or "")[:180]

    def _build_rag_query_candidates(
        self,
        outline: str,
        initial_prompt: str,
        document_title: str = "",
    ) -> List[str]:
        outline_text = " ".join(str(outline or "").split()).strip()
        prompt_text = " ".join(str(initial_prompt or "").split()).strip()
        document_text = " ".join(str(document_title or "").split()).strip()
        canonical_requirements = self._canonical_english_requirements(prompt_text)
        focus_text = self._extract_rag_focus(outline_text, prompt_text)

        domain_hint_terms: List[str] = []
        domain_probe = f"{document_text} {focus_text} {prompt_text} {canonical_requirements}".lower()
        # Disambiguate the word "learning": technical learning paradigms must
        # not fall into the education profile merely because they contain it.
        if re.search(
            r"\b(?:federated|machine|deep|reinforcement|self-supervised|representation|transfer)\s+learning\b",
            domain_probe,
        ):
            domain_hint_terms.extend(["machine learning", "artificial intelligence"])
        if re.search(r"\b(?:healthcare|health care|biomedical|clinical|patient)\b", domain_probe):
            domain_hint_terms.extend(["medical", "clinical"])
        if re.search(r"\b(?:vectorstore|reranker|natural language inference|\bnli\b|neural network|transformer)\b", domain_probe):
            domain_hint_terms.extend(["machine learning", "artificial intelligence"])
        if re.search(r"\b(?:multimodal|multi-modal|cross-modal|vision-language)\b", domain_probe) or "多模态" in domain_probe:
            domain_hint_terms.extend(["multimodal", "cross-modal", "vision language"])
            # Multimodal integration topics are under-specified when they only
            # say "fusion" or "integration"; retrieval then drifts to generic
            # image-fusion metrics. Add modality anchors so sources must cover
            # the broader text/image/audio/video problem family.
            domain_hint_terms.extend(["text", "image", "audio", "video"])
            if re.search(r"\b(?:product|products|foundation|generative|large language|llm|benchmark)\b", domain_probe):
                domain_hint_terms.extend(["multimodal large language models", "generative AI"])
        # Quantization, inference cost, and licensing also occur in ordinary
        # deployment-limitations sections. They must not switch the entire
        # query into the open-source ecosystem domain without an explicit
        # open-source/open-weight entity anchor.
        if re.search(
            r"\b(?:open[- ]source|open[- ]weight|open models?)\b",
            domain_probe,
        ):
            domain_hint_terms.extend([
                "open-source large language models",
                "open source software ecosystem",
                "open-source software projects",
                "open-weight models",
                "licensing",
                "deployment",
                "inference cost",
                "quantization",
                "release process",
                "version management",
                "source code",
                "software development",
                "bug tracking",
                "community governance",
                "contributors",
                "users",
                "enterprise adoption",
            ])
        if (
            re.search(r"\b(?:large language models?|language models?|\bllms?\b|foundation models?)\b", domain_probe)
            or "大语言模型" in domain_probe
        ):
            domain_hint_terms.extend(["large language models", "LLM", "foundation models"])
        modality_hints = []
        modality_map = {
            "文本": "text",
            "图像": "image",
            "语音": "speech",
            "视频": "video",
        }
        for zh, en in modality_map.items():
            if zh in domain_probe or re.search(rf"\b{re.escape(en)}\b", domain_probe):
                modality_hints.append(en)
        if len(modality_hints) >= 2:
            domain_hint_terms.extend(modality_hints)
        domain_hints = " ".join(dict.fromkeys(domain_hint_terms))

        # The document title is a global semantic anchor. Without it, narrow
        # subsection labels can retrieve authoritative but off-topic sources.
        merged = (document_text + " " + canonical_requirements + " " + domain_hints + " " + focus_text).strip()[:420]
        canonical_search_query = self._canonical_english_search_query(prompt_text)
        translated_query = canonical_requirements or self._english_rag_query_bridge(f"{prompt_text} {merged}".strip())
        core_domain_query = f"{canonical_requirements} {domain_hints} {focus_text}".strip()[:260]

        title_like = ""
        outline_lines = [line.strip("-•* 1234567890.\t") for line in str(focus_text).splitlines() if line.strip()]
        if outline_lines:
            title_like = outline_lines[0][:140]

        semantic_tokens = re.findall(r"[A-Za-z\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff\-_]{1,24}", merged)
        stop_tokens = {
            "section", "subsection", "outline", "prompt", "chapter", "write", "writing",
            "要求", "生成", "内容", "小节", "章节", "大纲", "写作", "包括", "以及", "关于"
        }
        semantic_terms: List[str] = []
        for token in semantic_tokens:
            normalized = token.strip().lower()
            if not normalized or normalized in stop_tokens or normalized.isdigit() or len(normalized) <= 1:
                continue
            if normalized not in semantic_terms:
                semantic_terms.append(normalized)
            if len(semantic_terms) >= 10:
                break
        semantic_query = " ".join(semantic_terms)[:140]

        globally_anchored_title = (document_text + " " + domain_hints + " " + title_like).strip()[:220]
        # Every candidate carries the document title once, in natural language.
        # Repeating it as bracket/suffix scaffolding causes the retrieval cleaner's
        # token budget to truncate the actual subsection concepts.
        multimodal_preferred_query = ""
        if "multimodal" in domain_hints:
            multimodal_preferred_query = (
                "multimodal large language models survey vision language text image audio video "
                "architectures benchmarks applications limitations"
            )
        open_source_llm_preferred_query = ""
        if "open-source large language models" in domain_hints or "open-weight models" in domain_hints:
            open_source_llm_preferred_query = (
                "open-source large language models open source software ecosystem OSS projects licensing deployment "
                "inference cost quantization open-weight model families governance release process version management "
                "source code contributors users bug tracking community enterprise adoption"
            )
        if open_source_llm_preferred_query:
            candidates_raw = [
                open_source_llm_preferred_query,
                (
                    f"{document_text} open-source large language model ecosystems licensing deployment "
                    "inference cost quantization governance release process version management source code community enterprise adoption"
                ).strip(),
                (
                    f"{document_text} open source software ecosystem OSS project governance release process "
                    "version management source code contributors users bug tracking large language models"
                ).strip(),
            ]
        else:
            candidates_raw = [
                multimodal_preferred_query,
                canonical_search_query,
                translated_query,
                core_domain_query,
                merged,
                globally_anchored_title,
                f"{document_text} {semantic_query}".strip(),
                f"{document_text} {canonical_requirements} {prompt_text[:180]}".strip(),
            ]
        candidates: List[str] = []
        seen = set()
        for candidate in candidates_raw:
            cleaned = " ".join(str(candidate or "").split()).strip()
            if not cleaned:
                continue
            normalized_cleaned = self._normalise_rag_pool_text(cleaned)
            anchor_suffix: List[str] = []

            def _anchor_present(anchor: str) -> bool:
                tokens = set(re.findall(r"[a-z0-9]+", self._normalise_rag_pool_text(anchor)))
                if not tokens:
                    return True
                observed = set(re.findall(r"[a-z0-9]+", normalized_cleaned))
                return len(tokens & observed) / len(tokens) >= 0.70

            if document_text and not _anchor_present(document_text):
                anchor_suffix.append(document_text)
            for hint in list(dict.fromkeys(domain_hint_terms))[:8]:
                if not _anchor_present(hint):
                    anchor_suffix.append(hint)
            suffix = " ".join(anchor_suffix).strip()
            if suffix:
                candidate_budget = max(80, 300 - len(suffix) - 1)
                cleaned = f"{cleaned[:candidate_budget]} {suffix}".strip()
            domain_anchored = cleaned[:300]
            key = domain_anchored.lower()
            if key in seen:
                continue
            seen.add(key)
            candidates.append(domain_anchored)

        return candidates[:4]

    def _canonical_english_requirements(self, text: str) -> str:
        """Return stable English topic anchors for multilingual requirements."""
        raw = " ".join(str(text or "").split()).strip()
        if not raw:
            return ""
        topic_raw = self._strip_generation_style_scaffold(raw)
        cjk_chars = len(re.findall(r"[\u4e00-\u9fff]", raw))
        latin_chars = len(re.findall(r"[A-Za-z]", topic_raw))
        if cjk_chars < 4 and latin_chars:
            return topic_raw[:360]

        translated = self._english_rag_query_bridge(topic_raw)
        terms: List[str] = []
        lower = topic_raw.lower()
        explicit_citation_topic = bool(
            re.search(
                r"\b(?:citation quality|citation control|citation drift|citation verification|reference verification|source attribution)\b",
                lower,
            )
        )
        explicit_evaluation_topic = bool(
            re.search(
                r"\b(?:evaluation methods?|evaluation metrics?|benchmark(?:s|ing)?|quality evaluation)\b",
                lower,
            )
        )
        if "2024" in topic_raw:
            terms.append("2024")
        phrase_map = [
            (("ai agents", "agent", "agents", "智能体"), "AI agents"),
            (("工具调用", "tool calling", "tool use", "tool-use", "工具使用"), "tool calling"),
            (("工作流", "workflow", "workflows"), "workflows"),
            (("概念演进", "conceptual evolution", "概念发展"), "conceptual evolution"),
            (("典型架构", "architecture", "architectures", "架构"), "representative architectures"),
            (("评估方法", "evaluation", "benchmark", "评估"), "evaluation methods"),
            (("应用场景", "application", "applications", "应用"), "applications"),
            (("风险", "risk", "risks", "safety", "安全"), "risks and safety"),
            (("未来趋势", "future trend", "future trends", "趋势"), "future trends"),
            (("大语言模型", "large language model", "llm", "llms"), "large language models"),
            (("rag", "检索增强"), "retrieval augmented generation"),
            (("引用", "citation", "citations"), "citation quality"),
        ]
        for needles, phrase in phrase_map:
            if phrase == "citation quality" and not explicit_citation_topic:
                continue
            if phrase == "evaluation methods" and not explicit_evaluation_topic:
                continue
            if any((needle in topic_raw) or (needle in lower) for needle in needles):
                terms.append(phrase)
        joined = " ".join(dict.fromkeys([translated, *terms]))
        return " ".join(joined.split())[:360]

    @staticmethod
    def _scope_requirements_to_subsection(requirements: str, local_target: str) -> str:
        """Keep only document requirements that are relevant to this subsection.

        Whole-document prompts often enumerate several unrelated section missions.
        Feeding that complete list to every subsection verifier creates false
        relevance failures.  Scope clauses by lexical overlap with the local title
        and outline; evaluation references are never consulted.
        """
        value = " ".join(str(requirements or "").split()).strip()
        local = " ".join(str(local_target or "").split()).strip().lower()
        if not value or not local:
            return ""
        stop = {
            "about", "after", "also", "among", "and", "are", "based", "for",
            "from", "into", "its", "of", "on", "or", "paper", "research",
            "the", "their", "this", "through", "to", "using", "with", "write",
        }

        def terms(text: str) -> set[str]:
            return {
                token
                for token in re.findall(r"[a-z][a-z0-9-]{2,}", text.lower())
                if token not in stop
            }

        local_terms = terms(local)
        clauses = re.split(r"\s*(?:[,;|:]|\band\b)\s*", value, flags=re.IGNORECASE)
        selected: List[str] = []
        for clause in clauses:
            cleaned = clause.strip(" .,-")
            clause_terms = terms(cleaned)
            if cleaned and clause_terms and (clause_terms & local_terms):
                selected.append(cleaned)
        return "; ".join(dict.fromkeys(selected))[:220]

    @staticmethod
    def _strip_generation_style_scaffold(text: str) -> str:
        """Remove writing-style scaffolding from retrieval topic anchors."""
        value = " ".join(str(text or "").split())
        scaffold_patterns = [
            r"\bwith citation-aware long-form structure\b",
            r"\bcitation-aware long-form structure\b",
            r"\bevidence-grounded academic research report\b",
            r"\bevidence-grounded\b",
            r"\bcitation-aware\b",
            r"\blong-form structure\b",
            r"\bensure quality evaluation\b",
            r"\bquality evaluation\b",
            r"\buse citations?\b",
            r"\bdo not invent references?\b",
        ]
        for pattern in scaffold_patterns:
            value = re.sub(pattern, " ", value, flags=re.IGNORECASE)
        value = re.sub(r"\s+", " ", value).strip(" ,.;")
        return value

    def _canonical_english_search_query(self, text: str) -> str:
        """Build a concise English academic search query from multilingual task text."""
        canonical = self._canonical_english_requirements(text)
        if not canonical:
            return ""
        lower = canonical.lower()
        style_stripped = self._strip_generation_style_scaffold(canonical)
        style_stripped_lower = style_stripped.lower()
        explicit_citation_topic = bool(
            re.search(
                r"\b(?:citation quality|citation control|citation drift|citation verification|reference verification|source attribution)\b",
                style_stripped_lower,
            )
        )
        explicit_evaluation_topic = bool(
            re.search(
                r"\b(?:evaluation methods?|evaluation metrics?|benchmark(?:s|ing)?|quality evaluation)\b",
                style_stripped_lower,
            )
        )
        preferred_phrases = [
            "2024",
            "multimodal",
            "AI agents",
            "large language models",
            "tool calling",
            "tool use",
            "workflows",
            "retrieval augmented generation",
            "open-source",
            "open source",
            "open-weight",
            "open weight",
            "licensing",
            "deployment",
            "inference cost",
            "quantization",
            "governance",
            "enterprise adoption",
            "representative architectures",
            "architectures",
            "evaluation methods" if explicit_evaluation_topic else "",
            "evaluation" if explicit_evaluation_topic else "",
            "applications",
            "risks",
            "safety",
            "citation quality" if explicit_citation_topic else "",
        ]
        parts: List[str] = []
        for phrase in preferred_phrases:
            if phrase and phrase.lower() in lower and phrase not in parts:
                parts.append(phrase)

        stop = {
            "about", "write", "long", "report", "research", "conceptual",
            "evolution", "future", "trends", "representative", "methods",
            "quality", "development", "developments", "overview",
        }
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,30}", canonical):
            norm = token.lower().strip("-_")
            if norm in stop or norm.isdigit():
                continue
            if any(norm in phrase.lower().split() for phrase in parts):
                continue
            if norm not in {part.lower() for part in parts}:
                parts.append(norm)
            if len(parts) >= 11:
                break
        if len(parts) >= 4 and "survey" not in {part.lower() for part in parts}:
            parts.append("survey")
        return " ".join(parts)[:170]

    def _append_canonical_english_task_anchors(self, text: str) -> str:
        """Attach a single English task-anchor block for multilingual prompts."""
        raw = str(text or "").strip()
        if not raw or "Canonical English task anchors:" in raw:
            return raw
        anchors = self._canonical_english_requirements(raw)
        if not anchors:
            return raw
        return (
            f"{raw}\n\n"
            "Canonical English task anchors: "
            f"{anchors}\n"
            "Use these anchors as the authoritative topic target for retrieval, generation, verification, and repair."
        ).strip()

    def _english_rag_query_bridge(self, text: str) -> str:
        """Convert CJK-heavy task text into an English academic search query."""
        raw = " ".join(str(text or "").split()).strip()
        if not raw or not self.rag_translate_cjk_query:
            return ""
        cjk_chars = len(re.findall(r"[\u4e00-\u9fff]", raw))
        latin_chars = len(re.findall(r"[A-Za-z]", raw))
        if cjk_chars < 8 or cjk_chars <= latin_chars * 0.6:
            return ""
        cache_key = raw[:500]
        if cache_key in self._rag_query_translation_cache:
            return self._rag_query_translation_cache[cache_key]

        translated = ""
        if os.getenv("FLOWERNET_RAG_TRANSLATE_CJK_WITH_LLM", "true").lower() == "true":
            try:
                prompt = (
                    "Convert the following research topic/subsection into one concise English academic search query. "
                    "Keep named methods, model names, product domains, evaluation terms, limitations, and technical nouns. "
                    "Return only the query, no explanation.\n\n"
                    f"TEXT:\n{raw[:900]}"
                )
                result = self._call_generator(prompt, max_tokens=90)
                candidate = str(result.get("draft") or "").strip() if isinstance(result, dict) else ""
                candidate = re.sub(r"(?im)^(query|search query)\s*[:：]\s*", "", candidate).strip()
                candidate = " ".join(re.findall(r"[A-Za-z0-9][A-Za-z0-9+./'_-]*", candidate))[:220]
                if len(candidate.split()) >= 4:
                    translated = candidate
            except Exception:
                translated = ""

        if not translated:
            phrase_map = {
                "多模态": "multimodal",
                "大语言模型": "large language models",
                "语言模型": "language models",
                "真实产品": "real-world products",
                "产品": "products",
                "落地": "deployment",
                "文本": "text",
                "图像": "image",
                "语音": "speech",
                "视频": "video",
                "融合": "fusion",
                "路径": "pathways",
                "评估": "evaluation",
                "指标": "metrics",
                "局限": "limitations",
                "架构": "architecture",
                "代理": "agents",
                "检索": "retrieval",
                "生成": "generation",
                "引用": "citation",
                "机器人": "robotics",
                "自动驾驶": "autonomous driving",
                "医疗": "healthcare",
                "教育": "education",
                "推荐": "recommendation",
                "安全": "safety",
                "隐私": "privacy",
                "公平": "fairness",
                "延迟": "latency",
                "用户体验": "user experience",
                "基准": "benchmark",
            }
            terms: List[str] = []
            for zh, en in phrase_map.items():
                if zh in raw and en not in terms:
                    terms.append(en)
            latin_terms = re.findall(r"\b[A-Za-z][A-Za-z0-9+./'_-]{2,}\b", raw)
            for term in latin_terms:
                if term.lower() not in {t.lower() for t in terms}:
                    terms.append(term)
                if len(terms) >= 18:
                    break
            translated = " ".join(terms[:18])[:220]

        self._rag_query_translation_cache[cache_key] = translated
        return translated

    @staticmethod
    def _normalise_rag_pool_text(value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip().lower())

    def _rag_source_pool_key(
        self,
        *,
        document_title: str,
        subsection_title: str,
        outline: str,
        initial_prompt: str,
    ) -> str:
        """Stable key for a topic/subsection evidence pool across ablations.

        It deliberately excludes document_id, system name, controller arm, and
        retry count so full and ablated variants compare writing/control logic
        against the same FlowerNet-generated RAG references.
        """
        canonical_prompt = self._canonical_english_requirements(initial_prompt)
        payload = "\n".join(
            [
                self._normalise_rag_pool_text(document_title),
                self._normalise_rag_pool_text(subsection_title),
                self._normalise_rag_pool_text(outline),
                self._normalise_rag_pool_text(canonical_prompt or initial_prompt),
            ]
        )
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    def _load_rag_source_pool_cache(self) -> Dict[str, Any]:
        if not self.rag_source_pool_path:
            return {"version": 1, "pools": {}}
        if isinstance(self._rag_source_pool_cache, dict):
            return self._rag_source_pool_cache
        path = self.rag_source_pool_path
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if isinstance(data, dict) and isinstance(data.get("pools"), dict):
                    self._rag_source_pool_cache = data
                    return data
        except Exception:
            pass
        self._rag_source_pool_cache = {"version": 1, "pools": {}}
        return self._rag_source_pool_cache

    def _write_rag_source_pool_cache(self, data: Dict[str, Any]) -> bool:
        if not self.rag_source_pool_path:
            return False
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.rag_source_pool_path)), exist_ok=True)
            tmp_path = f"{self.rag_source_pool_path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.rag_source_pool_path)
            self._rag_source_pool_cache = data
            return True
        except Exception:
            return False

    def _load_initial_draft_pool_cache(self) -> Dict[str, Any]:
        """Load real first-pass drafts used to pair stochastic ablations."""
        if not self.initial_draft_pool_path:
            return {"version": 1, "drafts": {}}
        if isinstance(self._initial_draft_pool_cache, dict):
            return self._initial_draft_pool_cache
        try:
            if os.path.exists(self.initial_draft_pool_path):
                with open(self.initial_draft_pool_path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if isinstance(data, dict) and isinstance(data.get("drafts"), dict):
                    self._initial_draft_pool_cache = data
                    return data
        except Exception:
            pass
        self._initial_draft_pool_cache = {"version": 1, "drafts": {}}
        return self._initial_draft_pool_cache

    def _write_initial_draft_pool_cache(self, data: Dict[str, Any]) -> bool:
        if not self.initial_draft_pool_path:
            return False
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.initial_draft_pool_path)), exist_ok=True)
            tmp_path = f"{self.initial_draft_pool_path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.initial_draft_pool_path)
            self._initial_draft_pool_cache = data
            return True
        except Exception:
            return False

    def _initial_draft_pool_key(self, prompt: str, max_tokens: int) -> str:
        payload = "\n".join([
            "paired_draft_protocol=2",
            f"provider={os.getenv('GENERATOR_PROVIDER', 'deepseek')}",
            f"model={os.getenv('GENERATOR_MODEL', 'deepseek-v4-flash')}",
            f"temperature={float(getattr(self, 'generator_temperature', 0.2)):.6f}",
            f"max_tokens={int(max_tokens)}",
            str(prompt or ""),
        ])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _get_paired_initial_draft(self, prompt: str, max_tokens: int) -> Optional[Dict[str, Any]]:
        if not self.initial_draft_pool_path:
            return None
        key = self._initial_draft_pool_key(prompt, max_tokens)
        entry = (self._load_initial_draft_pool_cache().get("drafts") or {}).get(key)
        if not isinstance(entry, dict) or not str(entry.get("draft") or "").strip():
            return None
        metadata = dict(entry.get("metadata") or {})
        metadata.update({
            "paired_initial_draft": True,
            "paired_initial_draft_reused": True,
            "paired_initial_draft_key": key,
        })
        return {"success": True, "draft": str(entry.get("draft") or ""), "metadata": metadata}

    def _remember_paired_initial_draft(
        self,
        prompt: str,
        max_tokens: int,
        result: Dict[str, Any],
    ) -> bool:
        if not self.initial_draft_pool_path or not bool(result.get("success")):
            return False
        draft = str(result.get("draft") or "")
        if not draft.strip():
            return False
        key = self._initial_draft_pool_key(prompt, max_tokens)
        data = self._load_initial_draft_pool_cache()
        drafts = data.setdefault("drafts", {})
        if key in drafts:
            return False
        metadata = dict(result.get("metadata") or {})
        drafts[key] = {
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "draft": draft,
            "metadata": metadata,
        }
        return self._write_initial_draft_pool_cache(data)

    def _get_fixed_rag_source_pool(self, key: str) -> Optional[Dict[str, Any]]:
        if not self.rag_source_pool_path or not key:
            return None
        data = self._load_rag_source_pool_cache()
        entry = (data.get("pools") or {}).get(key)
        if not isinstance(entry, dict):
            return None
        results = entry.get("results")
        if not isinstance(results, list) or not results:
            return None
        return {
            "success": True,
            "query": str(entry.get("selected_query") or ""),
            "results": [dict(item) for item in results if isinstance(item, dict)],
            "source_type": str(entry.get("source_type") or "fixed_rag_source_pool"),
            "reranker": str(entry.get("reranker") or ""),
            "vector_indexed": int(entry.get("vector_indexed", 0) or 0),
            "vector_backend": str(entry.get("vector_backend") or ""),
            "result_set_score": float(entry.get("result_set_score", 0.0) or 0.0),
            "source_pool_key": key,
            "source_pool_reused": True,
        }

    def _document_rag_source_pool_candidates(
        self,
        document_title: str,
        exclude_key: str = "",
    ) -> List[Dict[str, Any]]:
        """Return prior real source sets from the same document for revalidation.

        External retrieval can transiently fail for one subsection even after
        closely related evidence was indexed for another. Reuse is restricted
        to the same document and callers must rerun current-subsection semantic
        filtering before accepting any source.
        """
        target = self._normalise_rag_pool_text(document_title)
        if not target:
            return []
        candidates: List[Dict[str, Any]] = []
        for key, entry in (self._load_rag_source_pool_cache().get("pools") or {}).items():
            if str(key) == str(exclude_key) or not isinstance(entry, dict):
                continue
            if self._normalise_rag_pool_text(entry.get("document_title", "")) != target:
                continue
            results = [dict(item) for item in (entry.get("results") or []) if isinstance(item, dict)]
            if not results:
                continue
            candidates.append({
                "success": True,
                "query": str(entry.get("selected_query") or ""),
                "results": results,
                "source_type": "same_document_source_pool",
                "reranker": str(entry.get("reranker") or ""),
                "vector_indexed": int(entry.get("vector_indexed", 0) or 0),
                "vector_backend": str(entry.get("vector_backend") or ""),
            })
        return candidates

    def _remember_rag_source_pool(
        self,
        key: str,
        *,
        document_title: str,
        subsection_title: str,
        rag_search_result: Dict[str, Any],
        selected_query: str,
        tried_queries: List[str],
    ) -> bool:
        if not self.rag_source_pool_path or not key:
            return False
        results = [dict(item) for item in (rag_search_result.get("results") or []) if isinstance(item, dict)]
        if not results:
            return False
        if os.getenv("FLOWERNET_RAG_SOURCE_POOL_REQUIRE_INDEXED", "false").lower() == "true":
            if int(rag_search_result.get("vector_indexed", 0) or 0) <= 0:
                return False
            if not str(rag_search_result.get("reranker") or "").strip():
                return False
        data = self._load_rag_source_pool_cache()
        pools = data.setdefault("pools", {})
        if key in pools:
            return False
        pools[key] = {
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "document_title": document_title,
            "subsection_title": subsection_title,
            "selected_query": selected_query,
            "tried_queries": list(tried_queries or []),
            "source_type": str(rag_search_result.get("source_type") or "rag_search"),
            "reranker": str(rag_search_result.get("reranker") or ""),
            "vector_indexed": int(rag_search_result.get("vector_indexed", 0) or 0),
            "vector_backend": str(rag_search_result.get("vector_backend") or ""),
            "result_set_score": float(rag_search_result.get("result_set_score", 0.0) or 0.0),
            "results": results,
        }
        return self._write_rag_source_pool_cache(data)

    @staticmethod
    def _filter_rag_source_metadata(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Reject unusable metadata and collapse revision-equivalent sources."""
        filtered: List[Dict[str, Any]] = []
        seen_families: set[str] = set()
        seen_title_families: set[str] = set()
        for item in items or []:
            if not isinstance(item, dict):
                continue
            title = " ".join(str(item.get("title") or "").split()).strip()
            if (
                not title
                or title.lower() in {"none", "null", "n/a", "na", "untitled"}
                or re.fullmatch(r"[\d.\-_: /]{1,40}", title)
                or len(re.findall(r"[A-Za-z\u4e00-\u9fff]", title)) < 3
            ):
                continue
            normalized_title = re.sub(r"\b(?:revised?|revision|version|edition)\s*\d*\b", " ", title.lower())
            normalized_title = re.sub(r"\b(?:19|20)\d{2}\b", " ", normalized_title)
            normalized_title = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", normalized_title)
            title_family = " ".join(normalized_title.split())[:180]
            if title_family and title_family in seen_title_families:
                continue

            doi = str(item.get("doi") or "").strip().lower()
            if doi:
                family = re.sub(r"r\d{2,4}$", "", doi)
                family = re.sub(r"(?:[._-]v(?:ersion)?\d+)$", "", family)
                family = "doi:" + family
            else:
                family = "title:" + " ".join(normalized_title.split())[:180]
            if family in seen_families:
                continue
            seen_families.add(family)
            if title_family:
                seen_title_families.add(title_family)
            filtered.append(item)
        return filtered

    @staticmethod
    def _historical_source_year_cutoff(text: str) -> Optional[int]:
        value = " ".join(str(text or "").split()).lower()
        current_year = datetime.now(timezone.utc).year
        years = [int(year) for year in re.findall(r"\b(?:19|20)\d{2}\b", value)]
        past_years = [year for year in years if 1900 <= year < current_year]
        if not past_years:
            return None
        cutoff = max(past_years)
        explicit = bool(re.search(
            rf"\b(?:in|during|through|until|before|by|as of|up to)\s+(?:the end of\s+)?{cutoff}\b"
            rf"|\b(?:19|20)\d{{2}}\s*(?:-|–|—|to|through)\s*{cutoff}\b",
            value,
        ))
        return cutoff if explicit else None

    @staticmethod
    def _filter_sources_by_temporal_scope(
        items: List[Dict[str, Any]],
        topic: str,
    ) -> List[Dict[str, Any]]:
        """Exclude post-cutoff evidence for explicitly historical topics."""
        cutoff = DocumentGenerationOrchestrator._historical_source_year_cutoff(topic)
        if cutoff is None:
            return list(items or [])
        filtered: List[Dict[str, Any]] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            year_value = str(
                item.get("year")
                or item.get("publication_year")
                or item.get("published_year")
                or ""
            )
            if not year_value:
                year_value = f"{item.get('body', '')} {item.get('published', '')} {item.get('date', '')}"
            match = re.search(r"\b(?:19|20)\d{2}\b", year_value)
            if match and int(match.group(0)) > cutoff:
                continue
            filtered.append(item)
        return filtered

    @staticmethod
    def _passes_topic_core_anchor_gate(topic: str, source_text: str) -> bool:
        """Reject sources that only match generic evaluation/product words.

        The gate is activated only when the topic itself contains recognizable
        domain anchors. It is topic-derived rather than topic-id-specific, so it
        applies across datasets without hardcoding any experiment case.
        """
        topic_text = str(topic or "").lower()
        text = str(source_text or "").lower()
        if not topic_text.strip() or not text.strip():
            return True

        groups: List[Tuple[str, List[str]]] = []
        if any(term in topic_text for term in ("multimodal", "multi-modal", "多模态")):
            groups.append(("multimodal", ["multimodal", "multi-modal", "cross-modal", "vision-language", "multisensory"]))
            broad_multimodal_topic = not any(term in topic_text for term in ("medical", "clinical", "remote sensing", "遥感", "医学", "临床"))
        if any(term in topic_text for term in ("large language model", "language models", "llm", "llms", "大语言模型")):
            groups.append(("language_model", ["large language model", "language model", "language models", "llm", "llms", "foundation model", "generative ai"]))

        modality_terms = [
            term
            for term in ("text", "image", "vision", "speech", "audio", "video")
            if term in topic_text
        ]
        if len(modality_terms) >= 2:
            modality_hits = sum(1 for term in modality_terms if term in text)
            if modality_hits >= min(2, len(modality_terms)):
                return True

        if not groups:
            return True
        if "broad_multimodal_topic" in locals() and broad_multimodal_topic:
            comprehensive_multimodal = any(
                anchor in text
                for anchor in (
                    "vision-language", "vision language", "cross-modal",
                    "large language model", "large language models", "mllm", "mllms",
                    "foundation model", "foundation models", "generative ai",
                )
            )
            modality_families = [
                ("text", "language", "nlp"),
                ("image", "vision", "visual"),
                ("speech", "audio", "acoustic"),
                ("video", "temporal"),
            ]
            family_hits = sum(1 for family in modality_families if any(term in text for term in family))
            if not comprehensive_multimodal and family_hits < 2:
                return False
        # When a topic contains multiple strong domain anchors, a citable source
        # must satisfy all of them. Otherwise broad LLM/RAG papers can slip into
        # multimodal, healthcare, robotics, or other multi-anchor topics simply
        # because they share one generic phrase.
        return all(any(anchor in text for anchor in anchors) for _, anchors in groups)

    @staticmethod
    def _filter_citable_source_alignment(
        items: List[Dict[str, Any]],
        topic: str,
        min_semantic_score: float = 0.35,
    ) -> List[Dict[str, Any]]:
        """Expose only source ids that can pass the Verifier's semantic gate."""
        topic_text = str(topic or "").lower()
        latin_tokens = {
            token
            for token in re.findall(r"[a-z][a-z0-9+./_-]{2,40}", topic_text)
            if token not in {"the", "and", "for", "with", "from", "into", "this", "that", "section", "subsection"}
        }
        cjk_tokens = set(re.findall(r"[\u4e00-\u9fff]{2,12}", topic_text))
        topic_tokens = latin_tokens or cjk_tokens
        accepted: List[Dict[str, Any]] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            source_text = f"{item.get('title', '')} {item.get('body', '')}".lower()
            semantic_score = float(item.get("semantic_score", 0.0) or 0.0)
            retrieval_semantic_score = float(item.get("retrieval_query_semantic_score", 0.0) or 0.0)
            topic_alignment_score = float(item.get("topic_alignment_score", 0.0) or 0.0)
            source_tags = DocumentGenerationOrchestrator._source_axis_tags(item)
            strong_oss_project_context = bool(
                re.search(r"\b(?:open source software|open-source software|oss|software engineering|software development)\b", source_text)
                and re.search(r"\b(?:project|projects|release|version|contributors|users|bug|tracking|management|governance|community|communities|business models?|firms?|ecosystems?|development teams)\b", source_text)
            )
            passes_anchor_gate = DocumentGenerationOrchestrator._passes_topic_core_anchor_gate(topic_text, source_text)
            supplemental_oss_evidence = (
                not passes_anchor_gate
                and DocumentGenerationOrchestrator._topic_needs_open_source_llm_axes(topic_text)
                and "oss_project_ecosystem" in source_tags
                and strong_oss_project_context
                and max(semantic_score, topic_alignment_score) >= float(min_semantic_score)
                and semantic_score >= float(os.getenv("MIN_SUPPLEMENTAL_OSS_SOURCE_SEMANTIC_SCORE", "0.28"))
            )
            if not passes_anchor_gate and not supplemental_oss_evidence:
                continue
            if (
                not supplemental_oss_evidence
                and DocumentGenerationOrchestrator._source_has_cross_domain_application_drift(topic_text, source_text)
            ):
                continue
            if DocumentGenerationOrchestrator._topic_needs_open_source_llm_axes(topic_text):
                ecosystem_axes = {
                    "software_ecosystem",
                    "oss_project_ecosystem",
                    "licensing",
                    "deployment",
                    "governance",
                    "adoption",
                    "model_family",
                }
                # Sharing only "open-source LLM" is not enough for ecosystem
                # surveys. Otherwise application benchmarks pull source
                # alignment toward astronomy, medicine, exams, or leakage
                # reports instead of model families, licensing, deployment,
                # governance, and adoption.
                if source_tags.isdisjoint(ecosystem_axes):
                    continue
            lexical_score = 0.0
            core_hit_count = 0
            if topic_tokens:
                hit = 0
                for token in topic_tokens:
                    variants = {token}
                    if token.endswith("s") and len(token) > 4:
                        variants.add(token[:-1])
                    if token.endswith("ies") and len(token) > 5:
                        variants.add(token[:-3] + "y")
                    if token.endswith("y") and len(token) > 4:
                        variants.add(token[:-1] + "ies")
                    matched = any(variant in source_text for variant in variants)
                    if matched:
                        hit += 1
                        if token not in {
                            "survey", "review", "future", "trends", "methods",
                            "applications", "risks", "safety", "evaluation",
                        }:
                            core_hit_count += 1
                lexical_score = hit / max(1, len(topic_tokens))
            href = str(item.get("href") or item.get("url") or "").lower()
            scholarly = bool("doi.org/" in href or re.search(r"\b10\.\d{4,9}/", href))
            scholarly_core_near = bool(
                scholarly
                and core_hit_count >= 3
                and max(lexical_score, semantic_score, retrieval_semantic_score, topic_alignment_score)
                >= max(0.0, float(min_semantic_score) - 0.08)
            )
            if (
                semantic_score > 0.0
                and semantic_score < float(min_semantic_score)
                and retrieval_semantic_score < float(min_semantic_score)
                and lexical_score < float(min_semantic_score)
                and not scholarly_core_near
                and not supplemental_oss_evidence
            ):
                continue
            # A DOI or venue proves source reality, not topical fit.  Keep only a
            # small scholarly lift for near-threshold sources that already have
            # lexical/topic evidence, so pre-filtering matches Verifier source_check.
            scholarly_lift = (
                0.04
                if scholarly_core_near
                else 0.0
            )
            score = max(
                lexical_score + scholarly_lift,
                semantic_score,
                retrieval_semantic_score,
                topic_alignment_score,
            )
            enriched = dict(item)
            enriched["citable_semantic_score"] = round(float(score), 4)
            if supplemental_oss_evidence:
                enriched["supplemental_evidence_role"] = "oss_project_ecosystem"
            if score >= float(min_semantic_score):
                accepted.append(enriched)
        return accepted

    @staticmethod
    def _source_has_cross_domain_application_drift(topic_text: str, source_text: str) -> bool:
        """Reject application-domain sources whose domain is absent from the topic.

        A source can contain generic method terms such as "RAG" or "LLM" while
        actually being about chemistry, medicine, finance, or another applied
        domain. Those sources are real, but using their domain-specific n-grams
        as full source-alignment anchors can pull a general survey off topic.
        """
        topic_lower = str(topic_text or "").lower()
        source_lower = str(source_text or "").lower()
        domain_groups = [
            {"chemical", "chemistry", "metabolomics", "molecule", "molecular", "analytical chemistry", "drug discovery"},
            {"clinical", "medical", "medicine", "biomedical", "patient", "diagnosis", "healthcare"},
            {"pharmacy", "pharmacist", "pharmaceutical", "licensing examination", "board examination"},
            {"finance", "financial", "stock", "trading", "portfolio", "banking"},
            {"legal", "law", "court", "contract", "litigation", "statute"},
            {"agriculture", "crop", "soil", "farm", "farming"},
            {"education", "educational", "course design", "curriculum", "teaching", "learning", "industry-education"},
            {"blockchain", "nft", "tokenized", "tokenization", "rights object", "rights objects"},
        ]
        generic_method_terms = {
            "retrieval",
            "augmented",
            "generation",
            "rag",
            "language model",
            "large language model",
            "llm",
            "agent",
            "workflow",
        }
        has_generic_method = any(term in source_lower for term in generic_method_terms)
        if not has_generic_method:
            return False
        for group in domain_groups:
            source_hits = {term for term in group if term in source_lower}
            if not source_hits:
                continue
            topic_hits = {term for term in group if term in topic_lower}
            if not topic_hits:
                return True
        return False

    @staticmethod
    def _source_axis_tags(item: Dict[str, Any]) -> set[str]:
        text = f"{item.get('title', '')} {item.get('body', '')} {item.get('summary', '')}".lower()
        tags: set[str] = set()
        if re.search(r"\b(?:large language models?|llms?|foundation models?|open[- ]weight models?)\b", text):
            tags.add("llm")
        if re.search(r"\b(?:software|oss|project|release|version|source code|contributors|users|bug tracking|development)\b", text):
            tags.add("software_ecosystem")
        if re.search(r"\b(?:ecosystem|ecosystems|oss|software project|projects|release|version|contributors|users|bug tracking|project governance|community relationships|development teams)\b", text):
            tags.add("oss_project_ecosystem")
        if re.search(r"\b(?:licens(?:e|ing)|commercial use|redistribution|compliance)\b", text):
            tags.add("licensing")
        if re.search(r"\b(?:deployment|inference|quantization|private|local)\b", text):
            tags.add("deployment")
        if re.search(r"\b(?:governance|community|communities|maintain(?:er|ers|ance)|stewardship)\b", text):
            tags.add("governance")
        if re.search(r"\b(?:adoption|enterprise|production|commercialization|users?)\b", text):
            tags.add("adoption")
        if re.search(r"\b(?:model famil(?:y|ies)|compressed language models?|open[- ]weight models?|model lineage)\b", text):
            tags.add("model_family")
        return tags

    @staticmethod
    def _topic_needs_open_source_llm_axes(topic: str) -> bool:
        topic_text = str(topic or "").lower()
        return bool(
            re.search(r"\b(?:open[- ]source|open[- ]weight|oss)\b", topic_text)
            and re.search(r"\b(?:large language models?|llms?|foundation models?)\b", topic_text)
            and re.search(r"\b(?:software|ecosystems?|project|release|version|source code|contributors|users|bug tracking|licens(?:e|ing)|deployment|inference|quantization|governance|adoption|model famil(?:y|ies))\b", topic_text)
        )

    @staticmethod
    def _preserve_multi_axis_source_diversity(
        topic: str,
        ranked_items: List[Dict[str, Any]],
        candidate_pool: List[Dict[str, Any]],
        max_items: int,
    ) -> List[Dict[str, Any]]:
        """Keep evidence diversity for multi-axis topics after reranking.

        Rerankers can over-focus on the highest-scoring local phrase and drop a
        secondary but necessary source family.  For open-source LLM ecosystem
        topics, truthful coverage needs both LLM-specific evidence and broader
        OSS/software-project evidence.  This only preserves real retrieved
        items; it never adds fabricated sources.
        """
        limit = max(1, int(max_items or len(ranked_items) or len(candidate_pool) or 1))
        if not DocumentGenerationOrchestrator._topic_needs_open_source_llm_axes(topic):
            return list(ranked_items or [])[:limit]

        def key(item: Dict[str, Any]) -> str:
            return str(item.get("href") or item.get("url") or item.get("doi") or item.get("title") or "")

        selected: List[Dict[str, Any]] = []
        seen: set[str] = set()

        def add(item: Dict[str, Any]) -> None:
            if not isinstance(item, dict) or len(selected) >= limit:
                return
            item_key = key(item)
            if item_key in seen:
                return
            seen.add(item_key)
            selected.append(item)

        pool = [item for item in (candidate_pool or []) if isinstance(item, dict)]
        ranked = [item for item in (ranked_items or []) if isinstance(item, dict)]
        for item in ranked:
            add(item)
            if len(selected) >= min(2, limit):
                break

        selected_tags = set().union(*(DocumentGenerationOrchestrator._source_axis_tags(item) for item in selected)) if selected else set()
        required_axes = ("llm", "oss_project_ecosystem")
        for axis in required_axes:
            if axis in selected_tags:
                continue
            for item in pool:
                if axis in DocumentGenerationOrchestrator._source_axis_tags(item):
                    add(item)
                    break
            selected_tags = set().union(*(DocumentGenerationOrchestrator._source_axis_tags(item) for item in selected)) if selected else set()

        for item in ranked + pool:
            add(item)
            if len(selected) >= limit:
                break
        return selected[:limit]

    @staticmethod
    def _score_rag_result_set(
        document_title: str,
        subsection_outline: str,
        items: List[Dict[str, Any]],
    ) -> float:
        if not items:
            return 0.0
        anchor_text = f"{document_title} {subsection_outline}".lower()
        anchor_tokens = {
            token
            for token in re.findall(r"[a-z\u4e00-\u9fff][a-z0-9\u4e00-\u9fff_-]{2,}", anchor_text)
            if token not in {"the", "and", "for", "with", "from", "framework", "analysis", "study"}
        }
        source_text = " ".join(
            f"{item.get('title', '')} {item.get('body', '')}" for item in items
        ).lower()
        anchor_coverage = (
            sum(1 for token in anchor_tokens if token in source_text) / max(1, len(anchor_tokens))
        )
        quality = sum(
            max(
                float(item.get("quality_score", 0.0) or 0.0),
                float(item.get("semantic_score", 0.0) or 0.0),
                float(item.get("topic_alignment_score", 0.0) or 0.0),
                float(item.get("citable_semantic_score", 0.0) or 0.0),
            )
            for item in items
        ) / max(1, len(items))
        domains = {
            str(item.get("source") or item.get("provider") or "").strip().lower()
            for item in items
            if str(item.get("source") or item.get("provider") or "").strip()
        }
        diversity = min(1.0, len(domains) / max(1, min(3, len(items))))
        return round(0.55 * anchor_coverage + 0.35 * quality + 0.10 * diversity, 4)

    def _refresh_rag_for_evidence_repair(
        self,
        *,
        document_title: str,
        improved_outline: str,
        initial_prompt: str,
        document_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Retrieve a fresh evidence set after an evidence-targeted repair."""
        if not self.rag_enabled or self.search_engine is None:
            return None
        if getattr(self, "rag_source_pool_path", "") and os.getenv("FLOWERNET_FIXED_SOURCE_POOL_DISABLE_REFRESH", "true").lower() == "true":
            return None

        result_sets: List[Tuple[float, str, Dict[str, Any]]] = []
        query_candidates = self._build_rag_query_candidates(
            outline=improved_outline,
            initial_prompt=initial_prompt,
            document_title=document_title,
        )
        for query in query_candidates:
            result = self.search_engine.search(query)
            if not result.get("success") or not result.get("results"):
                continue
            filtered = self._filter_rag_source_metadata(result.get("results", []))
            if not filtered:
                continue
            result = dict(result)
            original_count = len(result.get("results", []))
            result["results"] = filtered
            result["metadata_filtered_count"] = original_count - len(filtered)
            set_score = self._score_rag_result_set(document_title, improved_outline, filtered)
            result["result_set_score"] = set_score
            result_sets.append((set_score, query, result))

        if not result_sets:
            return None

        set_score, selected_query, selected_result = max(result_sets, key=lambda item: item[0])
        min_set_score = float(os.getenv("FLOWERNET_EVIDENCE_REFRESH_MIN_SET_SCORE", "0.40"))
        if set_score < min_set_score:
            return None

        try:
            if self.vector_store is not None:
                reranked = self.vector_store.reranker.rerank(
                    selected_query,
                    selected_result.get("results", []),
                    top_k=max(3, len(selected_result.get("results", []))),
                )
                if reranked:
                    selected_result["results"] = reranked
                    selected_result["reranker"] = "flowernet_vector_reranker"
                selected_result["vector_indexed"] = self.vector_store.index_rag_results(
                    selected_query,
                    selected_result.get("results", []),
                    namespace=str(document_id or "global"),
                )
                selected_result["vector_backend"] = self.vector_store.active_backend
        except Exception as exc:
            selected_result["vector_index_error"] = str(exc)[:180]

        context = self.search_engine.format_search_context(
            selected_result,
            max_items=min(self.rag_max_results, len(selected_result.get("results", []) or [])),
        )
        return {
            "query": selected_query,
            "query_candidates": query_candidates,
            "result": selected_result,
            "context": context,
            "set_score": set_score,
        }

    def _build_local_outline_fallback(
        self,
        current_outline: str,
        original_outline: str,
        feedback: Dict[str, Any],
        rel_threshold: float,
        red_threshold: float,
        iteration: int,
    ) -> str:
        """当 Controller 不可用或返回无效结果时，本地规则改纲兜底。"""
        marker_start = "\n【本地修订约束】\n"
        marker_end = "\n【本地修订约束结束】"

        base_outline_raw = str(current_outline or original_outline or "").strip()
        block_start = base_outline_raw.find(marker_start)
        if block_start >= 0:
            block_end = base_outline_raw.find(marker_end, block_start)
            if block_end >= 0:
                base_outline = (base_outline_raw[:block_start] + base_outline_raw[block_end + len(marker_end):]).strip()
            else:
                base_outline = base_outline_raw[:block_start].strip()
        else:
            base_outline = base_outline_raw

        if not base_outline:
            return ""

        rel_score = float(feedback.get("relevancy_index", 0) or 0)
        red_score = float(feedback.get("redundancy_index", 0) or 0)
        feedback_text = str(feedback.get("feedback", "") or "")
        failed_dimensions = feedback.get("quality_dimensions_failed") if isinstance(feedback.get("quality_dimensions_failed"), list) else []
        dimension_check = feedback.get("quality_dimensions_check") if isinstance(feedback.get("quality_dimensions_check"), dict) else {}
        dimension_thresholds = feedback.get("dimension_thresholds") if isinstance(feedback.get("dimension_thresholds"), dict) else {}
        # Respect verifier-provided thresholds.  The old local fallback raised
        # logical_coherence to 0.25 when threshold metadata was missing, which
        # made long English academic paragraphs fail even when the verifier was
        # configured to use a lower, production-calibrated threshold.
        if not isinstance(dimension_thresholds, dict):
            dimension_thresholds = {}
        dimension_thresholds.setdefault("evidence_grounding", 0.18)
        dimension_thresholds.setdefault("logical_coherence", 0.075)
        dimension_messages = {
            "topic_alignment": "主题对齐不足：要更聚焦小节核心主题，补充关键定义、目标或必须回答的问题。",
            "coverage_completeness": "覆盖不完整：补齐小节应覆盖的关键子点、步骤、约束或对比维度。",
            "logical_coherence": "逻辑连贯性不足：重排为更清晰的因果、递进或问题-解决结构。",
            "evidence_grounding": "证据接地性不足：加入可验证事实、数据、引用或示例支撑，避免空话。",
            "novelty": "新颖性不足：引入新的角度、反例、比较对象或差异化信息，避免重复前文。",
            "structure_clarity": "结构清晰度不足：使用更明确的小标题、分点和步骤式组织。",
        }

        extra_lines: List[str] = []
        if rel_score < rel_threshold:
            extra_lines.append(
                f"- 聚焦要求：围绕本小节核心主题展开，确保 relevancy_index >= {rel_threshold:.2f}。"
            )
            keywords = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9_-]{2,}", str(original_outline or ""))
            key_tokens = []
            for token in keywords:
                if token not in key_tokens:
                    key_tokens.append(token)
                if len(key_tokens) >= 6:
                    break
            if key_tokens:
                extra_lines.append("- 必须覆盖关键词：" + "、".join(key_tokens) + "。")

        if red_score > red_threshold:
            extra_lines.append(
                f"- 去重要求：避免与前文表达重复，确保 redundancy_index <= {red_threshold:.2f}。"
            )
            extra_lines.append("- 内容策略：优先使用新的事实、案例、数据或反例，不复述已有段落。")

        if failed_dimensions:
            extra_lines.append("- 多维质量要求：以下维度必须分别修复，任何一个维度未达标都不允许通过。")
            for dim_name in failed_dimensions:
                dim_value = dimension_check.get(dim_name, {}).get("value") if isinstance(dimension_check, dict) else None
                dim_threshold = dimension_thresholds.get(dim_name)
                dim_text = dimension_messages.get(dim_name, f"{dim_name} 维度需要提升。")
                if isinstance(dim_value, (int, float)) and isinstance(dim_threshold, (int, float)):
                    extra_lines.append(
                        f"- {dim_name}: {dim_text} 当前值={float(dim_value):.4f}，阈值={float(dim_threshold):.4f}。"
                    )
                else:
                    extra_lines.append(f"- {dim_name}: {dim_text}")

        if feedback_text:
            extra_lines.append("- 验证反馈约束：" + feedback_text[:180])

        if not extra_lines:
            extra_lines.append("- 质量要求：保持主题聚焦与信息增量，避免泛化叙述。")

        adjustments = "\n".join(extra_lines).strip()
        suffix = (
            f"{marker_start}"
            f"- 第{iteration}轮修订：以下约束用于提升主题命中与信息增量。\n"
            f"{adjustments}\n"
            f"{marker_end}"
        )

        return (base_outline + "\n" + suffix).strip()

    def _generate_document_framing(
        self,
        title: str,
        sections: List[Dict[str, Any]],
        task_requirements: str = "",
    ) -> Dict[str, Any]:
        """Generate document-level opening and closing after the verified body exists."""
        if not self.document_framing_enabled:
            return {"success": True, "enabled": False, "introduction": "", "conclusion": "", "token_usage": {}}

        context_parts: List[str] = [f"Document title: {title}"]
        actual_section_titles: List[str] = []
        framing_source_terms: List[str] = []
        framing_source_phrases: List[str] = []
        framing_body_term_counts: Dict[str, int] = {}
        framing_stop = {
            "the", "and", "for", "with", "from", "into", "that", "this", "these", "those",
            "were", "have", "has", "are", "was", "its", "their", "about", "which", "such",
            "using", "used", "based", "between", "within", "without", "section", "source",
            "research", "article", "paper", "study", "analysis", "content", "toward",
        }
        for section in sections or []:
            section_title = str(section.get("section_title") or "").strip()
            if section_title:
                actual_section_titles.append(section_title)
                context_parts.append(f"Section: {section_title}")
            for subsection in section.get("subsections", []) or []:
                subsection_title = str(subsection.get("subsection_title") or "").strip()
                content = str(subsection.get("content") or "").strip()
                # Framing is grounded in the accepted body, not in a new search or
                # unsupported model knowledge. Keep enough from every subsection.
                content = re.sub(r"\[\d+\]", "", content)
                content = re.sub(r"\s+", " ", content).strip()
                if subsection_title:
                    context_parts.append(f"Subsection: {subsection_title}")
                if content:
                    for token in re.findall(r"[A-Za-z][A-Za-z0-9+./_-]{3,}|[\u4e00-\u9fff]{2,12}", content):
                        norm = token.lower()
                        if norm in framing_stop or norm.isdigit():
                            continue
                        framing_body_term_counts[norm] = framing_body_term_counts.get(norm, 0) + 1
                    # The opening alone over-represents definitions and caused
                    # framing to invent contributions or later sections. Sample
                    # the accepted beginning, middle and ending so the framing
                    # reflects mechanisms, limitations and implications too.
                    if len(content) <= 3000:
                        context_parts.append(content)
                    else:
                        window = 1000
                        middle_start = max(0, len(content) // 2 - window // 2)
                        context_parts.extend([
                            content[:window],
                            content[middle_start: middle_start + window],
                            content[-window:],
                        ])
                if getattr(self, "source_alignment_selector_enabled", False):
                    source_results = subsection.get("source_results") if isinstance(subsection.get("source_results"), list) else []
                    for phrase in self._source_title_phrases(source_results or []):
                        if phrase and phrase not in framing_source_phrases:
                            framing_source_phrases.append(phrase)
                        if len(framing_source_phrases) >= 8:
                            break
                    source_text = " ".join(
                        f"{item.get('title', '')} {self._source_body_for_prompt(item)}"
                        for item in (source_results or [])
                        if isinstance(item, dict)
                    )
                    for token in re.findall(r"[A-Za-z][A-Za-z0-9+./_-]{3,}|[\u4e00-\u9fff]{2,12}", source_text):
                        norm = token.lower()
                        if norm in framing_stop or norm.isdigit() or norm in framing_source_terms:
                            continue
                        framing_source_terms.append(norm)
                        if len(framing_source_terms) >= 24:
                            break
        body_context = "\n".join(context_parts)
        english = self._prefers_english_generation(title, body_context)
        section_map = "\n".join(f"- {item}" for item in actual_section_titles) or "- No titled body section"
        framing_body_terms = [
            term for term, _ in sorted(
                framing_body_term_counts.items(),
                key=lambda item: (-item[1], item[0]),
            )[:24]
        ]
        body_term_context = ", ".join(framing_body_terms)
        task_phrase_context = ", ".join(
            self._task_anchor_phrases(f"{title}\n{task_requirements}", limit=14)
        )
        source_alignment_context = ""
        if getattr(self, "source_alignment_selector_enabled", False) and (framing_source_phrases or framing_source_terms):
            if english:
                source_alignment_context = (
                    "\nSOURCE-AWARE FRAMING TERMS\n"
                    "When they are already supported by the accepted body, naturally preserve these source phrases and terms; "
                    "do not stuff keywords or add new claims.\n"
                    f"Source phrases: {', '.join(framing_source_phrases[:8])}\n"
                    f"Source terms: {', '.join(framing_source_terms[:18])}\n"
                )
            else:
                source_alignment_context = (
                    "\n【来源感知 framing 术语】\n"
                    "仅在已通过正文支持时，自然保留这些来源短语和术语；不要堆词，不要新增事实。\n"
                    f"来源短语：{', '.join(framing_source_phrases[:8])}\n"
                    f"来源术语：{', '.join(framing_source_terms[:18])}\n"
                )

        if english:
            prompts = {
                "introduction": f"""Write the Introduction for the completed academic paper below.

Use only claims and scope already present in the accepted body context. Write 3 coherent paragraphs (about 250-400 words total) covering the problem, scope, analytical contribution, and paper roadmap. Do not add a heading, References block, invented facts, URLs, or citation numbers. End with a complete sentence.
If source-aware framing terms are provided, use them naturally only when they are supported by the accepted body context.
The roadmap must follow the ACTUAL BODY SECTION MAP below. Use exact section titles or a non-numbered narrative sequence; never invent section numbers, experiments, datasets, results, case studies, or a proposed method that the accepted body does not contain. Describe a survey or analysis as a survey or analysis, not as an empirical contribution.
Paragraph 1 must directly define the title's central topic, its canonical architecture or mechanism, and the core problem using terminology dominant in the accepted body. Do not lead with peripheral deployment context unless deployment is the paper's title-level focus. Paragraph 2 states the supported scope and analytical synthesis. Paragraph 3 gives the exact non-invented roadmap.

DOMINANT ACCEPTED-BODY TERMS
{body_term_context}

CANONICAL TASK PHRASES
{task_phrase_context}
Preserve the applicable canonical task phrases verbatim where natural; do not replace every standard phrase with a synonym.

ACTUAL BODY SECTION MAP
{section_map}

ACCEPTED BODY CONTEXT
{body_context}
{source_alignment_context}
""",
                "conclusion": f"""Write the Conclusion for the completed academic paper below.

Synthesize only findings, limitations, and implications already supported by the accepted body context. Write 2-3 coherent paragraphs (about 200-300 words total). Do not add a heading, References block, invented facts, URLs, citation numbers, or a new unresolved transition. End with a decisive complete sentence.
If source-aware framing terms are provided, use them naturally only when they are supported by the accepted body context.
Do not claim experiments, datasets, benchmark results, case studies, or a proposed method unless those elements explicitly occur in the accepted body.
Begin directly with the principal supported finding; do not write meta-narration such as "The conclusion synthesizes" or "This conclusion discusses". Preserve the dominant accepted-body terminology naturally.
Use three synthesis duties: (1) state the principal finding by reconnecting the paper's central architecture or taxonomy, core failure/problem, and representative mechanisms; (2) explain the main limitations and trade-offs without narrowing the paper to one local technique; and (3) state supported governance/practical implications and future direction. Recover the breadth of the accepted body rather than over-focusing on its final paragraph.

DOMINANT ACCEPTED-BODY TERMS
{body_term_context}

CANONICAL TASK PHRASES
{task_phrase_context}
Preserve the applicable canonical task phrases verbatim where natural; do not replace every standard phrase with a synonym.

ACCEPTED BODY CONTEXT
{body_context}
{source_alignment_context}
""",
            }
        else:
            prompts = {
                "introduction": f"""请为下面已经完成的学术论文正文撰写 Introduction（引言）。

只能使用已通过正文中已有的论点和范围。写3个连贯自然段，约400-650个中文字符，说明问题、范围、分析贡献和全文结构。不要输出标题、参考文献块、虚构事实、URL或引用编号。必须以完整句子结束。
如果提供了来源感知 framing 术语，只能在已通过正文支持时自然使用，不要堆词。

已通过正文上下文
{body_context}
{source_alignment_context}
""",
                "conclusion": f"""请为下面已经完成的学术论文正文撰写 Conclusion（结论）。

只能综合已通过正文支持的发现、局限和启示。写2-3个连贯自然段，约300-500个中文字符。不要输出标题、参考文献块、虚构事实、URL、引用编号或新的悬空过渡。必须以明确的完整句子结束。
如果提供了来源感知 framing 术语，只能在已通过正文支持时自然使用，不要堆词。

已通过正文上下文
{body_context}
{source_alignment_context}
""",
            }

        framing: Dict[str, Any] = {
            "success": True,
            "enabled": True,
            "introduction": "",
            "conclusion": "",
            "token_usage": {
                "prompt_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "prompt_cache_hit_tokens": 0,
                "prompt_cache_miss_tokens": 0,
            },
        }
        min_chars = {"introduction": 500 if english else 260, "conclusion": 350 if english else 220}
        for name in ("introduction", "conclusion"):
            accepted = ""
            last_reason = "generation_failed"
            for attempt in range(2):
                prompt = prompts[name]
                if attempt:
                    prompt += (
                        "\nThe previous response was incomplete or used the wrong language. "
                        "English only. Keep within the requested length and finish every sentence."
                    )
                result = self._call_generator(prompt, max_tokens=self.document_framing_max_tokens)
                metadata = result.get("metadata") if isinstance(result, dict) and isinstance(result.get("metadata"), dict) else {}
                for key in framing["token_usage"]:
                    framing["token_usage"][key] += int(metadata.get(key, 0) or 0)
                candidate = self._sanitize_subsection_draft(result.get("draft", "") if isinstance(result, dict) else "")
                candidate = re.sub(r"(?im)^#{1,6}\s*(?:introduction|conclusion|引言|结论)\s*$", "", candidate).strip()
                if not result.get("success"):
                    last_reason = str(result.get("error") or "generation_failed")
                    continue
                if english and not self._is_predominantly_english_text(candidate):
                    last_reason = f"{name}_wrong_language"
                    continue
                if len(candidate) < min_chars[name]:
                    last_reason = f"{name}_too_short"
                    continue
                if re.search(r"(?im)^\s*(?:references?|bibliography|参考文献)\s*$", candidate):
                    last_reason = f"{name}_contains_reference_block"
                    continue
                if re.search(r"https?://|\[\d+\]", candidate):
                    last_reason = f"{name}_contains_unmapped_citation"
                    continue
                if not self._draft_is_complete(candidate, metadata):
                    last_reason = f"{name}_incomplete"
                    continue
                if name == "conclusion" and re.match(
                    r"(?i)^\s*(?:the|this)\s+conclusion\s+(?:synthesi[sz]es|discusses|summari[sz]es)",
                    candidate,
                ):
                    last_reason = "conclusion_meta_narration"
                    prompts[name] += (
                        "\nThe previous response began with meta-narration. Begin directly with the principal finding."
                    )
                    continue
                if name == "introduction":
                    unsupported_framing = []
                    candidate_lower = candidate.lower()
                    body_lower = body_context.lower()
                    if re.search(r"\bsection\s+\d+\b", candidate_lower):
                        unsupported_framing.append("invented_numbered_roadmap")
                    contribution_subject = r"(?:we|this\s+(?:paper|study|work|analysis))"
                    contribution_verb = r"(?:conducts?|performs?|presents?|reports?|introduces?|uses?|evaluates?)"
                    unsupported_claim_patterns = {
                        "experiments": (
                            rf"\b{contribution_subject}\s+{contribution_verb}\b[^.]*\bexperiments?\b"
                            r"|\bexperimental results?\s+(?:show|demonstrate|confirm|indicate)\b"
                        ),
                        "datasets": rf"\b{contribution_subject}\s+{contribution_verb}\b[^.]*\bdatasets?\b",
                        "benchmarks": rf"\b{contribution_subject}\s+{contribution_verb}\b[^.]*\bbenchmark(?:s| tasks?)?\b",
                        "case studies": rf"\b{contribution_subject}\s+{contribution_verb}\b[^.]*\bcase stud(?:y|ies)\b",
                    }
                    for label, pattern in unsupported_claim_patterns.items():
                        if re.search(pattern, candidate_lower) and not re.search(pattern, body_lower):
                            unsupported_framing.append(f"unsupported_{label.replace(' ', '_')}")
                    if unsupported_framing:
                        last_reason = f"{name}_unsupported_claims:" + ",".join(unsupported_framing)
                        prompts[name] += (
                            "\nThe previous framing invented unsupported roadmap or contribution claims. "
                            "Remove numbered section references and describe only the exact accepted body content."
                        )
                        continue
                accepted = candidate
                break
            if not accepted:
                framing.update({"success": False, "error": last_reason})
                return framing
            framing[name] = accepted
        return framing
    
    def generate_document(
        self,
        document_id: str,
        title: str,
        structure: Dict[str, Any],  # 从 Outliner 返回的结构
        content_prompts: List[Dict[str, Any]],  # 从 Outliner 返回的 content_prompts
        user_background: str,
        user_requirements: str,
        rel_threshold: float = 0.765,
        red_threshold: float = 0.265
    ) -> Dict[str, Any]:
        """
        完整文档生成流程
        
        按照结构，逐个 section/subsection 生成，每个通过才能生成下一个
        """
        print(f"\n{'='*70}")
        print(f"📚 开始生成文档: {title}")
        print(f"{'='*70}")
        print(f"Document ID: {document_id}")
        print(f"Section 数: {len(structure.get('sections', []))}")
        print(f"总 Subsection 数: {len(content_prompts)}")
        self._emit_progress_event(
            document_id=document_id,
            stage="document_start",
            message=f"文档生成已启动，目标小节数: {len(content_prompts)}",
        )
        
        document_result = {
            "success": True,
            "document_id": document_id,
            "title": title,
            "sections": [],
            "passed_subsections": 0,
            "failed_subsections": [],
            "forced_subsections": [],
            "total_iterations": 0,
            "generation_time": None,
            "rag_used_subsections": 0,
            "rag_search_success_subsections": 0,
            "rag_vector_indexed_total": 0,
            "rag_vector_backend": "",
            "rag_reranker_used_subsections": 0,
            "controller_effective_subsections": 0,
            "controller_triggered_subsections": 0,
            "verifier_failed_total": 0,
            "controller_calls_total": 0,
            "controller_success_total": 0,
            "controller_error_total": 0,
            "controller_unavailable_total": 0,
            "controller_ineffective_total": 0,
            "controller_fallback_outline_total": 0,
            "controller_exhausted_total": 0,
            "generator_short_draft_total": 0,
            "verifier_error_total": 0,
            "source_alignment_selector_used_subsections": 0,
            "source_alignment_low_pass_audits": 0,
            "source_alignment_score_sum": 0.0,
            "source_alignment_score_count": 0,
            "research_intelligence_enabled": bool(self.research_intelligence_enabled),
            "research_intelligence_subsections": 0,
            "claim_evidence_graph_subsections": 0,
            "claim_support_rate_sum": 0.0,
            "claim_support_rate_count": 0,
            "novelty_confidence_sum": 0.0,
            "novelty_confidence_count": 0,
            "evolvable_reviewer_enabled": True,
            "reviewer_score_sum": 0.0,
            "reviewer_score_count": 0,
            "reviewer_high_risk_subsections": 0,
            "reviewer_external_alignment_count": 0,
            "reviewer_external_alignment_failed": 0,
            "self_improving_harness_enabled": bool(self.self_improving_harness_enabled),
            "self_harness_weakness_total": 0,
            "self_harness_proposal_total": 0,
            "self_harness_records": [],
            "memory_optimizer_enabled": bool(self.memory_optimizer_enabled),
            "memory_optimizer_records": [],
            "memory_optimizer_proposal_total": 0,
            "flowernet_full_architecture_layers": [],
            "paired_initial_draft_created_total": 0,
            "paired_initial_draft_reused_total": 0,
            "token_usage": {
                "prompt_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "prompt_cache_hit_tokens": 0,
                "prompt_cache_miss_tokens": 0,
            },
            "quality_score_sum": 0.0,
            "quality_score_count": 0,
            "quality_overall_uncertainty_sum": 0.0,
            "quality_overall_uncertainty_count": 0,
            "quality_dimension_sums": {key: 0.0 for key in self._quality_dimension_keys()},
            "quality_dimension_counts": {key: 0 for key in self._quality_dimension_keys()},
            "quality_weights": {},
            "unieval_available_subsections": 0,
            "unieval_fallback_subsections": 0,
            "bandit_selected_arm_counts": {
                "llm": 0,
                "rule": 0,
                "rule_structured": 0,
                "defect_topic": 0,
                "defect_evidence": 0,
                "defect_novelty": 0,
                "defect_structure": 0,
                "novelty_repair": 0,
                "claim_evidence_repair": 0,
                "citation_grounding_repair": 0,
                "reviewer_risk_repair": 0,
                "external_metric_repair": 0,
                "structure_readability_repair": 0,
                "reproducibility_repair": 0,
            },
            "bandit_reward_sum": 0.0,
            "bandit_reward_count": 0,
            "bandit_reward_avg": 0.0,
            "bandit_drift_events": 0,
            "bandit_drift_triggered_subsections": 0,
            "bandit_last_selected_arm": "",
            "bandit_last_selection_mode": "",
            "bandit_last_constraints": {},
            "bandit_recent_events": [],
            "chapter_assets": [],
            "document_framing": {"success": False, "enabled": self.document_framing_enabled},
        }
        
        start_time_monotonic = time.monotonic()
        
        try:
            content_prompt_map = {
                f"{cp['section_id']}::{cp['subsection_id']}": cp
                for cp in content_prompts
                if cp.get("section_id") and cp.get("subsection_id")
            }

            # 为每个 subsection 创建追踪记录，并以数据库中的正式大纲作为初始值
            for section in structure.get("sections", []):
                section_id = section["id"]
                for subsection in section.get("subsections", []):
                    subsection_id = subsection["id"]
                    prompt_info = content_prompt_map.get(f"{section_id}::{subsection_id}", {})
                    initial_outline = self._resolve_subsection_outline(
                        document_id=document_id,
                        section_id=section_id,
                        subsection_id=subsection_id,
                        fallback_outline=str(
                            prompt_info.get("subsection_outline")
                            or subsection.get("outline")
                            or prompt_info.get("subsection_description")
                            or subsection.get("description")
                            or subsection.get("title", "")
                        ).strip(),
                    )

                    if self.history_manager:
                        self.history_manager.create_subsection_tracking(
                            document_id=document_id,
                            section_id=section_id,
                            subsection_id=subsection_id,
                            outline=initial_outline,
                        )
            
            total_subsections_expected = sum(
                len(sec.get("subsections", []))
                for sec in structure.get("sections", [])
            )
            processed_subsections = 0

            for section in structure.get("sections", []):
                section_id = section["id"]
                section_title = section["title"]
                
                section_result = {
                    "section_id": section_id,
                    "section_title": section_title,
                    "subsections": []
                }
                
                subsection_list = section.get("subsections", [])
                
                for subsection_index, subsection in enumerate(subsection_list):
                    processed_subsections += 1
                    subsection_id = subsection["id"]
                    subsection_title = subsection["title"]
                    prompt_info = content_prompt_map.get(f"{section_id}::{subsection_id}", {})
                    subsection_outline = self._resolve_subsection_outline(
                        document_id=document_id,
                        section_id=section_id,
                        subsection_id=subsection_id,
                        fallback_outline=str(
                            prompt_info.get("subsection_outline")
                            or subsection.get("outline")
                            or prompt_info.get("subsection_description")
                            or subsection.get("description")
                            or subsection_title
                        ).strip(),
                    )
                    content_prompt = str(prompt_info.get("content_prompt") or "").strip()
                    if not content_prompt:
                        content_prompt = f"请你作为专家，写作关于\"{subsection_title}\"的内容。\n\n要求：{subsection_outline}"
                    content_prompt = self._append_canonical_english_task_anchors(content_prompt)
                    
                    print(f"\n📖 生成 Section: {section_title} > Subsection: {subsection_title}")
                    print(f"   (顺序: {subsection_index + 1}/{len(subsection_list)})")
                    self._emit_progress_event(
                        document_id=document_id,
                        section_id=section_id,
                        subsection_id=subsection_id,
                        stage="subsection_trace_ready",
                        message=f"小节上下文已就绪: {section_title} > {subsection_title}",
                        metadata={
                            "section_title": section_title,
                            "subsection_title": subsection_title,
                            "subsection_order": subsection_index + 1,
                            "section_subsection_total": len(subsection_list),
                            "outline_chars": len(subsection_outline),
                            "content_prompt_chars": len(content_prompt),
                        },
                    )
                    self._emit_progress_event(
                        document_id=document_id,
                        section_id=section_id,
                        subsection_id=subsection_id,
                        stage="subsection_start",
                        message=f"开始处理小节: {section_title} > {subsection_title}",
                        metadata={
                            "section_title": section_title,
                            "subsection_title": subsection_title,
                            "subsection_order": subsection_index + 1,
                            "section_subsection_total": len(subsection_list),
                            "enable_controller": True,
                        },
                    )
                    
                    try:
                        passed_history = self._load_passed_history(document_id)
                        subsection_gen_result = self._generate_and_verify_subsection(
                            document_id=document_id,
                            document_title=title,
                            section_title=section_title,
                            section_id=section_id,
                            subsection_id=subsection_id,
                            subsection_title=subsection_title,
                            outline=subsection_outline,
                            initial_prompt=content_prompt,
                            passed_history=passed_history,
                            rel_threshold=rel_threshold,
                            red_threshold=red_threshold,
                        )
                        
                        document_result["total_iterations"] += subsection_gen_result.get("iterations", 0)
                        
                        if subsection_gen_result.get("success"):
                            generated_content = subsection_gen_result.get("draft", "")
                            verification = subsection_gen_result.get("verification", {})
                            self._accumulate_quality_summary(document_result, verification)
                            self._accumulate_bandit_summary(document_result, subsection_gen_result)
                            history_order = len(passed_history)
                            forced_pass = bool(subsection_gen_result.get("forced_pass", False))
                            force_reason = str(subsection_gen_result.get("force_reason", "") or "")
                            best_effort = bool(subsection_gen_result.get("best_effort", False))
                            best_effort_reason = str(subsection_gen_result.get("best_effort_reason", "") or "")
                            generated_content = subsection_gen_result.get("draft", "")
                            subsection_should_fail, subsection_failure_reason = self._subsection_success_failure_reason(
                                subsection_gen_result,
                                allow_forced_pass=self.allow_forced_pass,
                            )
                            controller_triggered = bool(subsection_gen_result.get("controller_triggered", False))
                            controller_retry_count = int(subsection_gen_result.get("controller_retry_count", 0) or 0)
                            rag_used = bool(subsection_gen_result.get("rag_used", False))
                            rag_search_success = bool(subsection_gen_result.get("rag_search_success", False))
                            controller_effective = bool(subsection_gen_result.get("controller_effective", False))
                            metrics = subsection_gen_result.get("metrics", {}) if isinstance(subsection_gen_result, dict) else {}

                            if rag_used:
                                document_result["rag_used_subsections"] += 1
                            if rag_search_success:
                                document_result["rag_search_success_subsections"] += 1
                            document_result["rag_vector_indexed_total"] += int(
                                subsection_gen_result.get("rag_vector_indexed", 0) or 0
                            )
                            if subsection_gen_result.get("rag_vector_backend"):
                                document_result["rag_vector_backend"] = str(subsection_gen_result.get("rag_vector_backend") or "")
                            if subsection_gen_result.get("rag_reranker"):
                                document_result["rag_reranker_used_subsections"] += 1
                            if controller_effective:
                                document_result["controller_effective_subsections"] += 1
                            if controller_triggered:
                                document_result["controller_triggered_subsections"] += 1

                            document_result["verifier_failed_total"] += int(metrics.get("verifier_failed", 0) or 0)
                            document_result["verifier_error_total"] += int(metrics.get("verifier_error", 0) or 0)
                            document_result["controller_calls_total"] += int(metrics.get("controller_calls", 0) or 0)
                            document_result["controller_success_total"] += int(metrics.get("controller_success", 0) or 0)
                            document_result["controller_error_total"] += int(metrics.get("controller_error", 0) or 0)
                            document_result["controller_unavailable_total"] += int(metrics.get("controller_unavailable", 0) or 0)
                            document_result["controller_ineffective_total"] += int(metrics.get("controller_ineffective", 0) or 0)
                            document_result["controller_fallback_outline_total"] += int(metrics.get("controller_fallback_outline", 0) or 0)
                            document_result["controller_exhausted_total"] += int(metrics.get("controller_exhausted", 0) or 0)
                            document_result["generator_short_draft_total"] += int(metrics.get("generator_short_draft", 0) or 0)
                            document_result["source_alignment_selector_used_subsections"] += int(metrics.get("source_alignment_selector_used", 0) or 0)
                            document_result["source_alignment_low_pass_audits"] += int(metrics.get("source_alignment_low_pass_audits", 0) or 0)
                            document_result["paired_initial_draft_created_total"] += int(metrics.get("paired_initial_draft_created", 0) or 0)
                            document_result["paired_initial_draft_reused_total"] += int(metrics.get("paired_initial_draft_reused", 0) or 0)
                            source_alignment = subsection_gen_result.get("source_alignment")
                            if not isinstance(source_alignment, dict):
                                source_alignment = verification.get("source_alignment") if isinstance(verification, dict) else {}
                            if isinstance(source_alignment, dict) and source_alignment.get("source_count", 0):
                                document_result["source_alignment_score_sum"] += float(source_alignment.get("score", 0.0) or 0.0)
                                document_result["source_alignment_score_count"] += 1
                            reviewer_assessment = (
                                verification.get("reviewer_assessment", {})
                                if isinstance(verification, dict) and isinstance(verification.get("reviewer_assessment"), dict)
                                else {}
                            )
                            if reviewer_assessment.get("enabled"):
                                document_result["reviewer_score_sum"] += float(
                                    reviewer_assessment.get("overall_reviewer_score", 0.0) or 0.0
                                )
                                document_result["reviewer_score_count"] += 1
                                dimensions = reviewer_assessment.get("reviewer_dimensions", {})
                                if isinstance(dimensions, dict) and any(
                                    isinstance(dim, dict) and dim.get("risk") == "high"
                                    for dim in dimensions.values()
                                ):
                                    document_result["reviewer_high_risk_subsections"] += 1
                                external_alignment = reviewer_assessment.get("external_alignment", {})
                                if isinstance(external_alignment, dict) and external_alignment.get("available"):
                                    document_result["reviewer_external_alignment_count"] += 1
                                    if external_alignment.get("aligned_with_external_metrics") is False:
                                        document_result["reviewer_external_alignment_failed"] += 1
                            research_intelligence = (
                                subsection_gen_result.get("research_intelligence")
                                if isinstance(subsection_gen_result.get("research_intelligence"), dict)
                                else {}
                            )
                            research_novelty = (
                                research_intelligence.get("novelty", {})
                                if isinstance(research_intelligence.get("novelty"), dict)
                                else {}
                            )
                            claim_evidence_graph = (
                                research_intelligence.get("claim_evidence_graph", {})
                                if isinstance(research_intelligence.get("claim_evidence_graph"), dict)
                                else {}
                            )
                            if research_intelligence.get("enabled"):
                                document_result["research_intelligence_subsections"] += 1
                            if claim_evidence_graph.get("claims"):
                                document_result["claim_evidence_graph_subsections"] += 1
                                document_result["claim_support_rate_sum"] += float(
                                    claim_evidence_graph.get("claim_support_rate", 0.0) or 0.0
                                )
                                document_result["claim_support_rate_count"] += 1
                            if research_novelty.get("literature_gaps"):
                                document_result["novelty_confidence_sum"] += float(
                                    research_novelty.get("novelty_confidence", 0.0) or 0.0
                                )
                                document_result["novelty_confidence_count"] += 1
                            subsection_trace = {
                                "document_id": document_id,
                                "section_id": section_id,
                                "subsection_id": subsection_id,
                                "topic_id": f"{document_id}:{section_id}:{subsection_id}",
                                "status": "ok" if not subsection_should_fail else "failed",
                                "success": not subsection_should_fail,
                                "verification": verification,
                                "bandit": subsection_gen_result.get("bandit", {}),
                                "research_intelligence": research_intelligence,
                                "research_novelty": research_novelty,
                                "claim_evidence_graph": claim_evidence_graph,
                                "report": generated_content,
                                "artifacts": subsection_gen_result.get("experiment_artifacts", []),
                                "source_alignment_score": (
                                    float(source_alignment.get("score", 0.0) or 0.0)
                                    if isinstance(source_alignment, dict)
                                    else 0.0
                                ),
                                "quality_score": (
                                    float(verification.get("quality_score", 0.0) or 0.0)
                                    if isinstance(verification, dict)
                                    else 0.0
                                ),
                                "reviewer_assessment": reviewer_assessment,
                            }
                            self_harness_record = self._build_self_harness_record(subsection_trace)
                            if self_harness_record.get("enabled"):
                                document_result["self_harness_weakness_total"] += int(
                                    self_harness_record.get("weakness_count", 0) or 0
                                )
                                document_result["self_harness_proposal_total"] += int(
                                    self_harness_record.get("proposal_count", 0) or 0
                                )
                                document_result.setdefault("self_harness_records", []).append({
                                    "section_id": section_id,
                                    "subsection_id": subsection_id,
                                    "weakness_count": int(self_harness_record.get("weakness_count", 0) or 0),
                                    "proposal_count": int(self_harness_record.get("proposal_count", 0) or 0),
                                    "status": self_harness_record.get("status", ""),
                                    "weaknesses": self_harness_record.get("weaknesses", [])[:5],
                                    "proposals": self_harness_record.get("proposals", [])[:3],
                                })
                            memory_optimizer_record = self._build_memory_optimizer_record(
                                document_title=title,
                                section_title=section_title,
                                subsection_title=subsection_title,
                                trace={
                                    **subsection_trace,
                                    "weaknesses": self_harness_record.get("weaknesses", []),
                                },
                                source_results=subsection_gen_result.get("source_results", []),
                            )
                            if memory_optimizer_record.get("enabled"):
                                document_result["memory_optimizer_proposal_total"] += 1
                                if not document_result.get("flowernet_full_architecture_layers"):
                                    document_result["flowernet_full_architecture_layers"] = memory_optimizer_record.get("architecture_layers", [])
                                optimizer_proposal = memory_optimizer_record.get("harness_optimizer", {})
                                memory_strategy = (memory_optimizer_record.get("memory", {}) or {}).get("topic_strategy", {})
                                document_result.setdefault("memory_optimizer_records", []).append({
                                    "section_id": section_id,
                                    "subsection_id": subsection_id,
                                    "domain": memory_strategy.get("domain", ""),
                                    "recommended_flow_order": optimizer_proposal.get("recommended_flow_order", []),
                                    "held_out_required": bool(optimizer_proposal.get("held_out_required", False)),
                                    "status": optimizer_proposal.get("status", ""),
                                })
                            for token_key in [
                                "prompt_tokens",
                                "output_tokens",
                                "total_tokens",
                                "prompt_cache_hit_tokens",
                                "prompt_cache_miss_tokens",
                            ]:
                                document_result["token_usage"][token_key] += int(metrics.get(token_key, 0) or 0)
                            
                            if self.history_manager and (not subsection_should_fail):
                                self.history_manager.add_entry(
                                    document_id=document_id,
                                    section_id=section_id,
                                    subsection_id=subsection_id,
                                    content=generated_content,
                                    metadata={
                                        "iterations": subsection_gen_result.get("iterations", 0),
                                        "verification": verification,
                                        "outline": subsection_gen_result.get("final_outline", subsection_outline),
                                        "forced_pass": forced_pass,
                                        "force_reason": force_reason,
                                        "best_effort": best_effort,
                                        "best_effort_reason": best_effort_reason,
                                        "controller_triggered": controller_triggered,
                                        "controller_retry_count": controller_retry_count,
                                        "source_alignment": source_alignment,
                                        "research_intelligence": research_intelligence,
                                        "self_harness_record": self_harness_record,
                                        "memory_optimizer_record": memory_optimizer_record,
                                        "source_results": subsection_gen_result.get("source_results", []),
                                        "pass_candidate_audit": subsection_gen_result.get("pass_candidate_audit", []),
                                    }
                                )
                                self.history_manager.add_passed_history(
                                    document_id=document_id,
                                    section_id=section_id,
                                    subsection_id=subsection_id,
                                    content=generated_content,
                                    order_index=history_order
                                )

                            if subsection_should_fail:
                                document_result["failed_subsections"].append({
                                    "section_id": section_id,
                                    "subsection_id": subsection_id,
                                    "reason": subsection_failure_reason or force_reason or "subsection_verification_failed",
                                    "iterations": subsection_gen_result.get("iterations", 0),
                                })
                                self._emit_progress_event(
                                    document_id=document_id,
                                    section_id=section_id,
                                    subsection_id=subsection_id,
                                    stage="subsection_failed",
                                    message=f"小节未通过: {section_title} > {subsection_title}",
                                    metadata={
                                        "iterations": subsection_gen_result.get("iterations", 0),
                                        "verification": verification,
                                        "forced_pass": forced_pass,
                                        "force_reason": force_reason,
                                        "failure_reason": subsection_failure_reason,
                                    },
                                )
                            else:
                                document_result["passed_subsections"] += 1
                            if not subsection_should_fail:
                                self._emit_progress_event(
                                    document_id=document_id,
                                    section_id=section_id,
                                    subsection_id=subsection_id,
                                    stage="subsection_passed",
                                    message=f"小节通过验证: {section_title} > {subsection_title}",
                                    metadata={
                                        "iterations": subsection_gen_result.get("iterations", 0),
                                        "verification": verification,
                                        "forced_pass": forced_pass,
                                        "force_reason": force_reason,
                                        "best_effort": best_effort,
                                        "best_effort_reason": best_effort_reason,
                                        "controller_triggered": controller_triggered,
                                        "controller_retry_count": controller_retry_count,
                                        "source_alignment": source_alignment,
                                        "pass_candidate_audit": subsection_gen_result.get("pass_candidate_audit", []),
                                    },
                                )
                            
                            section_result["subsections"].append({
                                "subsection_id": subsection_id,
                                "subsection_title": subsection_title,
                                "content": generated_content,
                                "outline": subsection_gen_result.get("final_outline", subsection_outline),
                                "success": not subsection_should_fail,
                                "iterations": subsection_gen_result.get("iterations", 0),
                                "verification": verification,
                                "bandit": subsection_gen_result.get("bandit", {}),
                                "forced_pass": forced_pass,
                                "force_reason": force_reason,
                                "best_effort": best_effort,
                                "best_effort_reason": best_effort_reason,
                                "controller_triggered": controller_triggered,
                                "controller_retry_count": controller_retry_count,
                                "rag_used": rag_used,
                                "rag_search_success": rag_search_success,
                                "rag_selected_query": str(subsection_gen_result.get("rag_selected_query", "") or ""),
                                "rag_vector_indexed": int(subsection_gen_result.get("rag_vector_indexed", 0) or 0),
                                "rag_vector_backend": str(subsection_gen_result.get("rag_vector_backend", "") or ""),
                                "rag_reranker": str(subsection_gen_result.get("rag_reranker", "") or ""),
                                "controller_effective": controller_effective,
                                "source_alignment": source_alignment,
                                "research_intelligence": research_intelligence,
                                "research_novelty": research_novelty,
                                "claim_evidence_graph": claim_evidence_graph,
                                "self_harness_record": self_harness_record,
                                "source_results": subsection_gen_result.get("source_results", []),
                                "pass_candidate_audit": subsection_gen_result.get("pass_candidate_audit", []),
                                "token_usage": {
                                    "prompt_tokens": int(metrics.get("prompt_tokens", 0) or 0),
                                    "output_tokens": int(metrics.get("output_tokens", 0) or 0),
                                    "total_tokens": int(metrics.get("total_tokens", 0) or 0),
                                    "prompt_cache_hit_tokens": int(metrics.get("prompt_cache_hit_tokens", 0) or 0),
                                    "prompt_cache_miss_tokens": int(metrics.get("prompt_cache_miss_tokens", 0) or 0),
                                },
                                "length": len(generated_content)
                            })
                            
                        else:
                            err = subsection_gen_result.get("error", "Unknown error")
                            print(f"⚠️ 当前小节返回失败结果: {err}")
                            failed_draft = str(subsection_gen_result.get("draft", "") or "").strip()
                            # Failed attempts are still real experimental observations. Preserve
                            # their RAG, Verifier, Controller, Bandit, and token telemetry.
                            self._accumulate_subsection_telemetry(document_result, subsection_gen_result)
                            if (
                                self.accept_best_real_draft
                                and self._is_usable_real_draft(failed_draft, subsection_outline)
                                and self._best_real_draft_quality_ok(subsection_gen_result.get("verification", {}) or {}, rel_threshold, red_threshold)
                            ):
                                verification = dict(subsection_gen_result.get("verification", {}) or {})
                                verification.update({
                                    "accepted_by_best_real_draft": True,
                                    "best_real_draft_reason": str(err),
                                    "forced_pass": False,
                                })
                                metrics = subsection_gen_result.get("metrics", {}) if isinstance(subsection_gen_result, dict) else {}
                                self_harness_record = self._build_self_harness_record({
                                    "document_id": document_id,
                                    "section_id": section_id,
                                    "subsection_id": subsection_id,
                                    "topic_id": f"{document_id}:{section_id}:{subsection_id}",
                                    "status": "best_effort",
                                    "success": True,
                                    "error": str(err),
                                    "verification": verification,
                                    "bandit": subsection_gen_result.get("bandit", {}),
                                    "research_intelligence": subsection_gen_result.get("research_intelligence", {}),
                                })
                                if self_harness_record.get("enabled"):
                                    document_result["self_harness_weakness_total"] += int(self_harness_record.get("weakness_count", 0) or 0)
                                    document_result["self_harness_proposal_total"] += int(self_harness_record.get("proposal_count", 0) or 0)
                                    document_result.setdefault("self_harness_records", []).append({
                                        "section_id": section_id,
                                        "subsection_id": subsection_id,
                                        "weakness_count": int(self_harness_record.get("weakness_count", 0) or 0),
                                        "proposal_count": int(self_harness_record.get("proposal_count", 0) or 0),
                                        "status": self_harness_record.get("status", ""),
                                        "weaknesses": self_harness_record.get("weaknesses", [])[:5],
                                        "proposals": self_harness_record.get("proposals", [])[:3],
                                    })
                                document_result["passed_subsections"] += 1
                                if self.history_manager:
                                    history_order = len(passed_history)
                                    self.history_manager.add_entry(
                                        document_id=document_id,
                                        section_id=section_id,
                                        subsection_id=subsection_id,
                                        content=failed_draft,
                                        metadata={
                                            "iterations": subsection_gen_result.get("iterations", 0),
                                            "verification": verification,
                                            "outline": subsection_gen_result.get("final_outline", subsection_outline),
                                            "best_effort": True,
                                            "best_effort_reason": str(err),
                                            "source_results": subsection_gen_result.get("source_results", []),
                                        },
                                    )
                                    self.history_manager.add_passed_history(
                                        document_id=document_id,
                                        section_id=section_id,
                                        subsection_id=subsection_id,
                                        content=failed_draft,
                                        order_index=history_order,
                                    )
                                section_result["subsections"].append({
                                    "subsection_id": subsection_id,
                                    "subsection_title": subsection_title,
                                    "content": failed_draft,
                                    "outline": subsection_gen_result.get("final_outline", subsection_outline),
                                    "success": True,
                                    "iterations": subsection_gen_result.get("iterations", 0),
                                    "verification": verification,
                                    "bandit": subsection_gen_result.get("bandit", {}),
                                    "forced_pass": False,
                                    "force_reason": "",
                                    "best_effort": True,
                                    "best_effort_reason": str(err),
                                    "rag_used": bool(subsection_gen_result.get("rag_used", False)),
                                    "rag_search_success": bool(subsection_gen_result.get("rag_search_success", False)),
                                    "rag_vector_indexed": int(subsection_gen_result.get("rag_vector_indexed", 0) or 0),
                                    "rag_vector_backend": str(subsection_gen_result.get("rag_vector_backend", "") or ""),
                                    "rag_reranker": str(subsection_gen_result.get("rag_reranker", "") or ""),
                                    "controller_effective": bool(subsection_gen_result.get("controller_effective", False)),
                                    "self_harness_record": self_harness_record,
                                    "source_results": subsection_gen_result.get("source_results", []),
                                    "token_usage": {
                                        "prompt_tokens": int(metrics.get("prompt_tokens", 0) or 0),
                                        "output_tokens": int(metrics.get("output_tokens", 0) or 0),
                                        "total_tokens": int(metrics.get("total_tokens", 0) or 0),
                                        "prompt_cache_hit_tokens": int(metrics.get("prompt_cache_hit_tokens", 0) or 0),
                                        "prompt_cache_miss_tokens": int(metrics.get("prompt_cache_miss_tokens", 0) or 0),
                                    },
                                    "length": len(failed_draft),
                                })
                                self._emit_progress_event(
                                    document_id=document_id,
                                    section_id=section_id,
                                    subsection_id=subsection_id,
                                    stage="subsection_passed",
                                    message=f"小节保留最佳真实草稿: {section_title} > {subsection_title}",
                                    metadata={
                                        "iterations": subsection_gen_result.get("iterations", 0),
                                        "verification": verification,
                                        "best_effort": True,
                                        "best_effort_reason": str(err),
                                    },
                                )
                                continue
                            self_harness_record = self._build_self_harness_record({
                                "document_id": document_id,
                                "section_id": section_id,
                                "subsection_id": subsection_id,
                                "topic_id": f"{document_id}:{section_id}:{subsection_id}",
                                "status": "failed",
                                "success": False,
                                "error": str(err),
                                "verification": subsection_gen_result.get("verification", {}),
                                "bandit": subsection_gen_result.get("bandit", {}),
                                "research_intelligence": subsection_gen_result.get("research_intelligence", {}),
                            })
                            if self_harness_record.get("enabled"):
                                document_result["self_harness_weakness_total"] += int(self_harness_record.get("weakness_count", 0) or 0)
                                document_result["self_harness_proposal_total"] += int(self_harness_record.get("proposal_count", 0) or 0)
                                document_result.setdefault("self_harness_records", []).append({
                                    "section_id": section_id,
                                    "subsection_id": subsection_id,
                                    "weakness_count": int(self_harness_record.get("weakness_count", 0) or 0),
                                    "proposal_count": int(self_harness_record.get("proposal_count", 0) or 0),
                                    "status": self_harness_record.get("status", ""),
                                    "weaknesses": self_harness_record.get("weaknesses", [])[:5],
                                    "proposals": self_harness_record.get("proposals", [])[:3],
                                })
                            section_result["subsections"].append({
                                "subsection_id": subsection_id,
                                "subsection_title": subsection_title,
                                "content": failed_draft,
                                "outline": subsection_outline,
                                "success": False,
                                "iterations": subsection_gen_result.get("iterations", 0),
                                "verification": subsection_gen_result.get("verification", {}),
                                "bandit": subsection_gen_result.get("bandit", {}),
                                "forced_pass": False,
                                "force_reason": "",
                                "rag_used": bool(subsection_gen_result.get("rag_used", False)),
                                "rag_search_success": bool(subsection_gen_result.get("rag_search_success", False)),
                                "rag_vector_indexed": int(subsection_gen_result.get("rag_vector_indexed", 0) or 0),
                                "rag_vector_backend": str(subsection_gen_result.get("rag_vector_backend", "") or ""),
                                "rag_reranker": str(subsection_gen_result.get("rag_reranker", "") or ""),
                                "controller_effective": bool(subsection_gen_result.get("controller_effective", False)),
                                "self_harness_record": self_harness_record,
                                "source_results": subsection_gen_result.get("source_results", []),
                                "length": len(failed_draft),
                            })
                            document_result["failed_subsections"].append({
                                "section_id": section_id,
                                "subsection_id": subsection_id,
                                "reason": str(err),
                                "iterations": subsection_gen_result.get("iterations", 0),
                            })
                            self._emit_progress_event(
                                document_id=document_id,
                                section_id=section_id,
                                subsection_id=subsection_id,
                                stage="subsection_failed",
                                message=f"小节生成失败: {section_title} > {subsection_title}",
                                metadata={"error": err},
                            )
                    
                    except Exception as e:
                        print(f"⚠️ 小节生成异常，记录失败并继续文档流程: {e}")
                        error_str = str(e)[:200]
                        section_result["subsections"].append({
                            "subsection_id": subsection_id,
                            "subsection_title": subsection_title,
                            "content": "",
                            "outline": subsection_outline,
                            "success": False,
                            "iterations": 0,
                            "verification": {},
                            "bandit": {},
                            "forced_pass": False,
                            "force_reason": "",
                            "length": 0,
                        })
                        document_result["failed_subsections"].append({
                            "section_id": section_id,
                            "subsection_id": subsection_id,
                            "reason": error_str or "subsection_exception",
                            "iterations": 0,
                        })
                        self._emit_progress_event(
                            document_id=document_id,
                            section_id=section_id,
                            subsection_id=subsection_id,
                            stage="subsection_failed",
                            message=f"小节异常失败: {section_title} > {subsection_title}",
                            metadata={"error": error_str},
                        )
                        continue
                
                chapter_assets = self._plan_chapter_assets(
                    document_id=document_id,
                    section_id=section_id,
                    section_title=section_title,
                    section_result=section_result,
                    document_title=title,
                    user_background=user_background,
                    user_requirements=user_requirements,
                )
                section_result["chapter_assets"] = chapter_assets
                document_result.setdefault("chapter_assets", []).extend(chapter_assets)
                document_result["sections"].append(section_result)

            document_framing = self._generate_document_framing(
                title=title,
                sections=document_result["sections"],
                task_requirements=user_requirements,
            )
            document_result["document_framing"] = document_framing
            for token_key in document_result["token_usage"]:
                document_result["token_usage"][token_key] += int(
                    (document_framing.get("token_usage") or {}).get(token_key, 0) or 0
                )
            
            elapsed = time.monotonic() - start_time_monotonic
            document_result["generation_time"] = f"{elapsed:.2f}s"
            
            # 计算总小节数
            total_subsections_expected = sum(
                len(section.get("subsections", []))
                for section in structure.get("sections", [])
            )
            total_subsections_generated = (
                document_result["passed_subsections"] + 
                len(document_result["failed_subsections"])
            )

            quality_score_count = max(1, int(document_result.get("quality_score_count", 0) or 0))
            overall_uncertainty_count = max(1, int(document_result.get("quality_overall_uncertainty_count", 0) or 0))
            document_result["quality_score_avg"] = round(
                float(document_result.get("quality_score_sum", 0.0) or 0.0) / quality_score_count,
                4,
            )
            document_result["quality_overall_uncertainty_avg"] = round(
                float(document_result.get("quality_overall_uncertainty_sum", 0.0) or 0.0) / overall_uncertainty_count,
                4,
            )
            document_result["quality_dimension_avgs"] = {
                key: round(
                    float(document_result.get("quality_dimension_sums", {}).get(key, 0.0) or 0.0)
                    / max(1, int(document_result.get("quality_dimension_counts", {}).get(key, 0) or 0)),
                    4,
                )
                for key in self._quality_dimension_keys()
            }
            document_result["unieval_available_ratio"] = round(
                float(document_result.get("unieval_available_subsections", 0) or 0) / max(1, total_subsections_generated),
                4,
            )
            document_result["unieval_fallback_ratio"] = round(
                float(document_result.get("unieval_fallback_subsections", 0) or 0) / max(1, total_subsections_generated),
                4,
            )
            document_result["source_alignment_score_avg"] = round(
                float(document_result.get("source_alignment_score_sum", 0.0) or 0.0)
                / max(1, int(document_result.get("source_alignment_score_count", 0) or 0)),
                4,
            )
            document_result["claim_support_rate_avg"] = round(
                float(document_result.get("claim_support_rate_sum", 0.0) or 0.0)
                / max(1, int(document_result.get("claim_support_rate_count", 0) or 0)),
                4,
            )
            document_result["novelty_confidence_avg"] = round(
                float(document_result.get("novelty_confidence_sum", 0.0) or 0.0)
                / max(1, int(document_result.get("novelty_confidence_count", 0) or 0)),
                4,
            )
            document_result["reviewer_score_avg"] = round(
                float(document_result.get("reviewer_score_sum", 0.0) or 0.0)
                / max(1, int(document_result.get("reviewer_score_count", 0) or 0)),
                4,
            )
            document_result["reviewer_high_risk_ratio"] = round(
                float(document_result.get("reviewer_high_risk_subsections", 0) or 0)
                / max(1, int(document_result.get("reviewer_score_count", 0) or 0)),
                4,
            )
            document_result["reviewer_external_alignment_failed_ratio"] = round(
                float(document_result.get("reviewer_external_alignment_failed", 0) or 0)
                / max(1, int(document_result.get("reviewer_external_alignment_count", 0) or 0)),
                4,
            )
            # Keep current-document Bandit statistics strictly isolated. Recent
            # history may be exposed separately for UI diagnostics, but must
            # never replace paper-grade per-document arm/reward counts.
            try:
                recent_stats = self._load_recent_bandit_stats(max_events=300)
                controller_calls_current_doc = int(document_result.get("controller_calls_total", 0) or 0)
                if controller_calls_current_doc > 0 and int(document_result.get("bandit_reward_count", 0) or 0) == 0:
                    document_result["bandit_stats_scope"] = "current_document_missing_controller_payload"
                elif controller_calls_current_doc == 0:
                    document_result["bandit_stats_scope"] = "current_document_no_controller_calls"
                else:
                    document_result["bandit_stats_scope"] = "current_document"
                document_result["bandit_recent_history"] = recent_stats
            except Exception:
                document_result["bandit_stats_scope"] = "current_document_history_unavailable"

            document_result["bandit_reward_avg"] = round(float(document_result.get("bandit_reward_avg", 0.0) or 0.0), 4)
            document_result["bandit_drift_trigger_rate"] = round(
                float(document_result.get("bandit_drift_triggered_subsections", 0) or 0) / max(1, total_subsections_generated),
                4,
            )
            cache_hits = int(document_result.get("token_usage", {}).get("prompt_cache_hit_tokens", 0) or 0)
            cache_misses = int(document_result.get("token_usage", {}).get("prompt_cache_miss_tokens", 0) or 0)
            document_result["prompt_cache_hit_rate"] = round(cache_hits / max(1, cache_hits + cache_misses), 4)

            # 文档级成功判定：存在失败小节则返回 partial/failed，避免掩盖真实质量问题
            framing_ok = bool(document_result.get("document_framing", {}).get("success", False))
            document_result["success"] = len(document_result["failed_subsections"]) == 0 and framing_ok
            if not document_result["success"]:
                failure_parts = []
                for item in document_result.get("failed_subsections", [])[:3]:
                    if not isinstance(item, dict):
                        continue
                    section_id = str(item.get("section_id") or "").strip()
                    subsection_id = str(item.get("subsection_id") or "").strip()
                    reason = str(item.get("reason") or item.get("error") or "subsection_failed").strip()
                    label = "::".join(part for part in (section_id, subsection_id) if part)
                    failure_parts.append(f"{label}: {reason}" if label else reason)
                if not framing_ok:
                    framing_reason = str(
                        document_result.get("document_framing", {}).get("error")
                        or "document_framing_failed"
                    )
                    failure_parts.append(f"document_framing: {framing_reason}")
                document_result["error"] = (
                    "subsection_failures: " + " | ".join(failure_parts)
                    if failure_parts
                    else "document_generation_failed"
                )
            
            print(f"\n{'='*70}")
            print(f"{'✅' if document_result['success'] else '❌'} 文档生成完成！")
            print(f"   - 预期小节数: {total_subsections_expected}")
            print(f"   - 实际生成: {total_subsections_generated}")
            print(f"   - 通过: {document_result['passed_subsections']}")
            print(f"   - 失败: {len(document_result['failed_subsections'])}")
            print(f"   - 兜底通过: {len(document_result['forced_subsections'])}")
            print(f"   - RAG 命中: {document_result['rag_search_success_subsections']}/{len(content_prompts)}")
            print(f"   - RAG 使用: {document_result['rag_used_subsections']}/{len(content_prompts)}")
            print(f"   - Controller 有效: {document_result['controller_effective_subsections']}/{len(content_prompts)}")
            print(f"   - Controller 触发小节: {document_result['controller_triggered_subsections']}/{len(content_prompts)}")
            print(f"   - Controller 调用总数: {document_result['controller_calls_total']}")
            print(f"   - 短草稿重写: {document_result['generator_short_draft_total']}")
            print(f"   - Token usage: {document_result['token_usage']}")
            print(f"   - Prompt cache hit rate: {document_result['prompt_cache_hit_rate']}")
            print(f"   - 总迭代: {document_result['total_iterations']} 次")
            print(f"   - UniEval 平均分: {document_result['quality_score_avg']}")
            if self.source_alignment_selector_enabled:
                print(f"   - Source alignment 平均分: {document_result['source_alignment_score_avg']}")
            print(f"   - Bandit 平均奖励: {document_result['bandit_reward_avg']}")
            print(f"   - 耗时: {document_result['generation_time']}")
            print(f"{'='*70}")
            self._emit_progress_event(
                document_id=document_id,
                stage="document_complete",
                message=(
                    f"文档流程结束：通过 {document_result['passed_subsections']}，"
                    f"失败 {len(document_result['failed_subsections'])}"
                ),
                metadata={
                    "success": document_result["success"],
                    "passed_subsections": document_result["passed_subsections"],
                    "failed_subsections": len(document_result["failed_subsections"]),
                    "forced_subsections": len(document_result["forced_subsections"]),
                    "total_iterations": document_result["total_iterations"],
                    "controller_calls_total": document_result["controller_calls_total"],
                    "generator_short_draft_total": document_result["generator_short_draft_total"],
                    "token_usage": document_result["token_usage"],
                    "prompt_cache_hit_rate": document_result["prompt_cache_hit_rate"],
                    "controller_triggered_subsections": document_result["controller_triggered_subsections"],
                    "verifier_failed_total": document_result["verifier_failed_total"],
                    "verifier_error_total": document_result["verifier_error_total"],
                },
            )
            
            return document_result
            
        except Exception as e:
            print(f"❌ 文档生成失败: {e}")
            import traceback
            traceback.print_exc()
            
            return {
                "success": False,
                "document_id": document_id,
                "title": title,
                "sections": document_result.get("sections", []),
                "passed_subsections": document_result.get("passed_subsections", 0),
                "failed_subsections": document_result.get("failed_subsections", []),
                "forced_subsections": document_result.get("forced_subsections", []),
                "total_iterations": document_result.get("total_iterations", 0),
                "generation_time": document_result.get("generation_time"),
                "rag_used_subsections": document_result.get("rag_used_subsections", 0),
                "rag_search_success_subsections": document_result.get("rag_search_success_subsections", 0),
                "controller_effective_subsections": document_result.get("controller_effective_subsections", 0),
                "controller_triggered_subsections": document_result.get("controller_triggered_subsections", 0),
                "verifier_failed_total": document_result.get("verifier_failed_total", 0),
                "controller_calls_total": document_result.get("controller_calls_total", 0),
                "controller_success_total": document_result.get("controller_success_total", 0),
                "controller_error_total": document_result.get("controller_error_total", 0),
                "controller_unavailable_total": document_result.get("controller_unavailable_total", 0),
                "controller_ineffective_total": document_result.get("controller_ineffective_total", 0),
                "controller_fallback_outline_total": document_result.get("controller_fallback_outline_total", 0),
                "controller_exhausted_total": document_result.get("controller_exhausted_total", 0),
                "generator_short_draft_total": document_result.get("generator_short_draft_total", 0),
                "verifier_error_total": document_result.get("verifier_error_total", 0),
                "token_usage": document_result.get("token_usage", {}),
                "prompt_cache_hit_rate": document_result.get("prompt_cache_hit_rate", 0.0),
                # Include quality metrics fields to avoid zero defaults on frontend
                "quality_score_avg": float(document_result.get("quality_score_avg", 0.0) or 0.0),
                "quality_overall_uncertainty_avg": float(document_result.get("quality_overall_uncertainty_avg", 0.0) or 0.0),
                "quality_dimension_avgs": document_result.get("quality_dimension_avgs", {}),
                "quality_weights": document_result.get("quality_weights", {}),
                "unieval_available_subsections": int(document_result.get("unieval_available_subsections", 0) or 0),
                "unieval_fallback_subsections": int(document_result.get("unieval_fallback_subsections", 0) or 0),
                "unieval_available_ratio": float(document_result.get("unieval_available_ratio", 0.0) or 0.0),
                "unieval_fallback_ratio": float(document_result.get("unieval_fallback_ratio", 0.0) or 0.0),
                "bandit_selected_arm_counts": document_result.get("bandit_selected_arm_counts", {}),
                "bandit_reward_sum": float(document_result.get("bandit_reward_sum", 0.0) or 0.0),
                "bandit_reward_count": int(document_result.get("bandit_reward_count", 0) or 0),
                "bandit_reward_avg": float(document_result.get("bandit_reward_avg", 0.0) or 0.0),
                "bandit_drift_events": int(document_result.get("bandit_drift_events", 0) or 0),
                "bandit_drift_triggered_subsections": int(document_result.get("bandit_drift_triggered_subsections", 0) or 0),
                "bandit_drift_trigger_rate": float(document_result.get("bandit_drift_trigger_rate", 0.0) or 0.0),
                "bandit_last_selected_arm": str(document_result.get("bandit_last_selected_arm", "") or ""),
                "bandit_last_selection_mode": str(document_result.get("bandit_last_selection_mode", "") or ""),
                "bandit_last_constraints": document_result.get("bandit_last_constraints", {}),
                "bandit_recent_events": document_result.get("bandit_recent_events", []),
                "chapter_assets": document_result.get("chapter_assets", []),
                "error": str(e),
                "warning": f"document_exception_fallback: {str(e)[:180]}",
            }
    
    def _generate_and_verify_subsection(
        self,
        document_id: str,
        section_id: str,
        subsection_id: str,
        subsection_title: str,
        outline: str,
        initial_prompt: str,
        passed_history: List[Dict[str, str]],
        document_title: str = "",
        section_title: str = "",
        rel_threshold: float = 0.50,
        red_threshold: float = 0.75,
    ) -> Dict[str, Any]:
        """
        生成单个 subsection 的完整循环（第二步和第三步）
        
        流程：
        1. Generator 根据大纲和已通过历史生成内容
        2. Verifier 验证
        3. 如果不通过，Controller 修改大纲
        4. 循环回步骤1
        """
        
        current_prompt = initial_prompt
        current_outline = outline
        seen_controller_outlines = {" ".join(str(outline or "").strip().split()).lower()}
        # Controller repairs may add operational instructions to the generation
        # plan, but they must not move the Verifier's original task target.
        verification_outline_base = str(outline or "").strip()
        verification_target_base = (
            " ".join(str(subsection_title or "").split()).strip()
            or self._extract_rag_gate_focus(self._extract_rag_focus(outline, initial_prompt))
        )
        canonical_verification_anchors = self._canonical_english_requirements(initial_prompt)
        canonical_verification_anchors = self._scope_requirements_to_subsection(
            canonical_verification_anchors,
            f"{subsection_title}\n{outline}",
        )
        if canonical_verification_anchors and canonical_verification_anchors.lower() not in verification_target_base.lower():
            verification_target_base = (
                f"{verification_target_base}\nCanonical English task anchors: "
                f"{canonical_verification_anchors}"
            ).strip()
        iterations = 0
        all_drafts = []
        controller_triggered = False
        controller_retry_count = 0
        controller_last_result: Dict[str, Any] = {}
        controller_effective = False
        any_controller_effective = False
        bandit_debug: Dict[str, Any] = {"events": []}
        pending_controller_outcome: Optional[Dict[str, Any]] = None
        # Realized outcomes, not proposal scores, drive this subsection-local
        # cooldown. It resets naturally for every new process_subsection call.
        controller_excluded_arms: set[str] = set()
        revision_draft = ""
        revision_arm = ""
        best_candidate: Optional[Dict[str, Any]] = None
        best_pass_candidate: Optional[Dict[str, Any]] = None
        accepted_candidates = 0
        pass_candidate_audit: List[Dict[str, Any]] = []
        # A fixed best-of-N policy spends another LLM call even when the first
        # strict pass is already comfortably above every gate.  Full therefore
        # defaults to an adaptive policy: one strong pass is enough, while a
        # threshold-near pass earns one additional candidate for comparison.
        min_accepted_candidates = max(1, int(os.getenv("FLOWERNET_FULL_MIN_ACCEPTED_CANDIDATES", "1")))
        max_accepted_candidates = max(
            min_accepted_candidates,
            int(os.getenv("FLOWERNET_FULL_MAX_ACCEPTED_CANDIDATES", "2")),
        )
        adaptive_best_of = str(os.getenv("FLOWERNET_FULL_ADAPTIVE_BEST_OF", "true")).strip().lower() in {
            "1", "true", "yes", "on",
        }
        generator_failure_streak = 0
        generator_degraded_mode = False
        metrics: Dict[str, int] = {
            "verifier_failed": 0,
            "controller_calls": 0,
            "controller_success": 0,
            "controller_error": 0,
            "controller_unavailable": 0,
            "controller_ineffective": 0,
            "controller_fallback_outline": 0,
            "controller_exhausted": 0,
            "generator_degraded_mode": 0,
            "generator_short_draft": 0,
            "verifier_error": 0,
            "prompt_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": 0,
            "source_alignment_selector_used": 0,
            "source_alignment_low_pass_audits": 0,
            "evidence_rag_refreshes": 0,
            "controller_targeted_revision_attempts": 0,
            "controller_strategy_only_repairs": 0,
            "paired_initial_draft_created": 0,
            "paired_initial_draft_reused": 0,
        }
        
        # 应用历史窗口：只使用最近N个小节（避免历史过长导致冗余度计算失真）
        windowed_history = passed_history[-self.history_window_size:] if passed_history else []
        
        # 构建历史文本
        if windowed_history:
            history_text = "\n\n---\n\n".join([h["content"] for h in windowed_history])
        else:
            history_text = ""
        
        total_history_count = len(passed_history)
        windowed_count = len(windowed_history)
        print(f"   📜 已通过的前置内容数: {total_history_count} (使用最近 {windowed_count} 个小节)")

        subsection_started_at = time.monotonic()
        rag_search_result: Dict[str, Any] = {"success": False, "results": []}
        rag_context = ""
        require_source_citations = False
        source_citation_required = False
        rag_used = False
        rag_selected_query = ""
        last_negative_constraints: Optional[Dict[str, Any]] = None

        if self.rag_enabled and self.search_engine is not None:
            structured_focus = " ".join(str(subsection_title or "").split()).strip()
            rag_outline = (
                f'Focus on "{structured_focus}". {current_outline}'
                if structured_focus
                else current_outline
            )
            rag_focus = self._extract_rag_focus(rag_outline, current_prompt)
            rag_gate_focus = structured_focus or self._extract_rag_gate_focus(rag_focus)
            rag_semantic_topic = f"{document_title} {rag_gate_focus}".strip()
            rag_query_candidates = self._build_rag_query_candidates(
                outline=rag_outline,
                initial_prompt=current_prompt,
                document_title=document_title,
            )
            rag_source_pool_key = self._rag_source_pool_key(
                document_title=document_title,
                subsection_title=subsection_title,
                outline=rag_outline,
                initial_prompt=initial_prompt,
            )
            fixed_rag_source_pool = self._get_fixed_rag_source_pool(rag_source_pool_key)
            used_fixed_rag_source_pool = False
            selected_query = ""
            rag_error = "unknown"
            candidate_result_sets: List[Tuple[float, str, Dict[str, Any]]] = []
            if fixed_rag_source_pool:
                used_fixed_rag_source_pool = True
                cached_query = str(fixed_rag_source_pool.get("query") or (rag_query_candidates[0] if rag_query_candidates else current_outline))
                fixed_rag_source_pool["results"] = self._filter_sources_by_temporal_scope(
                    fixed_rag_source_pool.get("results", []),
                    f"{document_title} {initial_prompt}",
                )
                if fixed_rag_source_pool.get("results"):
                    candidate_result_sets.append((9999.0, cached_query, fixed_rag_source_pool))
            else:
                for rag_query in rag_query_candidates:
                    candidate_result = self.search_engine.search(rag_query)
                    if candidate_result.get("success") and candidate_result.get("results"):
                        filtered_sources = self._filter_rag_source_metadata(
                            candidate_result.get("results", [])
                        )
                        filtered_sources = self._filter_sources_by_temporal_scope(
                            filtered_sources,
                            f"{document_title} {initial_prompt}",
                        )
                        citable_sources = self._filter_citable_source_alignment(
                            filtered_sources,
                            topic=f"{rag_semantic_topic} {rag_query}",
                            min_semantic_score=float(os.getenv("MIN_SEMANTIC_SOURCE_SCORE", "0.35")),
                        )
                        citable_sources = self._preserve_multi_axis_source_diversity(
                            f"{rag_semantic_topic} {rag_query}",
                            citable_sources,
                            citable_sources,
                            max_items=self.rag_max_results,
                        )
                        candidate_result["metadata_filtered_count"] = (
                            len(candidate_result.get("results", [])) - len(filtered_sources)
                        )
                        candidate_result["semantic_filtered_count"] = len(filtered_sources) - len(citable_sources)
                        candidate_result["results"] = (
                            citable_sources if len(citable_sources) >= min(2, self.rag_min_citations) else []
                        )
                    if candidate_result.get("success") and candidate_result.get("results"):
                        set_score = self._score_rag_result_set(
                            document_title,
                            rag_focus,
                            candidate_result.get("results", []),
                        )
                        selection_score = set_score
                        if re.search(
                            r"\bopen[- ]source large language models\b.*\b(open source software ecosystem|licensing|deployment|quantization|governance)\b",
                            str(rag_query or "").lower(),
                        ):
                            selection_score += float(os.getenv("FLOWERNET_OPEN_SOURCE_LLM_QUERY_PRIORITY_BONUS", "0.28"))
                        candidate_result["result_set_score"] = set_score
                        candidate_result["result_selection_score"] = round(selection_score, 4)
                        candidate_result_sets.append((selection_score, rag_query, candidate_result))
                    else:
                        rag_error = str(candidate_result.get("error", "unknown"))

                # Revalidate already retrieved evidence from another subsection
                # of this document before degrading to no-RAG generation.
                if not candidate_result_sets:
                    for sibling_pool in self._document_rag_source_pool_candidates(
                        document_title,
                        exclude_key=rag_source_pool_key,
                    ):
                        sibling_sources = self._filter_rag_source_metadata(
                            sibling_pool.get("results", [])
                        )
                        sibling_sources = self._filter_sources_by_temporal_scope(
                            sibling_sources,
                            f"{document_title} {initial_prompt}",
                        )
                        sibling_sources = self._filter_citable_source_alignment(
                            sibling_sources,
                            topic=f"{rag_semantic_topic} {rag_query_candidates[0] if rag_query_candidates else ''}",
                            min_semantic_score=float(os.getenv("MIN_SEMANTIC_SOURCE_SCORE", "0.35")),
                        )
                        sibling_sources = self._preserve_multi_axis_source_diversity(
                            rag_semantic_topic,
                            sibling_sources,
                            sibling_sources,
                            max_items=self.rag_max_results,
                        )
                        if len(sibling_sources) < min(2, self.rag_min_citations):
                            continue
                        sibling_pool["results"] = sibling_sources
                        set_score = self._score_rag_result_set(
                            document_title,
                            rag_focus,
                            sibling_sources,
                        )
                        if set_score < float(os.getenv("FLOWERNET_VECTOR_FALLBACK_MIN_SET_SCORE", "0.35")):
                            continue
                        sibling_pool["result_set_score"] = set_score
                        sibling_query = str(
                            sibling_pool.get("query")
                            or (rag_query_candidates[0] if rag_query_candidates else current_outline)
                        )
                        candidate_result_sets.append((set_score, sibling_query, sibling_pool))

            if candidate_result_sets:
                _, selected_query, rag_search_result = max(
                    candidate_result_sets,
                    key=lambda item: item[0],
                )
                rag_used = True
                rag_selected_query = selected_query

            if selected_query:
                    try:
                        if self.vector_store is not None:
                            reranked = self.vector_store.reranker.rerank(
                                selected_query,
                                rag_search_result.get("results", []),
                                top_k=max(3, len(rag_search_result.get("results", []))),
                            )
                            if reranked:
                                rag_search_result["results"] = self._preserve_multi_axis_source_diversity(
                                    selected_query,
                                    reranked,
                                    rag_search_result.get("results", []),
                                    max_items=self.rag_max_results,
                                )
                                rag_search_result["reranker"] = "flowernet_vector_reranker"
                            indexed = self.vector_store.index_rag_results(
                                selected_query,
                                rag_search_result.get("results", []),
                                namespace=str(document_id or "global"),
                            )
                            rag_search_result["vector_indexed"] = indexed
                            rag_search_result["vector_backend"] = self.vector_store.active_backend
                    except Exception as _e:
                        rag_search_result["vector_index_error"] = str(_e)[:180]
                    rag_search_result["source_pool_key"] = rag_source_pool_key
                    rag_search_result["source_pool_reused"] = bool(used_fixed_rag_source_pool)
                    if not used_fixed_rag_source_pool:
                        self._remember_rag_source_pool(
                            rag_source_pool_key,
                            document_title=document_title,
                            subsection_title=subsection_title,
                            rag_search_result=rag_search_result,
                            selected_query=selected_query,
                            tried_queries=rag_query_candidates,
                        )
                    rag_context = self.search_engine.format_search_context(
                        rag_search_result,
                        max_items=min(self.rag_max_results, len(rag_search_result.get("results", []) or [])),
                    )
                    # If RAG returned usable sources, citations must be used even when an
                    # old deployment env accidentally left RAG_FORCE_CITATION=false.
                    require_source_citations = True
                    print(f"   🌐 RAG检索成功: {len(rag_search_result.get('results', []))} 条来源")
                    self._emit_progress_event(
                        document_id=document_id,
                        section_id=section_id,
                        subsection_id=subsection_id,
                        stage="rag_search_success",
                        message="RAG 搜索成功，已注入来源上下文",
                        metadata={
                            "query": selected_query,
                            "tried_queries": rag_query_candidates,
                            "result_count": len(rag_search_result.get("results", [])),
                            "require_source_citations": require_source_citations,
                            "vector_indexed": rag_search_result.get("vector_indexed", 0),
                            "vector_backend": rag_search_result.get("vector_backend", ""),
                            "source_pool_key": rag_source_pool_key,
                            "source_pool_reused": bool(used_fixed_rag_source_pool),
                        },
                    )
            else:
                vector_hits: List[Dict[str, Any]] = []
                if self.vector_store is not None:
                    try:
                        # Query a wider pool before title-family deduplication and
                        # semantic gating. Query every semantic candidate because
                        # a single long query can over-weight document-level terms.
                        vector_top_k = max(50, self.rag_max_results * 4)
                        seen_vector_hits: set[str] = set()
                        for vector_query in (rag_query_candidates or [current_outline]):
                            query_hits = self.vector_store.query(
                                vector_query,
                                top_k=vector_top_k,
                                namespace=str(document_id or "global"),
                            )
                            if (
                                not query_hits
                                and os.getenv("FLOWERNET_ALLOW_GLOBAL_VECTOR_FALLBACK", "true").lower() == "true"
                            ):
                                query_hits = self.vector_store.query(vector_query, top_k=vector_top_k)
                            for hit in query_hits:
                                metadata = hit.get("metadata", {}) or {}
                                key = str(metadata.get("url") or hit.get("id") or hit.get("text", ""))
                                if not key or key in seen_vector_hits:
                                    continue
                                seen_vector_hits.add(key)
                                vector_hits.append(hit)
                    except Exception:
                        vector_hits = []
                if vector_hits:
                    mapped_vector_results = self._filter_rag_source_metadata([
                        {
                            "title": hit.get("metadata", {}).get("title") or hit.get("text", "")[:80],
                            "body": hit.get("text", ""),
                            "href": hit.get("metadata", {}).get("url", ""),
                            "quality_score": hit.get("rerank_score", 0.0),
                            "source": "vector_db",
                        }
                        for hit in vector_hits
                    ])
                    mapped_vector_results = self._filter_citable_source_alignment(
                        mapped_vector_results,
                        topic=rag_semantic_topic,
                        min_semantic_score=float(os.getenv("MIN_SEMANTIC_SOURCE_SCORE", "0.35")),
                    )
                    if len(mapped_vector_results) < min(2, self.rag_min_citations):
                        mapped_vector_results = []
                    vector_set_score = self._score_rag_result_set(
                        document_title,
                        rag_gate_focus,
                        mapped_vector_results,
                    )
                    if vector_set_score < float(os.getenv("FLOWERNET_VECTOR_FALLBACK_MIN_SET_SCORE", "0.35")):
                        mapped_vector_results = []
                if vector_hits and mapped_vector_results:
                    rag_search_result = {
                        "success": True,
                        "query": rag_query_candidates[0] if rag_query_candidates else current_outline,
                        "results": mapped_vector_results,
                        "source_type": "vector_db",
                        "vector_backend": getattr(self.vector_store, "active_backend", "memory"),
                        "result_set_score": vector_set_score,
                        "source_pool_key": rag_source_pool_key,
                        "source_pool_reused": False,
                    }
                    self._remember_rag_source_pool(
                        rag_source_pool_key,
                        document_title=document_title,
                        subsection_title=subsection_title,
                        rag_search_result=rag_search_result,
                        selected_query=str(rag_search_result.get("query") or ""),
                        tried_queries=rag_query_candidates,
                    )
                    rag_context = self.search_engine.format_search_context(
                        rag_search_result,
                        max_items=min(self.rag_max_results, len(rag_search_result.get("results", []) or [])),
                    )
                    require_source_citations = True
                    rag_used = True
                    rag_selected_query = str(rag_search_result.get("query") or "")
                    print(f"   🧠 Vector DB RAG 命中: {len(vector_hits)} 条来源")
                    self._emit_progress_event(
                        document_id=document_id,
                        section_id=section_id,
                        subsection_id=subsection_id,
                        stage="rag_vector_success",
                        message="Vector DB RAG 命中，已注入历史来源上下文",
                        metadata={
                            "query": rag_selected_query,
                            "result_count": len(vector_hits),
                            "vector_backend": getattr(self.vector_store, "active_backend", "memory"),
                            "require_source_citations": require_source_citations,
                        },
                    )
                else:
                    print("   ⚠️ RAG检索未返回可用来源，降级为常规生成")
                    self._emit_progress_event(
                        document_id=document_id,
                        section_id=section_id,
                        subsection_id=subsection_id,
                        stage="rag_search_failed",
                        message="RAG 搜索失败，降级为常规生成",
                        metadata={
                            "query": rag_query_candidates[0] if rag_query_candidates else "",
                            "tried_queries": rag_query_candidates,
                            "error": rag_error,
                        },
                    )
            source_citation_required = require_source_citations

        planned_research_intelligence = self._build_research_intelligence(
            document_title=document_title,
            section_title=section_title,
            subsection_title=subsection_title,
            outline=current_outline,
            prompt=current_prompt,
            source_results=rag_search_result.get("results", []) if isinstance(rag_search_result, dict) else [],
        )
        if planned_research_intelligence.get("enabled"):
            self._emit_progress_event(
                document_id=document_id,
                section_id=section_id,
                subsection_id=subsection_id,
                stage="research_intelligence_ready",
                message="Literature-anchored novelty miner 与 claim-evidence graph 已就绪",
                metadata={
                    "source_count": (
                        planned_research_intelligence.get("novelty", {}).get("source_count", 0)
                        if isinstance(planned_research_intelligence.get("novelty"), dict)
                        else 0
                    ),
                    "novelty_confidence": (
                        planned_research_intelligence.get("novelty", {}).get("novelty_confidence", 0.0)
                        if isinstance(planned_research_intelligence.get("novelty"), dict)
                        else 0.0
                    ),
                    "claim_count": len(
                        planned_research_intelligence.get("claim_evidence_graph", {}).get("claims", [])
                        if isinstance(planned_research_intelligence.get("claim_evidence_graph"), dict)
                        else []
                    ),
                    "reviewer_risk": (
                        planned_research_intelligence.get("novelty", {}).get("reviewer_risk", "")
                        if isinstance(planned_research_intelligence.get("novelty"), dict)
                        else ""
                    ),
                },
            )

        effective_attempt_cap = self.max_subsection_attempts if self.max_subsection_attempts > 0 else self.max_iterations
        
        while True:
            if self._deadline_exceeded():
                reason = "document_deadline_exceeded_before_subsection_generation"
                if (
                    self.accept_best_real_draft
                    and best_candidate
                    and self._is_usable_real_draft(best_candidate.get("draft", ""), best_candidate.get("outline", current_outline))
                    and self._best_real_draft_quality_ok(best_candidate.get("verification", {}) or {}, rel_threshold, red_threshold)
                ):
                    best_verification = best_candidate.get("verification", {})
                    self._emit_progress_event(
                        document_id=document_id,
                        section_id=section_id,
                        subsection_id=subsection_id,
                        stage="subsection_best_real_draft_accepted",
                        message="文档总时限已到，接收当前小节最佳真实草稿",
                        metadata={
                            "iteration": iterations,
                            "best_iteration": best_candidate.get("iteration", iterations),
                            "reason": reason,
                            "relevancy_index": best_verification.get("relevancy_index", 0),
                            "redundancy_index": best_verification.get("redundancy_index", 1),
                            "quality_score": best_verification.get("quality_score", 0),
                        },
                    )
                    return self._subsection_best_real_draft_result(
                        reason=reason,
                        draft=best_candidate.get("draft", ""),
                        final_outline=best_candidate.get("outline", current_outline),
                        iterations=iterations,
                        verification=best_verification,
                        all_drafts=all_drafts,
                        metrics=metrics,
                        rag_search_result={
                            **rag_search_result,
                            "results": best_candidate.get("source_results", rag_search_result.get("results", [])),
                            "success": best_candidate.get("rag_search_success", rag_search_result.get("success", False)),
                            "vector_indexed": best_candidate.get("rag_vector_indexed", rag_search_result.get("vector_indexed", 0)),
                            "vector_backend": best_candidate.get("rag_vector_backend", rag_search_result.get("vector_backend", "")),
                            "reranker": best_candidate.get("rag_reranker", rag_search_result.get("reranker", "")),
                        },
                        rag_used=bool(best_candidate.get("rag_used", rag_used)),
                        rag_selected_query=str(best_candidate.get("rag_selected_query", rag_selected_query) or ""),
                        controller_triggered=(metrics.get("controller_calls", 0) > 0) or controller_triggered,
                        controller_retry_count=controller_retry_count,
                        controller_last_result=controller_last_result,
                        bandit=best_candidate.get("bandit", {}),
                        pass_candidate_audit=pass_candidate_audit,
                    )
                self._emit_progress_event(
                    document_id=document_id,
                    section_id=section_id,
                    subsection_id=subsection_id,
                    stage="subsection_failed",
                    message="文档生成总时限已到，停止当前小节而不是兜底通过",
                    metadata={"iteration": iterations, "reason": reason},
                )
                return self._subsection_failure_result(
                    reason=reason,
                    draft=(best_candidate or {}).get("draft", "") if best_candidate else "",
                    final_outline=current_outline,
                    iterations=iterations,
                    verification=(best_candidate or {}).get("verification", {}) if best_candidate else {},
                    all_drafts=all_drafts,
                    metrics=metrics,
                    rag_search_result=rag_search_result,
                    rag_used=rag_used,
                    rag_selected_query=rag_selected_query,
                    controller_triggered=(metrics.get("controller_calls", 0) > 0) or controller_triggered,
                    controller_retry_count=controller_retry_count,
                    controller_last_result=controller_last_result,
                    bandit=(best_candidate or {}).get("bandit", {}) if best_candidate else {},
                )

            iterations += 1
            controller_regression_rolled_back = False
            elapsed_subsection = time.monotonic() - subsection_started_at
            timed_out = self.subsection_max_seconds > 0 and elapsed_subsection >= self.subsection_max_seconds
            reached_attempt_cap = (
                (not self.subsection_retry_forever)
                and effective_attempt_cap > 0
                and iterations > effective_attempt_cap
            )

            if timed_out or reached_attempt_cap:
                timeout_triggered = timed_out and not reached_attempt_cap
                if best_candidate and best_candidate.get("draft"):
                    best_verification = best_candidate.get("verification", {})
                    best_alignment = (
                        best_candidate.get("source_alignment")
                        if isinstance(best_candidate.get("source_alignment"), dict)
                        else best_verification.get("source_alignment")
                        if isinstance(best_verification.get("source_alignment"), dict)
                        else {}
                    )
                    if timeout_triggered:
                        pass_message = f"单小节耗时已达 {self.subsection_max_seconds}s，接收最佳真实草稿"
                        pass_reason = "subsection_timeout"
                    else:
                        pass_message = f"达到最大检测次数 {effective_attempt_cap}，接收最佳真实草稿"
                        pass_reason = "max_attempts_reached"
                    audited_pass_candidate = bool(
                        best_verification.get("source_alignment_low_pass_audit")
                        and best_verification.get("pre_source_alignment_audit_passed")
                        and self._is_usable_real_draft(best_candidate.get("draft", ""), best_candidate.get("outline", current_outline))
                        and self._best_real_draft_quality_ok(best_verification, rel_threshold, red_threshold)
                        and self._source_alignment_recoverable(
                            float(best_alignment.get("score", 0.0) or 0.0),
                            self.source_alignment_min_score,
                            self.source_alignment_pass_audit_min_score,
                            self.source_alignment_pass_epsilon,
                        )
                    )
                    if audited_pass_candidate:
                        recovered_alignment_score = float(best_alignment.get("score", 0.0) or 0.0)
                        alignment_marginal = recovered_alignment_score < self.source_alignment_pass_audit_min_score
                        recovered_verification = {
                            **best_verification,
                            "is_passed": True,
                            "source_alignment_audited_pass_accepted": True,
                            "source_alignment_audit_unresolved": True,
                            "source_alignment_marginal_pass": bool(alignment_marginal),
                            "source_alignment_deficit": round(
                                max(0.0, self.source_alignment_pass_audit_min_score - recovered_alignment_score),
                                6,
                            ),
                            "acceptance_mode": (
                                "source_alignment_marginal"
                                if alignment_marginal
                                else str(best_verification.get("acceptance_mode") or "strict")
                            ),
                            "force_reason": (
                                "source_alignment_marginal_pass"
                                if alignment_marginal
                                else "source_alignment_audited_pass_accepted"
                            ),
                        }
                        self._emit_progress_event(
                            document_id=document_id,
                            section_id=section_id,
                            subsection_id=subsection_id,
                            stage="source_alignment_audited_pass_accepted",
                            message="达到最大轮次：回收审计前已通过的真实草稿，标记 source-alignment 仍需后续增强",
                            metadata={
                                "iteration": iterations - 1,
                                "best_iteration": best_candidate.get("iteration", iterations - 1),
                                "relevancy_index": best_verification.get("relevancy_index", 0),
                                "redundancy_index": best_verification.get("redundancy_index", 1),
                                "quality_score": best_verification.get("quality_score", 0),
                                "source_alignment": best_alignment,
                                "hard_min_score": self.source_alignment_min_score,
                                "pass_audit_min_score": self.source_alignment_pass_audit_min_score,
                                "pass_epsilon": self.source_alignment_pass_epsilon,
                                "marginal_pass": bool(alignment_marginal),
                            },
                        )
                        return self._subsection_best_real_draft_result(
                            reason="source_alignment_audited_pass_accepted",
                            draft=best_candidate.get("draft", ""),
                            final_outline=best_candidate.get("outline", current_outline),
                            iterations=iterations - 1,
                            verification=recovered_verification,
                            all_drafts=all_drafts,
                            metrics=metrics,
                            rag_search_result={
                                **rag_search_result,
                                "results": best_candidate.get("source_results", rag_search_result.get("results", [])),
                                "success": best_candidate.get("rag_search_success", rag_search_result.get("success", False)),
                                "vector_indexed": best_candidate.get("rag_vector_indexed", rag_search_result.get("vector_indexed", 0)),
                                "vector_backend": best_candidate.get("rag_vector_backend", rag_search_result.get("vector_backend", "")),
                                "reranker": best_candidate.get("rag_reranker", rag_search_result.get("reranker", "")),
                            },
                            rag_used=bool(best_candidate.get("rag_used", rag_used)),
                            rag_selected_query=str(best_candidate.get("rag_selected_query", rag_selected_query) or ""),
                            controller_triggered=(metrics.get("controller_calls", 0) > 0) or controller_triggered,
                            controller_retry_count=controller_retry_count,
                            controller_last_result=controller_last_result,
                            bandit=best_candidate.get("bandit", {}),
                            pass_candidate_audit=pass_candidate_audit,
                        )
                    if (
                        self.accept_best_real_draft
                        and self._is_usable_real_draft(best_candidate.get("draft", ""), best_candidate.get("outline", current_outline))
                        and self._best_real_draft_quality_ok(best_verification, rel_threshold, red_threshold)
                    ):
                        self._emit_progress_event(
                            document_id=document_id,
                            section_id=section_id,
                            subsection_id=subsection_id,
                            stage="subsection_best_real_draft_accepted",
                            message=pass_message,
                            metadata={
                                "iteration": iterations - 1,
                                "best_iteration": best_candidate.get("iteration", iterations - 1),
                                "relevancy_index": best_verification.get("relevancy_index", 0),
                                "redundancy_index": best_verification.get("redundancy_index", 1),
                                "quality_score": best_verification.get("quality_score", 0),
                                "elapsed_seconds": round(elapsed_subsection, 2),
                                "reason": pass_reason,
                            },
                        )
                        return self._subsection_best_real_draft_result(
                            reason=pass_reason,
                            draft=best_candidate.get("draft", ""),
                            final_outline=best_candidate.get("outline", current_outline),
                            iterations=iterations - 1,
                            verification=best_verification,
                            all_drafts=all_drafts,
                            metrics=metrics,
                            rag_search_result={
                                **rag_search_result,
                                "results": best_candidate.get("source_results", rag_search_result.get("results", [])),
                                "success": best_candidate.get("rag_search_success", rag_search_result.get("success", False)),
                                "vector_indexed": best_candidate.get("rag_vector_indexed", rag_search_result.get("vector_indexed", 0)),
                                "vector_backend": best_candidate.get("rag_vector_backend", rag_search_result.get("vector_backend", "")),
                                "reranker": best_candidate.get("rag_reranker", rag_search_result.get("reranker", "")),
                            },
                            rag_used=bool(best_candidate.get("rag_used", rag_used)),
                            rag_selected_query=str(best_candidate.get("rag_selected_query", rag_selected_query) or ""),
                            controller_triggered=(metrics.get("controller_calls", 0) > 0) or controller_triggered,
                            controller_retry_count=controller_retry_count,
                            controller_last_result=controller_last_result,
                            bandit=best_candidate.get("bandit", {}),
                            pass_candidate_audit=pass_candidate_audit,
                        )

                # Preserve the best observed real candidate for diagnostics even
                # when strict quality gates correctly keep the subsection failed.
                fallback_verification: Dict[str, Any] = {}
                if best_candidate and self._is_usable_real_draft(
                    best_candidate.get("draft", ""),
                    best_candidate.get("outline", current_outline),
                ):
                    fallback_draft = str(best_candidate.get("draft", "") or "")
                    fallback_verification = dict(best_candidate.get("verification", {}) or {})
                    fallback_note = "（未通过严格阈值，保留最佳已验证草稿用于诊断）"
                elif all_drafts and len(all_drafts) > 0:
                    # 优先选择最近一次非大纲型的草稿作为兜底；若都像大纲，则退回最后一次草稿
                    chosen = None
                    for d in reversed(all_drafts):
                        if not self._is_outline_like(d, current_outline):
                            chosen = d
                            break
                    if chosen is None:
                        chosen = all_drafts[-1]
                        fallback_note = "（未完全验证，最后尝试的内容）"
                    else:
                        fallback_note = "（未完全验证，最后尝试的非大纲内容）"
                    fallback_draft = chosen
                else:
                    # 完全没有draft时，返回空内容而不是outline
                    fallback_draft = ""
                    fallback_note = "（内容生成失败，仍在恢复中）"
                fallback_is_meaningful = self._is_usable_real_draft(fallback_draft, current_outline)
                verifier_unavailable_only = (
                    int(metrics.get("verifier_error", 0) or 0) > 0
                    and int(metrics.get("verifier_failed", 0) or 0) == 0
                )
                best_effort_due_to_verifier = (
                    self.verifier_unavailable_best_effort
                    and verifier_unavailable_only
                    and fallback_is_meaningful
                )
                
                # 诊断日志：记录保留下来的失败草稿，便于排查 web 侧为何仍展示 outline
                try:
                    print(
                        f"[Orch] verifier_failed_no_usable_draft for {section_id}::{subsection_id} - "
                        f"chosen_len={len(fallback_draft or '')}, is_outline_like={self._is_outline_like(fallback_draft, current_outline)}, "
                        f"best_candidate_present={bool(best_candidate)}, total_drafts={len(all_drafts)}"
                    )
                except Exception:
                    pass

                placeholder_reason = (
                    "subsection_timeout_quality_not_met"
                    if timeout_triggered and fallback_is_meaningful
                    else "subsection_timeout_no_draft"
                    if timeout_triggered
                    else "max_attempts_quality_not_met"
                    if fallback_is_meaningful
                    else "max_attempts_no_draft"
                )
                if fallback_is_meaningful and self.accept_best_real_draft:
                    placeholder_reason = "verifier_unavailable" if best_effort_due_to_verifier else ("subsection_timeout_best_real_draft" if timeout_triggered else "max_attempts_best_real_draft")
                    recovered_verification = dict(fallback_verification or {})
                    recovered_verification.update({
                        "feedback": str(recovered_verification.get("feedback") or placeholder_reason),
                        "verifier_unavailable": verifier_unavailable_only,
                        "strict_failure_reason": placeholder_reason,
                        "best_effort": True,
                    })
                    self._emit_progress_event(
                        document_id=document_id,
                        section_id=section_id,
                        subsection_id=subsection_id,
                        stage="subsection_best_real_draft_accepted",
                        message=(
                            f"单小节耗时已达 {self.subsection_max_seconds}s，接收最后真实草稿"
                            if timeout_triggered
                            else f"达到最大检测次数 {effective_attempt_cap}，接收最后真实草稿"
                        ),
                        metadata={
                            "iteration": iterations - 1,
                            "elapsed_seconds": round(elapsed_subsection, 2),
                            "has_fallback_draft": len(all_drafts) > 0,
                            "fallback_note": fallback_note,
                            "reason": placeholder_reason,
                            "best_effort": True,
                        },
                    )
                    return self._subsection_best_real_draft_result(
                        reason=placeholder_reason,
                        draft=fallback_draft,
                        final_outline=current_outline,
                        iterations=iterations - 1,
                        verification=recovered_verification,
                        all_drafts=all_drafts,
                        metrics=metrics,
                        rag_search_result=rag_search_result,
                        rag_used=rag_used,
                        rag_selected_query=rag_selected_query,
                        controller_triggered=(metrics.get("controller_calls", 0) > 0) or controller_triggered,
                        controller_retry_count=controller_retry_count,
                        controller_last_result=controller_last_result,
                        pass_candidate_audit=pass_candidate_audit,
                    )
                self._emit_progress_event(
                    document_id=document_id,
                    section_id=section_id,
                    subsection_id=subsection_id,
                    stage="subsection_failed",
                    message=(
                        f"单小节耗时已达 {self.subsection_max_seconds}s，且没有可用真实草稿，停止当前小节"
                        if timeout_triggered
                        else f"达到最大检测次数 {effective_attempt_cap}，且没有可用真实草稿，停止当前小节"
                    ),
                    metadata={
                        "iteration": iterations - 1,
                        "elapsed_seconds": round(elapsed_subsection, 2),
                        "has_fallback_draft": len(all_drafts) > 0,
                        "fallback_note": fallback_note,
                    },
                )
                return self._subsection_failure_result(
                    reason=placeholder_reason,
                    draft=fallback_draft,
                    final_outline=current_outline,
                    iterations=iterations - 1,
                    verification={
                        **fallback_verification,
                        "feedback": str(fallback_verification.get("feedback") or placeholder_reason),
                        "verifier_unavailable": verifier_unavailable_only,
                        "strict_failure_reason": placeholder_reason,
                    },
                    all_drafts=all_drafts,
                    metrics=metrics,
                    rag_search_result=rag_search_result,
                    rag_used=rag_used,
                    rag_selected_query=rag_selected_query,
                    controller_triggered=(metrics.get("controller_calls", 0) > 0) or controller_triggered,
                    controller_retry_count=controller_retry_count,
                    controller_last_result=controller_last_result,
                    bandit=bandit_debug,
                )

            print(f"\n      尝试 {iterations}/{effective_attempt_cap}")
            
            effective_rel_threshold, effective_red_threshold = self._compute_effective_thresholds(
                iteration=iterations,
                rel_threshold=rel_threshold,
                red_threshold=red_threshold,
            )

            print(f"         🎯 调用 Generator...")
            self._emit_progress_event(
                document_id=document_id,
                section_id=section_id,
                subsection_id=subsection_id,
                stage="generator_start",
                message=f"第 {iterations} 轮：进入 Generator 生成",
                metadata={
                    "iteration": iterations,
                    "outline_chars": len(current_outline),
                    "prompt_chars": len(current_prompt),
                    "history_chars": len(history_text),
                    "effective_rel_threshold": round(float(effective_rel_threshold), 4),
                    "effective_red_threshold": round(float(effective_red_threshold), 4),
                    "generator_degraded_mode": generator_degraded_mode,
                    "rag_used": rag_used,
                },
            )

            active_revision_draft = revision_draft
            active_revision_arm = revision_arm
            if active_revision_draft:
                metrics["controller_targeted_revision_attempts"] += 1
                self._emit_progress_event(
                    document_id=document_id,
                    section_id=section_id,
                    subsection_id=subsection_id,
                    stage="controller_targeted_revision_start",
                    message=f"第 {iterations} 轮：按 {active_revision_arm or 'controller'} 定向修订上一版草稿",
                    metadata={
                        "iteration": iterations,
                        "selected_arm": active_revision_arm,
                        "revision_draft_chars": len(active_revision_draft),
                    },
                )
            enhanced_prompt = self._build_enhanced_prompt(
                original_prompt=current_prompt,
                outline=current_outline,
                history_text=history_text,
                rel_threshold=effective_rel_threshold,
                red_threshold=effective_red_threshold,
                rag_context=rag_context,
                require_source_citations=require_source_citations,
                available_source_count=len(rag_search_result.get("results", []) or []),
                negative_constraints=last_negative_constraints,
                source_results=rag_search_result.get("results", []) or [],
                research_intelligence=planned_research_intelligence,
                revision_draft=active_revision_draft,
                revision_arm=active_revision_arm,
            )
            
            paired_initial_draft = None
            if iterations == 1 and not active_revision_draft and self.initial_draft_pool_path:
                paired_initial_draft = self._get_paired_initial_draft(
                    enhanced_prompt,
                    self.generator_max_tokens,
                )
            if paired_initial_draft is not None:
                gen_result = paired_initial_draft
                metrics["paired_initial_draft_reused"] += 1
                print("      [Generator] 复用公平消融配对首稿")
                self._emit_progress_event(
                    document_id=document_id,
                    section_id=section_id,
                    subsection_id=subsection_id,
                    stage="paired_initial_draft_reused",
                    message="复用相同完整 prompt 生成的真实首稿，以隔离消融组件贡献",
                    metadata={
                        "iteration": iterations,
                        "cache_key": str((gen_result.get("metadata") or {}).get("paired_initial_draft_key") or ""),
                    },
                )
            else:
                gen_result = self._call_generator(enhanced_prompt)
                if (
                    iterations == 1
                    and not active_revision_draft
                    and self.initial_draft_pool_path
                    and self._remember_paired_initial_draft(
                        enhanced_prompt,
                        self.generator_max_tokens,
                        gen_result,
                    )
                ):
                    metrics["paired_initial_draft_created"] += 1
            generator_bandit = gen_result.get("bandit", {}) if isinstance(gen_result, dict) else {}
            if isinstance(generator_bandit, dict) and generator_bandit and not bandit_debug.get("events"):
                bandit_debug.update(generator_bandit)
            
            if not gen_result.get("success"):
                generator_failure_streak += 1
                print(f"         ⚠️ Generator 错误，继续重试当前小节: {gen_result.get('error')}")
                self._emit_progress_event(
                    document_id=document_id,
                    section_id=section_id,
                    subsection_id=subsection_id,
                    stage="generator_error",
                    message=f"第 {iterations} 轮：Generator 失败，准备重试",
                    metadata={
                        "iteration": iterations,
                        "error": gen_result.get("error", "unknown"),
                        "generator_failure_streak": generator_failure_streak,
                        "provider": str((gen_result.get("metadata") or {}).get("provider", "") or ""),
                        "prompt_chars": len(enhanced_prompt),
                    },
                )

                max_generator_failures = min(effective_attempt_cap, self.max_generator_failures_per_subsection)
                if generator_failure_streak >= max_generator_failures:
                    fail_error = str(gen_result.get("error", "generator_unavailable"))

                    # 首次达到连续失败阈值时，先简化上下文再重试一次生成链路。
                    if not generator_degraded_mode:
                        generator_degraded_mode = True
                        generator_failure_streak = 0
                        metrics["generator_degraded_mode"] += 1
                        rag_context = ""
                        source_citation_required = False
                        self._emit_progress_event(
                            document_id=document_id,
                            section_id=section_id,
                            subsection_id=subsection_id,
                            stage="generator_degraded_retry",
                            message=f"Generator 连续失败 {max_generator_failures} 次，切换降级模式后继续重试",
                            metadata={
                                "iteration": iterations,
                                "max_generator_failures": max_generator_failures,
                                "error": fail_error,
                                "degraded_mode": True,
                                "rag_context_cleared": True,
                                "source_citation_required": False,
                            },
                        )
                        time.sleep(self._compute_retry_delay(iterations))
                        continue

                    fail_outline = str(current_outline).strip()
                    # 改进：即使generator失败，也优先使用任何可用的草稿而不是outline
                    if all_drafts and len(all_drafts) > 0:
                        chosen = None
                        for d in reversed(all_drafts):
                            if not self._is_outline_like(d, current_outline):
                                chosen = d
                                break
                        if chosen is None:
                            chosen = all_drafts[-1]
                            fallback_note = "（Generator失败，保留最后尝试的内容用于诊断）"
                        else:
                            fallback_note = "（Generator失败，保留最后尝试的非大纲内容用于诊断）"
                        fallback_text = chosen
                    else:
                        fallback_text = ""
                        fallback_note = "（Generator失败，未产生可用草稿）"

                    try:
                        print(
                            f"[Orch] generator_failed_after_context_simplification for {section_id}::{subsection_id} - "
                            f"chosen_len={len(fallback_text or '')}, is_outline_like={self._is_outline_like(fallback_text, fail_outline)}, "
                            f"total_drafts={len(all_drafts)}"
                        )
                    except Exception:
                        pass
                    
                    if (
                        self.accept_best_real_draft
                        and self._is_usable_real_draft(fallback_text, current_outline)
                        and self._best_real_draft_quality_ok(
                            {
                                "relevancy_index": 0.0,
                                "redundancy_index": 1.0,
                                "quality_score": 0.0,
                                "source_check": {"passed": False},
                            },
                            rel_threshold,
                            red_threshold,
                        )
                    ):
                        self._emit_progress_event(
                            document_id=document_id,
                            section_id=section_id,
                            subsection_id=subsection_id,
                            stage="subsection_best_real_draft_accepted",
                            message="Generator 降级重试后仍失败，接收此前最佳真实草稿",
                            metadata={
                                "iteration": iterations,
                                "max_generator_failures": max_generator_failures,
                                "error": fail_error,
                                "degraded_mode": True,
                                "has_fallback_draft": len(all_drafts) > 0,
                                "fallback_note": fallback_note,
                            },
                        )
                        return self._subsection_best_real_draft_result(
                            reason="generator_repeated_failure_best_real_draft",
                            draft=fallback_text,
                            final_outline=current_outline,
                            iterations=iterations,
                            verification={
                                "relevancy_index": 0.0,
                                "redundancy_index": 1.0,
                                "feedback": "generator_repeated_failure_best_real_draft",
                            },
                            all_drafts=all_drafts,
                            metrics=metrics,
                            rag_search_result=rag_search_result,
                            rag_used=rag_used,
                            rag_selected_query=rag_selected_query,
                            controller_triggered=(metrics.get("controller_calls", 0) > 0) or controller_triggered,
                            controller_retry_count=controller_retry_count,
                            controller_last_result=controller_last_result,
                            pass_candidate_audit=pass_candidate_audit,
                        )

                    self._emit_progress_event(
                        document_id=document_id,
                        section_id=section_id,
                        subsection_id=subsection_id,
                        stage="subsection_failed",
                        message=f"Generator 在降级模式下仍连续失败 {generator_failure_streak} 次，停止当前小节；不进行兜底通过",
                        metadata={
                            "iteration": iterations,
                            "max_generator_failures": max_generator_failures,
                            "error": fail_error,
                            "degraded_mode": True,
                            "has_fallback_draft": len(all_drafts) > 0,
                            "fallback_note": fallback_note,
                        },
                    )
                    return self._subsection_failure_result(
                        reason="generator_repeated_failure_after_degraded_mode",
                        draft=fallback_text,
                        final_outline=current_outline,
                        iterations=iterations,
                        verification={
                            "relevancy_index": 0.0,
                            "redundancy_index": 1.0,
                            "feedback": "generator_repeated_failure_after_degraded_mode",
                        },
                        all_drafts=all_drafts,
                        metrics=metrics,
                        rag_search_result=rag_search_result,
                        rag_used=rag_used,
                        rag_selected_query=rag_selected_query,
                        controller_triggered=(metrics.get("controller_calls", 0) > 0) or controller_triggered,
                        controller_retry_count=controller_retry_count,
                        controller_last_result=controller_last_result,
                    )

                time.sleep(self._compute_retry_delay(iterations))
                continue

            generator_failure_streak = 0
            if active_revision_draft:
                revision_draft = ""
                revision_arm = ""
            
            generator_metadata = gen_result.get("metadata") if isinstance(gen_result.get("metadata"), dict) else {}
            prompt_tokens = int(generator_metadata.get("prompt_tokens", 0) or 0)
            output_tokens = int(generator_metadata.get("output_tokens", 0) or generator_metadata.get("completion_tokens", 0) or 0)
            total_tokens = int(generator_metadata.get("total_tokens", 0) or (prompt_tokens + output_tokens))
            prompt_cache_hit_tokens = int(generator_metadata.get("prompt_cache_hit_tokens", 0) or 0)
            prompt_cache_miss_tokens = int(generator_metadata.get("prompt_cache_miss_tokens", 0) or 0)
            metrics["prompt_tokens"] += prompt_tokens
            metrics["output_tokens"] += output_tokens
            metrics["total_tokens"] += total_tokens
            metrics["prompt_cache_hit_tokens"] += prompt_cache_hit_tokens
            metrics["prompt_cache_miss_tokens"] += prompt_cache_miss_tokens

            raw_draft = gen_result.get("draft", "")
            draft = self._sanitize_subsection_draft(raw_draft)
            before_length_limit_chars = len(draft)
            draft = self._limit_subsection_draft_length(draft)
            if len(draft) < before_length_limit_chars:
                self._emit_progress_event(
                    document_id=document_id,
                    section_id=section_id,
                    subsection_id=subsection_id,
                    stage="draft_length_limited",
                    message="已按公平评估目标压缩超长小节草稿",
                    metadata={
                        "iteration": iterations,
                        "before_chars": before_length_limit_chars,
                        "after_chars": len(draft),
                        "target_max_chars": self.target_draft_max_chars,
                    },
                )
            if source_citation_required and rag_search_result.get("results"):
                effective_min_source_citations = max(
                    1,
                    min(
                        int(self.rag_min_citations),
                        len(rag_search_result.get("results", []) or []),
                    ),
                )
                before_ref_count = self._inline_source_ref_count(
                    draft,
                    len(rag_search_result.get("results", []) or []),
                )
                draft = self._inject_missing_source_citations(
                    draft=draft,
                    source_results=rag_search_result.get("results", []) or [],
                    min_citations=effective_min_source_citations,
                )
                after_ref_count = self._inline_source_ref_count(
                    draft,
                    len(rag_search_result.get("results", []) or []),
                )
                if after_ref_count > before_ref_count:
                    self._emit_progress_event(
                        document_id=document_id,
                        section_id=section_id,
                        subsection_id=subsection_id,
                        stage="citation_markers_repaired",
                        message="已将当前 RAG 真实来源编号插入正文引用位置",
                        metadata={
                            "iteration": iterations,
                            "before_ref_count": before_ref_count,
                            "after_ref_count": after_ref_count,
                            "available_source_count": len(rag_search_result.get("results", []) or []),
                        },
                    )
            if not self._draft_is_complete(draft, generator_metadata):
                metrics["generator_short_draft"] += 1
                continuation = self._continue_incomplete_draft(draft, current_outline)
                continuation_metadata = (
                    continuation.get("metadata")
                    if isinstance(continuation, dict) and isinstance(continuation.get("metadata"), dict)
                    else {}
                )
                for token_key in (
                    "prompt_tokens",
                    "output_tokens",
                    "total_tokens",
                    "prompt_cache_hit_tokens",
                    "prompt_cache_miss_tokens",
                ):
                    metrics[token_key] += int(continuation_metadata.get(token_key, 0) or 0)
                if continuation.get("success"):
                    draft = str(continuation.get("draft") or "").strip()
                    self._emit_progress_event(
                        document_id=document_id,
                        section_id=section_id,
                        subsection_id=subsection_id,
                        stage="generator_draft_completed",
                        message="Generator 截断稿已自动续写补全，继续进入 Verifier",
                        metadata={
                            "iteration": iterations,
                            "draft_chars": len(draft),
                            "overlap_chars": int(continuation.get("overlap_chars", 0) or 0),
                        },
                    )
                else:
                    self._emit_progress_event(
                        document_id=document_id,
                        section_id=section_id,
                        subsection_id=subsection_id,
                        stage="generator_incomplete_draft",
                        message="Generator 输出未闭合且自动续写未完成，准备完整重写",
                        metadata={
                            "iteration": iterations,
                            "finish_reason": generator_metadata.get("finish_reason", ""),
                            "draft_chars": len(draft),
                            "continuation_error": continuation.get("error", "continuation_failed"),
                        },
                    )
                    time.sleep(self._compute_retry_delay(iterations))
                    continue

            all_drafts.append(draft)
            print(f"         ✅ 生成 {len(draft)} 字符")
            self._emit_progress_event(
                document_id=document_id,
                section_id=section_id,
                subsection_id=subsection_id,
                stage="generator_success",
                message=f"第 {iterations} 轮：Generator 已产出草稿 ({len(draft)} 字符)",
                metadata={
                    "iteration": iterations,
                    "draft_chars": len(draft),
                    "provider": str(generator_metadata.get("provider", "") or ""),
                    "prompt_tokens": prompt_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": total_tokens,
                    "prompt_cache_hit_tokens": prompt_cache_hit_tokens,
                    "prompt_cache_miss_tokens": prompt_cache_miss_tokens,
                    "generator_degraded_mode": generator_degraded_mode,
                },
            )

            if len(str(draft or "").strip()) < self.min_draft_chars:
                metrics["generator_short_draft"] += 1
                last_negative_constraints = {
                    "feedback": (
                        f"Generator 上轮草稿只有 {len(str(draft or '').strip())} 字符，"
                        f"低于最低要求 {self.min_draft_chars} 字符。必须扩展为完整小节正文。"
                    ),
                    "quality_dimensions_failed": [
                        "coverage_completeness",
                        "evidence_grounding",
                        "logical_coherence",
                    ],
                    "short_draft_chars": len(str(draft or "").strip()),
                    "min_draft_chars": self.min_draft_chars,
                }
                self._emit_progress_event(
                    document_id=document_id,
                    section_id=section_id,
                    subsection_id=subsection_id,
                    stage="generator_short_draft",
                    message=(
                        f"第 {iterations} 轮：草稿过短 ({len(str(draft or '').strip())}/{self.min_draft_chars})，"
                        "跳过 Verifier 并重新生成"
                    ),
                    metadata={
                        "iteration": iterations,
                        "draft_chars": len(str(draft or "").strip()),
                        "min_draft_chars": self.min_draft_chars,
                        "provider": str(generator_metadata.get("provider", "") or ""),
                    },
                )
                time.sleep(self._compute_retry_delay(iterations))
                continue

            provider_name = str(generator_metadata.get("provider", "")).strip().lower()
            if self.source_citation_relaxation_enabled and source_citation_required and provider_name == "ollama":
                source_citation_required = False
                self._emit_progress_event(
                    document_id=document_id,
                    section_id=section_id,
                    subsection_id=subsection_id,
                    stage="source_citation_relaxed",
                    message="检测到 Ollama 兜底提供商，自动放宽来源引用硬约束",
                    metadata={"iteration": iterations, "provider": provider_name},
                )
            
            print(f"         🔍 调用 Verifier...")
            self._emit_progress_event(
                document_id=document_id,
                section_id=section_id,
                subsection_id=subsection_id,
                stage="verifier_start",
                message=f"第 {iterations} 轮：进入 Verifier 检测",
                metadata={"iteration": iterations},
            )
            print(f"🎯 [DEBUG] Calling verifier with thresholds: rel={effective_rel_threshold:.2f}, red={effective_red_threshold:.2f}")
            verification_outline = (
                f"Document topic: {document_title}\nSubsection target: {verification_target_base}"
                if str(document_title or "").strip()
                else verification_target_base
            )
            effective_min_source_citations = max(
                1,
                min(
                    int(self.rag_min_citations),
                    len(rag_search_result.get("results", []) or []),
                ),
            )
            verify_result = self._call_verifier(
                draft=draft,
                outline=verification_outline,
                history=[h["content"] for h in windowed_history],
                rel_threshold=effective_rel_threshold,
                red_threshold=effective_red_threshold,
                context_text=current_prompt,
                source_results=rag_search_result.get("results", []),
                require_source_citations=source_citation_required,
                min_source_citations=effective_min_source_citations,
                research_intelligence=planned_research_intelligence,
            )
            
            if not verify_result.get("success"):
                print(f"         ⚠️ Verifier 错误，继续重试当前小节")
                verifier_error = str(verify_result.get("error") or "verifier_unavailable")
                metrics["verifier_error"] += 1
                self._emit_progress_event(
                    document_id=document_id,
                    section_id=section_id,
                    subsection_id=subsection_id,
                    stage="verifier_error",
                    message=f"第 {iterations} 轮：Verifier 调用失败，准备重试",
                    metadata={
                        "iteration": iterations,
                        "error": verifier_error[:500],
                        "draft_chars": len(str(draft or "")),
                    },
                )
                time.sleep(self._compute_retry_delay(iterations))
                continue
            
            is_passed = verify_result.get("is_passed", False)
            rel_score = verify_result.get("relevancy_index", 0)
            red_score = verify_result.get("redundancy_index", 0)
            feedback = verify_result.get("feedback", "")
            source_check = verify_result.get("source_check", {})
            reviewer_assessment = (
                verify_result.get("reviewer_assessment", {})
                if isinstance(verify_result.get("reviewer_assessment"), dict)
                else {}
            )
            if reviewer_assessment.get("enabled"):
                reviewer_score = float(reviewer_assessment.get("overall_reviewer_score", 0.0) or 0.0)
                metrics["reviewer_score_sum"] = metrics.get("reviewer_score_sum", 0.0) + reviewer_score
                metrics["reviewer_score_count"] = metrics.get("reviewer_score_count", 0) + 1
                if any(
                    isinstance(dim, dict) and dim.get("risk") == "high"
                    for dim in (reviewer_assessment.get("reviewer_dimensions", {}) or {}).values()
                ):
                    metrics["reviewer_high_risk"] = metrics.get("reviewer_high_risk", 0) + 1
            if source_citation_required and isinstance(source_check, dict):
                semantic_scores = source_check.get("matched_semantic_scores")
                if isinstance(semantic_scores, dict):
                    semantic_threshold = float(source_check.get("min_semantic_source_score", 0.35) or 0.35)
                    current_sources = rag_search_result.get("results", []) if isinstance(rag_search_result, dict) else []

                    def _source_threshold_for_ref(ref_key: str) -> float:
                        try:
                            ref_idx = int(str(ref_key).split(":", 1)[1])
                        except (IndexError, ValueError):
                            return semantic_threshold
                        source_item = (
                            current_sources[ref_idx - 1]
                            if 0 < ref_idx <= len(current_sources)
                            and isinstance(current_sources[ref_idx - 1], dict)
                            else {}
                        )
                        if source_item.get("supplemental_evidence_role"):
                            return min(
                                semantic_threshold,
                                float(os.getenv("MIN_SUPPLEMENTAL_OSS_SOURCE_SEMANTIC_SCORE", "0.28")),
                            )
                        return semantic_threshold

                    low_referenced_sources = []
                    for key, value in semantic_scores.items():
                        if not str(key).startswith("ref:"):
                            continue
                        ref_threshold = _source_threshold_for_ref(str(key))
                        try:
                            ref_idx = int(str(key).split(":", 1)[1])
                        except (IndexError, ValueError):
                            ref_idx = 0
                        source_item = (
                            current_sources[ref_idx - 1]
                            if 0 < ref_idx <= len(current_sources)
                            and isinstance(current_sources[ref_idx - 1], dict)
                            else {}
                        )
                        effective_semantic_score = max(
                            float(value or 0.0),
                            float(source_item.get("retrieval_query_semantic_score", 0.0) or 0.0),
                            float(source_item.get("citable_semantic_score", 0.0) or 0.0),
                        )
                        if effective_semantic_score < ref_threshold:
                            low_referenced_sources.append(key)
                    if low_referenced_sources:
                        low_ref_details: List[Dict[str, Any]] = []
                        for ref_key in low_referenced_sources:
                            try:
                                ref_idx = int(str(ref_key).split(":", 1)[1])
                            except (IndexError, ValueError):
                                continue
                            source_item = (
                                current_sources[ref_idx - 1]
                                if 0 < ref_idx <= len(current_sources)
                                and isinstance(current_sources[ref_idx - 1], dict)
                                else {}
                            )
                            low_ref_details.append({
                                "reference": ref_key,
                                "title": str(source_item.get("title", "") or "")[:160],
                                "href": str(source_item.get("href") or source_item.get("url") or "")[:180],
                                "score": float(semantic_scores.get(ref_key, 0.0) or 0.0),
                                "retrieval_query_semantic_score": float(source_item.get("retrieval_query_semantic_score", 0.0) or 0.0),
                                "citable_semantic_score": float(source_item.get("citable_semantic_score", 0.0) or 0.0),
                                "threshold": _source_threshold_for_ref(str(ref_key)),
                                "supplemental_evidence_role": str(source_item.get("supplemental_evidence_role") or ""),
                            })
                        source_check = dict(source_check)
                        source_check.update({
                            "passed": False,
                            "reason": "low_semantic_referenced_source_quality",
                            "low_semantic_referenced_sources": low_referenced_sources,
                            "low_semantic_referenced_source_details": low_ref_details,
                            "trigger_controller": True,
                        })
                        verify_result = dict(verify_result)
                        verify_result["source_check"] = source_check
                        verify_result["is_passed"] = False
                        is_passed = False
                        feedback = (
                            str(feedback or "").strip()
                            + "\nReferenced sources do not sufficiently cover the document topic and subsection; use globally anchored sources."
                        ).strip()
                        verify_result["feedback"] = feedback
            
            print(
                f"         相关性: {rel_score:.4f} (阈值: {effective_rel_threshold:.2f}), "
                f"冗余度: {red_score:.4f} (阈值: {effective_red_threshold:.2f}), "
                f"is_passed={is_passed}"
            )
            if source_citation_required:
                print(
                    f"         来源检查: valid={source_check.get('passed', False)} "
                    f"refs={source_check.get('reference_count', 0)}"
                )

            quality_score = float(verify_result.get("quality_score", 0.0) or 0.0)
            quality_threshold = float(verify_result.get("quality_threshold", 0.0) or 0.0)
            quality_passed = bool(verify_result.get("quality_score_passed", False))
            semantic_dimensions = verify_result.get("quality_dimensions", {}) if isinstance(verify_result.get("quality_dimensions"), dict) else {}
            dimension_check = verify_result.get("quality_dimensions_check", {}) if isinstance(verify_result.get("quality_dimensions_check"), dict) else {}
            failed_dimensions = verify_result.get("quality_dimensions_failed", []) if isinstance(verify_result.get("quality_dimensions_failed"), list) else []
            trained_reward = self._predict_reward_score(verify_result, iterations)
            source_check_full = dict(source_check) if isinstance(source_check, dict) else {}
            source_alignment = self._source_alignment_features(
                draft,
                rag_search_result.get("results", []) or [],
                topic_anchor_terms=self._source_alignment_topic_anchor_terms(
                    current_prompt,
                    current_outline,
                    limit=14,
                ),
                topic_anchor_phrases=self._task_anchor_phrases(
                    f"{current_prompt}\n{current_outline}",
                    limit=18,
                ),
            )
            if self.source_alignment_selector_enabled:
                metrics["source_alignment_selector_used"] = 1
                verify_result = dict(verify_result)
                verify_result["source_alignment"] = source_alignment
                if self._should_audit_low_source_alignment_pass(
                    is_passed=is_passed,
                    source_alignment=source_alignment,
                    iteration=iterations,
                    controller_calls=int(metrics.get("controller_calls", 0) or 0),
                    effective_attempt_cap=effective_attempt_cap,
                    hard_min_score=self.source_alignment_min_score,
                    pass_audit_min_score=self.source_alignment_pass_audit_min_score,
                    evidence_grounding=float(semantic_dimensions.get("evidence_grounding", 1.0) or 0.0),
                    relevancy=rel_score,
                    quality_score=quality_score,
                    prior_audit_count=int(metrics.get("source_alignment_low_pass_audits", 0) or 0),
                    last_selected_arm=str(bandit_debug.get("selected_arm") or ""),
                ):
                    verify_result["source_alignment_low_pass_audit"] = True
                    verify_result["pre_source_alignment_audit_passed"] = True
                    verify_result["is_passed"] = False
                    feedback = (
                        str(feedback or "").strip()
                        + "\nsource_alignment_low_pass: preserve more source terminology and source n-grams before accepting."
                    ).strip()
                    verify_result["feedback"] = feedback
                    is_passed = False
                    metrics["source_alignment_low_pass_audits"] += 1
            if pending_controller_outcome:
                pending_event = pending_controller_outcome.get("event")
                pending_before = pending_controller_outcome.get("before")
                if isinstance(pending_event, dict) and isinstance(pending_before, dict):
                    verify_result = dict(verify_result)
                    verify_result["is_passed"] = bool(is_passed)
                    realized_outcome = self._record_realized_controller_outcome(
                        event=pending_event,
                        before=pending_before,
                        after=verify_result,
                        document_id=document_id,
                        section_id=section_id,
                        subsection_id=subsection_id,
                    )
                    pending_event.update({
                        "realized_reward": float(realized_outcome.get("reward", 0.0) or 0.0),
                        "realized_effective": bool(realized_outcome.get("effective", False)),
                        "realized_utility_delta": float(realized_outcome.get("utility_delta", 0.0) or 0.0),
                        "realized_regressed": bool(realized_outcome.get("regressed", False)),
                        "realized_task_phrase_regression": bool(realized_outcome.get("task_phrase_regression", False)),
                        "realized_pass_transition": bool(realized_outcome.get("pass_transition", False)),
                    })
                    bandit_debug.update({
                        key: value for key, value in pending_event.items() if key != "events"
                    })
                    any_controller_effective = any_controller_effective or bool(realized_outcome.get("effective", False))
                    verify_result["controller_realized_outcome"] = realized_outcome
                    realized_arm = str(pending_event.get("selected_arm") or "").strip()
                    if realized_arm:
                        realized_reward = float(realized_outcome.get("reward", 0.0) or 0.0)
                        realized_effective = bool(realized_outcome.get("effective", False))
                        pass_transition = bool(realized_outcome.get("pass_transition", False))
                        reward_floor = float(os.getenv("CONTROLLER_ARM_COOLDOWN_REWARD_FLOOR", "0.025"))
                        if pass_transition and realized_effective and realized_reward > reward_floor:
                            controller_excluded_arms.discard(realized_arm)
                        else:
                            # A material local gain is useful training signal,
                            # but if the subsection still fails, repeating the
                            # same operation tends to optimize one dimension
                            # while leaving the remaining defect untouched.
                            # Also rotate when the draft technically flips to
                            # pass but the realized reward/effectiveness says
                            # the selected arm did not cause a targeted gain.
                            controller_excluded_arms.add(realized_arm)
                    if not bool(realized_outcome.get("effective", False)):
                        current_outline = str(
                            pending_controller_outcome.get("before_outline")
                            or verification_outline_base
                        )
                        before_rag_result = pending_controller_outcome.get("before_rag_result")
                        if isinstance(before_rag_result, dict):
                            rag_search_result = dict(before_rag_result)
                        rag_context = str(pending_controller_outcome.get("before_rag_context") or "")
                        rag_selected_query = str(
                            pending_controller_outcome.get("before_rag_selected_query") or ""
                        )
                        rag_used = bool(pending_controller_outcome.get("before_rag_used", rag_used))
                        require_source_citations = bool(
                            pending_controller_outcome.get(
                                "before_require_source_citations",
                                require_source_citations,
                            )
                        )
                        source_citation_required = bool(
                            pending_controller_outcome.get(
                                "before_source_citation_required",
                                source_citation_required,
                            )
                        )
                        controller_regression_rolled_back = True
                        revision_draft = ""
                        revision_arm = ""
                        if self.history_manager:
                            try:
                                self.history_manager.update_subsection_content(
                                    document_id=document_id,
                                    section_id=section_id,
                                    subsection_id=subsection_id,
                                    outline=current_outline,
                                    iteration_count=iterations,
                                )
                            except Exception as exc:
                                print(f"⚠️  Controller 回退大纲写库失败: {exc}")
                        self._emit_progress_event(
                            document_id=document_id,
                            section_id=section_id,
                            subsection_id=subsection_id,
                            stage="controller_ineffective_rollback",
                            message="Controller 修复未带来真实定向收益或造成回退，已恢复修复前纲要与来源集合",
                            metadata={
                                "iteration": iterations,
                                "selected_arm": str(pending_event.get("selected_arm") or ""),
                                "utility_delta": float(realized_outcome.get("utility_delta", 0.0) or 0.0),
                                "effective": bool(realized_outcome.get("effective", False)),
                                "targeted_effective": bool(realized_outcome.get("targeted_effective", False)),
                                "regressed": bool(realized_outcome.get("regressed", False)),
                                "dimension_regressions": realized_outcome.get("dimension_regressions", {}),
                            },
                        )
                pending_controller_outcome = None
            available_source_reference_count = len(rag_search_result.get("results", []) or [])
            source_reference_count = max(
                int(source_check_full.get("reference_count", 0) or 0),
                available_source_reference_count,
            )
            source_check_full["reference_count"] = source_reference_count
            bound_research_intelligence = self._build_research_intelligence(
                document_title=document_title,
                section_title=section_title,
                subsection_title=subsection_title,
                outline=current_outline,
                prompt=current_prompt,
                source_results=rag_search_result.get("results", []) if isinstance(rag_search_result, dict) else [],
                draft=draft,
                planned_graph=(
                    planned_research_intelligence.get("claim_evidence_graph")
                    if isinstance(planned_research_intelligence, dict)
                    else {}
                ),
            )
            if bound_research_intelligence.get("enabled"):
                verify_result = dict(verify_result)
                verify_result["research_novelty"] = bound_research_intelligence.get("novelty", {})
                verify_result["claim_evidence_graph"] = bound_research_intelligence.get("claim_evidence_graph", {})
            self._emit_progress_event(
                document_id=document_id,
                section_id=section_id,
                subsection_id=subsection_id,
                stage="verifier_result",
                message=f"第 {iterations} 轮：Verifier 完成，结果={'通过' if is_passed else '未通过'}",
                metadata={
                    "iteration": iterations,
                    "is_passed": bool(is_passed),
                    "strict_is_passed": bool(verify_result.get("strict_is_passed", is_passed)),
                    "acceptance_mode": str(verify_result.get("acceptance_mode") or ("strict" if is_passed else "failed")),
                    "single_metric_epsilon_pass": verify_result.get("single_metric_epsilon_pass", {}),
                    "relevancy_index": rel_score,
                    "redundancy_index": red_score,
                    "rel_threshold": effective_rel_threshold,
                    "red_threshold": effective_red_threshold,
                    "feedback": str(feedback or "")[:260],
                    "quality_score": quality_score,
                    "quality_score_threshold": quality_threshold,
                    "quality_score_passed": quality_passed,
                    "quality_dimensions": semantic_dimensions,
                    "quality_dimensions_check": dimension_check,
                    "quality_dimensions_failed": failed_dimensions,
                    "quality_dimensions_passed": bool(verify_result.get("quality_dimensions_passed", False)),
                    "dimension_thresholds": verify_result.get("dimension_thresholds", {}),
                    "source_check": source_check_full,
                    "source_check_passed": bool(source_check_full.get("passed", False)),
                    "source_reference_count": source_reference_count,
                    "source_alignment": source_alignment,
                    "research_novelty": (
                        bound_research_intelligence.get("novelty", {})
                        if isinstance(bound_research_intelligence, dict)
                        else {}
                    ),
                    "claim_evidence_graph": (
                        bound_research_intelligence.get("claim_evidence_graph", {})
                        if isinstance(bound_research_intelligence, dict)
                        else {}
                    ),
                    "trained_reward_score": trained_reward.get("score", 0.0),
                    "trained_reward_model_used": bool(trained_reward.get("used", False)),
                    "trained_reward_model_version": trained_reward.get("model_version", ""),
                },
            )

            if (not is_passed) and source_citation_required:
                source_reason = str(source_check.get("reason", "") or "").lower()
                citation_feedback = str(feedback or "").lower()
                citation_failed = (
                    "insufficient_citations" in source_reason
                    or "invalid_source_url" in source_reason
                    or "low_semantic_source_quality" in source_reason
                    or "citation" in citation_feedback
                    or "来源" in citation_feedback
                )
                if self.source_citation_relaxation_enabled and citation_failed:
                    source_citation_required = False
                    self._emit_progress_event(
                        document_id=document_id,
                        section_id=section_id,
                        subsection_id=subsection_id,
                        stage="source_citation_relaxed",
                        message=f"第 {iterations} 轮：来源引用校验连续失败，后续轮次放宽硬约束",
                        metadata={
                            "iteration": iterations,
                            "source_reason": source_check.get("reason", ""),
                            "feedback": feedback[:180],
                        },
                    )

            if is_passed and self._verification_borderline_audit(
                verify_result,
                effective_rel_threshold,
                effective_red_threshold,
                iterations,
                int(metrics.get("controller_calls", 0) or 0),
            ):
                audit_reason = (
                    "borderline_redundancy_or_quality_risk: "
                    f"rel={float(rel_score):.4f}, red={float(red_score):.4f}, "
                    f"quality={float(quality_score):.4f}"
                )
                verify_result = dict(verify_result)
                verify_result["is_passed"] = False
                verify_result["borderline_audit_triggered"] = True
                verify_result["borderline_audit_reason"] = audit_reason
                feedback = (str(feedback or "").strip() + "\n" + audit_reason).strip()
                verify_result["feedback"] = feedback
                is_passed = False
                self._emit_progress_event(
                    document_id=document_id,
                    section_id=section_id,
                    subsection_id=subsection_id,
                    stage="borderline_audit_triggered",
                    message="首轮草稿已通过但接近风险阈值，触发一次 Controller 审计",
                    metadata={
                        "iteration": iterations,
                        "relevancy_index": rel_score,
                        "redundancy_index": red_score,
                        "red_threshold": effective_red_threshold,
                        "quality_score": quality_score,
                        "quality_threshold": quality_threshold,
                        "reason": audit_reason,
                    },
                )
            
            if is_passed:
                pass_candidate = {
                    "rag_used": rag_used,
                    "rag_search_success": bool(rag_search_result.get("success", False)),
                    "rag_result_count": len(rag_search_result.get("results", [])),
                    "rag_selected_query": rag_selected_query,
                    "rag_vector_indexed": int(rag_search_result.get("vector_indexed", 0) or 0),
                    "rag_vector_backend": str(rag_search_result.get("vector_backend", "") or ""),
                    "rag_reranker": str(rag_search_result.get("reranker", "") or ""),
                    "controller_effective": bool(any_controller_effective),
                    "controller_source": controller_last_result.get("source", ""),
                    "bandit": bandit_debug,
                    "source_results": rag_search_result.get("results", []),
                    "source_alignment": source_alignment,
                    "trained_reward": trained_reward,
                    "research_intelligence": bound_research_intelligence,
                    "draft": draft,
                    "outline": current_outline,
                    "iteration": iterations,
                    "verification": verify_result,
                }

                def pass_candidate_score(candidate: Dict[str, Any]) -> float:
                    verification = candidate.get("verification", {}) if isinstance(candidate, dict) else {}
                    alignment = candidate.get("source_alignment") if isinstance(candidate.get("source_alignment"), dict) else {}
                    trained = candidate.get("trained_reward", {}) if isinstance(candidate.get("trained_reward"), dict) else {}
                    text = str(candidate.get("draft") or "")
                    rel = float(verification.get("relevancy_index", 0.0) or 0.0)
                    quality = float(verification.get("quality_score", 0.0) or 0.0)
                    align = float(alignment.get("score", 0.0) or 0.0)
                    term_coverage = float(alignment.get("term_coverage", align) or 0.0)
                    bigram_overlap = float(alignment.get("bigram_overlap", align) or 0.0)
                    task_phrase = float(alignment.get("topic_phrase_coverage", 0.0) or 0.0)
                    reward = float(trained.get("score", 0.0) or 0.0) if trained.get("used") else 0.0
                    dimensions = verification.get("quality_dimensions") if isinstance(verification.get("quality_dimensions"), dict) else {}
                    evidence_grounding = float(dimensions.get("evidence_grounding", quality) or 0.0)
                    coverage_completeness = float(dimensions.get("coverage_completeness", quality) or 0.0)
                    topic_alignment = float(dimensions.get("topic_alignment", rel) or 0.0)
                    concept_terms = self._source_outline_concept_terms(
                        original_prompt=current_prompt,
                        outline=str(candidate.get("outline") or current_outline),
                        section_title=section_title,
                        subsection_title=subsection_title,
                        source_results=candidate.get("source_results", []) if isinstance(candidate.get("source_results"), list) else [],
                    )
                    concept_chain = self._concept_chain_coverage_score(text, concept_terms)
                    discourse = self._discourse_structure_score(text)
                    return (
                        0.20 * rel
                        + 0.22 * quality
                        + 0.11 * evidence_grounding
                        + 0.08 * coverage_completeness
                        + 0.07 * topic_alignment
                        + 0.05 * align
                        + 0.04 * term_coverage
                        + 0.04 * bigram_overlap
                        + 0.08 * task_phrase
                        + 0.09 * concept_chain
                        + 0.04 * discourse
                        + 0.02 * reward
                    )

                audit_concept_terms = self._source_outline_concept_terms(
                    original_prompt=current_prompt,
                    outline=current_outline,
                    section_title=section_title,
                    subsection_title=subsection_title,
                    source_results=rag_search_result.get("results", []) or [],
                )
                pass_candidate_audit.append({
                    "iteration": iterations,
                    "chars": len(draft),
                    "score": pass_candidate_score(pass_candidate),
                    "relevancy_index": rel_score,
                    "redundancy_index": red_score,
                    "quality_score": quality_score,
                    "source_alignment_score": float(source_alignment.get("score", 0.0) or 0.0),
                    "source_term_coverage": float(source_alignment.get("term_coverage", 0.0) or 0.0),
                    "source_bigram_overlap": float(source_alignment.get("bigram_overlap", 0.0) or 0.0),
                    "source_topic_anchor_coverage": float(source_alignment.get("topic_anchor_coverage", 0.0) or 0.0),
                    "task_phrase_coverage": float(source_alignment.get("topic_phrase_coverage", 0.0) or 0.0),
                    "concept_chain_score": self._concept_chain_coverage_score(draft, audit_concept_terms),
                    "discourse_structure_score": self._discourse_structure_score(draft),
                    "quality_dimensions": semantic_dimensions,
                    "trained_reward_score": float(trained_reward.get("score", 0.0) or 0.0),
                    "acceptance_mode": str(verify_result.get("acceptance_mode") or "strict"),
                })
                if best_pass_candidate is None or self._pass_candidate_should_replace(
                    pass_candidate,
                    best_pass_candidate,
                    pass_candidate_score(pass_candidate),
                    pass_candidate_score(best_pass_candidate),
                ):
                    best_pass_candidate = pass_candidate
                # Keep the global best aligned with a real pass for later
                # timeout/recovery paths, but never let an earlier failed
                # candidate compete inside the pass-only selector.
                best_candidate = best_pass_candidate
                accepted_candidates += 1
                dimension_thresholds = (
                    verify_result.get("dimension_thresholds", {})
                    if isinstance(verify_result.get("dimension_thresholds"), dict)
                    else {}
                )
                adaptive_risk_reasons = self._adaptive_best_of_risk_reasons(
                    relevancy=rel_score,
                    relevancy_threshold=effective_rel_threshold,
                    quality=quality_score,
                    quality_threshold=quality_threshold,
                    source_alignment=float(source_alignment.get("score", 0.0) or 0.0),
                    source_alignment_threshold=self.source_alignment_pass_audit_min_score,
                    quality_dimensions=semantic_dimensions,
                    dimension_thresholds=dimension_thresholds,
                )

                required_candidates = min_accepted_candidates
                marginal_pass = str(verify_result.get("acceptance_mode") or "") in {
                    "single_metric_epsilon",
                    "multidim_soft",
                }
                remaining_attempts = max(0, int(effective_attempt_cap) - int(iterations))
                comparison_budget_available = remaining_attempts >= 2
                if (
                    adaptive_best_of
                    and adaptive_risk_reasons
                    and not marginal_pass
                    and comparison_budget_available
                ):
                    required_candidates = max(required_candidates, 2)
                required_candidates = min(required_candidates, max_accepted_candidates)
                selector_decision = {
                    "mode": "adaptive" if adaptive_best_of else "fixed",
                    "accepted_candidates": accepted_candidates,
                    "required_candidates": required_candidates,
                    "max_accepted_candidates": max_accepted_candidates,
                    "remaining_attempts": remaining_attempts,
                    "comparison_budget_available": comparison_budget_available,
                    "continue": bool(
                        accepted_candidates < required_candidates
                        and iterations < effective_attempt_cap
                    ),
                    "risk_reasons": adaptive_risk_reasons,
                    "marginal_pass_stop": bool(marginal_pass),
                }
                pass_candidate_audit[-1]["selector_decision"] = selector_decision
                if selector_decision["continue"]:
                    last_negative_constraints = {
                        "feedback": (
                            "full_best_of_coverage_audit: the draft passed strict verification but remains close "
                            "to one or more quality gates, so compare one additional valid candidate. "
                            "Generate an alternative complete subsection that preserves readability while improving "
                            "coverage of source terminology, source n-grams, benchmark/evaluation terms, mechanisms, "
                            "limitations, and concrete evidence from the injected RAG sources. Do not pad mechanically."
                        ),
                        "quality_dimensions_failed": [
                            "coverage_completeness",
                            "evidence_grounding",
                            "source_alignment",
                        ],
                        "full_best_of_coverage_audit": True,
                        "accepted_candidates": accepted_candidates,
                        "required_candidates": required_candidates,
                        "selector_risk_reasons": adaptive_risk_reasons,
                    }
                    self._emit_progress_event(
                        document_id=document_id,
                        section_id=section_id,
                        subsection_id=subsection_id,
                        stage="full_best_of_coverage_audit",
                        message="Full 已获得严格通过候选，继续生成一个 source/coverage-aware 候选再择优收口",
                        metadata={
                            "iteration": iterations,
                            "accepted_candidates": accepted_candidates,
                            "required_candidates": required_candidates,
                            "selector_risk_reasons": adaptive_risk_reasons,
                            "draft_chars": len(draft),
                            "source_alignment": source_alignment,
                            "relevancy_index": rel_score,
                            "quality_score": quality_score,
                        },
                    )
                    continue
                selected_candidate = (
                    best_pass_candidate
                    if isinstance(best_pass_candidate, dict)
                    else pass_candidate
                )
                selected_verification = selected_candidate.get("verification", verify_result)
                selected_alignment = selected_candidate.get("source_alignment", source_alignment)
                selected_draft = str(selected_candidate.get("draft") or draft)
                selected_outline = str(selected_candidate.get("outline") or current_outline)
                selected_iteration = int(selected_candidate.get("iteration", iterations) or iterations)
                last_negative_constraints = None
                print(f"         ✨ 验证通过!")
                self._emit_progress_event(
                    document_id=document_id,
                    section_id=section_id,
                    subsection_id=subsection_id,
                    stage="verifier_passed",
                    message=f"第 {iterations} 轮：Verifier 判定通过",
                    metadata={
                        "iteration": iterations,
                        "selected_iteration": selected_iteration,
                        "selected_candidate_score": pass_candidate_score(selected_candidate),
                        "pass_candidate_audit": pass_candidate_audit,
                        "relevancy_index": rel_score,
                        "redundancy_index": red_score,
                        "source_alignment": selected_alignment,
                    },
                )
                
                if self.history_manager:
                    self.history_manager.update_subsection_content(
                        document_id=document_id,
                        section_id=section_id,
                        subsection_id=subsection_id,
                        generated_content=selected_draft,
                        outline=selected_outline,
                        relevancy_index=selected_verification.get("relevancy_index", rel_score),
                        redundancy_index=selected_verification.get("redundancy_index", red_score),
                        is_passed=True,
                        iteration_count=selected_iteration
                    )
                
                return {
                    "success": True,
                    "draft": selected_draft,
                    "final_outline": selected_outline,
                    "iterations": iterations,
                    "source_results": selected_candidate.get("source_results", rag_search_result.get("results", [])),
                    "rag_used": bool(selected_candidate.get("rag_used", rag_used)),
                    "rag_search_success": bool(selected_candidate.get("rag_search_success", bool(rag_search_result.get("success", False)))),
                    "rag_result_count": int(selected_candidate.get("rag_result_count", len(rag_search_result.get("results", []))) or 0),
                    "rag_selected_query": str(selected_candidate.get("rag_selected_query", rag_selected_query) or ""),
                    "rag_vector_indexed": int(selected_candidate.get("rag_vector_indexed", rag_search_result.get("vector_indexed", 0)) or 0),
                    "rag_vector_backend": str(selected_candidate.get("rag_vector_backend", rag_search_result.get("vector_backend", "")) or ""),
                    "rag_reranker": str(selected_candidate.get("rag_reranker", rag_search_result.get("reranker", "")) or ""),
                    "controller_effective": bool(selected_candidate.get("controller_effective", any_controller_effective)),
                    "controller_source": selected_candidate.get("controller_source", controller_last_result.get("source", "")),
                    "verification": selected_verification,
                    "source_alignment": selected_alignment,
                    "bandit": selected_candidate.get("bandit", bandit_debug),
                    "trained_reward": selected_candidate.get("trained_reward", trained_reward),
                    "research_intelligence": selected_candidate.get("research_intelligence", bound_research_intelligence),
                    "pass_candidate_audit": pass_candidate_audit,
                    "all_drafts": all_drafts,
                    "forced_pass": False,
                    "force_reason": "",
                    "controller_triggered": (metrics.get("controller_calls", 0) > 0) or controller_triggered,
                    "controller_retry_count": controller_retry_count,
                    "metrics": metrics,
                }

            current_candidate = {
                "rag_used": rag_used,
                "rag_search_success": bool(rag_search_result.get("success", False)),
                "rag_result_count": len(rag_search_result.get("results", [])),
                "rag_selected_query": rag_selected_query,
                "rag_vector_indexed": int(rag_search_result.get("vector_indexed", 0) or 0),
                "rag_vector_backend": str(rag_search_result.get("vector_backend", "") or ""),
                "rag_reranker": str(rag_search_result.get("reranker", "") or ""),
                "controller_effective": bool(any_controller_effective),
                "controller_source": controller_last_result.get("source", ""),
                "bandit": bandit_debug,
                "source_results": rag_search_result.get("results", []),
                "source_alignment": source_alignment,
                "trained_reward": trained_reward,
                "research_intelligence": bound_research_intelligence,
                "draft": draft,
                "outline": current_outline,
                "iteration": iterations,
                "verification": verify_result,
            }
            last_negative_constraints = verify_result
            if best_candidate is None:
                best_candidate = current_candidate
            else:
                def candidate_gap(candidate: Dict[str, Any]) -> float:
                    verification = candidate.get("verification", {}) if isinstance(candidate, dict) else {}
                    cand_rel = float(verification.get("relevancy_index", 0) or 0)
                    cand_red = _coerce_float(verification.get("redundancy_index"), 1.0)
                    cand_quality = float(verification.get("quality_score", 0) or 0)
                    cand_quality_threshold = float(verification.get("quality_threshold", quality_threshold) or quality_threshold or 0)
                    cand_failed_dims = verification.get("quality_dimensions_failed", [])
                    failed_dim_count = len(cand_failed_dims) if isinstance(cand_failed_dims, list) else 0
                    trained = candidate.get("trained_reward", {}) if isinstance(candidate.get("trained_reward"), dict) else {}
                    trained_score = float(trained.get("score", 0.0) or 0.0) if trained.get("used") else 0.0
                    alignment = candidate.get("source_alignment") if isinstance(candidate.get("source_alignment"), dict) else {}
                    alignment_score = float(alignment.get("score", 0.0) or 0.0) if self.source_alignment_selector_enabled else 0.0
                    return (
                        max(0.0, rel_threshold - cand_rel) * 1.25
                        + max(0.0, cand_red - red_threshold)
                        + max(0.0, cand_quality_threshold - cand_quality) * 0.85
                        + failed_dim_count * 0.08
                        - trained_score * 0.20
                        - alignment_score * self.source_alignment_best_weight
                    )

                best_gap = candidate_gap(best_candidate)
                current_gap = candidate_gap(current_candidate)
                best_quality = float(best_candidate.get("verification", {}).get("quality_score", 0) or 0)
                best_rel = float(best_candidate.get("verification", {}).get("relevancy_index", 0) or 0)
                if self._should_replace_best_candidate(
                    best_candidate=best_candidate,
                    current_candidate=current_candidate,
                    best_gap=best_gap,
                    current_gap=current_gap,
                    source_alignment_preferred=self._source_alignment_candidate_preferred(current_candidate, best_candidate),
                    current_quality=quality_score,
                    current_rel=rel_score,
                    best_quality=best_quality,
                    best_rel=best_rel,
                ):
                    best_candidate = current_candidate

            if best_candidate and controller_triggered:
                best_verification = best_candidate.get("verification", {})
                current_is_worse_than_best = current_candidate is not best_candidate
                allow_best_effort_pass = os.getenv("FLOWERNET_ALLOW_BEST_EFFORT_PASS", "false").lower() == "true"
                if (
                    allow_best_effort_pass
                    and
                    current_is_worse_than_best
                    and self._verification_near_pass(best_verification, rel_threshold, red_threshold)
                ):
                    self._emit_progress_event(
                        document_id=document_id,
                        section_id=section_id,
                        subsection_id=subsection_id,
                        stage="verifier_best_effort_pass",
                        message="Controller 后续轮次未优于最佳草稿，保留最佳近通过结果并继续",
                        metadata={
                            "iteration": iterations,
                            "best_iteration": best_candidate.get("iteration", iterations),
                            "relevancy_index": best_verification.get("relevancy_index", 0),
                            "redundancy_index": best_verification.get("redundancy_index", 1),
                            "quality_score": best_verification.get("quality_score", 0),
                            "source_alignment": best_candidate.get("source_alignment", {}),
                            "trained_reward_score": (best_candidate.get("trained_reward", {}) if isinstance(best_candidate.get("trained_reward"), dict) else {}).get("score", 0.0),
                            "trained_reward_model_used": bool((best_candidate.get("trained_reward", {}) if isinstance(best_candidate.get("trained_reward"), dict) else {}).get("used", False)),
                            "reason": "prevent_controller_regression",
                        },
                    )
                    return {
                        "success": True,
                        "draft": best_candidate.get("draft", ""),
                        "final_outline": best_candidate.get("outline", current_outline),
                        "iterations": iterations,
                        "source_results": best_candidate.get("source_results", rag_search_result.get("results", [])),
                        "rag_used": bool(best_candidate.get("rag_used", rag_used)),
                        "rag_search_success": bool(best_candidate.get("rag_search_success", bool(rag_search_result.get("success", False)))),
                        "rag_result_count": int(best_candidate.get("rag_result_count", len(rag_search_result.get("results", []))) or 0),
                        "rag_selected_query": str(best_candidate.get("rag_selected_query", rag_selected_query) or ""),
                        "rag_vector_indexed": int(best_candidate.get("rag_vector_indexed", rag_search_result.get("vector_indexed", 0)) or 0),
                        "rag_vector_backend": str(best_candidate.get("rag_vector_backend", rag_search_result.get("vector_backend", "")) or ""),
                        "rag_reranker": str(best_candidate.get("rag_reranker", rag_search_result.get("reranker", "")) or ""),
                        "controller_effective": bool(best_candidate.get("controller_effective", any_controller_effective)),
                        "controller_source": str(best_candidate.get("controller_source", controller_last_result.get("source", "")) or ""),
                        "verification": {
                            **best_verification,
                            "forced_pass": False,
                            "force_reason": "prevent_controller_regression",
                        },
                        "source_alignment": best_candidate.get("source_alignment", {}),
                        "bandit": best_candidate.get("bandit", {}),
                        "trained_reward": best_candidate.get("trained_reward", {}),
                        "all_drafts": all_drafts,
                        "forced_pass": False,
                        "force_reason": "prevent_controller_regression",
                        "controller_triggered": (metrics.get("controller_calls", 0) > 0) or controller_triggered,
                        "controller_retry_count": controller_retry_count,
                        "metrics": metrics,
                    }

            if controller_regression_rolled_back:
                self._emit_progress_event(
                    document_id=document_id,
                    section_id=section_id,
                    subsection_id=subsection_id,
                    stage="controller_regression_retry_same_outline",
                    message="回退后保留 Verifier 反馈，使用原任务纲要进行下一轮再生成",
                    metadata={"iteration": iterations},
                )
                continue

            if verify_result.get("source_alignment_low_pass_audit"):
                self._emit_progress_event(
                    document_id=document_id,
                    section_id=section_id,
                    subsection_id=subsection_id,
                    stage="source_alignment_controller_repair",
                    message="Source-alignment 审计触发：交由 Controller 做来源术语与证据锚点定向修复",
                    metadata={
                        "iteration": iterations,
                        "relevancy_index": rel_score,
                        "redundancy_index": red_score,
                        "quality_score": quality_score,
                        "source_alignment": source_alignment,
                    },
                )
                revision_draft = draft
                revision_arm = "defect_evidence"

            borderline_rel_gap = max(0.0, float(effective_rel_threshold) - float(rel_score or 0.0))
            borderline_rel_retry_max = max(
                0.0,
                float(os.getenv("CONTROLLER_BORDERLINE_REL_RETRY_MAX_GAP", "0.015")),
            )
            if (
                borderline_rel_gap > 0.0
                and borderline_rel_gap <= borderline_rel_retry_max
                and float(red_score or 0.0) <= float(effective_red_threshold)
                and bool(source_check.get("passed", False))
                and quality_passed
                and not failed_dimensions
            ):
                self._emit_progress_event(
                    document_id=document_id,
                    section_id=section_id,
                    subsection_id=subsection_id,
                    stage="borderline_relevance_regenerate_same_outline",
                    message="相关性轻微低于阈值：保留近通过草稿，并交由 Controller 做最小定向修复",
                    metadata={
                        "iteration": iterations,
                        "relevancy_index": rel_score,
                        "rel_threshold": effective_rel_threshold,
                        "gap": round(borderline_rel_gap, 4),
                        "max_gap": borderline_rel_retry_max,
                    },
                )
                revision_draft = draft
                revision_arm = "defect_topic"

            if effective_attempt_cap > 0 and iterations >= effective_attempt_cap:
                self._emit_progress_event(
                    document_id=document_id,
                    section_id=section_id,
                    subsection_id=subsection_id,
                    stage="controller_skipped_no_generation_budget",
                    message="已到最后生成轮次，不再调用无法被下一轮采用的 Controller 修复",
                    metadata={
                        "iteration": iterations,
                        "effective_attempt_cap": effective_attempt_cap,
                    },
                )
                continue

            print(f"         🔧 调用 Controller...")
            self._emit_progress_event(
                document_id=document_id,
                section_id=section_id,
                subsection_id=subsection_id,
                stage="verifier_failed",
                message=f"第 {iterations} 轮：Verifier 判定不通过，进入 Controller",
                metadata={
                    "iteration": iterations,
                    "relevancy_index": rel_score,
                    "redundancy_index": red_score,
                },
            )
            print(f"🎯 [DEBUG] About to call controller (iteration={iterations}, is_passed={is_passed})")
            metrics["verifier_failed"] += 1

            controller_retry = 0
            controller_updated = False
            while True:
                controller_retry += 1
                controller_retry_count += 1
                if controller_retry > self.max_controller_retries:
                    # 改进：Controller 完全失败时，不再用 outline fallback，而是继续下一轮
                    # 系统会在 verifier 失败后继续尝试，最终通过 best_candidate 机制返回最好的 draft
                    print(f"         ⚠️  Controller 失败 {self.max_controller_retries} 次，不应用改纲而直接继续")
                    self._emit_progress_event(
                        document_id=document_id,
                        section_id=section_id,
                        subsection_id=subsection_id,
                        stage="controller_exhausted",
                        message=(
                            f"第 {iterations} 轮：Controller 连续失败 {self.max_controller_retries} 次，"
                            "跳过改纲继续重试"
                        ),
                        metadata={
                            "iteration": iterations,
                            "controller_retry": self.max_controller_retries,
                            "skipped_outline_fallback": True,
                        },
                    )
                    metrics["controller_exhausted"] += 1
                    break

                self._emit_progress_event(
                    document_id=document_id,
                    section_id=section_id,
                    subsection_id=subsection_id,
                    stage="controller_start",
                    message=f"第 {iterations} 轮：Controller 第 {controller_retry} 次尝试改纲",
                    metadata={
                        "iteration": iterations,
                        "controller_retry": controller_retry,
                        "failed_rel_threshold": effective_rel_threshold,
                        "failed_red_threshold": effective_red_threshold,
                        "draft_chars": len(draft),
                    },
                )
                controller_triggered = True
                metrics["controller_calls"] += 1
                # 增强 feedback 对象，包含强化改纲策略约束
                enhanced_feedback = dict(verify_result or {})
                enhanced_feedback["controller_strategy_instruction"] = (
                    f"【改纲强化指令】本轮必须通过改进大纲来强化以下方面：\n"
                    f"1. 加强对【{current_outline.split(chr(10))[0][:60]}】的核心概念论述\n"
                    f"2. 确保大纲中的每个要点都能找到相关学术/可靠来源支持\n"
                    f"3. 增加具体数据、案例、图表引用的placeholder\n"
                    f"4. 避免与其他学科跨域（若涉及数学/语言学等边界主题，需明确标注）\n"
                    f"5. 改进逻辑递进：前置概念 → 核心观点 → 证据支撑 → 实际应用\n"
                    f"【最终验证】改纲后的大纲必须能支撑 ≥{effective_rel_threshold:.2f} 相关性 && ≤{effective_red_threshold:.2f} 冗余度"
                )
                controller_result = self._call_controller(
                    old_outline=self._resolve_subsection_outline(
                        document_id=document_id,
                        section_id=section_id,
                        subsection_id=subsection_id,
                        fallback_outline=current_outline,
                    ),
                    failed_draft=draft,
                    feedback=enhanced_feedback,
                    outline=outline,
                    history=[h["content"] for h in windowed_history],
                    iteration=iterations,
                    rel_threshold=effective_rel_threshold,
                    red_threshold=effective_red_threshold,
                    document_id=document_id,
                    section_id=section_id,
                    subsection_id=subsection_id,
                    excluded_arms=sorted(controller_excluded_arms),
                )
                controller_last_result = controller_result
                controller_error_text = str(controller_result.get("error", "") or "")

                improved_outline = str(controller_result.get("improved_outline", "")).strip()
                controller_effective = bool(controller_result.get("effective", controller_result.get("success", False)))
                controller_changed = bool(controller_result.get("changed", True))
                # Guard against false-positive changed flags from controller fallback responses.
                def _norm_outline(text: str) -> str:
                    return " ".join(str(text or "").strip().split()).lower()
                real_outline_changed = bool(improved_outline) and (_norm_outline(improved_outline) != _norm_outline(current_outline))
                improved_outline_key = _norm_outline(improved_outline)
                outline_cycle_detected = bool(
                    real_outline_changed
                    and improved_outline_key
                    and improved_outline_key in seen_controller_outlines
                )
                if controller_changed and not real_outline_changed:
                    controller_changed = False

                def _outline_terms(text: str) -> set:
                    raw = str(text or "").lower()
                    tokens = re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z][a-zA-Z0-9_-]{2,}", raw)
                    stop = {
                        "section", "subsection", "outline", "draft", "content", "please",
                        "generate", "analysis", "example", "examples", "reference",
                        "references", "research", "academic", "chapter",
                        "小节", "章节", "大纲", "内容", "生成", "请帮", "高质量",
                        "长文档", "参考文献", "研究", "分析",
                    }
                    return {tok for tok in tokens if tok not in stop}

                def _outline_guard(old_text: str, new_text: str) -> tuple[bool, str, float]:
                    if not self.controller_guard_enabled:
                        return True, "guard_disabled", 1.0
                    old = str(old_text or "").strip()
                    new = str(new_text or "").strip()
                    if not new:
                        return False, "empty_outline", 0.0
                    if len(old) >= 80 and len(new) < max(80, int(len(old) * 0.55)):
                        return False, "outline_too_short", 0.0
                    prompt_like_patterns = [
                        "请帮我生成", "帮我生成", "please generate", "as an ai",
                        "下面是", "以下是", "高质量长文档", "document topic",
                    ]
                    lowered = new.lower()
                    if any(pat in lowered for pat in prompt_like_patterns):
                        return False, "prompt_like_outline", 0.0
                    old_terms = _outline_terms(old)
                    new_terms = _outline_terms(new)
                    if old_terms:
                        retention = len(old_terms & new_terms) / max(1, len(old_terms))
                        old_cjk_bigrams = set()
                        new_cjk_text = "".join(re.findall(r"[\u4e00-\u9fff]+", new))
                        for chunk in re.findall(r"[\u4e00-\u9fff]{3,}", old):
                            for idx in range(0, max(0, len(chunk) - 1)):
                                gram = chunk[idx:idx + 2]
                                if gram not in {"本小", "小节", "当前", "大纲", "内容", "生成", "写作", "文档", "主题", "要求"}:
                                    old_cjk_bigrams.add(gram)
                        if old_cjk_bigrams:
                            cjk_retention = sum(1 for gram in old_cjk_bigrams if gram in new_cjk_text) / max(1, len(old_cjk_bigrams))
                            retention = max(retention, cjk_retention)
                        if retention < self.controller_min_outline_retention:
                            return False, f"low_topic_retention:{retention:.2f}", retention
                        return True, "accepted", retention
                    return True, "accepted_no_old_terms", 1.0

                outline_guard_ok, outline_guard_reason, outline_retention = _outline_guard(current_outline, improved_outline)
                if controller_changed and not outline_guard_ok:
                    controller_changed = False
                    controller_effective = False
                if outline_cycle_detected:
                    controller_changed = False
                    controller_effective = False
                    outline_guard_ok = False
                    outline_guard_reason = "outline_cycle_detected"

                # 提取 bandit 信息：优先从 Controller 响应，否则从最新 bandit 事件读取
                _selected_arm = str(controller_result.get("selected_arm", "") or "")
                _reward_val = float(controller_result.get("reward", 0.0) or 0.0)
                _selection_mode = ""
                
                # 尝试从 controller 响应的 bandit.selection.mode 提取 mode
                bandit_obj = controller_result.get("bandit") if isinstance(controller_result.get("bandit"), dict) else {}
                selection_obj = bandit_obj.get("selection") if isinstance(bandit_obj.get("selection"), dict) else {}
                _selection_mode = str(selection_obj.get("mode", "") or "")
                _trained_policy_used = bool(selection_obj.get("trained_policy_used", False))
                _trained_policy_version = str(selection_obj.get("trained_policy_version", "") or "")
                _trained_policy_blend = float(selection_obj.get("trained_policy_blend", 0.0) or 0.0)
                
                # 关键：如果本轮 controller 成功但响应中没有 arm/reward，才从最近 bandit 事件读取。
                # controller 超时/不可用时不能读取旧事件，否则会把上一轮 arm 误记成本轮真实 Bandit 使用。
                if self._can_use_last_bandit_event(controller_result) and (not _selected_arm or _reward_val == 0.0):
                    last_ev = self._read_last_bandit_event()
                    if isinstance(last_ev, dict):
                        _selected_arm = _selected_arm or str(last_ev.get("chosen_arm") or "")
                        if _reward_val == 0.0:  # 如果响应中没有 reward，则用文件中的
                            try:
                                _reward_val = float(last_ev.get("reward", 0.0) or 0.0)
                            except Exception:
                                pass

                controller_defect_graph = (
                    controller_result.get("defect_graph")
                    if isinstance(controller_result.get("defect_graph"), dict)
                    else bandit_obj.get("defect_graph")
                    if isinstance(bandit_obj.get("defect_graph"), dict)
                    else {}
                )
                strategy_only_repair = bool(
                    not controller_changed
                    and improved_outline
                    and not outline_cycle_detected
                    and self._strategy_only_repair_allowed(_selected_arm, controller_defect_graph)
                )

                print(f"🎯 [Bandit Emit] 发出 controller_result 事件: arm={_selected_arm}, reward={_reward_val}, mode={_selection_mode}")
                self._emit_progress_event(
                    document_id=document_id,
                    section_id=section_id,
                    subsection_id=subsection_id,
                    stage="controller_result",
                    message=(
                        f"第 {iterations} 轮：Controller 返回 {'有效' if controller_effective else '可用' if (controller_result.get('success') and improved_outline and controller_changed) else '无效'} 改纲"
                    ),
                    metadata={
                        "iteration": iterations,
                        "controller_retry": controller_retry,
                        "success": bool(controller_result.get("success", False)),
                        "effective": controller_effective,
                        "changed": controller_changed,
                        "changed_real": real_outline_changed,
                        "outline_cycle_detected": outline_cycle_detected,
                        "outline_guard_ok": outline_guard_ok,
                        "outline_guard_reason": outline_guard_reason,
                        "outline_term_retention": outline_retention,
                        "selected_arm": _selected_arm,
                        "reward": float(_reward_val or 0.0),
                        "selection_mode": _selection_mode,
                        "trained_policy_used": _trained_policy_used,
                        "trained_policy_version": _trained_policy_version,
                        "trained_policy_blend": _trained_policy_blend,
                        "improved_outline_chars": len(improved_outline),
                        "strategy_only_repair": strategy_only_repair,
                        "error": controller_error_text[:260],
                    },
                )

                controller_ok_for_next_round = (
                    controller_result.get("success")
                    and improved_outline
                    and (
                        (
                            controller_changed
                            and (
                                controller_effective
                                or (
                                    self.accept_ineffective_controller_outline
                                    and not self.strict_controller_effective
                                )
                            )
                        )
                        or strategy_only_repair
                    )
                )

                # Preserve every Controller decision in this subsection. The
                # Controller reward is a candidate/proposal score; application
                # acceptance is recorded separately because the Generator guard
                # can legitimately reject an otherwise high-scoring proposal.
                bandit_event = dict(bandit_obj)
                bandit_event.update({
                    "selected_arm": _selected_arm,
                    "reward": float(_reward_val or 0.0),
                    "proposal_reward": float(_reward_val or 0.0),
                    "reward_semantics": "controller_candidate_proxy_pre_regeneration",
                    "application_accepted": bool(controller_ok_for_next_round),
                    "controller_effective": bool(controller_effective),
                    "strategy_only_repair": strategy_only_repair,
                    "outline_guard_ok": bool(outline_guard_ok),
                    "outline_guard_reason": str(outline_guard_reason or ""),
                    "outline_term_retention": float(outline_retention or 0.0),
                })
                bandit_debug.setdefault("events", []).append(bandit_event)
                bandit_debug.update({
                    key: value for key, value in bandit_event.items() if key != "events"
                })
                controller_result["bandit"] = bandit_debug

                if not controller_ok_for_next_round:
                    if _selected_arm:
                        # A proposal rejected by the outline/application guard
                        # has already demonstrated that it cannot affect the
                        # next generation. Cool it immediately instead of
                        # waiting for a Verifier outcome that will never exist.
                        controller_excluded_arms.add(_selected_arm)
                    bandit_event.update({
                        "realized_reward": 0.0,
                        "realized_effective": False,
                        "realized_utility_delta": 0.0,
                        "realized_regressed": False,
                        "realized_pass_transition": False,
                    })
                    bandit_debug.update({
                        key: value for key, value in bandit_event.items() if key != "events"
                    })

                if controller_ok_for_next_round:
                    if controller_changed and improved_outline_key:
                        seen_controller_outlines.add(improved_outline_key)
                    if strategy_only_repair:
                        metrics["controller_strategy_only_repairs"] += 1
                    pending_controller_outcome = {
                        "event": bandit_event,
                        "before": dict(verify_result),
                        "before_outline": current_outline,
                        "before_rag_result": dict(rag_search_result),
                        "before_rag_context": rag_context,
                        "before_rag_selected_query": rag_selected_query,
                        "before_rag_used": rag_used,
                        "before_require_source_citations": require_source_citations,
                        "before_source_citation_required": source_citation_required,
                    }
                    revision_draft = draft
                    revision_arm = _selected_arm
                    if _selected_arm == "defect_evidence":
                        refreshed_rag = self._refresh_rag_for_evidence_repair(
                            document_title=document_title,
                            improved_outline=improved_outline,
                            initial_prompt=current_prompt,
                            document_id=document_id,
                        )
                        if refreshed_rag:
                            refreshed_result = refreshed_rag.get("result", {})
                            refreshed_sources = refreshed_result.get("results", []) or []
                            old_sources = rag_search_result.get("results", []) or []
                            old_set_score = self._score_rag_result_set(
                                document_title,
                                improved_outline,
                                old_sources,
                            )
                            refreshed_set_score = float(refreshed_rag.get("set_score", 0.0) or 0.0)
                            old_urls = {
                                str(item.get("href") or item.get("url") or "").strip()
                                for item in old_sources
                                if str(item.get("href") or item.get("url") or "").strip()
                            }
                            refreshed_urls = {
                                str(item.get("href") or item.get("url") or "").strip()
                                for item in refreshed_sources
                                if str(item.get("href") or item.get("url") or "").strip()
                            }
                            max_score_drop = float(os.getenv("FLOWERNET_EVIDENCE_REFRESH_MAX_SCORE_DROP", "0.02"))
                            refresh_applied = bool(
                                refreshed_sources
                                and refreshed_urls != old_urls
                                and refreshed_set_score + max_score_drop >= old_set_score
                            )
                            if refresh_applied:
                                rag_search_result = refreshed_result
                                rag_context = str(refreshed_rag.get("context") or "")
                                rag_selected_query = str(refreshed_rag.get("query") or "")
                                rag_used = True
                                require_source_citations = True
                                source_citation_required = True
                                metrics["evidence_rag_refreshes"] += 1
                            self._emit_progress_event(
                                document_id=document_id,
                                section_id=section_id,
                                subsection_id=subsection_id,
                                stage=("evidence_rag_refresh_applied" if refresh_applied else "evidence_rag_refresh_retained"),
                                message=(
                                    "Evidence arm 已刷新并应用更匹配的来源集合"
                                    if refresh_applied
                                    else "Evidence arm 已检索新来源，但保留质量更高的原来源集合"
                                ),
                                metadata={
                                    "iteration": iterations,
                                    "old_result_count": len(old_sources),
                                    "new_result_count": len(refreshed_sources),
                                    "old_set_score": old_set_score,
                                    "new_set_score": refreshed_set_score,
                                    "query": str(refreshed_rag.get("query") or ""),
                                    "applied": refresh_applied,
                                },
                            )
                    current_outline = improved_outline if controller_changed else current_outline
                    controller_updated = True
                    # 回写 controller 改进的大纲到数据库
                    if self.history_manager:
                        try:
                            self.history_manager.update_subsection_content(
                                document_id=document_id,
                                section_id=section_id,
                                subsection_id=subsection_id,
                                outline=current_outline,
                                iteration_count=iterations,
                            )
                        except Exception as _e:
                            print(f"⚠️  回写改进大纲失败: {_e}")
                    print(f"         ✅ 大纲已改进（controller重试 {controller_retry} 次）")
                    self._emit_progress_event(
                        document_id=document_id,
                        section_id=section_id,
                        subsection_id=subsection_id,
                        stage="controller_success",
                        message=f"第 {iterations} 轮：Controller 改纲成功，返回 Generator",
                        metadata={
                            "iteration": iterations,
                            "controller_retry": controller_retry,
                            "effective": controller_effective,
                            "changed": controller_changed,
                            "strict_controller_effective": self.strict_controller_effective,
                            "outline_guard_ok": outline_guard_ok,
                            "outline_guard_reason": outline_guard_reason,
                            "outline_term_retention": outline_retention,
                            "strategy_only_repair": strategy_only_repair,
                        },
                    )
                    metrics["controller_success"] += 1
                    break

                if controller_result.get("success") and not improved_outline:
                    # 改进：Controller 返回空改纲时，不应用 outline fallback
                    # 而是继续下一轮，让 best_candidate 机制选择最好的 draft
                    print(f"         ⚠️  Controller 返回空改纲，不应用fallback而直接继续")
                    self._emit_progress_event(
                        document_id=document_id,
                        section_id=section_id,
                        subsection_id=subsection_id,
                        stage="controller_empty_outline",
                        message=f"第 {iterations} 轮：Controller 返回空改纲，跳过应用",
                        metadata={
                            "iteration": iterations,
                            "controller_retry": controller_retry,
                            "skipped_outline_fallback": True,
                        },
                    )
                    break

                if controller_result.get("success") and (not controller_changed or (self.strict_controller_effective and not controller_effective)):
                    metrics["controller_ineffective"] += 1
                    self._emit_progress_event(
                        document_id=document_id,
                        section_id=section_id,
                        subsection_id=subsection_id,
                        stage="controller_ineffective",
                        message=f"第 {iterations} 轮：Controller 改纲无效，继续重试",
                        metadata={
                            "iteration": iterations,
                            "controller_retry": controller_retry,
                            "effective": controller_effective,
                            "changed": controller_changed,
                            "outline_guard_ok": outline_guard_ok,
                            "outline_guard_reason": outline_guard_reason,
                            "outline_term_retention": outline_retention,
                            "error": controller_error_text[:260],
                            "improved_outline_chars": len(improved_outline),
                        },
                    )
                    time.sleep(self._compute_retry_delay(controller_retry))
                    continue

                transient_unavailable = any(token in controller_error_text.lower() for token in [
                    "connection refused",
                    "max retries exceeded",
                    "timed out",
                    "read timeout",
                    "service unavailable",
                    "name or service not known",
                ])
                if transient_unavailable:
                    metrics["controller_unavailable"] += 1
                    # 改进：Controller 暂不可用时，不应用 outline fallback
                    # 而是继续重试，让系统通过 best_candidate 机制选择最好的 draft
                    print(f"         ⚠️  Controller 暂不可用，跳过outline fallback直接重试")
                    self._emit_progress_event(
                        document_id=document_id,
                        section_id=section_id,
                        subsection_id=subsection_id,
                        stage="controller_unavailable",
                        message=f"第 {iterations} 轮：Controller 暂不可用，继续重试",
                        metadata={
                            "iteration": iterations,
                            "controller_retry": controller_retry,
                            "error": controller_error_text[:260],
                            "skipped_outline_fallback": True,
                        },
                    )
                    time.sleep(self._compute_retry_delay(controller_retry))
                    continue

                print(f"         ⚠️  Controller 失败，继续重试（第 {controller_retry} 次）")
                metrics["controller_error"] += 1
                self._emit_progress_event(
                    document_id=document_id,
                    section_id=section_id,
                    subsection_id=subsection_id,
                    stage="controller_error",
                    message=f"第 {iterations} 轮：Controller 改纲失败，继续重试",
                    metadata={
                        "iteration": iterations,
                        "controller_retry": controller_retry,
                        "error": controller_error_text[:260],
                    },
                )
                time.sleep(self._compute_retry_delay(controller_retry))

            if not controller_updated:
                if self.local_outline_fallback_enabled:
                    # Optional fallback only. It is disabled by default because
                    # heuristic outline rewrites can drift from the topic and
                    # make later verifier snapshots worse.
                    current_outline = self._build_local_outline_fallback(
                        current_outline=current_outline,
                        original_outline=outline,
                        feedback=verify_result,
                        rel_threshold=effective_rel_threshold,
                        red_threshold=effective_red_threshold,
                        iteration=iterations,
                    )
            
            # Controller改纲完成，继续回到外层循环尝试下一轮生成
            continue

    def _prefers_english_generation(self, outline: str, original_prompt: str) -> bool:
        text = f"{outline or ''}\n{original_prompt or ''}"
        latin = len(re.findall(r"\b[A-Za-z][A-Za-z0-9+._/-]*\b", text))
        chinese = len(re.findall(r"[\u4e00-\u9fff]", text))
        return latin >= 10 and latin > max(3, chinese)

    @staticmethod
    def _is_predominantly_english_text(text: str) -> bool:
        raw = str(text or "")
        latin = len(re.findall(r"\b[A-Za-z][A-Za-z0-9+._/-]*\b", raw))
        cjk = len(re.findall(r"[\u4e00-\u9fff]", raw))
        return latin >= 20 and cjk <= max(12, latin * 0.25)

    def _build_enhanced_prompt(
        self,
        original_prompt: str,
        outline: str,
        history_text: str,
        rel_threshold: float,
        red_threshold: float,
        rag_context: str,
        require_source_citations: bool,
        available_source_count: int = 0,
        negative_constraints: Optional[Dict[str, Any]] = None,
        source_results: Optional[List[Dict[str, Any]]] = None,
        research_intelligence: Optional[Dict[str, Any]] = None,
        revision_draft: str = "",
        revision_arm: str = "",
    ) -> str:
        """
        构建增强的生成提示，按照正确流程:
        - 大纲（已此前存储在数据库的 subsection outline）
        - history（已通过验证的前置小节）
        一起发送给 LLM，提示生成高相关性、低冗余度的内容。
        """
        def _clip(text: str, max_chars: int, label: str) -> str:
            raw = str(text or "").strip()
            if len(raw) <= max_chars:
                return raw
            head = max(100, int(max_chars * 0.8))
            tail = max(60, max_chars - head)
            return (
                raw[:head]
                + f"\n\n[...{label}已裁剪，原始长度 {len(raw)} 字，保留首尾关键信息...]\n\n"
                + raw[-tail:]
            )

        outline = _clip(outline, self.prompt_outline_max_chars, "outline")
        original_prompt = _clip(original_prompt, self.prompt_original_max_chars, "original_prompt")
        rag_context = _clip(rag_context, self.prompt_rag_max_chars, "rag_context")
        history_limit = (
            self.prompt_novelty_history_max_chars
            if revision_arm == "defect_novelty"
            else self.prompt_history_max_chars
        )
        history_text = _clip(history_text, history_limit, "history")
        revision_draft = _clip(revision_draft, self.prompt_revision_max_chars, "revision_draft")
        if original_prompt and outline:
            compact_original = " ".join(original_prompt.split())
            compact_outline = " ".join(outline.split())
            if compact_outline and compact_outline in compact_original:
                original_prompt = compact_original.replace(compact_outline, "[同当前小节详细大纲，已省略重复文本]")

        english_generation = self._prefers_english_generation(outline, original_prompt)
        coverage_terms = self._extract_generation_coverage_terms(
            outline=outline,
            original_prompt=original_prompt,
            source_results=source_results or [],
            english=english_generation,
        )
        source_alignment_terms: List[str] = []
        source_alignment_phrases: List[str] = []
        source_alignment_repair_terms: List[str] = []
        source_alignment_topic_terms: List[str] = []
        source_alignment_audit_active = False
        if self.source_alignment_selector_enabled and source_results:
            source_alignment_topic_terms = self._source_alignment_topic_anchor_terms(
                original_prompt,
                outline,
                limit=14,
            )
            source_alignment_phrases = self._source_title_phrases(source_results or [])
            stop = {
                "the", "and", "for", "with", "from", "into", "that", "this", "these", "those",
                "were", "have", "has", "are", "was", "their", "which", "about", "source",
            }
            source_text = " ".join(
                f"{item.get('title', '')} {self._source_body_for_prompt(item)}"
                for item in (source_results or [])
                if isinstance(item, dict)
            )
            for token in re.findall(r"[A-Za-z][A-Za-z0-9+./_-]{3,}|[\u4e00-\u9fff]{2,12}", source_text):
                norm = token.lower()
                if norm in stop or norm.isdigit() or norm in source_alignment_terms:
                    continue
                source_alignment_terms.append(norm)
                if len(source_alignment_terms) >= 18:
                    break
            source_alignment_diag = (
                negative_constraints.get("source_alignment")
                if isinstance(negative_constraints, dict) and isinstance(negative_constraints.get("source_alignment"), dict)
                else {}
            )
            source_alignment_audit_active = bool(
                isinstance(negative_constraints, dict)
                and (
                    negative_constraints.get("source_alignment_low_pass_audit")
                    or "source_alignment_low_pass" in str(negative_constraints.get("feedback", "")).lower()
                )
            )
            diag_missing_terms = (
                source_alignment_diag.get("missing_terms")
                if isinstance(source_alignment_diag, dict) and isinstance(source_alignment_diag.get("missing_terms"), list)
                else []
            )
            for raw_term in diag_missing_terms:
                term = " ".join(str(raw_term or "").strip().split())
                if not term:
                    continue
                norm = term.lower()
                if norm in stop or norm.isdigit():
                    continue
                if norm not in {item.lower() for item in source_alignment_repair_terms}:
                    source_alignment_repair_terms.append(term)
                if len(source_alignment_repair_terms) >= 10:
                    break
        evidence_slots = self._build_evidence_slot_plan(source_results or [], english=english_generation)
        if english_generation:
            target_length_rule = (
                f"Keep this subsection substantial and publication-style; drafts below {self.min_draft_chars} characters "
                "are treated as incomplete and must be expanded. Do not shorten the answer for benchmarking. "
                "If the subsection is already long, improve topic-specific coverage, evidence grounding, transitions, "
                "and non-redundant detail instead of compressing the analysis."
            )
            academic_style_rule = "Use polished academic English with clear paragraph logic, explicit evidence, and natural transitions."
            heading_rule = (
                "Do not use Markdown heading markers such as #, ##, or #### inside this subsection; use paragraphs "
                "or numbered sentences only when necessary."
            )
            transition_examples = "therefore, however, moreover, in contrast, consequently"
            final_instruction = (
                "Output only the final subsection body in English. Do not add any preface or afterword. "
                "Do not output Chinese prose unless a Chinese term is explicitly quoted as a source term."
            )
        else:
            target_length_rule = (
                f"保持充分、完整的小节篇幅；低于 {self.min_draft_chars} 字符会被系统视为短草稿并要求重写；"
                "不要为了评估压缩正文。若小节已经较长，应继续补强主题覆盖、证据接地、逻辑过渡和非重复细节，"
                "而不是把内容压缩成短答。"
            )
            academic_style_rule = "采用专业中文学术文体，段落之间逻辑清晰、证据明确、过渡自然。"
            heading_rule = "不要使用 Markdown 标题符号（例如 #、##、####）；如需分层，用自然段或“1.”“2.”编号句，并保持每个编号单独成段。"
            transition_examples = "因此、然而、此外、总之 / therefore, however, moreover, in conclusion"
            final_instruction = "请直接输出该小节的正文内容，不要添加任何前言或后语。"

        if english_generation:
            enhanced = f"""You are writing one subsection of a larger academic document.

[FlowerNet Stable Writing Protocol]
These fixed rules apply across topics and subsections and must be followed before any local instruction.

Writing boundary
1. Write strictly within the current subsection outline; do not repeat the prompt or reveal the generation process.
2. Complete only the current subsection. Do not expand into other subsections and do not write the whole-document conclusion early.
3. Output the subsection body directly without prefaces such as "Here is the text".
4. The output must be a complete, publication-style subsection body, not an outline, abstract, title list, or task restatement.
5. Extra requirements in the original task are formatting, quality, test, or style constraints unless they are explicitly part of the article topic.

Academic quality
1. {academic_style_rule}
2. Each core paragraph should follow a Claim -> Evidence -> Reasoning -> Transition -> Implication argument chain.
3. Ground theoretical concepts, technical mechanisms, empirical findings, historical facts, policy judgments, and strong conclusions in verifiable support.
4. Avoid generic filler, unsupported data, cross-topic examples, repeated definitions, and unnecessary background.
5. Avoid copying or paraphrasing previous content; every paragraph must contribute new information or a new analytical angle.

Citations and evidence
1. If reference material is provided, prioritize sources that are highly relevant, professional, and credible for the current subsection.
2. Use compact IEEE-style inline markers such as [1][2]; inline markers must match the final References numbering.
3. Place citation markers next to the claim they support, not only at paragraph ends or in the References section.
4. Do not invent papers, DOIs, URLs, authors, publication venues, or numeric results.
5. If no URL is available, it is still acceptable to cite real books, classic papers, authoritative reviews, standards, reports, or credible institutional sources.
6. Do not cite a source if it only shares keywords but does not belong to the same problem domain.

Three-step source-alignment workflow
Step 1 - Extract: read each source title, abstract, keywords, and visible content; extract its problem, method, object, and conclusion.
Step 2 - Match: decide whether the source directly supports a core point in the current subsection outline.
Allowed matches include same-field theory, same-problem methods, same-object empirical evidence, and authoritative reviews on the same topic.
Rejected matches include accidental keyword overlap, wrong discipline/object/problem, or only generic relevance.
Step 3 - Cite conditionally: use [n] only after the source passes matching; otherwise skip it.

Format and readability
1. {heading_rule}
2. Define important terms on first use, then reuse the same canonical terminology where natural to avoid topic-coverage dilution.
3. Write formulas as clear linear math or LaTeX-style expressions.
4. Keep paragraphs readable; avoid page-length single paragraphs.
5. End by returning to the current subsection goal or creating a natural transition, not by writing a whole-paper conclusion.
"""
        else:
            enhanced = f"""你正在撰写一篇文档的某个小节。

【FlowerNet稳定写作协议（跨主题、跨小节复用，用于提高DeepSeek prompt cache 命中）】
以下规则是固定协议。无论主题、章节、大纲、用户背景和参考资料如何变化，都必须优先遵守。

一、写作边界
1. 严格围绕当前小节大纲写作，不复述提示词，不输出生成过程。
2. 当前小节只完成当前大纲要求的内容，不扩写到其他小节，不提前总结全文。
3. 直接输出小节正文，不添加“以下是正文”“下面开始”等前言。
4. 输出必须是完整、可发表长文档的小节正文，不允许只写提纲、摘要、列表标题或任务复述。
5. 原始写作任务中的“附加要求/额外要求/Extra requirements”只作为格式、质量、测试或风格约束；除非其中明确要求作为正文主题，否则不得把测试、复测、修复、引用格式等约束词写成正文内容点。

二、学术质量
1. {academic_style_rule}
2. 每个核心段落采用 Claim（主张）→ Evidence（证据）→ Reasoning（推理）→ Transition（过渡）→ Implication（小结）的论证链。
3. 对理论概念、技术机制、实证结果、历史事实、政策判断和强结论给出可验证支撑。
4. 避免空泛套话、泛化结论、重复定义、无来源数据、跨主题案例和不必要的背景铺垫。
5. 避免复制前文，避免换词复述，确保每一段都贡献新的信息或新的分析角度。

三、引用与证据
1. 如果提供了参考资料，必须优先使用与当前小节主题高度匹配、专业且可信的来源。
2. 正文引用必须使用紧凑 IEEE 标记，如 [1][2]；正文标记必须和 References 中的编号一致。
3. 引用标记必须出现在真正被来源支撑的句子旁边，不能只在段末或 References 中堆积。
4. 禁止虚构论文、虚构 DOI、虚构 URL、虚构作者、虚构出版物。
5. 没有 URL 时也可以引用真实书籍、经典论文、权威综述、标准、报告或高可信机构资料。
6. 若某来源与当前小节不属于同一问题域，即使看起来学术，也不得强行引用。

四、引用使用的三步证据对齐工作流
第1步 - 提取摘要：
  读取来源的标题、摘要、关键词和可见内容，提取核心问题、方法、对象和结论。
第2步 - 判定匹配：
  判断该来源是否能直接支撑当前小节大纲中的某个核心要点。
  允许：同领域理论、同问题方法、同对象实证、同主题权威综述。
  禁止：关键词偶然相同但学科/对象/问题不一致，或只能泛泛关联的来源。
第3步 - 条件引用：
  通过匹配后才在正文中使用 [序号]；未通过则跳过该来源。

五、格式与可读性
1. {heading_rule}
2. 术语第一次出现时给出必要解释，后续使用同一术语，避免同义词漂移造成 topic coverage 稀释。
3. 公式必须用清楚的线性数学表达或 LaTeX 风格表达，不能输出乱码。
4. 段落长度适中，避免整页单段；术语第一次出现时给出必要解释。
5. 结尾应自然过渡到下一小节或回扣当前小节目标，不做全文结论。
"""

        drift_prompt = CITATION_DRIFT_PREVENTION_PROMPT_EN if english_generation else CITATION_DRIFT_PREVENTION_PROMPT
        if drift_prompt:
            if english_generation:
                enhanced += f"""

[Citation Drift Prevention - fixed protocol supplement]
{drift_prompt}
"""
            else:
                enhanced += f"""

【引用漂移防护（固定协议补充，必须遵守）】
{drift_prompt}
"""

        if english_generation:
            enhanced += f"""

[Current Subsection Outline - complete scope and boundary]
{outline}

"""
        else:
            enhanced += f"""

【当前小节的详细大纲（这是内容的完整范围和边界，必须100%严格遵循）】
{outline}

"""

        if coverage_terms:
            coverage_label = "[Topic-specific coverage checklist - integrate naturally, do not stuff keywords]" if english_generation else "【Topic-specific coverage checklist（必须自然覆盖，不要原样堆词）】"
            enhanced += f"""{coverage_label}
{coverage_terms}
- Use canonical terminology consistently: define central terms once, then reuse the same standard terms where natural instead of replacing them with loose paraphrases. This improves topic coverage and reader traceability.

"""

        if evidence_slots:
            evidence_label = "[Evidence grounding plan - match sources before writing, do not invent references]" if english_generation else "【Evidence grounding plan（写作前先匹配证据，不要编造来源）】"
            enhanced += f"""{evidence_label}
{evidence_slots}

"""

        research_prompt_block = ""
        if isinstance(research_intelligence, dict):
            research_prompt_block = str(research_intelligence.get("prompt_block") or "").strip()
        if research_prompt_block:
            enhanced += f"""{research_prompt_block}

"""

        if source_alignment_terms or source_alignment_phrases:
            source_alignment_label = "[Full-only source-alignment selector hints - use naturally, do not stuff terms]" if english_generation else "【Full-only source-alignment selector hints（自然使用，不要堆词）】"
            enhanced += f"""{source_alignment_label}
To protect reference-based metrics and traceability, preserve both task vocabulary and source vocabulary when they are substantively correct.
Keep these task/outline anchors visible in the argument, especially in definitions, comparisons, and transition sentences:
{", ".join(source_alignment_topic_terms)}
Prefer complete canonical phrases from source titles before introducing abbreviations:
{", ".join(source_alignment_phrases)}
Naturally integrate the most relevant of these source terms in claim-bearing sentences:
{", ".join(source_alignment_terms)}

"""

        if source_alignment_audit_active and (source_alignment_terms or source_alignment_phrases or source_alignment_repair_terms):
            if english_generation:
                enhanced += f"""[Source-alignment repair target - full pipeline only]
The previous draft passed the main verifier but was rejected by the source-alignment audit. Rewrite it as a complete, readable subsection that preserves the verified thesis while improving source phrase and source n-gram grounding.
- Use the injected sources in their current numbering; do not invent new sources.
- Build roughly four developed paragraphs, each with a distinct claim, source-supported evidence, reasoning, and transition.
- Reuse the most relevant exact source-title phrases or canonical source concepts only where they are substantively correct.
- Cover these missing/source-sensitive terms naturally: {", ".join((source_alignment_repair_terms or source_alignment_terms)[:14])}
- Preserve these source phrases where they fit the argument: {", ".join(source_alignment_phrases[:6])}
- Do not append a keyword list, do not stuff terms, and do not sacrifice coherence or readability.

"""
            else:
                enhanced += f"""【Source-alignment 定向修复目标（仅 full pipeline 使用）】
上一版已经通过主要 verifier，但被 source-alignment 审计拦下。请在保留已验证核心论点的基础上，重写为完整、可读的小节，并增强来源短语和来源 n-gram 接地。
- 使用当前注入来源的编号，不得编造新来源。
- 写成约四个充分展开的自然段，每段包含不同主张、来源支撑、推理和过渡。
- 只在语义正确时复用来源标题短语或来源概念，不要机械堆词。
- 自然补入这些缺失/来源敏感术语：{", ".join((source_alignment_repair_terms or source_alignment_terms)[:14])}
- 在论证适合处保留这些来源短语：{", ".join(source_alignment_phrases[:6])}
- 不要追加关键词列表，不要牺牲连贯性和可读性。

"""

        if original_prompt:
            original_label = "[Original writing task and style requirements - follow compatibly]" if english_generation else "【原始写作任务与风格要求（必须兼容遵循）】"
            enhanced += f"""{original_label}
{original_prompt}

"""

        persona_block = os.getenv("PERSONA_PROMPT", "").strip()
        if persona_block:
            persona_label = "[Persona style constraints - required]" if english_generation else "【Persona 风格约束（必须遵守）】"
            enhanced += f"""{persona_label}
{persona_block}

"""

        if rag_context:
            enhanced += f"""{rag_context}

"""

        if history_text:
            if english_generation:
                enhanced += f"""[Previously verified subsections - use as generated context and avoid redundancy]
{history_text}

[Strict generation requirements]
1. Relevance, must be >= {rel_threshold:.2f}:
   - Every sentence must directly correspond to a point in the outline.
   - Do not include content or examples unrelated to the outline.
   - Ensure each paragraph directly supports the subsection scope.
   - Verification question: if this paragraph were removed, would an outline point lose its corresponding content? If yes, keep it.

2. Redundancy control, must be <= {red_threshold:.2f}:
   - Do not repeat, paraphrase, or assemble information already present in the previously verified subsections.
   - Every sentence must contribute a new, non-redundant claim or explanation.

3. Quality requirements:
   - Maintain logical continuity with previous subsections while developing a new angle and new information.
   - {target_length_rule}
   - {heading_rule}
   - Use precise, professional language and avoid empty filler.
   - Use the Claim -> Evidence -> Reasoning -> Transition -> Implication argument chain.
   - Use at least one explicit transition term, such as {transition_examples}.
   - Strong conclusions such as "must", "proves", or "it is clear" require verifiable evidence or citation support.

"""
            else:
                enhanced += f"""【前面已通过验证的小节内容（作为已生成内容的参考，避免冗余）】
{history_text}

【严格的生成要求】
1. 相关性（必须 >= {rel_threshold:.2f}）：
   - 内容的每一句话都必须直接对应大纲中的某个要点
   - 不允许任何与大纲无关的内容或例子
   - 确保段落标题直接来自或对应大纲的标题
   - 验证：如果删除某段文字，是否会让大纲的某个要点失去对应内容？如果是，则保留

2. 避免冗余（必须 <= {red_threshold:.2f}）：
   - 严禁重复、改写或拼凑上面《已通过小节内容》中已有的信息
   - 每句话都要贡献新的、未重复的观点
   
3. 质量要求：
   - 与前面小节保持逻辑连贯，但展开全新的视角和信息
   - {target_length_rule}
   - {heading_rule}
   - 表述专业、准确、避免空洞内容
   - 必须采用论证链结构：Claim（主张）→ Evidence（证据）→ Reasoning（推理）→ Transition（过渡）→ Implication（小结）
   - 至少使用 1 个显式过渡词（例如：{transition_examples}）
   - 若出现强结论（如“必须”“证明了”“it is clear”），必须附带可验证事实或引用

"""
        else:
            if english_generation:
                enhanced += f"""[Strict generation requirements]
1. Relevance, must be >= {rel_threshold:.2f}:
   - Every sentence must directly correspond to a point in the outline.
   - Do not include content or examples unrelated to the outline.
   - Ensure each paragraph directly supports the subsection scope.

2. Quality requirements:
   - {target_length_rule}
   - {heading_rule}
   - Use precise, professional language and avoid empty filler.
   - Use the Claim -> Evidence -> Reasoning -> Transition -> Implication argument chain.
   - Use at least one explicit transition term, such as {transition_examples}.
   - Strong conclusions such as "must", "proves", or "it is clear" require verifiable evidence or citation support.

"""
            else:
                enhanced += f"""【严格的生成要求】
1. 相关性（必须 >= {rel_threshold:.2f}）：
   - 内容的每一句话都必须直接对应大纲中的某个要点
   - 不允许任何与大纲无关的内容或例子
   - 确保段落标题直接来自或对应大纲的标题

2. 质量要求：
   - {target_length_rule}
   - {heading_rule}
   - 表述专业、准确、避免空洞内容
   - 必须采用论证链结构：Claim（主张）→ Evidence（证据）→ Reasoning（推理）→ Transition（过渡）→ Implication（小结）
   - 至少使用 1 个显式过渡词（例如：{transition_examples}）
   - 若出现强结论（如“必须”“证明了”“it is clear”），必须附带可验证事实或引用

"""

        if require_source_citations:
            source_count = max(0, int(available_source_count or 0))
            allowed_ids = (", ".join(f"[{idx}]" for idx in range(1, source_count + 1)) if english_generation else "、".join(f"[{idx}]" for idx in range(1, source_count + 1))) or "[1]"
            min_marker_count = 2 if source_count >= 2 else 1
            if english_generation:
                enhanced += f"""[Source citation requirements - CRITICAL]
- Available citation IDs for this subsection: {allowed_ids}
- If the outline, original task, or previous feedback mentions IDs outside this set, such as [6][7][8], ignore them and use only the available IDs.
- Inline citation requirement: insert at least {min_marker_count} professional source markers in the subsection body, using compact IEEE-style markers such as [1][2].
- Key facts, data, theories, and frameworks require citation support.
- Do not output a References, Bibliography, or reference list at the end of this subsection; the whole document will collect References once at the end.
- Do not invent papers, links, or unrelated citations. If no URL is available, preserve the most credible real book, paper, review, standard, report, or institutional source.
- Minimum standard: at least {min_marker_count} inline citation markers, and every marker must come from the available ID set.
"""
            else:
                enhanced += f"""【来源引用硬性要求（CRITICAL - 强制执行）】
✓ 本小节可用引用编号只有：{allowed_ids}
✓ 如果大纲、原始任务或上轮反馈中出现超出上述范围的编号（例如 [6][7][8]），必须忽略并改用上述可用编号，禁止输出不存在的编号
✓ 内联引用标记强制要求：本小节正文至少插入 {min_marker_count} 处专业来源引用，使用紧凑 IEEE 标记如 [1][2]
✓ 关键事实/数据处必须有引用，理论/框架处必须有引用
✓ 不要在本小节末尾输出 References / Bibliography / 参考文献块；整篇文档会在最后统一汇总 References
✓ 禁止虚构论文、编造链接、引用不相关来源；没有 URL 时也必须保留最可信的真实书籍、论文或权威综述来源
✓ 最低标准：正文至少 {min_marker_count} 个内联引用标记，且每个编号必须来自可用编号集合
"""

        if negative_constraints:
            failed_dims = negative_constraints.get("quality_dimensions_failed") if isinstance(negative_constraints.get("quality_dimensions_failed"), list) else []
            source_check = negative_constraints.get("source_check") if isinstance(negative_constraints.get("source_check"), dict) else {}
            coverage_diag = negative_constraints.get("coverage_diagnostics") if isinstance(negative_constraints.get("coverage_diagnostics"), dict) else {}
            evidence_diag = negative_constraints.get("evidence_diagnostics") if isinstance(negative_constraints.get("evidence_diagnostics"), dict) else {}
            blacklist = source_check.get("blacklist_matches") if isinstance(source_check.get("blacklist_matches"), list) else []
            low_ref_details = (
                source_check.get("low_semantic_referenced_source_details")
                if isinstance(source_check.get("low_semantic_referenced_source_details"), list)
                else []
            )
            feedback_text = str(negative_constraints.get("feedback", "") or "").strip()
            missing_terms = [str(x) for x in coverage_diag.get("missing_terms", []) if str(x).strip()][:10] if coverage_diag else []
            missing_aspects = [str(x) for x in coverage_diag.get("missing_aspects", []) if str(x).strip()][:6] if coverage_diag else []
            missing_evidence = [str(x) for x in evidence_diag.get("missing_evidence_types", []) if str(x).strip()][:6] if evidence_diag else []

            if english_generation:
                enhanced += """[Negative-constraint retry - CRITICAL]
The previous draft failed Verifier checks. This round must make a measurable improvement or it will be marked as failed.
"""
                if failed_dims:
                    enhanced += f"\n[Failed dimensions to repair]: {', '.join(str(x) for x in failed_dims)}\n"
                if feedback_text:
                    enhanced += f"[Verifier feedback to correct]: {feedback_text[:280]}\n"
                if missing_terms:
                    enhanced += "[Missing topic terms to cover naturally]: {} \n".format(", ".join(missing_terms))
                if missing_aspects:
                    enhanced += "[Missing content aspects to complete]: {} \n".format(", ".join(missing_aspects))
                if missing_evidence:
                    enhanced += "[Missing evidence types to add]: {} \n".format(", ".join(missing_evidence))
                if blacklist:
                    enhanced += "[Do not cite again - cross-domain source blacklist]:\n"
                    for item in blacklist[:6]:
                        title = str(item.get("title", "") or "")[:120]
                        matched = str(item.get("match", "") or item.get("match_keyword", "") or "")[:40]
                        if title or matched:
                            enhanced += f"  - {title} (blacklist keyword: {matched})\n"
                if low_ref_details:
                    enhanced += "[Avoid or replace weakly matched cited sources]:\n"
                    for item in low_ref_details[:6]:
                        ref = str(item.get("reference", "") or "")
                        title = str(item.get("title", "") or "")[:140]
                        score = float(item.get("score", 0.0) or 0.0)
                        threshold = float(item.get("threshold", 0.0) or 0.0)
                        if ref or title:
                            enhanced += f"  - {ref} {title} (semantic score {score:.3f} < {threshold:.3f})\n"
                enhanced += f"""
[This-round requirements]
- Strengthen content, data, cases, and evidence directly related to the current subsection: {outline.split(chr(10))[0][:60]}.
- Remove or replace claims and citations that are inconsistent with the subsection topic.
- Provide at least two factual sentences with valid inline [number] citations.
- Use only the citation IDs available in the current reference material; do not invent [6][7][8] or any unavailable IDs.
- Prefer the strongest topic-matched source IDs over weakly matched IDs; cite a weak source only if the sentence explicitly discusses that narrow case.
- Improve logical coherence so each paragraph directly answers an outline point.
- If the previous draft was shorter than {self.min_draft_chars} characters, expand this round into a complete publication-style subsection body.
- If the previous draft was already long, use targeted expansion: add missing topic points, evidence explanations, transition sentences, and non-redundant details; do not compress it into a short answer.
"""
            else:
                enhanced += """【负向约束重试（CRITICAL - 本轮必须改进）】
上一轮被 Verifier 判定失败，本轮MUST IMPROVE或系统自动标记为"失败"。需要在以下方面明确加强：
"""
                if failed_dims:
                    enhanced += f"\n【失败维度 - 本轮必须改进】：{', '.join(str(x) for x in failed_dims)}\n"
                if feedback_text:
                    enhanced += f"【Verifier反馈 - 必须立即纠正】：{feedback_text[:280]}\n"
                if missing_terms:
                    enhanced += "【缺失主题词 - 本轮必须自然覆盖】：{} \n".format("、".join(missing_terms))
                if missing_aspects:
                    enhanced += "【缺失内容面向 - 本轮必须补齐】：{} \n".format("、".join(missing_aspects))
                if missing_evidence:
                    enhanced += "【缺失证据类型 - 本轮必须补齐】：{} \n".format("、".join(missing_evidence))
                if blacklist:
                    enhanced += "【禁止再次引用 - 跨领域来源黑名单】：\n"
                    for item in blacklist[:6]:
                        title = str(item.get("title", "") or "")[:120]
                        matched = str(item.get("match", "") or item.get("match_keyword", "") or "")[:40]
                        if title or matched:
                            enhanced += f"  ❌ {title} (黑名单关键词: {matched})\n"
                if low_ref_details:
                    enhanced += "【避免或替换弱匹配引用来源】：\n"
                    for item in low_ref_details[:6]:
                        ref = str(item.get("reference", "") or "")
                        title = str(item.get("title", "") or "")[:140]
                        score = float(item.get("score", 0.0) or 0.0)
                        threshold = float(item.get("threshold", 0.0) or 0.0)
                        if ref or title:
                            enhanced += f"  - {ref} {title}（语义分 {score:.3f} < {threshold:.3f}）\n"
                enhanced += f"""
【本轮强制要求】：
- 加强与当前小节【{outline.split(chr(10))[0][:60]}】直接相关的内容、数据、案例
- 删除或替换与小节主题不一致的观点和引用
- 至少提供 2+ 处事实句 + [序号] 内联引用（不是末尾列表！）
- 只能使用当前参考资料中存在的编号；不要为了“增加引用”而输出不存在的 [6][7][8] 等编号
- 优先使用语义最贴合当前小节的来源编号；弱匹配来源只能在明确讨论该窄案例时引用
- 改进逻辑连贯性：确保每个段落都能直接回答大纲中的某个要点
- 若上一轮短于 {self.min_draft_chars} 字符，本轮必须扩展为完整、可发表的小节正文
- 若上一轮已经较长，本轮仍应做 targeted expansion：补齐缺失主题点、证据解释、过渡句和非重复细节；不得压缩成短答
"""

        if revision_draft:
            if english_generation:
                revision_min_ratio = 0.95 if revision_arm == "defect_evidence" else 0.90
                revision_max_ratio = 1.20 if revision_arm == "defect_evidence" else 1.10
                revision_min_chars = max(self.min_draft_chars, int(len(revision_draft) * revision_min_ratio))
                revision_max_chars = max(revision_min_chars + 100, int(len(revision_draft) * revision_max_ratio))
                arm_instruction = {
                    "defect_topic": "Restore direct topic alignment while preserving valid evidence, citations, and non-failing dimensions.",
                    "defect_evidence": (
                        "Keep the argument and structure, but bind unsupported claims to the available sources and remove unsupported specifics. "
                        "Add only missing source-supported concepts, evidence explanations, boundaries, or comparisons needed for coverage. "
                        "Do not remove valid existing citation markers. Preserve concise, canonical source terminology only when it directly supports "
                        "the task and subsection anchors; do not import source-specific proper nouns or reproduce full paper titles merely to raise lexical overlap."
                    ),
                    "defect_novelty": "Keep unique claims and valid citations, replace history-overlapping passages one-for-one with subsection-exclusive mechanisms, cases, criteria, and boundaries. Preserve the exact subsection mission: the opening thesis and every paragraph must explicitly connect its new material to the original topic anchors. Do not add length on top of repeated text.",
                    "defect_structure": "Preserve claims and evidence while reorganizing paragraphs and transitions for a clearer argument chain.",
                    "rule_structured": "Preserve correct content while repairing only the diagnosed organization and coherence defects.",
                    "novelty_repair": "Repair novelty as source-grounded information gain: add subsection-exclusive mechanisms, cases, criteria, and limitations while preserving valid citations and evidence grounding.",
                    "claim_evidence_repair": "Repair claim-evidence binding: every major claim must be supported by evidence and one explicit reasoning sentence; preserve all valid citations.",
                    "citation_grounding_repair": "Repair citation faithfulness: remove unsupported citation markers, keep only citations that support same-sentence claims, and do not invent sources.",
                    "reviewer_risk_repair": "Repair likely reviewer concerns without broad rewriting: support weak claims, clarify novelty, add limitations, and preserve all no-harm dimensions.",
                    "external_metric_repair": "Improve source/reference lexical and semantic alignment naturally by preserving key source terms in claim-bearing sentences; do not mechanically stuff keywords.",
                    "structure_readability_repair": "Repair readability and argument flow while preserving claims, evidence, source markers, and novelty.",
                    "reproducibility_repair": "Add supported reproducibility anchors such as dataset, protocol, parameter, implementation, audit trail, or replication constraints; unsupported details must be framed as requirements.",
                }.get(revision_arm, "Revise only the diagnosed defects while preserving all dimensions that already passed.")
                enhanced += f"""
[TARGETED CONTROLLER REVISION - CRITICAL]
Selected repair arm: {revision_arm or 'controller'}
{arm_instruction}
Edit the supplied draft instead of writing a new article from scratch. Preserve correct topic-specific claims, valid source markers, useful details, and successful dimensions. Return one complete revised subsection only.
Keep the revised body between approximately {revision_min_chars} and {revision_max_chars} characters. Replace weak or repeated passages one-for-one; do not solve redundancy by deleting half of the document or by padding unchanged text.
"""
                if revision_arm == "defect_evidence" and source_alignment_repair_terms:
                    enhanced += f"""
Source-alignment repair targets:
Naturally cover several of these currently missing retrieved-source terms in claim-bearing sentences, but only where they are substantively true for the current subsection. Do not list them mechanically and do not invent citations:
{", ".join(source_alignment_repair_terms)}
"""
                enhanced += f"""

[PREVIOUS REAL DRAFT TO REVISE]
{revision_draft}
[END PREVIOUS DRAFT]
"""
            else:
                revision_min_ratio = 0.95 if revision_arm == "defect_evidence" else 0.90
                revision_max_ratio = 1.20 if revision_arm == "defect_evidence" else 1.10
                revision_min_chars = max(self.min_draft_chars, int(len(revision_draft) * revision_min_ratio))
                revision_max_chars = max(revision_min_chars + 100, int(len(revision_draft) * revision_max_ratio))
                enhanced += f"""
【Controller 定向修订 - 关键要求】
修复 arm：{revision_arm or 'controller'}
请直接编辑下面的上一版真实草稿，只修复 Verifier 检出的缺陷；保留已通过的主题内容、有效证据、引用编号、结构优点和其他质量维度。不要从零另写一篇文章。
修订后正文长度应约为 {revision_min_chars}-{revision_max_chars} 字符；必须等长替换重复或薄弱段落，不得通过删除一半正文或原文填充来“降低冗余”。

【待修订草稿】
{revision_draft}
【待修订草稿结束】
"""

        final_label = "[Final output instruction]" if english_generation else "【原始生成指令】"
        enhanced += f"""{final_label}
    {final_instruction}
    """

        return enhanced.strip()

    def _is_transient_generator_error(self, error_text: str) -> bool:
        lowered = str(error_text or "").lower()
        transient_tokens = [
            "timeout", "timed out", "connection", "temporarily", "try again",
            "429", "502", "503", "504", "rate", "quota", "overloaded",
            "resource_exhausted", "service unavailable", "upstream",
        ]
        return any(token in lowered for token in transient_tokens)

    def _extract_generation_coverage_terms(
        self,
        outline: str,
        original_prompt: str,
        source_results: List[Dict[str, Any]],
        max_terms: int = 20,
        english: bool = False,
    ) -> str:
        stop = {
            "section", "subsection", "outline", "prompt", "chapter", "content",
            "write", "writing", "draft", "article", "document", "quality",
            "要求", "生成", "内容", "小节", "章节", "大纲", "写作", "文档",
            "分析", "研究", "说明", "包括", "以及", "关于", "当前", "必须",
            "and", "or", "the", "for", "with", "about", "use", "using", "uses",
            "this", "that", "these", "those", "into", "from",
        }
        terms: List[str] = []

        def add_canonical_topic_phrases(text: str) -> None:
            if not english:
                return
            source = str(text or "")
            lowered = source.lower()
            mapping = [
                (("ai agents", "agentic ai", "智能体", "代理"), ["AI agents", "intelligent agents", "software agents", "agentic AI"]),
                (("tool calling", "tool use", "tool-using", "工具调用", "工具", "调用"), ["tool use", "tool calling", "tool-using workflows", "external tools"]),
                (("workflow", "workflows", "工作流", "工作流程"), ["workflows", "multi-step workflows"]),
                (("concept", "概念", "定义", "边界"), ["conceptual evolution", "definition", "scope"]),
                (("architecture", "architectures", "架构", "框架"), ["agent architectures", "architectural frameworks"]),
                (("evaluation", "metric", "benchmark", "评估", "指标", "基准"), ["evaluation methods", "benchmark metrics"]),
                (("application", "scenario", "应用", "场景", "案例"), ["application scenarios", "deployment cases"]),
                (("risk", "limitation", "风险", "局限", "挑战"), ["risks", "limitations"]),
                (("future", "trend", "未来", "趋势"), ["future trends", "future directions"]),
                (("machine learning", "机器学习"), ["machine learning"]),
                (
                    ("artificial intelligence", "ai ", "machine learning", "deep learning", "neural", "foundation model", "large language model", "llm", "llms", "generative ai"),
                    [
                        "artificial intelligence",
                        "machine learning",
                        "deep learning",
                        "neural networks",
                        "foundation models",
                        "generative AI",
                        "training data",
                        "inference",
                        "evaluation",
                    ],
                ),
                (
                    ("multimodal", "multi-modal", "vision-language", "cross-modal", "image", "audio", "video"),
                    [
                        "multimodal learning",
                        "vision-language models",
                        "cross-modal alignment",
                        "text",
                        "image",
                        "audio",
                        "video",
                    ],
                ),
                (("reinforcement learning", "强化学习"), ["reinforcement learning", "reward function", "objective function"]),
                (
                    ("open-source large language model", "open source large language model", "open-source llm", "open source llm", "open-weight", "open weight"),
                    [
                        "open-source LLM ecosystems",
                        "open-source software ecosystem",
                        "OSS projects",
                        "model families",
                        "licensing regimes",
                        "deployment trade-offs",
                        "inference cost",
                        "quantization",
                        "release process",
                        "versioning",
                        "version management",
                        "source code",
                        "software development practices",
                        "bug tracking",
                        "contributors",
                        "user communities",
                        "community governance",
                        "enterprise adoption",
                    ],
                ),
            ]
            for needles, phrases in mapping:
                if any(needle.lower() in lowered or needle in source for needle in needles):
                    for phrase in phrases:
                        if phrase not in terms:
                            terms.append(phrase)
                        if len(terms) >= max_terms:
                            return

        def add_terms(text: str) -> None:
            for token in re.findall(r"[A-Za-z][A-Za-z0-9+._/-]{2,}|[\u4e00-\u9fff]{2,12}", str(text or "")):
                normalized = token.strip(" \t\r\n.,;:!?()[]{}<>\"'“”‘’")
                if not normalized or normalized.lower() in stop or normalized.isdigit():
                    continue
                if english and re.search(r"[\u4e00-\u9fff]", normalized):
                    continue
                if len(normalized) > 28:
                    continue
                if normalized not in terms:
                    terms.append(normalized)
                if len(terms) >= max_terms:
                    return

        add_canonical_topic_phrases(f"{outline}\n{original_prompt}")
        add_terms(outline)
        add_terms(original_prompt)
        for item in source_results[:4]:
            add_terms(" ".join([
                str((item or {}).get("title") or ""),
                str((item or {}).get("body") or ""),
                str((item or {}).get("description") or ""),
                str((item or {}).get("summary") or ""),
            ])[:900])
            if len(terms) >= max_terms:
                break

        if not terms:
            return ""
        if english:
            return (
                "- Cover most of these topic anchors and turn them into concrete claims: "
                + ", ".join(terms[:max_terms])
                + "\n- Each core paragraph must develop at least one topic anchor; avoid generic governance, generic pros/cons, or vague background."
                + "\n- Cover at least four of these aspect types where relevant: definition/boundary, mechanism/method, application/case, evaluation/metric, risk/limitation, and future direction."
            )
        return (
            "- 必须覆盖这些主题锚点中的大多数，并把它们写成具体论点："
            + "、".join(terms[:max_terms])
            + "\n- 每个核心段落至少围绕 1 个主题锚点展开，不能只写通用治理、通用优缺点或泛泛背景。"
            + "\n- 至少覆盖：定义/边界、机制/方法、应用/案例、评估/指标、风险/局限、未来方向中的 4 类。"
        )

    def _build_evidence_slot_plan(self, source_results: List[Dict[str, Any]], max_items: int = 4, english: bool = False) -> str:
        if not source_results:
            return ""
        if english:
            lines = [
                "- First decide which concrete claim each source can support; do not cite sources that cannot support the current subsection claim.",
                "- Write each key claim as: claim sentence + [number] + one sentence explaining how the evidence supports the claim.",
            ]
        else:
            lines = [
                "- 先决定每个来源能支撑哪个具体主张；不能支撑当前小节主张的来源不要引用。",
                "- 每条关键主张都要写成：主张句 + [编号] + 证据如何支撑该主张的一句解释。",
            ]
        for idx, item in enumerate(source_results[:max_items], 1):
            title = re.sub(r"\s+", " ", str((item or {}).get("title") or "")).strip()[:120]
            snippet = self._source_body_for_prompt(item or "")[:180]
            href = str((item or {}).get("href") or (item or {}).get("url") or "").strip()[:160]
            if english:
                lines.append(f"- [{idx}] Available source: {title or 'Untitled source'}; cue: {snippet or href or 'use only when it directly matches the subsection topic'}")
            else:
                lines.append(f"- [{idx}] 可用来源：{title or 'Untitled source'}；线索：{snippet or href or '仅在与主题直接匹配时使用'}")
        return "\n".join(lines)

    @staticmethod
    def _source_body_for_prompt(item: Any) -> str:
        if not isinstance(item, dict):
            return ""
        raw = str(item.get("snippet") or item.get("summary") or item.get("description") or "").strip()
        if not raw:
            body = str(item.get("body") or "").strip()
            # CrossRef-style bodies often look like "Author, Author | 2026 | Venue".
            # They are useful metadata, but not concepts the generator should
            # force into the paper body.
            raw = "" if "|" in body[:180] else body
        return re.sub(r"\s+", " ", raw).strip()

    @staticmethod
    def _source_title_phrases(source_results: List[Dict[str, Any]], max_phrases: int = 8) -> List[str]:
        stop = {
            "the", "and", "for", "with", "from", "into", "that", "this", "these", "those",
            "survey", "review", "comprehensive", "open", "challenges", "toward", "efficient",
        }
        phrases: List[str] = []
        for item in source_results or []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "")
            title = re.sub(r"[:：].*$", "", title)
            tokens = [
                tok.lower()
                for tok in re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", title)
                if tok.lower() not in stop and not tok.isdigit()
            ]
            for n in (4, 3, 2):
                for i in range(0, max(0, len(tokens) - n + 1)):
                    phrase = " ".join(tokens[i:i + n])
                    if phrase and phrase not in phrases:
                        phrases.append(phrase)
                    if len(phrases) >= max_phrases:
                        return phrases
        return phrases
    
    def _call_generator(self, prompt: str, max_tokens: Optional[int] = None) -> Dict[str, Any]:
        """调用 Generator API（优先使用本地实例）"""
        print(f"      [_call_generator] Starting (local_gen={self._local_generator is not None})")
        effective_max_tokens = int(max_tokens or self.generator_max_tokens)
        if self._local_generator is not None:
            try:
                print(f"      [_call_generator] Calling local generator.generate_draft...")
                start = time.time()
                call_prompt = prompt
                call_tokens = effective_max_tokens
                used_compact_prompt = False
                if (
                    self.orch_compact_generation_enabled
                    and len(str(prompt or "")) >= self.orch_compact_prompt_trigger_chars
                    and hasattr(self._local_generator, "_build_compact_generation_prompt")
                ):
                    call_prompt = self._local_generator._build_compact_generation_prompt(prompt)
                    call_tokens = min(effective_max_tokens, self.orch_compact_max_tokens)
                    used_compact_prompt = True
                    print(
                        "      [_call_generator] Using compact prompt "
                        f"({len(str(prompt or ''))} -> {len(str(call_prompt or ''))} chars)"
                    )

                request_temperature = float(
                    getattr(
                        self,
                        "generator_temperature",
                        float(os.getenv("GENERATOR_TEMPERATURE", "0.2") or 0.2),
                    )
                )

                def _invoke_local_generator():
                    try:
                        return self._local_generator.generate_draft(
                            prompt=call_prompt,
                            max_tokens=call_tokens,
                            temperature=request_temperature,
                        )
                    except TypeError as exc:
                        if "temperature" not in str(exc):
                            raise
                        return self._local_generator.generate_draft(
                            prompt=call_prompt,
                            max_tokens=call_tokens,
                        )

                hard_timeout = max(0.0, float(os.getenv("ORCH_LOCAL_GENERATOR_HARD_TIMEOUT_SECONDS", "180")))
                if hard_timeout > 0:
                    executor = ThreadPoolExecutor(max_workers=1)
                    future = executor.submit(_invoke_local_generator)
                    try:
                        result = future.result(timeout=hard_timeout)
                    except FuturesTimeoutError:
                        future.cancel()
                        executor.shutdown(wait=False, cancel_futures=True)
                        elapsed = time.time() - start
                        print(f"⚠️ 本地Generator硬超时: {elapsed:.1f}s > {hard_timeout:.1f}s")
                        return {
                            "success": False,
                            "draft": "",
                            "error": f"local generator hard timeout after {hard_timeout:.1f}s",
                            "metadata": {
                                "elapsed_seconds": round(elapsed, 2),
                                "hard_timeout_seconds": hard_timeout,
                            },
                        }
                    else:
                        executor.shutdown(wait=False, cancel_futures=True)
                else:
                    result = _invoke_local_generator()
                elapsed = time.time() - start
                print(f"      [_call_generator] Local call returned in {elapsed:.1f}s: success={result.get('success')}")
                if used_compact_prompt and isinstance(result, dict):
                    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
                    metadata["orchestrator_compact_prompt"] = True
                    metadata["original_prompt_chars"] = len(str(prompt or ""))
                    metadata["compact_prompt_chars"] = len(str(call_prompt or ""))
                    result["metadata"] = metadata
                return result
            except Exception as e:
                print(f"⚠️ 本地Generator调用失败: {e}，回退到HTTP调用")

        last_error = "generator_unknown_error"
        for attempt in range(1, self.orch_generator_retries + 1):
            try:
                print(
                    f"      [Generator] 发起HTTP请求... "
                    f"(attempt {attempt}/{self.orch_generator_retries})"
                )
                response = self.session.post(
                    f"{self.generator_url}/generate",
                    json={
                        "prompt": prompt,
                        "max_tokens": effective_max_tokens,
                        "temperature": float(
                            getattr(
                                self,
                                "generator_temperature",
                                float(os.getenv("GENERATOR_TEMPERATURE", "0.2") or 0.2),
                            )
                        ),
                    },
                    timeout=self.generator_http_timeout,
                )

                print(f"      [Generator] 收到响应 (status={response.status_code}, size={len(response.text)})")
                if response.status_code == 200:
                    result = response.json()
                    print(f"      [Generator] 解析成功: success={result.get('success')}")
                    if result.get("success"):
                        return result

                    last_error = str(result.get("error") or "generator_failed")
                    can_retry = (
                        attempt < self.orch_generator_retries
                        and self._is_transient_generator_error(last_error)
                    )
                    if can_retry:
                        delay = min(
                            self.orch_generator_max_backoff,
                            self.orch_generator_backoff * (2 ** (attempt - 1)),
                        )
                        delay += random.uniform(0.0, 0.35)
                        print(f"      [Generator] 瞬时失败，{delay:.2f}s 后重试: {last_error[:120]}")
                        time.sleep(delay)
                        continue
                    return {"success": False, "error": last_error}

                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                can_retry = (
                    attempt < self.orch_generator_retries
                    and (response.status_code in (429, 502, 503, 504) or self._is_transient_generator_error(last_error))
                )
                if can_retry:
                    delay = min(
                        self.orch_generator_max_backoff,
                        self.orch_generator_backoff * (2 ** (attempt - 1)),
                    )
                    delay += random.uniform(0.0, 0.35)
                    print(f"      [Generator] HTTP可重试错误，{delay:.2f}s 后重试")
                    time.sleep(delay)
                    continue

                return {"success": False, "error": last_error}
            except requests.Timeout:
                last_error = f"Generator 响应超时 ({self.generator_http_timeout}秒)"
                if attempt < self.orch_generator_retries:
                    delay = min(
                        self.orch_generator_max_backoff,
                        self.orch_generator_backoff * (2 ** (attempt - 1)),
                    )
                    delay += random.uniform(0.0, 0.35)
                    print(f"      [Generator] 请求超时，{delay:.2f}s 后重试")
                    time.sleep(delay)
                    continue
                return {"success": False, "error": last_error}
            except Exception as e:
                last_error = f"{type(e).__name__}: {str(e)[:100]}"
                print(f"      [Generator] 异常: {last_error}")
                if attempt < self.orch_generator_retries and self._is_transient_generator_error(last_error):
                    delay = min(
                        self.orch_generator_max_backoff,
                        self.orch_generator_backoff * (2 ** (attempt - 1)),
                    )
                    delay += random.uniform(0.0, 0.35)
                    print(f"      [Generator] 异常可重试，{delay:.2f}s 后重试")
                    time.sleep(delay)
                    continue
                return {"success": False, "error": last_error}

        return {"success": False, "error": last_error}
    
    def _call_verifier(
        self,
        draft: str,
        outline: str,
        history: List[str],
        rel_threshold: float,
        red_threshold: float,
        context_text: str = "",
        source_results: Optional[List[Dict[str, Any]]] = None,
        require_source_citations: bool = False,
        min_source_citations: int = 3,
        research_intelligence: Optional[Dict[str, Any]] = None,
        external_metrics: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        调用 Verifier API，内部最多重试5次（应对 Render 冷启动），避免浪费生成轮次。
        
        超时配置优化：
        - 单次请求超时：180秒（原 90秒，增加 2 倍）
        - 重试次数：5 次（原 3 次）
        - 重试间隔：8秒（原 5秒）
        - 总容忍时间：180 + 5*8 = 220 秒
        
        这样可以容忍 Render Free Plan 的冷启动延迟（30-60s）和高负载情况。
        """
        print(
            f"      [_call_verifier] Starting verifier call "
            f"(timeout={self.verifier_http_timeout}s, max_retries={self.verifier_max_retries})..."
        )
        last_error = "unknown"
        max_retries = self.verifier_max_retries
        retry_delay = self.verifier_retry_delay
        
        for attempt in range(1, max_retries + 1):
            try:
                print(f"      [_call_verifier] Attempt {attempt}/{max_retries}, sending request...")
                start = time.time()
                response = self.session.post(
                    f"{self.verifier_url}/verify",
                    json={
                        "draft": draft,
                        "outline": outline,
                        "history": history,
                        "rel_threshold": rel_threshold,
                        "red_threshold": red_threshold,
                        "context_text": context_text,
                        "source_results": source_results or [],
                        "require_source_citations": require_source_citations,
                        "min_source_citations": max(1, int(min_source_citations)),
                        "unieval_endpoint": os.getenv("UNIEVAL_ENDPOINT", ""),
                        "require_multidim_quality": (
                            os.getenv("REQUIRE_MULTIDIM_QUALITY", "true").lower() == "true"
                        ),
                        "research_intelligence": research_intelligence or {},
                        "external_metrics": external_metrics or {},
                    },
                    timeout=self.verifier_http_timeout,
                )
                if response.status_code == 200:
                    elapsed = time.time() - start
                    result = response.json()
                    result["success"] = True
                    print(f"      [_call_verifier] Response received in {elapsed:.1f}s: success=True")
                    return result
                else:
                    elapsed = time.time() - start
                    try:
                        response_text = response.text[:500]
                    except Exception:
                        response_text = ""
                    last_error = f"HTTP {response.status_code}: {response_text}".strip()
                    print(f"      [_call_verifier] HTTP {response.status_code} after {elapsed:.1f}s: {response_text[:180]}")
            except Exception as e:
                elapsed = time.time() - start
                last_error = str(e)
                print(f"      [_call_verifier] Exception after {elapsed:.1f}s: {e}")
            
            if attempt < max_retries:
                adaptive_delay = min(30.0, retry_delay * (1.0 + 0.2 * attempt))
                print(f"         ⚠️ Verifier 第{attempt}次调用失败 ({last_error[:80]})，{adaptive_delay:.1f}s 后重试...")
                time.sleep(adaptive_delay)
        
        print(f"      [_call_verifier] All {max_retries} attempts failed: {last_error}")
        return {"success": False, "error": last_error}
    
    def _call_controller(
        self,
        old_outline: str,
        failed_draft: str,
        feedback: Dict[str, Any],
        outline: str,
        history: Optional[List[str]] = None,
        iteration: int = 1,
        rel_threshold: float = 0.85,
        red_threshold: float = 0.40,
        document_id: Optional[str] = None,
        section_id: Optional[str] = None,
        subsection_id: Optional[str] = None,
        excluded_arms: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """调用 Controller API 改进大纲"""
        timeout = max(30, int(os.getenv("CONTROLLER_HTTP_TIMEOUT", "120")))
        try:
            payload: Dict[str, Any] = {
                "original_outline": outline,
                "current_outline": old_outline,
                "failed_draft": failed_draft,
                "feedback": feedback,
                "history": history or [],
                "iteration": iteration,
                "rel_threshold": rel_threshold,
                "red_threshold": red_threshold,
                "quality_dimensions_failed": feedback.get("quality_dimensions_failed", []),
                "quality_dimensions_check": feedback.get("quality_dimensions_check", {}),
                "dimension_thresholds": feedback.get("dimension_thresholds", {}),
                "quality_dimensions": feedback.get("quality_dimensions", {}),
                "controller_bandit_enabled": (
                    os.getenv("CONTROLLER_BANDIT_ENABLED", "true").lower() == "true"
                ),
                "controller_fixed_arm": os.getenv("CONTROLLER_FIXED_ARM", ""),
                "controller_no_bandit_allow_llm": (
                    os.getenv("CONTROLLER_NO_BANDIT_ALLOW_LLM", "false").lower() == "true"
                ),
                "controller_excluded_arms": sorted({
                    str(arm).strip() for arm in (excluded_arms or []) if str(arm).strip()
                }),
            }
            if document_id:
                payload["document_id"] = document_id
            if section_id:
                payload["section_id"] = section_id
            if subsection_id:
                payload["subsection_id"] = subsection_id
            response = self.session.post(
                f"{self.controller_url}/improve-outline",
                json=payload,
                timeout=timeout
            )
            
            if response.status_code == 200:
                try:
                    body = response.json()
                except Exception:
                    return {
                        "success": False,
                        "error": "controller_invalid_json",
                    }
                if "success" not in body:
                    body["success"] = True
                return body
            else:
                return {
                    "success": False,
                    "error": f"HTTP {response.status_code}: {response.text[:200]}"
                }
        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }
