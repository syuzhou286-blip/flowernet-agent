from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Dict, Any, Optional, Tuple
import os
import sys

_SERVICE_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR = os.path.dirname(_SERVICE_DIR)
for _path in (_SERVICE_DIR, _ROOT_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from controler import FlowerNetController
import requests
import re
import time
from collections import Counter
import json
import random
import threading
import math

try:
    from flowernet_trained_models import (
        load_json_model,
        predict_controller_arm_prior,
        resolve_model_path,
    )
except Exception:
    load_json_model = None  # type: ignore
    predict_controller_arm_prior = None  # type: ignore
    resolve_model_path = None  # type: ignore

app = FastAPI(title="FlowerNet Controller API")

# 初始化 Controller
controller = FlowerNetController()

outliner_url = None
BANDIT_LOCK = threading.Lock()
TRAINED_POLICY_CACHE: Dict[str, Any] = {"path": "", "mtime": 0.0, "model": None}


def _project_root_path(*parts: str) -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, *parts)


def _bandit_state_path() -> str:
    raw = os.getenv("CONTROLLER_BANDIT_STATE_PATH", "controller_bandit_state.json").strip()
    if os.path.isabs(raw):
        return raw
    return _project_root_path(raw)


def _default_bandit_state(feature_dim: int) -> Dict[str, Any]:
    return {
        "version": 2,
        "feature_dim": feature_dim,
        "total_rounds": 0,
        "drift": {
            "method": "page_hinkley",
            "mean": 0.0,
            "cum_sum": 0.0,
            "min_cum_sum": 0.0,
            "count": 0,
            "drift_events": 0,
            "last_drift_round": 0,
        },
        "constraints": {
            "lambda_latency": 0.0,
            "lambda_cost": 0.0,
            "avg_latency": 0.0,
            "avg_cost": 0.0,
        },
        "arms": {
            "llm": {"count": 0, "weights": [0.0] * feature_dim, "bias": 0.0, "avg_latency": 0.0, "avg_cost": 0.0, "ineffective_streak": 0},
            "rule": {"count": 0, "weights": [0.0] * feature_dim, "bias": 0.0, "avg_latency": 0.0, "avg_cost": 0.0, "ineffective_streak": 0},
            "rule_structured": {"count": 0, "weights": [0.0] * feature_dim, "bias": 0.0, "avg_latency": 0.0, "avg_cost": 0.0, "ineffective_streak": 0},
            "defect_topic": {"count": 0, "weights": [0.0] * feature_dim, "bias": 0.0, "avg_latency": 0.0, "avg_cost": 0.0, "ineffective_streak": 0},
            "defect_evidence": {"count": 0, "weights": [0.0] * feature_dim, "bias": 0.0, "avg_latency": 0.0, "avg_cost": 0.0, "ineffective_streak": 0},
            "defect_novelty": {"count": 0, "weights": [0.0] * feature_dim, "bias": 0.0, "avg_latency": 0.0, "avg_cost": 0.0, "ineffective_streak": 0},
            "defect_structure": {"count": 0, "weights": [0.0] * feature_dim, "bias": 0.0, "avg_latency": 0.0, "avg_cost": 0.0, "ineffective_streak": 0},
            "novelty_repair": {"count": 0, "weights": [0.0] * feature_dim, "bias": 0.0, "avg_latency": 0.0, "avg_cost": 0.0, "ineffective_streak": 0},
            "claim_evidence_repair": {"count": 0, "weights": [0.0] * feature_dim, "bias": 0.0, "avg_latency": 0.0, "avg_cost": 0.0, "ineffective_streak": 0},
            "citation_grounding_repair": {"count": 0, "weights": [0.0] * feature_dim, "bias": 0.0, "avg_latency": 0.0, "avg_cost": 0.0, "ineffective_streak": 0},
            "reviewer_risk_repair": {"count": 0, "weights": [0.0] * feature_dim, "bias": 0.0, "avg_latency": 0.0, "avg_cost": 0.0, "ineffective_streak": 0},
            "external_metric_repair": {"count": 0, "weights": [0.0] * feature_dim, "bias": 0.0, "avg_latency": 0.0, "avg_cost": 0.0, "ineffective_streak": 0},
            "structure_readability_repair": {"count": 0, "weights": [0.0] * feature_dim, "bias": 0.0, "avg_latency": 0.0, "avg_cost": 0.0, "ineffective_streak": 0},
            "reproducibility_repair": {"count": 0, "weights": [0.0] * feature_dim, "bias": 0.0, "avg_latency": 0.0, "avg_cost": 0.0, "ineffective_streak": 0},
        },
    }


def _load_bandit_state(feature_dim: int) -> Dict[str, Any]:
    path = _bandit_state_path()
    default_state = _default_bandit_state(feature_dim)
    try:
        if not os.path.exists(path):
            return default_state
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return default_state
        if int(data.get("feature_dim", 0)) != feature_dim:
            return default_state
        arms = data.get("arms") if isinstance(data.get("arms"), dict) else {}
        for arm_name in default_state["arms"].keys():
            arm_data = arms.get(arm_name, {})
            if not arm_data:
                continue
            weights = arm_data.get("weights") if isinstance(arm_data, dict) else None
            if not isinstance(weights, list) or len(weights) != feature_dim:
                return default_state

        # Forward compatibility with evolving state schema.
        merged = default_state
        merged["total_rounds"] = int(data.get("total_rounds", 0))
        if isinstance(data.get("drift"), dict):
            merged["drift"].update(data["drift"])
        if isinstance(data.get("constraints"), dict):
            merged["constraints"].update(data["constraints"])
        for arm_name, arm_default in default_state["arms"].items():
            arm_data = arms.get(arm_name, {}) if isinstance(arms, dict) else {}
            if isinstance(arm_data, dict):
                arm_default.update(arm_data)
        return merged
    except Exception:
        return default_state


def _save_bandit_state(state: Dict[str, Any]) -> None:
    path = _bandit_state_path()
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️  保存 bandit state 失败: {e}")


def _dot(weights: List[float], features: List[float]) -> float:
    return sum(float(w) * float(x) for w, x in zip(weights, features))


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _coerce_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _trained_controller_policy_path() -> str:
    raw = os.getenv("CONTROLLER_TRAINED_POLICY_PATH", os.path.join("models", "controller_policy.json"))
    if resolve_model_path is not None:
        return resolve_model_path(raw, "controller_policy.json")
    return raw if os.path.isabs(raw) else _project_root_path(raw)


def _load_trained_controller_policy() -> Optional[Dict[str, Any]]:
    if os.getenv("CONTROLLER_TRAINED_POLICY_ENABLED", "true").lower() != "true":
        return None
    if load_json_model is None:
        return None
    path = _trained_controller_policy_path()
    try:
        mtime = os.path.getmtime(path)
    except Exception:
        return None
    if str(TRAINED_POLICY_CACHE.get("path") or "") == path and abs(float(TRAINED_POLICY_CACHE.get("mtime") or 0.0) - mtime) < 1e-9:
        cached = TRAINED_POLICY_CACHE.get("model")
        return cached if isinstance(cached, dict) else None
    model = load_json_model(path, "controller_policy")
    TRAINED_POLICY_CACHE.update({"path": path, "mtime": mtime, "model": model})
    if model:
        print(f"✅ Loaded trained controller policy: {path}")
    return model


def _extract_numeric_suffix(value: Optional[str]) -> float:
    text = str(value or "")
    m = re.search(r"(\d+)(?!.*\d)", text)
    if not m:
        return 0.0
    return min(1.0, int(m.group(1)) / 20.0)


def _ope_events_path() -> str:
    raw = os.getenv("CONTROLLER_BANDIT_EVENTS_PATH", "controller_bandit_events.jsonl").strip()
    if os.path.isabs(raw):
        return raw
    return _project_root_path(raw)


def _append_ope_event(event: Dict[str, Any]) -> None:
    try:
        path = _ope_events_path()
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"⚠️  写入 OPE 事件失败: {e}")


def _extract_quality_dims(feedback: Dict[str, Any]) -> Dict[str, float]:
    dims_raw = feedback.get("quality_dimensions") if isinstance(feedback.get("quality_dimensions"), dict) else {}
    return {k: _clip01(float(v)) for k, v in dims_raw.items() if isinstance(v, (int, float))}


def _extract_uncertainty_dims(feedback: Dict[str, Any]) -> Dict[str, float]:
    unc_raw = feedback.get("quality_dimensions_uncertainty") if isinstance(feedback.get("quality_dimensions_uncertainty"), dict) else {}
    return {k: _clip01(float(v)) for k, v in unc_raw.items() if isinstance(v, (int, float))}


def _reviewer_dimension_scores(feedback: Dict[str, Any]) -> Dict[str, float]:
    reviewer = feedback.get("reviewer_assessment") if isinstance(feedback.get("reviewer_assessment"), dict) else {}
    dims = reviewer.get("reviewer_dimensions") if isinstance(reviewer.get("reviewer_dimensions"), dict) else {}
    out: Dict[str, float] = {}
    for name, payload in dims.items():
        if isinstance(payload, dict):
            out[str(name)] = _clip01(_coerce_float(payload.get("score"), 0.0))
    return out


def _reviewer_external_alignment(feedback: Dict[str, Any]) -> Dict[str, Any]:
    reviewer = feedback.get("reviewer_assessment") if isinstance(feedback.get("reviewer_assessment"), dict) else {}
    alignment = reviewer.get("external_alignment") if isinstance(reviewer.get("external_alignment"), dict) else {}
    return alignment if isinstance(alignment, dict) else {}


def _build_defect_graph(feedback: Dict[str, Any], rel_score: float, red_score: float, rel_threshold: float, red_threshold: float) -> Dict[str, float]:
    dims = _extract_quality_dims(feedback)
    unc = _extract_uncertainty_dims(feedback)
    thresholds = feedback.get("dimension_thresholds") if isinstance(feedback.get("dimension_thresholds"), dict) else {}
    failed_dims_raw = feedback.get("quality_dimensions_failed")
    failed_dims = {str(x) for x in failed_dims_raw} if isinstance(failed_dims_raw, list) else set()
    evidence_diag = feedback.get("evidence_diagnostics") if isinstance(feedback.get("evidence_diagnostics"), dict) else {}
    source_check = feedback.get("source_check") if isinstance(feedback.get("source_check"), dict) else {}
    source_alignment = feedback.get("source_alignment") if isinstance(feedback.get("source_alignment"), dict) else {}
    try:
        source_usage = float(evidence_diag.get("source_usage_coverage", 1.0))
    except Exception:
        source_usage = 1.0
    try:
        claim_alignment = float(evidence_diag.get("claim_evidence_alignment", 1.0))
    except Exception:
        claim_alignment = 1.0
    source_failures = evidence_diag.get("source_failures") if isinstance(evidence_diag.get("source_failures"), list) else []
    severe_source_failures = {
        str(item)
        for item in source_failures
        if str(item) not in {"insufficient_citations", "missing_evidence_type:application_or_case"}
    }
    soft_citation_only_failure = bool(source_failures) and not severe_source_failures
    source_passed = bool(source_check.get("passed", True))
    source_alignment_score = _clip01(_coerce_float(source_alignment.get("score"), 1.0))
    source_alignment_terms = _clip01(_coerce_float(source_alignment.get("term_coverage"), source_alignment_score))
    source_alignment_bigrams = _clip01(_coerce_float(source_alignment.get("bigram_overlap"), source_alignment_score))
    source_alignment_count = int(source_alignment.get("source_count", 0) or 0)
    # This is a controller target, not the verifier's hard accept threshold.
    # RAG-backed paper generation needs enough source phrase preservation for
    # traceability and external ROUGE/BERTScore stability, while the generator
    # prompt still forbids mechanical keyword stuffing.
    source_alignment_floor = _clip01(float(os.getenv("CONTROLLER_SOURCE_ALIGNMENT_FLOOR", "0.76")))
    source_alignment_defect = 0.0
    if source_alignment_count > 0 and source_alignment_score < source_alignment_floor:
        source_alignment_defect = max(
            max(0.0, source_alignment_floor - source_alignment_score) / max(0.05, source_alignment_floor),
            0.65 * max(0.0, source_alignment_floor - source_alignment_terms) / max(0.05, source_alignment_floor),
            0.45 * max(0.0, source_alignment_floor - source_alignment_bigrams) / max(0.05, source_alignment_floor),
        )
        source_alignment_defect = _clip01(max(0.12, source_alignment_defect))

    def dim_gap(name: str, default_threshold: float) -> float:
        threshold = float(thresholds.get(name, default_threshold) or default_threshold)
        value = float(dims.get(name, 0.0) or 0.0)
        raw_gap = max(0.0, threshold - value) / max(0.05, threshold)
        if name in failed_dims:
            return _clip01(max(raw_gap, 0.12))
        return _clip01(raw_gap)

    relevance_gate_failed = rel_score < rel_threshold
    topic_defect = _clip01(max(0.0, rel_threshold - rel_score) / max(0.05, rel_threshold) + dim_gap("topic_alignment", 0.41))
    if relevance_gate_failed:
        # A failed top-level relevance gate is a hard defect. Soft evidence
        # diagnostics must not redirect the repair away from the failed gate.
        topic_defect = max(topic_defect, 0.12)
    coverage_defect = dim_gap("coverage_completeness", 0.40)
    evidence_soft_defect = max(
        0.0,
        0.55 * max(0.0, 0.92 - source_usage),
        0.70 * max(0.0, 0.62 - claim_alignment),
        source_alignment_defect,
        0.35 if severe_source_failures else 0.10 if soft_citation_only_failure else 0.0,
        0.25 if (not source_passed and not soft_citation_only_failure) else 0.0,
    )
    evidence_hard_defect = (
        "evidence_grounding" in failed_dims
        or (not source_passed and not soft_citation_only_failure)
        or bool(severe_source_failures)
        or source_alignment_defect >= float(os.getenv("CONTROLLER_SOURCE_ALIGNMENT_HARD_DEFECT_MIN", "0.12"))
    )
    evidence_defect = _clip01(max(dim_gap("evidence_grounding", 0.18), evidence_soft_defect))
    novelty_defect = dim_gap("novelty", 0.66)
    structure_defect = dim_gap("structure_clarity", 0.36)
    coherence_defect = dim_gap("logical_coherence", 0.25)
    redundancy_gate_failed = red_score > red_threshold
    redundancy_defect = _clip01(max(0.0, red_score - red_threshold) / max(0.05, 1.0 - red_threshold))
    if redundancy_gate_failed:
        redundancy_defect = max(redundancy_defect, 0.12)

    # Hard acceptance-gate failures take priority over soft diagnostics. A soft
    # claim-alignment warning may guide the prompt, but it must not redirect the
    # selected arm away from a failed relevance or redundancy gate.
    hard_gate_priority = max(topic_defect if relevance_gate_failed else 0.0, redundancy_defect)
    if hard_gate_priority and not evidence_hard_defect:
        evidence_defect = min(evidence_defect, max(0.0, hard_gate_priority - 0.01))

    # Uncertainty boosts risk-sensitive repair priority.
    uncertainty_pressure = _clip01(sum(unc.values()) / max(1, len(unc))) if unc else 0.0
    reviewer_dims = _reviewer_dimension_scores(feedback)

    def reviewer_gap(name: str, floor: float = 0.70) -> float:
        if name not in reviewer_dims:
            return 0.0
        return _clip01(max(0.0, floor - reviewer_dims[name]) / max(0.05, floor))

    external_alignment = _reviewer_external_alignment(feedback)
    external_metric_defect = 0.0
    if external_alignment.get("available"):
        external_mean = _coerce_float(external_alignment.get("external_mean"), 0.0)
        internal_mean = _coerce_float(external_alignment.get("internal_mean"), 0.0)
        if external_alignment.get("aligned_with_external_metrics") is False:
            external_metric_defect = max(0.18, _clip01(max(0.0, internal_mean - external_mean) / 0.35))

    citation_grounding_defect = max(reviewer_gap("citation_faithfulness"), evidence_defect if not source_passed else 0.0)
    claim_evidence_defect = max(reviewer_gap("claim_support"), evidence_defect * 0.75)
    novelty_repair_defect = max(reviewer_gap("novelty_strength"), novelty_defect, redundancy_defect * 0.6)
    reviewer_risk_defect = max(reviewer_gap("reviewer_concern_prediction"), external_metric_defect * 0.75)
    structure_readability_defect = max(
        reviewer_gap("logical_coherence"),
        reviewer_gap("experimental_completeness") * 0.35,
        structure_defect,
        coherence_defect,
    )
    reproducibility_defect = max(reviewer_gap("reproducibility_risk"), reviewer_gap("experimental_completeness") * 0.55)

    return {
        "topic": round(topic_defect, 4),
        "coverage": round(coverage_defect, 4),
        "evidence": round(evidence_defect, 4),
        "source_alignment": round(source_alignment_defect, 4),
        "novelty": round(novelty_defect, 4),
        "structure": round(structure_defect, 4),
        "coherence": round(coherence_defect, 4),
        "redundancy": round(redundancy_defect, 4),
        "hard_relevance_gate": 1.0 if relevance_gate_failed else 0.0,
        "hard_redundancy_gate": 1.0 if redundancy_gate_failed else 0.0,
        "uncertainty_pressure": round(uncertainty_pressure, 4),
        "novelty_repair": round(novelty_repair_defect, 4),
        "claim_evidence": round(claim_evidence_defect, 4),
        "citation_grounding": round(citation_grounding_defect, 4),
        "reviewer_risk": round(reviewer_risk_defect, 4),
        "external_metric": round(external_metric_defect, 4),
        "structure_readability": round(structure_readability_defect, 4),
        "reproducibility": round(reproducibility_defect, 4),
    }


def _build_dimension_guidance(feedback: Dict[str, Any]) -> List[str]:
    """把 Verifier 的失败维度转换成可直接写入改纲 prompt 的约束文本。"""
    if not isinstance(feedback, dict):
        return []

    failed_dims = feedback.get("quality_dimensions_failed")
    if not isinstance(failed_dims, list):
        failed_dims = []

    checks = feedback.get("quality_dimensions_check") if isinstance(feedback.get("quality_dimensions_check"), dict) else {}
    thresholds = feedback.get("dimension_thresholds") if isinstance(feedback.get("dimension_thresholds"), dict) else {}
    dims = feedback.get("quality_dimensions") if isinstance(feedback.get("quality_dimensions"), dict) else {}

    dimension_messages = {
        "topic_alignment": "主题对齐不足：重新强化该小节的中心论点、关键定义和必须回答的问题，避免泛化叙述。",
        "coverage_completeness": "覆盖不完整：补全大纲中缺失的关键子点、流程步骤、约束条件或对比维度。",
        "logical_coherence": "逻辑连贯性不足：按因果/递进/问题-解决结构重排小节层级，并显式要求 Claim→Evidence→Reasoning→Transition→Implication。",
        "evidence_grounding": "证据接地性不足：明确要求加入可验证事实、引用、示例或数据支撑，避免空泛结论。",
        "novelty": "新颖性不足：要求引入新的角度、反例、比较对象或未覆盖的信息，避免重复前文。",
        "structure_clarity": "结构清晰度不足：要求使用清晰的小标题、分点或步骤式结构，增强可读性和条理性。",
    }

    guidance: List[str] = []
    for dim in failed_dims:
        dim_name = str(dim)
        value = dims.get(dim_name)
        threshold = thresholds.get(dim_name)
        check = checks.get(dim_name) if isinstance(checks, dict) else {}
        if not isinstance(check, dict):
            check = {}
        if dim_name in dimension_messages:
            if isinstance(value, (int, float)) and isinstance(threshold, (int, float)):
                guidance.append(
                    f"- {dim_name}: {dimension_messages[dim_name]} 当前值={float(value):.4f}，阈值={float(threshold):.4f}，margin={float(check.get('margin', float(value) - float(threshold))):.4f}。"
                )
            else:
                guidance.append(f"- {dim_name}: {dimension_messages[dim_name]}")

    # Persona consistency guidance (if verifier provides it)
    persona_check = feedback.get("persona_check") if isinstance(feedback.get("persona_check"), dict) else {}
    persona_passed = bool(feedback.get("persona_passed", True))
    persona_threshold = float(feedback.get("persona_threshold", 0.0) or 0.0)
    persona_similarity = float(persona_check.get("similarity", 0.0) or 0.0) if persona_check else 0.0
    if persona_check and not persona_passed:
        guidance.append(
            f"- persona_consistency: 风格一致性不足，要求生成文本严格贴合指定 persona 语气、术语和叙述方式。当前={persona_similarity:.4f}，阈值={persona_threshold:.4f}。"
        )

    # Stronger coherence repairs when this dimension fails.
    if "logical_coherence" in failed_dims:
        guidance.append("- coherence_template: 每段需包含『主张句 + 证据句 + 推理句』最小结构，段尾添加过渡句连接下一段。")
        guidance.append("- transition_requirement: 每个小节至少使用 1 个显式过渡词（例如：因此/然而/此外/总之）。")
        guidance.append("- unsupported_claim_guard: 出现强结论词（必须/证明/it is clear）时，必须绑定可验证事实或引用。")

    coverage_diag = feedback.get("coverage_diagnostics") if isinstance(feedback.get("coverage_diagnostics"), dict) else {}
    if coverage_diag:
        missing_terms = [str(x) for x in coverage_diag.get("missing_terms", []) if str(x).strip()][:10]
        missing_aspects = [str(x) for x in coverage_diag.get("missing_aspects", []) if str(x).strip()][:6]
        if missing_terms:
            guidance.append("- targeted_coverage_terms: 下一版必须自然覆盖这些缺失主题词，并把它们写成具体论点而不是词表：" + "、".join(missing_terms) + "。")
        if missing_aspects:
            guidance.append("- targeted_coverage_aspects: 下一版必须补齐这些内容面向：" + "、".join(missing_aspects) + "。")

    evidence_diag = feedback.get("evidence_diagnostics") if isinstance(feedback.get("evidence_diagnostics"), dict) else {}
    if evidence_diag:
        missing_types = [str(x) for x in evidence_diag.get("missing_evidence_types", []) if str(x).strip()][:6]
        source_terms = [str(x) for x in evidence_diag.get("source_topic_terms", []) if str(x).strip()][:8]
        failures = [str(x) for x in evidence_diag.get("source_failures", []) if str(x).strip()][:5]
        if missing_types:
            guidance.append("- targeted_evidence_types: 下一版必须补齐这些证据类型：" + "、".join(missing_types) + "。")
        if source_terms:
            guidance.append("- source_grounding_terms: 优先围绕检索来源中的这些主题词构造可验证论点：" + "、".join(source_terms) + "。")
        if failures:
            guidance.append("- source_failure_repair: 修复这些来源问题：" + "、".join(failures) + "；不要编造来源或继续引用跨域来源。")

    return guidance


def _estimate_arm_cost_latency(source: str, prompt_len: int, output_len: int, llm_elapsed: float) -> Tuple[float, float]:
    # A simple, stable proxy used for constrained optimization and reproducible OPE logs.
    if source == "llm":
        token_cost = max(1.0, (prompt_len + output_len) / 4.0)
        latency = max(0.01, float(llm_elapsed))
    elif source in ("rule", "rule_structured"):
        token_cost = max(1.0, output_len / 8.0)
        latency = 0.01
    elif source == "defect_topic":
        token_cost = max(1.0, output_len / 7.0)
        latency = 0.02
    elif source in {"defect_evidence", "claim_evidence_repair", "citation_grounding_repair", "external_metric_repair", "reproducibility_repair", "reviewer_risk_repair"}:
        token_cost = max(1.0, output_len / 7.0)
        latency = 0.02
    elif source in {"defect_novelty", "novelty_repair"}:
        token_cost = max(1.0, output_len / 7.0)
        latency = 0.02
    elif source in {"defect_structure", "structure_readability_repair"}:
        token_cost = max(1.0, output_len / 7.0)
        latency = 0.02
    else:
        token_cost = max(1.0, output_len / 8.0)
        latency = 0.02
    return float(token_cost), float(latency)


def _safe_sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def _selection_probabilities(scores: Dict[str, Dict[str, float]], epsilon: float) -> Dict[str, float]:
    if not scores:
        return {}
    arms = list(scores.keys())
    best_arm = max(arms, key=lambda arm: scores[arm]["total"])
    k = len(arms)
    base = epsilon / max(1, k)
    probs = {arm: base for arm in arms}
    probs[best_arm] = min(1.0, probs[best_arm] + (1.0 - epsilon))
    return probs


def _update_constraints(state: Dict[str, Any], observed_latency: float, observed_cost: float) -> Dict[str, float]:
    constraints = state.get("constraints") if isinstance(state.get("constraints"), dict) else {}
    target_latency = max(0.05, float(os.getenv("CONTROLLER_CONSTRAINT_TARGET_LATENCY", "2.0")))
    target_cost = max(1.0, float(os.getenv("CONTROLLER_CONSTRAINT_TARGET_COST", "600")))
    dual_lr = max(0.0005, min(0.2, float(os.getenv("CONTROLLER_CONSTRAINT_DUAL_LR", "0.02"))))
    ema_alpha = max(0.01, min(0.5, float(os.getenv("CONTROLLER_CONSTRAINT_EMA_ALPHA", "0.08"))))

    avg_latency = float(constraints.get("avg_latency", 0.0))
    avg_cost = float(constraints.get("avg_cost", 0.0))
    if avg_latency <= 0:
        avg_latency = observed_latency
    else:
        avg_latency = (1 - ema_alpha) * avg_latency + ema_alpha * observed_latency

    if avg_cost <= 0:
        avg_cost = observed_cost
    else:
        avg_cost = (1 - ema_alpha) * avg_cost + ema_alpha * observed_cost

    lambda_latency = max(0.0, float(constraints.get("lambda_latency", 0.0)) + dual_lr * (avg_latency - target_latency))
    lambda_cost = max(0.0, float(constraints.get("lambda_cost", 0.0)) + dual_lr * ((avg_cost - target_cost) / target_cost))

    constraints["avg_latency"] = float(avg_latency)
    constraints["avg_cost"] = float(avg_cost)
    constraints["lambda_latency"] = float(lambda_latency)
    constraints["lambda_cost"] = float(lambda_cost)
    state["constraints"] = constraints

    return {
        "target_latency": target_latency,
        "target_cost": target_cost,
        "avg_latency": round(avg_latency, 4),
        "avg_cost": round(avg_cost, 4),
        "lambda_latency": round(lambda_latency, 4),
        "lambda_cost": round(lambda_cost, 4),
    }


def _update_drift_and_maybe_reset(state: Dict[str, Any], reward: float) -> Dict[str, Any]:
    drift = state.get("drift") if isinstance(state.get("drift"), dict) else {}
    alpha = max(0.001, min(0.2, float(os.getenv("CONTROLLER_DRIFT_ALPHA", "0.03"))))
    threshold = max(0.01, float(os.getenv("CONTROLLER_DRIFT_THRESHOLD", "0.2")))
    decay = max(0.0, min(1.0, float(os.getenv("CONTROLLER_DRIFT_DECAY", "0.6"))))

    mean = float(drift.get("mean", 0.0))
    count = int(drift.get("count", 0)) + 1
    mean = (1 - alpha) * mean + alpha * float(reward)
    centered = float(reward) - mean - 0.01
    cum_sum = float(drift.get("cum_sum", 0.0)) + centered
    min_cum = min(float(drift.get("min_cum_sum", 0.0)), cum_sum)
    statistic = cum_sum - min_cum
    triggered = statistic > threshold

    drift["mean"] = mean
    drift["count"] = count
    drift["cum_sum"] = cum_sum
    drift["min_cum_sum"] = min_cum

    if triggered:
        drift["drift_events"] = int(drift.get("drift_events", 0)) + 1
        drift["last_drift_round"] = int(state.get("total_rounds", 0))
        drift["cum_sum"] = 0.0
        drift["min_cum_sum"] = 0.0
        for arm_data in (state.get("arms") or {}).values():
            if not isinstance(arm_data, dict):
                continue
            weights = arm_data.get("weights") if isinstance(arm_data.get("weights"), list) else []
            arm_data["weights"] = [float(w) * decay for w in weights]
            arm_data["bias"] = float(arm_data.get("bias", 0.0)) * decay
            arm_data["count"] = int(float(arm_data.get("count", 0)) * decay)

    state["drift"] = drift
    return {
        "triggered": triggered,
        "statistic": round(statistic, 4),
        "threshold": round(threshold, 4),
        "drift_events": int(drift.get("drift_events", 0)),
        "last_drift_round": int(drift.get("last_drift_round", 0)),
    }


def _build_bandit_context_features(
    rel_score: float,
    red_score: float,
    rel_threshold: float,
    red_threshold: float,
    iteration: int,
    history: List[str],
    feedback: Dict[str, Any],
    defect_graph: Dict[str, float],
    section_id: Optional[str],
    subsection_id: Optional[str],
) -> List[float]:
    dims = _extract_quality_dims(feedback)
    unc = _extract_uncertainty_dims(feedback)
    rel_gap = max(0.0, rel_threshold - float(rel_score))
    red_gap = max(0.0, float(red_score) - red_threshold)
    source_passed = 1.0 if (feedback.get("source_check") or {}).get("passed") else 0.0
    unc_overall = _clip01(float(feedback.get("quality_overall_uncertainty", 0.0)))

    return [
        _clip01(rel_gap),
        _clip01(red_gap),
        _clip01(float(iteration) / 8.0),
        1.0 if history else 0.0,
        source_passed,
        _clip01(float(dims.get("topic_alignment", 0.0))),
        _clip01(float(dims.get("novelty", 0.0))),
        _clip01(float(dims.get("evidence_grounding", 0.0))),
        _clip01(float(dims.get("logical_coherence", 0.0))),
        _clip01(float(dims.get("coverage_completeness", 0.0))),
        _clip01(float(dims.get("structure_clarity", 0.0))),
        _clip01(unc_overall),
        _clip01(sum(unc.values()) / max(1, len(unc))) if unc else 0.0,
        _clip01(float(defect_graph.get("topic", 0.0))),
        _clip01(float(defect_graph.get("evidence", 0.0))),
        _clip01(float(defect_graph.get("novelty", 0.0))),
        _clip01(float(defect_graph.get("redundancy", 0.0))),
        _clip01(float(defect_graph.get("structure", 0.0))),
        _clip01(float(defect_graph.get("coherence", 0.0))),
        _clip01(float(defect_graph.get("uncertainty_pressure", 0.0))),
        _extract_numeric_suffix(section_id),
        _extract_numeric_suffix(subsection_id),
    ]


def _bandit_predict(arm_state: Dict[str, Any], features: List[float], explore_c: float) -> Tuple[float, float, float]:
    weights = arm_state.get("weights", [])
    bias = float(arm_state.get("bias", 0.0))
    count = int(arm_state.get("count", 0))
    exploit = bias + _dot(weights, features)
    explore = float(explore_c) / ((count + 1) ** 0.5)
    total = exploit + explore
    return total, exploit, explore


def _bandit_choose_arm(
    state: Dict[str, Any],
    available_arms: List[str],
    features: List[float],
    defect_graph: Dict[str, float],
) -> Tuple[str, Dict[str, Any]]:
    epsilon = max(0.0, min(0.5, float(os.getenv("CONTROLLER_BANDIT_EPSILON", "0.15"))))
    explore_c = max(0.0, float(os.getenv("CONTROLLER_BANDIT_EXPLORE_C", "0.12")))
    cooldown_streak = max(1, int(os.getenv("CONTROLLER_ARM_COOLDOWN_STREAK", "2")))
    min_alt_ratio = max(0.0, min(1.0, float(os.getenv("CONTROLLER_COOLDOWN_MIN_ALT_SCORE_RATIO", "0.55"))))
    constraints = state.get("constraints") if isinstance(state.get("constraints"), dict) else {}
    lambda_latency = float(constraints.get("lambda_latency", 0.0))
    lambda_cost = float(constraints.get("lambda_cost", 0.0))
    trained_policy = _load_trained_controller_policy()
    trained_blend = max(0.0, min(1.0, float(os.getenv("CONTROLLER_TRAINED_POLICY_BLEND", "0.35"))))

    # Defect-aware preference map for explainable arm alignment.
    arm_alignment = {
        "llm": 0.35 * defect_graph.get("topic", 0.0) + 0.25 * defect_graph.get("coverage", 0.0) + 0.20 * defect_graph.get("coherence", 0.0),
        "rule": 0.25 * defect_graph.get("redundancy", 0.0) + 0.25 * defect_graph.get("topic", 0.0),
        "rule_structured": 0.5 * defect_graph.get("structure", 0.0) + 0.25 * defect_graph.get("coherence", 0.0),
        "defect_topic": 0.8 * defect_graph.get("topic", 0.0) + 0.2 * defect_graph.get("coverage", 0.0),
        "defect_evidence": 0.8 * defect_graph.get("evidence", 0.0) + 0.2 * defect_graph.get("coverage", 0.0),
        "defect_novelty": 0.75 * defect_graph.get("novelty", 0.0) + 0.15 * defect_graph.get("redundancy", 0.0) + 0.10 * defect_graph.get("coverage", 0.0),
        "defect_structure": 0.8 * defect_graph.get("structure", 0.0) + 0.2 * defect_graph.get("coherence", 0.0),
        "novelty_repair": 0.8 * defect_graph.get("novelty_repair", defect_graph.get("novelty", 0.0)) + 0.2 * defect_graph.get("redundancy", 0.0),
        "claim_evidence_repair": 0.85 * defect_graph.get("claim_evidence", defect_graph.get("evidence", 0.0)) + 0.15 * defect_graph.get("evidence", 0.0),
        "citation_grounding_repair": 0.85 * defect_graph.get("citation_grounding", defect_graph.get("evidence", 0.0)) + 0.15 * defect_graph.get("source_alignment", 0.0),
        "reviewer_risk_repair": 0.65 * defect_graph.get("reviewer_risk", 0.0) + 0.20 * defect_graph.get("external_metric", 0.0) + 0.15 * defect_graph.get("uncertainty_pressure", 0.0),
        "external_metric_repair": 0.85 * defect_graph.get("external_metric", 0.0) + 0.15 * defect_graph.get("source_alignment", 0.0),
        "structure_readability_repair": 0.65 * defect_graph.get("structure_readability", defect_graph.get("structure", 0.0)) + 0.25 * defect_graph.get("coherence", 0.0) + 0.10 * defect_graph.get("structure", 0.0),
        "reproducibility_repair": 0.85 * defect_graph.get("reproducibility", 0.0) + 0.15 * defect_graph.get("evidence", 0.0),
    }

    scores: Dict[str, Dict[str, float]] = {}
    for arm in available_arms:
        arm_state = state.get("arms", {}).get(arm)
        if not isinstance(arm_state, dict):
            continue
        total, exploit, explore = _bandit_predict(arm_state, features, explore_c)
        avg_latency = max(0.0, float(arm_state.get("avg_latency", 0.0)))
        avg_cost = max(0.0, float(arm_state.get("avg_cost", 0.0)))
        latency_penalty = lambda_latency * min(2.0, avg_latency / max(0.1, float(os.getenv("CONTROLLER_CONSTRAINT_TARGET_LATENCY", "2.0"))))
        cost_penalty = lambda_cost * min(2.0, avg_cost / max(1.0, float(os.getenv("CONTROLLER_CONSTRAINT_TARGET_COST", "600"))))

        # Risk-aware confidence shaping: high uncertainty pressure favors defect-specific arms.
        alignment_bonus = 0.15 * _clip01(float(arm_alignment.get(arm, 0.0)))
        trained_prior = None
        trained_bonus = 0.0
        if trained_policy and predict_controller_arm_prior is not None:
            trained_prior = predict_controller_arm_prior(trained_policy, arm, features)
            if trained_prior is not None:
                trained_bonus = trained_blend * float(trained_prior)
        constrained_total = total - latency_penalty - cost_penalty + alignment_bonus + trained_bonus

        scores[arm] = {
            "total": round(constrained_total, 4),
            "base_total": round(total, 4),
            "exploit": round(exploit, 4),
            "explore": round(explore, 4),
            "latency_penalty": round(latency_penalty, 4),
            "cost_penalty": round(cost_penalty, 4),
            "alignment_bonus": round(alignment_bonus, 4),
            "trained_prior": round(float(trained_prior), 4) if trained_prior is not None else None,
            "trained_bonus": round(trained_bonus, 4),
            "avg_latency": round(avg_latency, 4),
            "avg_cost": round(avg_cost, 4),
            "count": int(arm_state.get("count", 0)),
        }

    if not scores:
        return available_arms[0], {"mode": "fallback", "scores": {}}

    hard_gate_min = max(0.0, min(1.0, float(os.getenv("CONTROLLER_HARD_GATE_ARM_MIN_DEFECT", "0.08"))))
    hard_gate_choice = ""
    if (
        "defect_topic" in scores
        and float(defect_graph.get("hard_relevance_gate", 0.0) or 0.0) > 0.0
        and float(defect_graph.get("topic", 0.0) or 0.0) >= hard_gate_min
    ):
        hard_gate_choice = "defect_topic"
    elif (
        "defect_novelty" in scores
        and float(defect_graph.get("hard_redundancy_gate", 0.0) or 0.0) > 0.0
        and max(
            float(defect_graph.get("novelty", 0.0) or 0.0),
            float(defect_graph.get("redundancy", 0.0) or 0.0),
        )
        >= hard_gate_min
    ):
        hard_gate_choice = "defect_novelty"
    else:
        research_gate_map = {
            "external_metric": "external_metric_repair",
            "reviewer_risk": "reviewer_risk_repair",
            "citation_grounding": "citation_grounding_repair",
            "claim_evidence": "claim_evidence_repair",
            "reproducibility": "reproducibility_repair",
            "structure_readability": "structure_readability_repair",
            "novelty_repair": "novelty_repair",
        }
        research_gate_min = max(0.0, min(1.0, float(os.getenv("CONTROLLER_RESEARCH_DEFECT_GATE_MIN", "0.30"))))
        eligible_research_gates = [
            (float(defect_graph.get(defect, 0.0) or 0.0), arm, defect)
            for defect, arm in research_gate_map.items()
            if arm in scores and float(defect_graph.get(defect, 0.0) or 0.0) >= research_gate_min
        ]
        if eligible_research_gates:
            _, hard_gate_choice, research_gate_defect = max(eligible_research_gates, key=lambda row: row[0])
    if hard_gate_choice:
        probs = {arm: (1.0 if arm == hard_gate_choice else 0.0) for arm in scores}
        return hard_gate_choice, {
            "mode": "hard_defect_gate" if hard_gate_choice.startswith("defect_") else "research_defect_gate",
            "scores": scores,
            "propensity": probs,
            "epsilon": 0.0,
            "cooldown_streak": cooldown_streak,
            "hard_gate": {
                "selected": hard_gate_choice,
                "hard_relevance_gate": float(defect_graph.get("hard_relevance_gate", 0.0) or 0.0),
                "hard_redundancy_gate": float(defect_graph.get("hard_redundancy_gate", 0.0) or 0.0),
                "topic": float(defect_graph.get("topic", 0.0) or 0.0),
                "novelty": float(defect_graph.get("novelty", 0.0) or 0.0),
                "redundancy": float(defect_graph.get("redundancy", 0.0) or 0.0),
                "research_defect": locals().get("research_gate_defect", ""),
            },
            "trained_policy_used": bool(trained_policy),
            "trained_policy_blend": trained_blend if trained_policy else 0.0,
            "trained_policy_version": trained_policy.get("version", "") if trained_policy else "",
        }

    drift = state.get("drift") if isinstance(state.get("drift"), dict) else {}
    recent_drift = int(state.get("total_rounds", 0) or 0) - int(drift.get("last_drift_round", 0) or 0) <= 3
    effective_epsilon = min(0.5, epsilon * (2.0 if recent_drift else 1.0))

    if random.random() < effective_epsilon:
        chosen_arm = random.choice(list(scores.keys()))
        mode = "drift_recover" if recent_drift else "epsilon_explore"
    else:
        ranked = sorted(scores.keys(), key=lambda a: scores[a]["total"], reverse=True)
        chosen_arm = ranked[0]
        mode = "score_exploit"
        chosen_state = state.get("arms", {}).get(chosen_arm, {}) if isinstance(state.get("arms"), dict) else {}
        ineffective_streak = int(chosen_state.get("ineffective_streak", 0) or 0) if isinstance(chosen_state, dict) else 0
        if ineffective_streak >= cooldown_streak and len(ranked) > 1:
            best_total = max(1e-6, float(scores[chosen_arm]["total"]))
            for alt in ranked[1:]:
                alt_state = state.get("arms", {}).get(alt, {}) if isinstance(state.get("arms"), dict) else {}
                alt_streak = int(alt_state.get("ineffective_streak", 0) or 0) if isinstance(alt_state, dict) else 0
                if alt_streak < cooldown_streak and float(scores[alt]["total"]) >= best_total * min_alt_ratio:
                    chosen_arm = alt
                    mode = "cooldown_shift"
                    break

    probs = _selection_probabilities(scores=scores, epsilon=effective_epsilon)
    return chosen_arm, {
        "mode": mode,
        "scores": scores,
        "propensity": probs,
        "epsilon": round(effective_epsilon, 4),
        "cooldown_streak": cooldown_streak,
        "trained_policy_used": bool(trained_policy),
        "trained_policy_blend": trained_blend if trained_policy else 0.0,
        "trained_policy_version": trained_policy.get("version", "") if trained_policy else "",
    }


def _apply_request_local_arm_exclusions(
    candidates: Dict[str, Dict[str, Any]],
    excluded_arms: List[str],
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """Exclude arms disproven in this subsection without creating a dead end."""
    exclusions = {
        str(arm).strip()
        for arm in (excluded_arms or [])
        if str(arm).strip()
    }
    if not exclusions:
        return dict(candidates), {}
    eligible = {
        arm: candidate
        for arm, candidate in candidates.items()
        if arm not in exclusions
    }
    if eligible:
        return eligible, {
            "request_local_exclusions": sorted(exclusions),
            "request_local_exclusions_applied": True,
        }
    return dict(candidates), {
        "request_local_exclusions": sorted(exclusions),
        "request_local_exclusions_applied": False,
        "request_local_exclusions_fallback": "all_candidates_excluded",
    }


def _filter_defect_compatible_candidates(
    candidates: Dict[str, Dict[str, Any]],
    defect_graph: Dict[str, float],
    failed_dims: List[str],
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """Keep exploration within arms capable of addressing an active defect."""
    failed = {str(name) for name in (failed_dims or [])}
    active = set()
    threshold = float(os.getenv("CONTROLLER_ARM_COMPATIBILITY_MIN_DEFECT", "0.08"))
    for defect in (
        "topic", "evidence", "novelty", "structure", "coherence", "redundancy",
        "claim_evidence", "citation_grounding", "reviewer_risk", "external_metric",
        "structure_readability", "reproducibility", "novelty_repair",
    ):
        if float(defect_graph.get(defect, 0.0) or 0.0) >= threshold:
            active.add(defect)
    dimension_defects = {
        "topic_alignment": "topic",
        "coverage_completeness": "topic",
        "evidence_grounding": "evidence",
        "novelty": "novelty",
        "structure_clarity": "structure",
        "logical_coherence": "coherence",
    }
    active.update(dimension_defects[name] for name in failed if name in dimension_defects)
    if not active:
        return dict(candidates), {}

    capabilities = {
        "defect_topic": {"topic"},
        "defect_evidence": {"evidence"},
        "defect_novelty": {"novelty", "redundancy"},
        "defect_structure": {"structure", "coherence"},
        "novelty_repair": {"novelty", "novelty_repair", "redundancy"},
        "claim_evidence_repair": {"claim_evidence", "evidence"},
        "citation_grounding_repair": {"citation_grounding", "claim_evidence", "evidence"},
        "reviewer_risk_repair": {"reviewer_risk", "external_metric", "claim_evidence", "citation_grounding"},
        "external_metric_repair": {"external_metric", "citation_grounding", "claim_evidence", "evidence"},
        "structure_readability_repair": {"structure_readability", "structure", "coherence"},
        "reproducibility_repair": {"reproducibility"},
        "rule_structured": {"structure", "coherence", "evidence"},
        "rule": {"topic", "novelty", "redundancy"},
        "llm": {"topic", "evidence", "novelty", "structure", "coherence"},
    }
    compatible = {
        arm: candidate
        for arm, candidate in candidates.items()
        if capabilities.get(arm, set()) & active
    }
    if not compatible:
        return dict(candidates), {
            "active_defects": sorted(active),
            "defect_compatibility_applied": False,
            "defect_compatibility_fallback": "no_compatible_candidate",
        }
    return compatible, {
        "active_defects": sorted(active),
        "defect_compatibility_applied": True,
        "defect_incompatible_arms_removed": sorted(set(candidates) - set(compatible)),
    }


def _bandit_update(
    state: Dict[str, Any],
    arm: str,
    features: List[float],
    reward: float,
    predicted_exploit: float,
    observed_latency: float,
    observed_cost: float,
    uncertainty_pressure: float,
    effective: bool = True,
) -> None:
    base_lr = max(0.001, min(0.2, float(os.getenv("CONTROLLER_BANDIT_LR", "0.05"))))
    # Higher uncertainty encourages faster adaptation under non-stationarity.
    lr = min(0.25, base_lr * (1.0 + 0.8 * _clip01(uncertainty_pressure)))
    reward = _clip01(reward)
    arm_state = state.get("arms", {}).get(arm)
    if not isinstance(arm_state, dict):
        return

    weights = arm_state.get("weights") or []
    if len(weights) != len(features):
        return
    bias = float(arm_state.get("bias", 0.0))
    error = reward - float(predicted_exploit)

    new_weights = []
    for w, x in zip(weights, features):
        new_weights.append(float(w) + lr * error * float(x))

    arm_state["weights"] = new_weights
    arm_state["bias"] = bias + lr * error
    arm_state["count"] = int(arm_state.get("count", 0)) + 1

    ema_alpha = max(0.01, min(0.5, float(os.getenv("CONTROLLER_CONSTRAINT_EMA_ALPHA", "0.08"))))
    prev_latency = float(arm_state.get("avg_latency", 0.0))
    prev_cost = float(arm_state.get("avg_cost", 0.0))
    arm_state["avg_latency"] = float(observed_latency if prev_latency <= 0 else (1 - ema_alpha) * prev_latency + ema_alpha * observed_latency)
    arm_state["avg_cost"] = float(observed_cost if prev_cost <= 0 else (1 - ema_alpha) * prev_cost + ema_alpha * observed_cost)
    weak_reward = reward < float(os.getenv("CONTROLLER_ARM_WEAK_REWARD_THRESHOLD", "0.025"))
    arm_state["ineffective_streak"] = 0 if effective and not weak_reward else int(arm_state.get("ineffective_streak", 0) or 0) + 1


def _get_outliner_session():
    s = requests.Session()
    s.trust_env = False
    return s


def _outliner_post(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    base = (os.getenv("OUTLINER_URL", "http://localhost:8003")).rstrip("/")
    timeout = int(os.getenv("OUTLINER_HTTP_TIMEOUT", "30"))
    resp = _get_outliner_session().post(f"{base}{path}", json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _get_controller_llm_session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    return s


def _parse_llm_content_from_response(data: Any) -> str:
    container = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else data
    choice = ((container.get("choices") or [{}])[0] or {}) if isinstance(container, dict) else {}
    msg = choice.get("message") if isinstance(choice, dict) else None
    content = ""
    if isinstance(msg, str):
        content = msg
    elif isinstance(msg, dict):
        content = msg.get("content", "")
    if isinstance(content, list):
        parts = [str(item.get("text", "")) for item in content if isinstance(item, dict)]
        content = "".join(parts)
    return str(content or "").strip()


def _generate_outline_with_sensenova(prompt: str, max_tokens: int, timeout: int, retries: int) -> Tuple[Optional[str], str]:
    api_key = os.getenv("CONTROLLER_SENSENOVA_API_KEY", os.getenv("SENSENOVA_API_KEY", "")).strip()
    if not api_key:
        return None, "CONTROLLER_SENSENOVA_API_KEY not set"

    api_url = os.getenv(
        "CONTROLLER_SENSENOVA_API_URL",
        os.getenv("SENSENOVA_API_URL", "https://api.sensenova.cn/v1/llm/chat-completions")
    ).rstrip("/")
    model = os.getenv(
        "CONTROLLER_SENSENOVA_MODEL",
        os.getenv("SENSENOVA_MODEL", "SenseNova-V6-5-Turbo")
    )

    payload_variants = [
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "stream": False,
            "max_tokens": max_tokens,
        },
        {
            "model": model,
            "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
            "temperature": 0.3,
            "stream": False,
            "max_tokens": max_tokens,
        },
    ]
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    last_error = "unknown"
    session = _get_controller_llm_session()
    for attempt in range(1, retries + 1):
        for payload in payload_variants:
            try:
                resp = session.post(api_url, json=payload, headers=headers, timeout=timeout)
                if resp.status_code >= 400:
                    text = (resp.text or "").strip()
                    last_error = f"SenseNova HTTP {resp.status_code}: {text[:300]}"
                    # 4xx 可能是 content 格式差异，允许切换 payload 继续尝试
                    continue
                content = _parse_llm_content_from_response(resp.json())
                if content:
                    return content, ""
                last_error = "SenseNova empty response"
            except Exception as e:
                last_error = f"SenseNova request failed: {str(e)}"

        if attempt < retries:
            time.sleep(min(6, 1.5 * attempt))

    return None, last_error


def _generate_outline_via_generator(prompt: str, max_tokens: int, timeout: int, retries: int) -> Tuple[Optional[str], str]:
    generator_base = (os.getenv("GENERATOR_URL", "http://localhost:8002")).rstrip("/")
    last_error = "generator_unavailable"
    for attempt in range(1, retries + 1):
        try:
            sess = _get_controller_llm_session()
            resp = sess.post(
                f"{generator_base}/generate",
                json={"prompt": prompt, "max_tokens": max_tokens},
                timeout=timeout,
            )
            if resp.status_code == 200:
                body = resp.json()
                if body.get("success") and body.get("draft"):
                    return _sanitize_outline_text(str(body["draft"]).strip()), ""
                last_error = str(body.get("error") or "llm_generate_unsuccessful")
            else:
                last_error = f"HTTP {resp.status_code}: {(resp.text or '')[:200]}"
        except Exception as e:
            last_error = str(e)

        if attempt < retries:
            time.sleep(min(4, 1.2 * attempt))

    return None, last_error


def _fetch_subsection_outline_from_db(
    document_id: Optional[str],
    section_id: Optional[str],
    subsection_id: Optional[str],
) -> Optional[str]:
    """通过 outliner 服务从数据库中取 subsection 当前大纲。"""
    if not (document_id and section_id and subsection_id):
        return None
    try:
        body = _outliner_post(
            "/subsection-tracking/get",
            {"document_id": document_id, "section_id": section_id, "subsection_id": subsection_id},
        )
        tracking = body.get("tracking") or {}
        outline = (tracking.get("outline") or "").strip()
        if outline:
            return outline
    except Exception as e:
        print(f"⚠️  从 DB 读取 subsection outline 失败: {e}")
    try:
        body = _outliner_post(
            "/outline/get",
            {
                "document_id": document_id,
                "outline_type": "subsection",
                "section_id": section_id,
                "subsection_id": subsection_id,
            },
        )
        outline = (body.get("outline") or "").strip()
        if outline:
            return outline
    except Exception as e:
        print(f"⚠️  从 DB 读取 subsection outline（outline表）失败: {e}")
    return None


def _save_improved_outline_to_db(
    document_id: Optional[str],
    section_id: Optional[str],
    subsection_id: Optional[str],
    improved_outline: str,
    iteration_count: Optional[int] = None,
):
    """把改进后的大纲回写到 subsection_tracking 表。"""
    if not (document_id and section_id and subsection_id):
        return
    try:
        payload: Dict[str, Any] = {
            "document_id": document_id,
            "section_id": section_id,
            "subsection_id": subsection_id,
            "outline": improved_outline,
        }
        if iteration_count is not None:
            payload["iteration_count"] = iteration_count
        _outliner_post("/subsection-tracking/update", payload)
        print(f"✅ 改进大纲已写入 DB: {subsection_id}")
    except Exception as e:
        print(f"⚠️  回写改进大纲失败: {e}")


def _sanitize_outline_text(
    text: Optional[str],
    *,
    strip_repair_blocks: bool = True,
) -> str:
    """清洗被规则降级文本污染的大纲，避免相关性计算被元信息拉低。"""
    outline = (text or "").strip()
    if not outline:
        return ""

    # Internal repair blocks are generation instructions, not canonical outline
    # content. Strip them before scoring or composing the next repair so retries
    # do not accumulate identical blocks and become no-ops.
    repair_markers = (
        "【第 ",
        "【失败维度定向修复建议】",
        "【主题恢复 / Topic Recovery】",
        "【主题锁定】",
        "【证据计划 / Evidence Plan】",
        "【新颖性修复 / Novelty Repair】",
        "【结构化修复约束】",
        "【本地修订约束】",
        "[Topic Recovery]",
        "[Topic Lock]",
        "[Evidence Plan]",
        "[Novelty Repair]",
        "[Structure Repair]",
        "[Local Revision Constraints]",
    )
    if strip_repair_blocks:
        marker_positions = [outline.find(marker) for marker in repair_markers]
        marker_positions = [position for position in marker_positions if position > 0]
        if marker_positions:
            outline = outline[:min(marker_positions)].strip()

    # 去掉常见前缀标签
    for prefix in ("改进后大纲：", "改进后的大纲：", "大纲："):
        if outline.startswith(prefix):
            outline = outline[len(prefix):].strip()

    return outline


def _prefers_english_repair_text(*texts: str) -> bool:
    joined = "\n".join(str(text or "") for text in texts)
    latin = len(re.findall(r"\b[A-Za-z][A-Za-z0-9+._/-]*\b", joined))
    cjk = len(re.findall(r"[\u4e00-\u9fff]", joined))
    return latin >= 8 and latin > max(3, cjk)


def _tokenize_text(text: str) -> List[str]:
    tokens = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9_-]{2,}", text or "")
    return [token.lower() for token in tokens if token.strip()]


def _extract_anchor_terms(text: str, max_terms: int = 16) -> List[str]:
    counter = Counter(_tokenize_text(text))
    return [term for term, _ in counter.most_common(max_terms)]


def _keyword_coverage(candidate: str, anchors: List[str]) -> float:
    if not anchors:
        return 0.0
    normalized = (candidate or "").lower()
    hit = sum(1 for term in anchors if term and term in normalized)
    return hit / max(1, len(anchors))


def _token_overlap_ratio(text_a: str, text_b: str) -> float:
    set_a = set(_tokenize_text(text_a))
    set_b = set(_tokenize_text(text_b))
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def _structure_score(outline: str) -> float:
    lines = [line.strip() for line in (outline or "").splitlines() if line.strip()]
    if not lines:
        return 0.0
    bullet_like = sum(1 for line in lines if line.startswith(("-", "*", "1.", "2.", "3.", "①", "②", "③")))
    line_count_score = min(1.0, len(lines) / 4.0)
    bullet_score = min(1.0, bullet_like / max(1, len(lines)))
    return 0.6 * line_count_score + 0.4 * bullet_score


def _normalize_outline_for_compare(text: str) -> str:
    """Normalize outline text for semantic-equivalent comparison."""
    normalized = re.sub(r"\s+", " ", str(text or "").strip())
    return normalized.lower()


def _coherence_outline_signal(outline: str) -> float:
    """Estimate whether an outline is likely to produce coherent prose.

    Signals:
    - has explicit argument chain markers (claim/evidence/reasoning/transition)
    - has transition connectors
    - has stepwise structure
    """
    text = str(outline or "")
    lower = text.lower()

    claim_hit = 1.0 if re.search(r"主张|核心结论|结论|claim|thesis", lower) else 0.0
    evidence_hit = 1.0 if re.search(r"证据|数据|案例|引用|evidence|citation|data|example", lower) else 0.0
    reasoning_hit = 1.0 if re.search(r"推理|原因|机制|because|reasoning|rationale", lower) else 0.0
    implication_hit = 1.0 if re.search(r"小结|启示|结论延伸|implication|takeaway", lower) else 0.0

    transition_count = len(re.findall(r"因此|然而|此外|总之|同时|所以|therefore|however|moreover|in conclusion|meanwhile", lower))
    transition_score = min(1.0, transition_count / 4.0)

    step_markers = len(re.findall(r"(^|\n)\s*(\d+[\.|\)]|[①②③④⑤]|first|second|third|finally)", lower))
    step_score = min(1.0, step_markers / 3.0)

    chain_score = (claim_hit + evidence_hit + reasoning_hit + implication_hit) / 4.0
    return _clip01(0.45 * chain_score + 0.30 * transition_score + 0.25 * step_score)


def _evidence_outline_signal(outline: str) -> float:
    """Estimate whether an outline can realistically repair weak grounding.

    A useful evidence repair outline should not merely say "add evidence"; it
    should specify claim slots, acceptable evidence types, citation placement,
    and anti-hallucination constraints. These features are cheap to score and
    make the bandit reward reflect the actual generator-facing repair quality.
    """
    text = str(outline or "")
    lower = text.lower()

    claim_slot = 1.0 if re.search(r"主张|核心论点|claim|thesis|待验证命题", lower) else 0.0
    evidence_slot = 1.0 if re.search(r"证据槽|证据计划|evidence slot|source slot|来源槽", lower) else 0.0
    citation_slot = 1.0 if re.search(r"引用位置|引用标记|citation|\\[source\\]|\\[ref\\]|\\[来源\\]", lower) else 0.0
    source_type = 1.0 if re.search(r"论文|报告|数据集|基准|实验|统计|案例|可靠来源|retrieved source|source_results", lower) else 0.0
    guard = 1.0 if re.search(r"不得编造|不可虚构|缺少来源|无来源则|hallucination|unsupported", lower) else 0.0

    numbered_slots = len(re.findall(r"证据槽\s*[A-Z0-9一二三四五六七八九十]*|evidence slot", lower))
    slot_score = min(1.0, numbered_slots / 3.0)

    return _clip01(
        0.18 * claim_slot
        + 0.22 * evidence_slot
        + 0.18 * citation_slot
        + 0.16 * source_type
        + 0.16 * guard
        + 0.10 * slot_score
    )


def _score_outline_candidate(
    candidate_outline: str,
    original_outline: str,
    working_outline: str,
    failed_draft: str,
    history: List[str],
    rel_score: float,
    red_score: float,
    rel_threshold: float,
    red_threshold: float,
    feedback: Optional[Dict[str, Any]] = None,
    defect_graph: Optional[Dict[str, float]] = None,
) -> Dict[str, float]:
    anchors = _extract_anchor_terms(original_outline)
    relevance_anchor = _keyword_coverage(candidate_outline, anchors)
    similarity_to_working = 1.0 - _token_overlap_ratio(candidate_outline, working_outline)

    history_text = "\n".join(history[-3:]) if history else ""
    overlap_history = _token_overlap_ratio(candidate_outline, history_text) if history_text else 0.0
    overlap_failed = _token_overlap_ratio(candidate_outline, failed_draft)
    novelty = 1.0 - min(1.0, 0.65 * overlap_history + 0.35 * overlap_failed)

    structure = _structure_score(candidate_outline)
    coherence_signal = _coherence_outline_signal(candidate_outline)
    evidence_signal = _evidence_outline_signal(candidate_outline)

    rel_gap = max(0.0, rel_threshold - rel_score)
    red_gap = max(0.0, red_score - red_threshold)

    defect_graph = defect_graph or {}
    coherence_need = _clip01(float(defect_graph.get("coherence", 0.0)))
    topic_need = _clip01(max(rel_gap, float(defect_graph.get("topic", 0.0))))
    novelty_need = _clip01(max(red_gap, float(defect_graph.get("redundancy", 0.0)), float(defect_graph.get("novelty", 0.0))))
    structure_need = _clip01(float(defect_graph.get("structure", 0.0)))
    evidence_need = _clip01(float(defect_graph.get("evidence", 0.0)))

    # Dynamic weights to align Controller objective with Verifier failures.
    rel_weight = 0.32 + 0.20 * topic_need
    novelty_weight = 0.14 + 0.18 * novelty_need
    structure_weight = 0.12 + 0.16 * structure_need
    coherence_weight = 0.18 + 0.28 * coherence_need
    evidence_weight = 0.12 + 0.30 * evidence_need
    delta_weight = 0.06

    # If verifier explicitly failed logical_coherence, push more weight to coherence optimization.
    failed_dims = feedback.get("quality_dimensions_failed") if isinstance(feedback, dict) and isinstance(feedback.get("quality_dimensions_failed"), list) else []
    if "logical_coherence" in failed_dims:
        coherence_weight += 0.10
        rel_weight = max(0.20, rel_weight - 0.05)
        novelty_weight = max(0.10, novelty_weight - 0.03)
    if "evidence_grounding" in failed_dims:
        evidence_weight += 0.12
        structure_weight += 0.03
    if "novelty" in failed_dims:
        novelty_weight += 0.16
        # Novelty repair must add information without weakening source support.
        # Keep evidence in the objective so a "new" outline does not drift away
        # from claim-evidence alignment and then get rolled back by no-harm.
        evidence_weight += 0.02
        coherence_weight += 0.03
    if topic_need > evidence_need + 0.15:
        # When topic drift is the dominant defect, do not let evidence planning
        # overwhelm the repair objective. Evidence still matters, but topic
        # recovery must come first.
        evidence_weight *= 0.35
        rel_weight += 0.18
        coherence_weight += 0.04

    total_weight = max(1e-6, rel_weight + novelty_weight + structure_weight + coherence_weight + evidence_weight + delta_weight)
    rel_weight /= total_weight
    novelty_weight /= total_weight
    structure_weight /= total_weight
    coherence_weight /= total_weight
    evidence_weight /= total_weight
    delta_weight /= total_weight

    total = (
        rel_weight * relevance_anchor
        + novelty_weight * novelty
        + structure_weight * structure
        + coherence_weight * coherence_signal
        + evidence_weight * evidence_signal
        + delta_weight * similarity_to_working
    )

    return {
        "total": round(total, 4),
        "relevance_anchor": round(relevance_anchor, 4),
        "novelty": round(novelty, 4),
        "structure": round(structure, 4),
        "coherence_signal": round(coherence_signal, 4),
        "evidence_signal": round(evidence_signal, 4),
        "delta_from_working": round(similarity_to_working, 4),
        "weights": {
            "relevance_anchor": round(rel_weight, 4),
            "novelty": round(novelty_weight, 4),
            "structure": round(structure_weight, 4),
            "coherence_signal": round(coherence_weight, 4),
            "evidence_signal": round(evidence_weight, 4),
            "delta_from_working": round(delta_weight, 4),
        },
    }


def _score_value(candidate: Dict[str, Any], name: str, default: float = 0.0) -> float:
    score = candidate.get("score", {}) if isinstance(candidate.get("score"), dict) else {}
    try:
        return float(score.get(name, default) or default)
    except Exception:
        return default


def _evidence_active_for_novelty_repair(
    evidence_diag: Dict[str, Any],
    failed_dims: List[str],
    defect_graph: Dict[str, float],
) -> bool:
    """Treat soft evidence weakness as active during novelty repairs.

    Novelty repairs often rewrite the subsection to reduce repetition. If the
    current draft already has weak claim-evidence alignment, selecting a
    novelty-only candidate can make the repair look locally safe while harming
    grounding and downstream lexical/semantic metrics.
    """
    failed = {str(x) for x in failed_dims or []}
    source_usage = float(evidence_diag.get("source_usage_coverage", 0.0) or 0.0) if isinstance(evidence_diag, dict) else 0.0
    evidence_type = float(evidence_diag.get("evidence_type_coverage", 0.0) or 0.0) if isinstance(evidence_diag, dict) else 0.0
    claim_alignment = float(evidence_diag.get("claim_evidence_alignment", 0.0) or 0.0) if isinstance(evidence_diag, dict) else 0.0
    source_failures = evidence_diag.get("source_failures") if isinstance(evidence_diag, dict) else []
    if not isinstance(source_failures, list):
        source_failures = []
    severe_source_failures = {
        str(item)
        for item in source_failures
        if str(item) not in {"insufficient_citations", "missing_evidence_type:application_or_case"}
    }
    weak_source_alignment = (
        source_usage < float(os.getenv("CONTROLLER_NOVELTY_SOURCE_USAGE_FLOOR", "0.92"))
        and claim_alignment < float(os.getenv("CONTROLLER_NOVELTY_CLAIM_ALIGNMENT_FLOOR", "0.65"))
    )
    severe_claim_gap = claim_alignment < float(os.getenv("CONTROLLER_NOVELTY_SEVERE_CLAIM_ALIGNMENT_FLOOR", "0.45"))
    severe_evidence_type_gap = evidence_type < float(os.getenv("CONTROLLER_NOVELTY_SEVERE_EVIDENCE_TYPE_FLOOR", "0.55"))
    return bool(
        "evidence_grounding" in failed
        or float(defect_graph.get("evidence", 0.0) or 0.0) >= 0.25
        or bool(severe_source_failures)
        or weak_source_alignment
        or severe_claim_gap
        or severe_evidence_type_gap
    )


def _select_evidence_preserving_novelty_candidate(
    chosen: Dict[str, Any],
    evidence_candidate: Optional[Dict[str, Any]],
    evidence_guard_active: bool,
) -> Optional[Dict[str, Any]]:
    """Prefer an evidence arm when it preserves novelty within a small slack."""
    if not evidence_guard_active or not chosen or not evidence_candidate:
        return None
    chosen_novelty = _score_value(chosen, "novelty")
    chosen_total = _score_value(chosen, "total")
    novelty_slack = float(os.getenv("CONTROLLER_EVIDENCE_NOVELTY_SLACK", "0.025"))
    total_slack = float(os.getenv("CONTROLLER_EVIDENCE_NOVELTY_TOTAL_SLACK", "0.06"))
    if (
        _score_value(evidence_candidate, "relevance_anchor") >= 0.92
        and _score_value(evidence_candidate, "evidence_signal") >= 0.75
        and _score_value(evidence_candidate, "novelty") >= chosen_novelty - novelty_slack
        and _score_value(evidence_candidate, "total") >= chosen_total - total_slack
    ):
        return evidence_candidate
    return None


def _coherence_repair_blocks_evidence_override(
    *,
    chosen_source: str,
    failed_dims: List[str],
    defect_graph: Dict[str, float],
    evidence_diag: Dict[str, Any],
) -> bool:
    """Keep coherence repairs targeted unless source/evidence failure is severe."""
    if str(chosen_source or "") not in {"rule_structured", "defect_structure"}:
        return False
    failed = {str(item) for item in failed_dims or []}
    if "logical_coherence" not in failed or "evidence_grounding" in failed:
        return False
    evidence_need = max(
        float((defect_graph or {}).get("evidence", 0.0) or 0.0),
        float((defect_graph or {}).get("source_alignment", 0.0) or 0.0),
    )
    if evidence_need >= float(os.getenv("CONTROLLER_COHERENCE_GUARD_SEVERE_EVIDENCE_NEED", "0.55")):
        return False
    diag = evidence_diag if isinstance(evidence_diag, dict) else {}
    source_usage = float(diag.get("source_usage_coverage", 0.0) or 0.0)
    claim_alignment = float(diag.get("claim_evidence_alignment", 0.0) or 0.0)
    if source_usage < float(os.getenv("CONTROLLER_COHERENCE_GUARD_SEVERE_SOURCE_USAGE", "0.55")):
        return False
    if claim_alignment < float(os.getenv("CONTROLLER_COHERENCE_GUARD_SEVERE_CLAIM_ALIGNMENT", "0.45")):
        return False
    source_failures = diag.get("source_failures") if isinstance(diag.get("source_failures"), list) else []
    severe_source_failures = {
        str(item)
        for item in source_failures
        if str(item) not in {"insufficient_citations", "missing_evidence_type:application_or_case"}
    }
    if severe_source_failures:
        return False
    return True


def _select_stalled_topic_complement_candidate(
    *,
    hard_gate_arm: str,
    chosen: Dict[str, Any],
    best_candidate_by_source: Dict[str, Dict[str, Any]],
    feedback: Dict[str, Any],
    rel_score: float,
    rel_threshold: float,
    iteration: int,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """Allow a source/evidence complement when topic hard-gate repair stalls.

    This keeps the hard relevance gate strict on clear topic failures, while
    avoiding repeated same-arm repairs when the draft is near the relevance
    threshold and every other verifier dimension already looks safe.
    """
    if hard_gate_arm != "defect_topic" or not chosen:
        return None, {}
    if int(iteration or 1) < int(os.getenv("CONTROLLER_TOPIC_COMPLEMENT_MIN_ITERATION", "2")):
        return None, {}
    source_check = feedback.get("source_check") if isinstance(feedback.get("source_check"), dict) else {}
    if not bool(source_check.get("passed", False)):
        return None, {}
    failed_dims = feedback.get("quality_dimensions_failed") if isinstance(feedback.get("quality_dimensions_failed"), list) else []
    if failed_dims:
        return None, {}
    if feedback.get("quality_score_passed") is False:
        return None, {}
    rel_gap = max(0.0, float(rel_threshold or 0.0) - float(rel_score or 0.0))
    max_gap = max(0.0, float(os.getenv("CONTROLLER_TOPIC_COMPLEMENT_MAX_REL_GAP", "0.055")))
    if rel_gap <= 0.0 or rel_gap > max_gap:
        return None, {}

    chosen_total = _score_value(chosen, "total")
    chosen_relevance = _score_value(chosen, "relevance_anchor")
    min_total_ratio = max(0.0, min(1.0, float(os.getenv("CONTROLLER_TOPIC_COMPLEMENT_MIN_TOTAL_RATIO", "0.88"))))
    min_relevance_anchor = max(0.0, min(1.0, float(os.getenv("CONTROLLER_TOPIC_COMPLEMENT_MIN_RELEVANCE_ANCHOR", "0.92"))))
    min_evidence_signal = max(0.0, min(1.0, float(os.getenv("CONTROLLER_TOPIC_COMPLEMENT_MIN_EVIDENCE_SIGNAL", "0.72"))))
    candidates = [
        cand
        for source, cand in (best_candidate_by_source or {}).items()
        if (
            source in {"defect_evidence", "rule_structured"}
            and _score_value(cand, "evidence_signal") >= min_evidence_signal
            and _score_value(cand, "relevance_anchor") >= min(min_relevance_anchor, max(0.0, chosen_relevance - 0.03))
            and _score_value(cand, "total") >= max(1e-6, chosen_total) * min_total_ratio
        )
    ]
    if not candidates:
        return None, {}
    selected = max(
        candidates,
        key=lambda cand: (
            0.48 * _score_value(cand, "total")
            + 0.28 * _score_value(cand, "relevance_anchor")
            + 0.24 * _score_value(cand, "evidence_signal")
        ),
    )
    return selected, {
        "reason": "stalled_topic_source_anchor_complement",
        "hard_gate_arm": hard_gate_arm,
        "previous_arm": str(chosen.get("source", "")),
        "new_arm": str(selected.get("source", "")),
        "rel_gap": round(rel_gap, 4),
        "max_gap": round(max_gap, 4),
        "iteration": int(iteration or 1),
        "previous_total": round(chosen_total, 4),
        "new_total": round(_score_value(selected, "total"), 4),
        "new_relevance_anchor": round(_score_value(selected, "relevance_anchor"), 4),
        "new_evidence_signal": round(_score_value(selected, "evidence_signal"), 4),
    }


def _select_source_anchored_topic_repair_candidate(
    *,
    hard_gate_arm: str,
    chosen: Dict[str, Any],
    best_candidate_by_source: Dict[str, Dict[str, Any]],
    feedback: Dict[str, Any],
    rel_score: float,
    rel_threshold: float,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """Let a strong evidence arm repair topic when it preserves topic anchors.

    Some relevance failures come from weak source grounding rather than missing
    topic words. In those cases, a source/evidence repair can improve both
    reference metrics and topic alignment better than a pure topic rewrite.
    """
    if hard_gate_arm != "defect_topic" or not chosen:
        return None, {}
    evidence = (best_candidate_by_source or {}).get("defect_evidence")
    if not evidence:
        return None, {}
    source_check = feedback.get("source_check") if isinstance(feedback.get("source_check"), dict) else {}
    if not bool(source_check.get("passed", False)):
        return None, {}
    source_alignment = feedback.get("source_alignment") if isinstance(feedback.get("source_alignment"), dict) else {}
    source_alignment_count = int(source_alignment.get("source_count", 0) or 0)
    source_alignment_score = _coerce_float(source_alignment.get("score"), 1.0)
    alignment_floor = float(os.getenv("CONTROLLER_SOURCE_ANCHORED_TOPIC_ALIGNMENT_FLOOR", "0.45"))
    if source_alignment_count <= 0 or source_alignment_score >= alignment_floor:
        return None, {}
    failed_dims = feedback.get("quality_dimensions_failed") if isinstance(feedback.get("quality_dimensions_failed"), list) else []
    if failed_dims:
        return None, {}
    if feedback.get("quality_score_passed") is False:
        return None, {}

    chosen_rel = _score_value(chosen, "relevance_anchor")
    evidence_rel = _score_value(evidence, "relevance_anchor")
    chosen_total = _score_value(chosen, "total")
    evidence_total = _score_value(evidence, "total")
    min_relevance = float(os.getenv("CONTROLLER_SOURCE_ANCHORED_TOPIC_MIN_RELEVANCE", "0.94"))
    relevance_slack = float(os.getenv("CONTROLLER_SOURCE_ANCHORED_TOPIC_RELEVANCE_SLACK", "0.015"))
    min_evidence_signal = float(os.getenv("CONTROLLER_SOURCE_ANCHORED_TOPIC_MIN_EVIDENCE_SIGNAL", "0.82"))
    total_slack = float(os.getenv("CONTROLLER_SOURCE_ANCHORED_TOPIC_TOTAL_SLACK", "0.04"))
    if (
        evidence_rel >= min_relevance
        and evidence_rel >= chosen_rel - relevance_slack
        and _score_value(evidence, "evidence_signal") >= min_evidence_signal
        and evidence_total >= chosen_total - total_slack
    ):
        rel_gap = max(0.0, float(rel_threshold or 0.0) - float(rel_score or 0.0))
        return evidence, {
            "reason": "source_anchored_topic_repair",
            "hard_gate_arm": hard_gate_arm,
            "previous_arm": str(chosen.get("source", "")),
            "new_arm": str(evidence.get("source", "")),
            "rel_gap": round(rel_gap, 4),
            "source_alignment_score": round(source_alignment_score, 4),
            "alignment_floor": round(alignment_floor, 4),
            "previous_relevance_anchor": round(chosen_rel, 4),
            "new_relevance_anchor": round(evidence_rel, 4),
            "new_evidence_signal": round(_score_value(evidence, "evidence_signal"), 4),
        }
    return None, {}


def _should_soften_hard_gate_for_aligned_candidate(
    *,
    hard_gate_arm: str,
    chosen_source: str,
    chosen: Dict[str, Any],
    defect_graph: Dict[str, float],
    alignment_override: Dict[str, Any],
) -> bool:
    if str(hard_gate_arm or "") != "defect_topic":
        return False
    if str(chosen_source or "") != "defect_evidence":
        return False
    if not isinstance(alignment_override, dict) or alignment_override.get("reason") != "multi_defect_pareto_alignment":
        return False
    if str(alignment_override.get("previous_arm") or "") != hard_gate_arm:
        return False
    if str(alignment_override.get("new_arm") or "") != chosen_source:
        return False
    score = chosen.get("score", {}) if isinstance(chosen.get("score"), dict) else {}
    if float(score.get("relevance_anchor", 0.0) or 0.0) < float(os.getenv("CONTROLLER_HARD_GATE_SOFTEN_RELEVANCE_ANCHOR", "0.95")):
        return False
    if float(score.get("evidence_signal", 0.0) or 0.0) < float(os.getenv("CONTROLLER_HARD_GATE_SOFTEN_EVIDENCE_SIGNAL", "0.75")):
        return False
    if max(
        float(defect_graph.get("evidence", 0.0) or 0.0),
        float(defect_graph.get("source_alignment", 0.0) or 0.0),
    ) < float(os.getenv("CONTROLLER_HARD_GATE_SOFTEN_EVIDENCE_DEFECT", "0.08")):
        return False
    previous_utility = float(alignment_override.get("previous_utility", 0.0) or 0.0)
    new_utility = float(alignment_override.get("new_utility", 0.0) or 0.0)
    return new_utility >= previous_utility + float(os.getenv("CONTROLLER_HARD_GATE_SOFTEN_MIN_UTILITY_GAIN", "0.03"))


def _select_topic_hard_gate_no_harm_evidence_candidate(
    *,
    hard_gate_arm: str,
    topic_candidate: Optional[Dict[str, Any]],
    evidence_candidate: Optional[Dict[str, Any]],
    defect_graph: Dict[str, float],
    feedback: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    if str(hard_gate_arm or "") != "defect_topic" or not topic_candidate or not evidence_candidate:
        return None, {}
    topic_score = topic_candidate.get("score", {}) if isinstance(topic_candidate.get("score"), dict) else {}
    evidence_score = evidence_candidate.get("score", {}) if isinstance(evidence_candidate.get("score"), dict) else {}
    topic_total = float(topic_score.get("total", 0.0) or 0.0)
    evidence_total = float(evidence_score.get("total", 0.0) or 0.0)
    close_margin = float(os.getenv("CONTROLLER_TOPIC_HARD_GATE_EVIDENCE_CLOSE_MARGIN", "0.035"))
    if evidence_total < topic_total - close_margin:
        return None, {}
    topic_rel = float(topic_score.get("relevance_anchor", 0.0) or 0.0)
    evidence_rel = float(evidence_score.get("relevance_anchor", 0.0) or 0.0)
    rel_drop_max = float(os.getenv("CONTROLLER_TOPIC_HARD_GATE_EVIDENCE_MAX_REL_DROP", "0.025"))
    if evidence_rel < topic_rel - rel_drop_max:
        return None, {}
    feedback = feedback if isinstance(feedback, dict) else {}
    dimensions = feedback.get("quality_dimensions") if isinstance(feedback.get("quality_dimensions"), dict) else {}
    checks = feedback.get("quality_dimensions_check") if isinstance(feedback.get("quality_dimensions_check"), dict) else {}

    def dimension_confirmed(name: str, default_threshold: float) -> bool:
        check = checks.get(name) if isinstance(checks.get(name), dict) else {}
        value = float(dimensions.get(name, check.get("value", 0.0)) or 0.0)
        threshold = float(check.get("threshold", default_threshold) or default_threshold)
        return bool(check.get("passed", value >= threshold)) and value >= threshold + 0.08

    semantic_topic_confirmed = (
        dimension_confirmed("topic_alignment", 0.52)
        and dimension_confirmed("coverage_completeness", 0.70)
    )
    if (
        float(defect_graph.get("hard_relevance_gate", 0.0) or 0.0) > 0.0
        and not semantic_topic_confirmed
    ):
        min_rel_gain = float(os.getenv("CONTROLLER_TOPIC_HARD_GATE_EVIDENCE_MIN_REL_GAIN", "0.02"))
        if evidence_rel < topic_rel + min_rel_gain:
            return None, {}
    topic_evidence = float(topic_score.get("evidence_signal", 0.0) or 0.0)
    evidence_signal = float(evidence_score.get("evidence_signal", 0.0) or 0.0)
    min_evidence_gain = float(os.getenv("CONTROLLER_TOPIC_HARD_GATE_EVIDENCE_MIN_GAIN", "0.18"))
    if evidence_signal < topic_evidence + min_evidence_gain:
        return None, {}
    topic_need = float(defect_graph.get("topic", 0.0) or 0.0)
    coverage_need = float(defect_graph.get("coverage", 0.0) or 0.0)
    evidence_need = max(
        float(defect_graph.get("evidence", 0.0) or 0.0),
        float(defect_graph.get("source_alignment", 0.0) or 0.0),
    )
    material_topic_gap = max(topic_need, coverage_need)
    max_soft_topic_gap = float(os.getenv("CONTROLLER_TOPIC_HARD_GATE_EVIDENCE_MAX_TOPIC_GAP", "0.16"))
    evidence_dominance_margin = float(os.getenv("CONTROLLER_TOPIC_HARD_GATE_EVIDENCE_DOMINANCE_MARGIN", "0.34"))
    if material_topic_gap > max_soft_topic_gap and not semantic_topic_confirmed:
        evidence_dominates = evidence_need >= material_topic_gap + evidence_dominance_margin
        no_topic_anchor_loss = evidence_rel >= topic_rel - min(0.005, rel_drop_max)
        if not (evidence_dominates and no_topic_anchor_loss and evidence_signal >= 0.92):
            return None, {}
    # Keep the hard topic gate strict for catastrophic topic failures unless
    # the evidence candidate is both close in total score and much safer for
    # source/evidence preservation.
    catastrophic_topic_gap = float(os.getenv("CONTROLLER_TOPIC_HARD_GATE_CATASTROPHIC_TOPIC", "0.92"))
    if topic_need >= catastrophic_topic_gap and evidence_need < 0.08 and evidence_signal < 0.85:
        return None, {}
    return evidence_candidate, {
        "reason": (
            "semantic_topic_confirmed_evidence_repair"
            if semantic_topic_confirmed
            else "topic_hard_gate_no_harm_evidence"
        ),
        "hard_gate_arm": hard_gate_arm,
        "previous_arm": "defect_topic",
        "new_arm": "defect_evidence",
        "semantic_topic_confirmed": semantic_topic_confirmed,
        "topic_total": round(topic_total, 4),
        "evidence_total": round(evidence_total, 4),
        "topic_relevance_anchor": round(topic_rel, 4),
        "evidence_relevance_anchor": round(evidence_rel, 4),
        "topic_evidence_signal": round(topic_evidence, 4),
        "evidence_signal": round(evidence_signal, 4),
        "topic_need": round(topic_need, 4),
        "evidence_need": round(evidence_need, 4),
        "margin": round(close_margin, 4),
    }


def _select_defect_aligned_candidate(
    chosen: Dict[str, Any],
    best_candidate_by_source: Dict[str, Dict[str, Any]],
    defect_graph: Dict[str, float],
    failed_dims: List[str],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Prefer a candidate aligned with the complete verifier defect profile.

    Bandit history, exploration, and cooldown are useful, but they should not
    select a repair that ignores the actual verifier diagnosis. Multi-defect
    cases are scored as a Pareto problem before the dominant-defect fallback,
    so repairing coherence cannot silently discard evidence grounding.
    """
    if not chosen or not best_candidate_by_source:
        return chosen, {}

    failed = {str(x) for x in failed_dims or []}
    defect_values = {
        "topic": float(defect_graph.get("topic", 0.0) or 0.0),
        "evidence": float(defect_graph.get("evidence", 0.0) or 0.0),
        "novelty": max(
            float(defect_graph.get("novelty", 0.0) or 0.0),
            float(defect_graph.get("redundancy", 0.0) or 0.0),
        ),
        "structure": float(defect_graph.get("structure", 0.0) or 0.0),
        "coherence": float(defect_graph.get("coherence", 0.0) or 0.0),
    }
    if "topic_alignment" in failed or "coverage_completeness" in failed:
        defect_values["topic"] = max(defect_values["topic"], 0.12)
    if "evidence_grounding" in failed:
        defect_values["evidence"] = max(defect_values["evidence"], 0.12)
    if "novelty" in failed:
        defect_values["novelty"] = max(defect_values["novelty"], 0.12)
    if "structure_clarity" in failed:
        defect_values["structure"] = max(defect_values["structure"], 0.12)
    if "logical_coherence" in failed:
        defect_values["coherence"] = max(defect_values["coherence"], 0.12)

    metric_for_defect = {
        "topic": "relevance_anchor",
        "evidence": "evidence_signal",
        "novelty": "novelty",
        "structure": "structure",
        "coherence": "coherence_signal",
    }
    if "logical_coherence" in failed:
        evidence_need = max(
            defect_values["evidence"],
            float(defect_graph.get("source_alignment", 0.0) or 0.0),
        )
        severe_evidence_failure = (
            "evidence_grounding" in failed
            or evidence_need >= float(os.getenv("CONTROLLER_HARD_COHERENCE_SEVERE_EVIDENCE_MIN", "0.55"))
        )
        if not severe_evidence_failure:
            chosen_coherence = _score_value(chosen, "coherence_signal")
            chosen_evidence = _score_value(chosen, "evidence_signal")
            chosen_relevance = _score_value(chosen, "relevance_anchor")
            relevance_floor = float(os.getenv("CONTROLLER_HARD_COHERENCE_RELEVANCE_FLOOR", "0.90"))
            coherence_gain = float(os.getenv("CONTROLLER_HARD_COHERENCE_MIN_GAIN", "0.08"))
            evidence_slack = float(os.getenv("CONTROLLER_HARD_COHERENCE_EVIDENCE_SLACK", "0.18"))
            structure_candidates = [
                candidate
                for source in ("rule_structured", "defect_structure")
                for candidate in [best_candidate_by_source.get(source)]
                if candidate
                and _score_value(candidate, "relevance_anchor") >= min(relevance_floor, chosen_relevance)
                and _score_value(candidate, "coherence_signal") >= chosen_coherence + coherence_gain
                and _score_value(candidate, "evidence_signal") >= chosen_evidence - evidence_slack
            ]
            if structure_candidates:
                hard_coherence_candidate = max(
                    structure_candidates,
                    key=lambda candidate: (
                        _score_value(candidate, "coherence_signal"),
                        _score_value(candidate, "structure"),
                        _score_value(candidate, "total"),
                    ),
                )
                if str(hard_coherence_candidate.get("source", "")) != str(chosen.get("source", "")):
                    return hard_coherence_candidate, {
                        "reason": "hard_coherence_defect_alignment",
                        "previous_arm": str(chosen.get("source", "")),
                        "new_arm": str(hard_coherence_candidate.get("source", "")),
                        "previous_coherence": round(chosen_coherence, 4),
                        "new_coherence": round(_score_value(hard_coherence_candidate, "coherence_signal"), 4),
                        "evidence_need": round(evidence_need, 4),
                        "evidence_slack": evidence_slack,
                    }

    active_defects = {
        defect: value
        for defect, value in defect_values.items()
        if value >= float(os.getenv("CONTROLLER_MULTI_DEFECT_ACTIVE_MIN", "0.08"))
    }
    if len(active_defects) >= 2:
        weight_total = sum(active_defects.values()) or 1.0
        normalized_weights = {
            defect: value / weight_total for defect, value in active_defects.items()
        }
        relevance_floor = float(os.getenv("CONTROLLER_MULTI_DEFECT_RELEVANCE_FLOOR", "0.90"))
        metric_floor = float(os.getenv("CONTROLLER_MULTI_DEFECT_METRIC_FLOOR", "0.30"))
        total_weight = float(os.getenv("CONTROLLER_MULTI_DEFECT_TOTAL_WEIGHT", "0.20"))

        def multi_defect_utility(candidate: Dict[str, Any]) -> float:
            aligned = sum(
                weight * _score_value(candidate, metric_for_defect[defect])
                for defect, weight in normalized_weights.items()
            )
            return aligned + total_weight * _score_value(candidate, "total")

        eligible = [
            candidate
            for candidate in best_candidate_by_source.values()
            if (
                _score_value(candidate, "relevance_anchor") >= relevance_floor
                and all(
                    _score_value(candidate, metric_for_defect[defect]) >= metric_floor
                    for defect in active_defects
                )
            )
        ]
        if eligible:
            pareto_candidate = max(
                eligible,
                key=lambda candidate: (
                    multi_defect_utility(candidate),
                    _score_value(candidate, "total"),
                    _score_value(candidate, "evidence_signal"),
                    _score_value(candidate, "novelty"),
                ),
            )
            chosen_utility = multi_defect_utility(chosen)
            pareto_utility = multi_defect_utility(pareto_candidate)
            utility_margin = float(os.getenv("CONTROLLER_MULTI_DEFECT_UTILITY_MARGIN", "0.015"))
            if (
                str(pareto_candidate.get("source", "")) != str(chosen.get("source", ""))
                and pareto_utility >= chosen_utility + utility_margin
            ):
                return pareto_candidate, {
                    "reason": "multi_defect_pareto_alignment",
                    "active_defects": {
                        defect: round(value, 4) for defect, value in active_defects.items()
                    },
                    "previous_arm": str(chosen.get("source", "")),
                    "new_arm": str(pareto_candidate.get("source", "")),
                    "previous_utility": round(chosen_utility, 4),
                    "new_utility": round(pareto_utility, 4),
                    "utility_margin": utility_margin,
                }

    dominant_defect, dominant_value = max(defect_values.items(), key=lambda item: item[1])
    if dominant_value < float(os.getenv("CONTROLLER_DOMINANT_DEFECT_MIN", "0.08")):
        return chosen, {}

    arm_for_defect = {
        "topic": "defect_topic",
        "evidence": "defect_evidence",
        "novelty": "defect_novelty",
        "structure": "defect_structure",
        "coherence": "rule_structured",
    }
    target_arm = arm_for_defect.get(dominant_defect, "")
    target = best_candidate_by_source.get(target_arm)
    if not target:
        return chosen, {}

    chosen_source = str(chosen.get("source", ""))
    chosen_total = _score_value(chosen, "total")
    higher_total_margin = float(os.getenv("CONTROLLER_NO_HARM_HIGHER_TOTAL_MARGIN", "0.075"))
    no_harm_relevance_slack = float(os.getenv("CONTROLLER_NO_HARM_RELEVANCE_SLACK", "0.030"))
    no_harm_novelty_slack = float(os.getenv("CONTROLLER_NO_HARM_NOVELTY_SLACK", "0.025"))
    no_harm_evidence_slack = float(os.getenv("CONTROLLER_NO_HARM_EVIDENCE_SLACK", "0.050"))
    no_harm_structure_slack = float(os.getenv("CONTROLLER_NO_HARM_STRUCTURE_SLACK", "0.080"))
    higher_total_candidates = [
        cand
        for cand in best_candidate_by_source.values()
        if (
            str(cand.get("source", "")).startswith("defect_")
            and str(cand.get("source", "")) != chosen_source
            and _score_value(cand, "total") >= chosen_total + higher_total_margin
            and _score_value(cand, "relevance_anchor") >= _score_value(chosen, "relevance_anchor") - no_harm_relevance_slack
            and _score_value(cand, "novelty") >= _score_value(chosen, "novelty") - no_harm_novelty_slack
            and (
                _score_value(cand, "evidence_signal") >= _score_value(chosen, "evidence_signal") - no_harm_evidence_slack
                or _score_value(cand, "evidence_signal") >= 0.75
            )
            and _score_value(cand, "structure") >= _score_value(chosen, "structure") - no_harm_structure_slack
        )
    ]
    if higher_total_candidates:
        best_higher_total = max(
            higher_total_candidates,
            key=lambda cand: (
                _score_value(cand, "total"),
                _score_value(cand, metric_for_defect.get(dominant_defect, "total")),
                _score_value(cand, "evidence_signal"),
                _score_value(cand, "novelty"),
            ),
        )
        return best_higher_total, {
            "reason": "no_harm_higher_total_defect_arm",
            "dominant_defect": dominant_defect,
            "previous_arm": chosen_source,
            "new_arm": str(best_higher_total.get("source", "")),
            "previous_total": round(chosen_total, 4),
            "new_total": round(_score_value(best_higher_total, "total"), 4),
            "higher_total_margin": higher_total_margin,
        }

    if chosen_source == target_arm:
        return chosen, {}

    metric_name = metric_for_defect[dominant_defect]
    chosen_metric = _score_value(chosen, metric_name)
    target_metric = _score_value(target, metric_name)
    target_total = _score_value(target, "total")
    metric_slack = float(os.getenv("CONTROLLER_OFF_DEFECT_METRIC_SLACK", "0.012"))
    total_slack = float(os.getenv("CONTROLLER_DEFECT_ALIGNMENT_TOTAL_SLACK", "0.075"))
    hard_gate_alignment = (
        (dominant_defect == "topic" and float(defect_graph.get("hard_relevance_gate", 0.0) or 0.0) > 0.0)
        or (dominant_defect == "novelty" and float(defect_graph.get("hard_redundancy_gate", 0.0) or 0.0) > 0.0)
    )

    hard_gate_metric_slack = max(
        metric_slack,
        float(os.getenv("CONTROLLER_HARD_GATE_METRIC_SLACK", "0.05")),
    )
    if hard_gate_alignment and dominant_defect == "topic" and chosen_source == "defect_evidence":
        keep_margin = float(os.getenv("CONTROLLER_HARD_GATE_KEEP_HIGHER_TOTAL_EVIDENCE_MARGIN", "0.075"))
        evidence_need = max(
            float(defect_graph.get("evidence", 0.0) or 0.0),
            float(defect_graph.get("source_alignment", 0.0) or 0.0),
        )
        evidence_active_min = float(os.getenv("CONTROLLER_HARD_GATE_KEEP_EVIDENCE_ACTIVE_MIN", "0.08"))
        min_evidence_signal = float(os.getenv("CONTROLLER_HARD_GATE_KEEP_EVIDENCE_SIGNAL", "0.75"))
        if (
            evidence_need >= evidence_active_min
            and chosen_total >= target_total + keep_margin
            and chosen_metric >= target_metric - hard_gate_metric_slack
            and _score_value(chosen, "evidence_signal") >= min_evidence_signal
        ):
            return chosen, {}
    if hard_gate_alignment and target_metric >= chosen_metric - hard_gate_metric_slack:
        return target, {
            "reason": "hard_gate_defect_alignment",
            "dominant_defect": dominant_defect,
            "target_arm": target_arm,
            "previous_arm": chosen_source,
            "previous_total": round(chosen_total, 4),
            "target_total": round(target_total, 4),
            "metric": metric_name,
            "previous_metric": round(chosen_metric, 4),
            "target_metric": round(target_metric, 4),
        }

    if (
        dominant_defect == "novelty"
        and chosen_source == "defect_evidence"
        and _score_value(chosen, "evidence_signal") >= 0.75
        and chosen_metric >= target_metric - metric_slack
        and chosen_total >= target_total + float(os.getenv("CONTROLLER_OFF_DEFECT_TOTAL_ADVANTAGE", "0.02"))
    ):
        return chosen, {}

    if target_metric >= chosen_metric - metric_slack and target_total >= chosen_total - total_slack:
        return target, {
            "reason": "dominant_defect_alignment",
            "dominant_defect": dominant_defect,
            "target_arm": target_arm,
            "previous_arm": chosen_source,
            "previous_total": round(chosen_total, 4),
            "target_total": round(target_total, 4),
            "metric": metric_name,
            "previous_metric": round(chosen_metric, 4),
            "target_metric": round(target_metric, 4),
        }

    return chosen, {}


# ============ API 数据模型 ============

class RefinePromptRequest(BaseModel):
    """Prompt 修改请求"""
    old_prompt: str
    failed_draft: str
    feedback: Dict[str, Any]  # Verifier 返回的完整反馈
    outline: str
    history: List[str] = []
    iteration: int = 1


class AnalyzeFailureRequest(BaseModel):
    """失败模式分析请求"""
    failed_drafts: List[str]
    feedback_list: List[Dict[str, Any]]


class ImproveOutlineRequest(BaseModel):
    """改进大纲的请求（用于第三步）"""
    original_outline: str  # 原始大纲
    current_outline: str  # 当前大纲（可能已被改进过）
    failed_draft: str  # 验证失败的内容
    feedback: Dict[str, Any]  # Verifier 的反馈
    history: List[str] = []  # 已通过小节的历史内容（用于去重指导）
    iteration: int = 1  # 当前迭代轮次（第几次改纲）
    rel_threshold: float = 0.80  # 实际生效的相关性阈值
    red_threshold: float = 0.40  # 实际生效的冗余度阈值
    # 可选：如果传了这三个 ID，controller 会直接从数据库读取最新大纲并在成功后回写
    document_id: Optional[str] = None
    section_id: Optional[str] = None
    subsection_id: Optional[str] = None
    # Per-request experiment controls. The controller normally reads these from
    # its service environment, but local benchmarks run multiple variants
    # against one long-lived service process, so the caller must be able to
    # override them without restarting the service between rows.
    controller_bandit_enabled: Optional[bool] = None
    controller_fixed_arm: Optional[str] = None
    controller_no_bandit_allow_llm: Optional[bool] = None
    # Arms proven ineffective by a real regenerate-and-verify cycle in the
    # current subsection. This is request-local and must not leak to another
    # subsection or become a topic-specific permanent ban.
    controller_excluded_arms: List[str] = []


class BanditOutcomeRequest(BaseModel):
    selected_arm: str
    feature_vector: List[float]
    before_verification: Dict[str, Any]
    after_verification: Dict[str, Any]
    predicted_exploit: float = 0.0
    proposal_reward: float = 0.0
    application_accepted: bool = True
    document_id: Optional[str] = None
    section_id: Optional[str] = None
    subsection_id: Optional[str] = None


def _verification_outcome_utility(verification: Dict[str, Any]) -> float:
    dimensions = verification.get("quality_dimensions")
    if not isinstance(dimensions, dict):
        dimensions = {}
    dim_values = [
        _clip01(float(value or 0.0))
        for value in dimensions.values()
        if isinstance(value, (int, float))
    ]
    dim_mean = sum(dim_values) / max(1, len(dim_values))
    source_check = verification.get("source_check")
    source_passed = bool(source_check.get("passed", False)) if isinstance(source_check, dict) else False
    return _clip01(
        0.22 * _clip01(float(verification.get("relevancy_index", 0.0) or 0.0))
        + 0.12 * (1.0 - _clip01(_coerce_float(verification.get("redundancy_index"), 1.0)))
        + 0.22 * _clip01(float(verification.get("quality_score", 0.0) or 0.0))
        + 0.24 * dim_mean
        + 0.12 * (1.0 if source_passed else 0.0)
        + 0.08 * (1.0 if verification.get("is_passed", False) else 0.0)
    )


def _source_alignment_outcome_gain(before: Dict[str, Any], after: Dict[str, Any]) -> float:
    """Reward evidence repairs that make retrieved sources visibly usable.

    The Verifier's evidence_grounding scalar can stay flat even when a repair
    improves source term coverage and citation/source phrase alignment. Counting
    that gain keeps the bandit policy aligned with FlowerNet's RAG contribution
    instead of over-learning generic verifier-score changes.
    """
    before_alignment = before.get("source_alignment") if isinstance(before.get("source_alignment"), dict) else {}
    after_alignment = after.get("source_alignment") if isinstance(after.get("source_alignment"), dict) else {}
    if not before_alignment or not after_alignment:
        return 0.0
    if int(before_alignment.get("source_count", 0) or 0) <= 0 or int(after_alignment.get("source_count", 0) or 0) <= 0:
        return 0.0
    before_score = _coerce_float(before_alignment.get("score"), 0.0)
    after_score = _coerce_float(after_alignment.get("score"), 0.0)
    before_terms = _coerce_float(before_alignment.get("term_coverage"), before_score)
    after_terms = _coerce_float(after_alignment.get("term_coverage"), after_score)
    before_bigrams = _coerce_float(before_alignment.get("bigram_overlap"), before_score)
    after_bigrams = _coerce_float(after_alignment.get("bigram_overlap"), after_score)
    before_task_phrases = _coerce_float(before_alignment.get("topic_phrase_coverage"), before_score)
    after_task_phrases = _coerce_float(after_alignment.get("topic_phrase_coverage"), after_score)
    return round(
        _clip01(
            0.45 * max(0.0, after_score - before_score)
            + 0.25 * max(0.0, after_terms - before_terms)
            + 0.15 * max(0.0, after_bigrams - before_bigrams)
            + 0.15 * max(0.0, after_task_phrases - before_task_phrases)
        ),
        6,
    )


def _reviewer_score_map(verification: Dict[str, Any]) -> Dict[str, float]:
    reviewer = verification.get("reviewer_assessment") if isinstance(verification.get("reviewer_assessment"), dict) else {}
    dims = reviewer.get("reviewer_dimensions") if isinstance(reviewer.get("reviewer_dimensions"), dict) else {}
    scores: Dict[str, float] = {}
    for name, payload in dims.items():
        if isinstance(payload, dict):
            scores[str(name)] = _clip01(_coerce_float(payload.get("score"), 0.0))
    return scores


def _external_metric_proxy(verification: Dict[str, Any]) -> Optional[float]:
    reviewer = verification.get("reviewer_assessment") if isinstance(verification.get("reviewer_assessment"), dict) else {}
    alignment = reviewer.get("external_alignment") if isinstance(reviewer.get("external_alignment"), dict) else {}
    if isinstance(alignment, dict) and alignment.get("available"):
        return _clip01(_coerce_float(alignment.get("external_mean"), 0.0))
    raw = verification.get("external_metrics") if isinstance(verification.get("external_metrics"), dict) else {}
    vals = [
        _clip01(_coerce_float(raw.get(key), 0.0))
        for key in ("rouge1", "rouge2", "rougeL", "bertscore_f1")
        if raw.get(key) is not None
    ]
    if vals:
        return sum(vals) / len(vals)
    return None


def _controller_no_harm_gate(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    """Strict no-harm gate for external-metric-aligned controller outcomes."""
    reviewer_tolerance = float(os.getenv("CONTROLLER_REVIEWER_NO_HARM_TOLERANCE", "0.025"))
    external_tolerance = float(os.getenv("CONTROLLER_EXTERNAL_METRIC_NO_HARM_TOLERANCE", "0.015"))
    readability_tolerance = float(os.getenv("CONTROLLER_READABILITY_NO_HARM_TOLERANCE", "0.025"))
    grounding_tolerance = float(os.getenv("CONTROLLER_GROUNDING_NO_HARM_TOLERANCE", "0.02"))

    before_scores = _reviewer_score_map(before)
    after_scores = _reviewer_score_map(after)
    protected_reviewer_dims = (
        "citation_faithfulness",
        "claim_support",
        "logical_coherence",
        "experimental_completeness",
        "reproducibility_risk",
    )
    reviewer_regressions = {}
    for name in protected_reviewer_dims:
        if name in before_scores and name in after_scores:
            delta = after_scores[name] - before_scores[name]
            if delta < -reviewer_tolerance:
                reviewer_regressions[name] = round(delta, 4)

    before_external = _external_metric_proxy(before)
    after_external = _external_metric_proxy(after)
    external_metric_regression = (
        before_external is not None
        and after_external is not None
        and after_external < before_external - external_tolerance
    )

    before_dims = before.get("quality_dimensions") if isinstance(before.get("quality_dimensions"), dict) else {}
    after_dims = after.get("quality_dimensions") if isinstance(after.get("quality_dimensions"), dict) else {}
    thresholds = after.get("dimension_thresholds") if isinstance(after.get("dimension_thresholds"), dict) else {}
    readability_regression = False
    for name in ("logical_coherence", "structure_clarity"):
        if name in before_dims and name in after_dims:
            before_value = _coerce_float(before_dims.get(name), 0.0)
            after_value = _coerce_float(after_dims.get(name), 0.0)
            threshold = _coerce_float(thresholds.get(name), 0.0)
            still_safe = bool(
                threshold > 0.0
                and after_value >= threshold + float(os.getenv("CONTROLLER_READABILITY_SAFE_MARGIN", "0.08"))
                and before_value - after_value < float(os.getenv("CONTROLLER_READABILITY_SEVERE_DROP", "0.10"))
            )
            if after_value < before_value - readability_tolerance and not still_safe:
                readability_regression = True

    evidence_grounding_regression = False
    if "evidence_grounding" in before_dims and "evidence_grounding" in after_dims:
        evidence_grounding_regression = (
            _coerce_float(after_dims.get("evidence_grounding"), 0.0)
            < _coerce_float(before_dims.get("evidence_grounding"), 0.0) - grounding_tolerance
        )
    before_source = before.get("source_check") if isinstance(before.get("source_check"), dict) else {}
    after_source = after.get("source_check") if isinstance(after.get("source_check"), dict) else {}
    false_citation_risk = bool(before_source.get("passed", False)) and not bool(after_source.get("passed", False))

    before_alignment = before.get("source_alignment") if isinstance(before.get("source_alignment"), dict) else {}
    after_alignment = after.get("source_alignment") if isinstance(after.get("source_alignment"), dict) else {}
    source_alignment_regression = bool(
        before_alignment.get("source_count", 0)
        and after_alignment.get("source_count", 0)
        and _coerce_float(after_alignment.get("score"), 0.0)
        < _coerce_float(before_alignment.get("score"), 0.0) - grounding_tolerance
    )

    passed = not any(
        [
            reviewer_regressions,
            external_metric_regression,
            readability_regression,
            evidence_grounding_regression,
            false_citation_risk,
            source_alignment_regression,
        ]
    )
    return {
        "passed": bool(passed),
        "reviewer_regressions": reviewer_regressions,
        "external_metric_regression": bool(external_metric_regression),
        "readability_regression": bool(readability_regression),
        "evidence_grounding_regression": bool(evidence_grounding_regression),
        "source_alignment_regression": bool(source_alignment_regression),
        "false_citation_risk": bool(false_citation_risk),
        "before_external_metric_proxy": None if before_external is None else round(before_external, 4),
        "after_external_metric_proxy": None if after_external is None else round(after_external, 4),
    }


def _controller_proposal_reward_quality(
    *,
    chosen_source: str,
    score_gain: float,
    rel_anchor_gain: float,
    novelty_gain: float,
    structure_gain: float,
    evidence_gain: float,
    defect_graph: Dict[str, float],
) -> float:
    """Estimate proposal reward with arm-target alignment.

    Proposal scoring happens before the next real Verifier observation, so it
    must be conservative. Generic outline improvements should not teach an
    evidence arm that it succeeded unless the evidence/source target also
    improved; otherwise full can over-repair while simpler ablations stay
    closer to the stronger draft.
    """
    source = str(chosen_source or "")
    defect_graph = defect_graph or {}
    base = _clip01(
        max(0.0, score_gain) * 0.38
        + max(0.0, rel_anchor_gain) * 0.16
        + max(0.0, novelty_gain) * 0.12
        + max(0.0, structure_gain) * 0.10
        + max(0.0, evidence_gain) * 0.24
    )

    if source in {"defect_evidence", "claim_evidence_repair", "citation_grounding_repair", "external_metric_repair", "reproducibility_repair"}:
        target = max(0.0, evidence_gain)
        source_need = max(
            float(defect_graph.get("evidence", 0.0) or 0.0),
            float(defect_graph.get("source_alignment", 0.0) or 0.0),
            float(defect_graph.get("claim_evidence", 0.0) or 0.0),
            float(defect_graph.get("citation_grounding", 0.0) or 0.0),
            float(defect_graph.get("external_metric", 0.0) or 0.0) * 0.5,
            float(defect_graph.get("reproducibility", 0.0) or 0.0) * 0.5,
        )
        if source_need >= 0.12 and target < float(os.getenv("CONTROLLER_PROPOSAL_MIN_EVIDENCE_TARGET_GAIN", "0.03")):
            return _clip01(min(base * 0.25, 0.08))
        return _clip01(0.45 * base + 0.55 * min(1.0, target * 2.2))

    if source == "defect_topic":
        target = max(0.0, rel_anchor_gain, score_gain * 0.35)
        topic_need = float(defect_graph.get("topic", 0.0) or 0.0)
        if topic_need >= 0.12 and target < float(os.getenv("CONTROLLER_PROPOSAL_MIN_TOPIC_TARGET_GAIN", "0.02")):
            return _clip01(min(base * 0.35, 0.10))
        return _clip01(0.60 * base + 0.40 * min(1.0, target * 1.8))

    if source in {"defect_novelty", "novelty_repair"}:
        target = max(0.0, novelty_gain)
        novelty_need = max(
            float(defect_graph.get("novelty", 0.0) or 0.0),
            float(defect_graph.get("redundancy", 0.0) or 0.0),
            float(defect_graph.get("novelty_repair", 0.0) or 0.0),
        )
        if novelty_need >= 0.12 and target < float(os.getenv("CONTROLLER_PROPOSAL_MIN_NOVELTY_TARGET_GAIN", "0.02")):
            return _clip01(min(base * 0.35, 0.10))
        return _clip01(0.60 * base + 0.40 * min(1.0, target * 1.8))

    if source in {"defect_structure", "rule_structured", "structure_readability_repair", "reviewer_risk_repair"}:
        target = max(0.0, structure_gain)
        return _clip01(0.62 * base + 0.38 * min(1.0, target * 1.7))

    return base


# ============ API 端点 ============

@app.get("/")
def read_root():
    """根端点 - 检查服务状态"""
    return {
        "status": "online", 
        "message": "FlowerNet Controller is ready.", 
        "public_url": controller.public_url,
        "endpoints": {
            "/refine_prompt": "根据 Verifier 反馈修改 prompt",
            "/analyze_failures": "分析失败模式并给出建议"
        }
    }


@app.get("/health")
@app.get("/health/live")
def health_check():
    """Lightweight health endpoint for Render and upstream service probes."""
    return {"status": "ok", "service": "flowernet-controller"}


@app.post("/bandit-outcome")
async def record_bandit_outcome(req: BanditOutcomeRequest):
    """Apply delayed reward from the next real Verifier observation."""
    before = dict(req.before_verification or {})
    after = dict(req.after_verification or {})
    before_utility = _verification_outcome_utility(before)
    after_utility = _verification_outcome_utility(after)
    delta = after_utility - before_utility
    no_harm_gate = _controller_no_harm_gate(before, after)

    before_dims = before.get("quality_dimensions") if isinstance(before.get("quality_dimensions"), dict) else {}
    after_dims = after.get("quality_dimensions") if isinstance(after.get("quality_dimensions"), dict) else {}
    dimension_regression_tolerance = float(os.getenv("CONTROLLER_OUTCOME_DIMENSION_NO_HARM_TOLERANCE", "0.025"))
    safe_margin = float(os.getenv("CONTROLLER_OUTCOME_DIMENSION_SAFE_MARGIN", "0.02"))
    severe_drop = float(os.getenv("CONTROLLER_OUTCOME_DIMENSION_SEVERE_DROP", "0.10"))
    thresholds = after.get("dimension_thresholds") if isinstance(after.get("dimension_thresholds"), dict) else {}
    if not thresholds:
        checks = after.get("quality_dimensions_check") if isinstance(after.get("quality_dimensions_check"), dict) else {}
        thresholds = {
            key: value.get("threshold")
            for key, value in checks.items()
            if isinstance(value, dict) and value.get("threshold") is not None
        }
    dimension_regressions = {}
    for key, value in before_dims.items():
        if key not in after_dims:
            continue
        before_value = float(value or 0.0)
        after_value = float(after_dims.get(key, 0.0) or 0.0)
        drop = after_value - before_value
        if drop >= -dimension_regression_tolerance:
            continue
        threshold = _coerce_float(thresholds.get(key), 0.0) if isinstance(thresholds, dict) else 0.0
        still_safe = bool(threshold > 0.0 and after_value >= threshold + safe_margin and abs(drop) < severe_drop)
        if still_safe:
            continue
        dimension_regressions[key] = round(drop, 4)
    before_source = before.get("source_check") if isinstance(before.get("source_check"), dict) else {}
    after_source = after.get("source_check") if isinstance(after.get("source_check"), dict) else {}
    before_alignment = before.get("source_alignment") if isinstance(before.get("source_alignment"), dict) else {}
    after_alignment = after.get("source_alignment") if isinstance(after.get("source_alignment"), dict) else {}
    redundancy_regression = _coerce_float(after.get("redundancy_index"), 1.0) > _coerce_float(before.get("redundancy_index"), 1.0) + 0.015
    source_alignment_regression = bool(
        before_alignment.get("source_count", 0)
        and after_alignment.get("source_count", 0)
        and float(after_alignment.get("score", 0.0) or 0.0) < float(before_alignment.get("score", 0.0) or 0.0) - 0.02
    )
    task_phrase_regression = bool(
        before_alignment.get("source_count", 0)
        and after_alignment.get("source_count", 0)
        and _coerce_float(after_alignment.get("topic_phrase_coverage"), 0.0)
        < _coerce_float(before_alignment.get("topic_phrase_coverage"), 0.0)
        - float(os.getenv("CONTROLLER_OUTCOME_TASK_PHRASE_NO_HARM_TOLERANCE", "0.06"))
    )
    regressed = bool(
        dimension_regressions
        or float(after.get("relevancy_index", 0.0) or 0.0) < float(before.get("relevancy_index", 0.0) or 0.0) - 0.015
        or float(after.get("quality_score", 0.0) or 0.0) < float(before.get("quality_score", 0.0) or 0.0) - 0.02
        or redundancy_regression
        or source_alignment_regression
        or task_phrase_regression
        or (bool(before_source.get("passed", False)) and not bool(after_source.get("passed", False)))
        or not bool(no_harm_gate.get("passed", True))
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
        "external_metric_repair": ("evidence_grounding", "topic_alignment", "novelty"),
        "reviewer_risk_repair": ("evidence_grounding", "logical_coherence", "novelty"),
        "structure_readability_repair": ("structure_clarity", "logical_coherence"),
        "reproducibility_repair": ("evidence_grounding",),
    }
    target_gains = [
        float(after_dims.get(name, 0.0) or 0.0) - float(before_dims.get(name, 0.0) or 0.0)
        for name in arm_targets.get(req.selected_arm, tuple(before_dims))
        if name in before_dims and name in after_dims
    ]
    if req.selected_arm in {"defect_novelty", "novelty_repair"}:
        target_gains.append(
            _coerce_float(before.get("redundancy_index"), 1.0)
            - _coerce_float(after.get("redundancy_index"), 1.0)
        )
    if req.selected_arm == "defect_topic":
        target_gains.append(
            float(after.get("relevancy_index", 0.0) or 0.0)
            - float(before.get("relevancy_index", 0.0) or 0.0)
        )
    source_alignment_gain = _source_alignment_outcome_gain(before, after)
    if req.selected_arm in {"defect_evidence", "claim_evidence_repair", "citation_grounding_repair", "external_metric_repair", "reproducibility_repair"}:
        target_gains.append(source_alignment_gain)
    before_reviewer = _reviewer_score_map(before)
    after_reviewer = _reviewer_score_map(after)
    reviewer_targets = {
        "claim_evidence_repair": ("claim_support",),
        "citation_grounding_repair": ("citation_faithfulness",),
        "reviewer_risk_repair": ("reviewer_concern_prediction",),
        "structure_readability_repair": ("logical_coherence",),
        "reproducibility_repair": ("reproducibility_risk", "experimental_completeness"),
        "novelty_repair": ("novelty_strength",),
    }
    for reviewer_dim in reviewer_targets.get(req.selected_arm, ()):
        if reviewer_dim in before_reviewer and reviewer_dim in after_reviewer:
            target_gains.append(after_reviewer[reviewer_dim] - before_reviewer[reviewer_dim])
    if req.selected_arm == "external_metric_repair":
        before_external = _external_metric_proxy(before)
        after_external = _external_metric_proxy(after)
        if before_external is not None and after_external is not None:
            target_gains.append(after_external - before_external)
    targeted_gain = max(target_gains, default=delta)
    targeted_effective = targeted_gain >= float(os.getenv("CONTROLLER_OUTCOME_MIN_TARGET_GAIN", "0.01"))
    pass_transition = bool(after.get("is_passed", False)) and not bool(before.get("is_passed", False))
    effective = bool(req.application_accepted) and not regressed and targeted_effective and delta >= -0.002
    reward = 0.0 if (regressed or not targeted_effective) else _clip01(max(0.0, delta) * 3.0 + min(0.25, targeted_gain) + (0.15 if pass_transition else 0.0))

    feature_vector = [float(value or 0.0) for value in req.feature_vector]
    with BANDIT_LOCK:
        state = _load_bandit_state(len(feature_vector))
        arm_state = (state.get("arms") or {}).get(req.selected_arm)
        if not isinstance(arm_state, dict) or len(arm_state.get("weights") or []) != len(feature_vector):
            raise HTTPException(status_code=422, detail="unknown arm or feature dimension mismatch")
        selection_count = int(arm_state.get("count", 0) or 0)
        _bandit_update(
            state=state,
            arm=req.selected_arm,
            features=feature_vector,
            reward=reward,
            predicted_exploit=float(req.predicted_exploit or 0.0),
            observed_latency=float(arm_state.get("avg_latency", 0.0) or 0.0),
            observed_cost=float(arm_state.get("avg_cost", 0.0) or 0.0),
            uncertainty_pressure=float(after.get("quality_overall_uncertainty", 0.0) or 0.0),
            effective=effective,
        )
        arm_state["count"] = selection_count
        arm_state["outcome_count"] = int(arm_state.get("outcome_count", 0) or 0) + 1
        arm_state["realized_reward_ema"] = (
            0.85 * float(arm_state.get("realized_reward_ema", reward) or 0.0) + 0.15 * reward
        )
        state["total_outcomes"] = int(state.get("total_outcomes", 0) or 0) + 1
        drift_debug = _update_drift_and_maybe_reset(state=state, reward=reward)
        _save_bandit_state(state)

    event = {
        "timestamp": time.time(),
        "event_type": "realized_controller_outcome",
        "chosen_arm": req.selected_arm,
        "feature_vector": feature_vector,
        "proposal_reward": round(float(req.proposal_reward or 0.0), 6),
        "reward": round(float(reward), 6),
        "effective": effective,
        "application_accepted": bool(req.application_accepted),
        "before_utility": round(before_utility, 6),
        "after_utility": round(after_utility, 6),
        "utility_delta": round(delta, 6),
        "pass_transition": pass_transition,
        "regressed": regressed,
        "dimension_regressions": dimension_regressions,
        "redundancy_regression": redundancy_regression,
        "source_alignment_regression": source_alignment_regression,
        "task_phrase_regression": task_phrase_regression,
        "no_harm_gate": no_harm_gate,
        "source_alignment_gain": round(source_alignment_gain, 6),
        "targeted_gain": round(targeted_gain, 6),
        "targeted_effective": targeted_effective,
        "document_id": req.document_id,
        "section_id": req.section_id,
        "subsection_id": req.subsection_id,
        "drift_debug": drift_debug,
    }
    _append_ope_event(event)
    return {"success": True, **event}


@app.post("/refine_prompt")
async def refine_prompt(req: RefinePromptRequest):
    """
    根据 Verifier 反馈修改 Prompt
    
    输入：
    - old_prompt: 原始 prompt
    - failed_draft: 验证失败的 draft
    - feedback: Verifier 的验证反馈（包含 relevancy_index 和 redundancy_index）
    - outline: 段落大纲
    - history: 历史内容列表
    - iteration: 当前迭代次数
    
    输出：优化后的新 prompt
    """
    try:
        new_prompt = controller.refine_prompt(
            old_prompt=req.old_prompt,
            failed_draft=req.failed_draft,
            feedback=req.feedback,
            outline=req.outline,
            history=req.history,
            iteration=req.iteration
        )
        return {
            "success": True,
            "prompt": new_prompt
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@app.post("/analyze_failures")
async def analyze_failures(req: AnalyzeFailureRequest):
    """
    分析多次失败的模式
    
    输入：
    - failed_drafts: 所有失败的 draft 列表
    - feedback_list: 对应的验证反馈列表
    
    输出：失败模式分析结果
    """
    try:
        analysis = controller.analyze_failure_patterns(
            failed_drafts=req.failed_drafts,
            feedback_list=req.feedback_list
        )
        return {
            "success": True,
            "analysis": analysis
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@app.post("/improve-outline")
async def improve_outline(req: ImproveOutlineRequest):
    """
    根据验证反馈改进大纲（第三步中使用）

    如果传入了 document_id / section_id / subsection_id，
    则从数据库读取当前最新大纲作为改进起点，改进后写回数据库。
    """
    try:
        rel_score = req.feedback.get("relevancy_index", 0)
        red_score = req.feedback.get("redundancy_index", 0)
        feedback_text = req.feedback.get("feedback", "")
        # 使用请求中传入的实际值（不再从 feedback 里尝试读取）
        iteration = req.iteration
        rel_threshold = req.rel_threshold
        red_threshold = req.red_threshold

        # 优先从 DB 里取最新大纲，这样 controller 能感知之前已经改过的版本
        db_outline = _fetch_subsection_outline_from_db(
            document_id=req.document_id,
            section_id=req.section_id,
            subsection_id=req.subsection_id,
        )
        working_outline = _sanitize_outline_text(db_outline if db_outline else req.current_outline)
        original_outline = _sanitize_outline_text(req.original_outline) or working_outline
        english_repair = _prefers_english_repair_text(
            original_outline,
            working_outline,
            req.failed_draft,
            str(req.feedback.get("feedback", "") or ""),
        )

        # 构建历史上下文（已通过小节的摘要，供去重指导）
        history_context = ""
        if req.history:
            recent = req.history[-3:]  # 仅取最近3条避免 prompt 过长
            history_context = "\n\n已通过的前置内容（请避免与这些内容重复）：\n"
            for i, h in enumerate(recent, 1):
                history_context += f"[已通过小节 {i}]（前200字）: {h[:200]}\n"

        dimension_guidance = _build_dimension_guidance(req.feedback)
        dimension_guidance_text = "\n".join(dimension_guidance) if dimension_guidance else "- 暂无明确失败维度，按笼统反馈进行最小修改。"
        coverage_diag = req.feedback.get("coverage_diagnostics") if isinstance(req.feedback.get("coverage_diagnostics"), dict) else {}
        evidence_diag = req.feedback.get("evidence_diagnostics") if isinstance(req.feedback.get("evidence_diagnostics"), dict) else {}
        missing_terms = [str(x) for x in coverage_diag.get("missing_terms", []) if str(x).strip()][:10] if coverage_diag else []
        missing_aspects = [str(x) for x in coverage_diag.get("missing_aspects", []) if str(x).strip()][:6] if coverage_diag else []
        source_terms = [str(x) for x in evidence_diag.get("source_topic_terms", []) if str(x).strip()][:8] if evidence_diag else []
        missing_evidence_types = [str(x) for x in evidence_diag.get("missing_evidence_types", []) if str(x).strip()][:6] if evidence_diag else []
        targeted_gap_text = "\n".join(
            line for line in [
                ("- 缺失主题词：" + "、".join(missing_terms)) if missing_terms else "",
                ("- 缺失内容面向：" + "、".join(missing_aspects)) if missing_aspects else "",
                ("- 检索来源主题锚点：" + "、".join(source_terms)) if source_terms else "",
                ("- 缺失证据类型：" + "、".join(missing_evidence_types)) if missing_evidence_types else "",
            ]
            if line
        ) or "- 暂无结构化缺口。"

        improvement_prompt = f"""
你是一个文档写作指导专家。本次你的任务是改进一个小节的详细写作大纲，使得根据该大纲生成的内容能够通过以下验证指标：
- 相关性（relevancy_index）需 >= {rel_threshold:.2f}（当前: {rel_score:.4f}）
- 冗余度（redundancy_index）需 <= {red_threshold:.2f}（当前: {red_score:.4f}）

这是第 {iteration} 次改纲尝试。

【原始大纲（保持原始意图不变）】
{original_outline}

【当前大纲（第 {iteration-1} 轮的版本，本次需要改进）】
{working_outline}

【验证失败的内容（前500字）】
{req.failed_draft[:500]}

【Verifier 反馈】
{feedback_text}

【失败维度定向修复建议】
{dimension_guidance_text}

【结构化缺口（必须转化为正文扩写计划，不要原样堆词）】
{targeted_gap_text}

{history_context}
【改进要求】
{"1. 相关性不足（" + str(round(rel_score,4)) + " < " + str(rel_threshold) + "）：大纲要更明确、具体，强调该小节的核心主题，列出必须涵盖的关键点，确保每个写作要点都与主题直接相关。" if rel_score < rel_threshold else "1. 相关性已满足，保持当前主题聚焦度。"}
{"2. 冗余度过高（" + str(round(red_score,4)) + " > " + str(red_threshold) + "）：大纲中必须明确指出哪些角度/信息已被前文覆盖，要求写全新的视角，可以列出具体的禁止重复方向。" if red_score > red_threshold else "2. 冗余度已满足，保持现有差异化要求。"}
3. 你不是格式修补器，而是专业编辑：优先补内容覆盖、证据支撑、论证深度和主题专属性；只有必要时才调整格式。
4. 输出的大纲必须包含 Targeted Expansion Plan：列出 2-3 个最关键的新增论点，每个论点绑定一个证据槽和一个避免重复的说明；不要把所有缺口机械扩写成超长章节。

请直接输出改进后的详细大纲文本（仍然是大纲，不是正文），不要添加任何前言或解释标签。
"""

        improved_outline = None
        llm_outline = None
        llm_elapsed_sec = 0.0

        llm_error = ""
        use_llm_outline = os.getenv("CONTROLLER_USE_LLM_OUTLINE", "true").lower() == "true"
        require_llm_source = os.getenv("CONTROLLER_REQUIRE_LLM_SOURCE", "false").lower() == "true"
        llm_timeout = max(20, int(os.getenv("CONTROLLER_LLM_TIMEOUT", "60")))
        llm_retries = max(1, int(os.getenv("CONTROLLER_LLM_RETRIES", "2")))
        llm_call_mode = os.getenv("CONTROLLER_LLM_CALL_MODE", "direct").strip().lower()
        llm_fallback_to_generator = os.getenv("CONTROLLER_LLM_FALLBACK_TO_GENERATOR", "true").lower() == "true"

        if use_llm_outline:
            llm_start = time.perf_counter()
            if llm_call_mode == "generator":
                llm_outline, llm_error = _generate_outline_via_generator(
                    prompt=improvement_prompt,
                    max_tokens=700,
                    timeout=llm_timeout,
                    retries=llm_retries,
                )
            else:
                llm_outline, llm_error = _generate_outline_with_sensenova(
                    prompt=improvement_prompt,
                    max_tokens=700,
                    timeout=llm_timeout,
                    retries=llm_retries,
                )
                if not llm_outline and llm_fallback_to_generator:
                    fallback_outline, fallback_error = _generate_outline_via_generator(
                        prompt=improvement_prompt,
                        max_tokens=700,
                        timeout=llm_timeout,
                        retries=max(1, min(2, llm_retries)),
                    )
                    if fallback_outline:
                        llm_outline = fallback_outline
                        llm_error = ""
                    else:
                        llm_error = f"direct={llm_error} | fallback_generator={fallback_error}"
            llm_elapsed_sec = max(0.0, time.perf_counter() - llm_start)
        else:
            llm_error = "llm_outline_disabled"

        if not llm_outline and llm_error:
            print(f"⚠️  LLM 改进大纲失败（会使用规则降级）: {llm_error}")

        anchors = _extract_anchor_terms(original_outline, max_terms=12)
        failed_lower = str(req.failed_draft or "").lower()
        missing_anchor_terms = [term for term in anchors if term and term not in failed_lower][:6]

        history_text = "\n".join(req.history[-3:]) if req.history else ""
        history_terms = set(_extract_anchor_terms(history_text, max_terms=12)) if history_text else set()
        failed_terms = _extract_anchor_terms(req.failed_draft, max_terms=12)
        repeated_terms = [term for term in failed_terms if term in history_terms][:5]

        fallback_lines = [working_outline or original_outline]
        if rel_score < rel_threshold:
            fallback_lines.append(
                "Additional requirement: begin with the subsection's core claim, then develop outline points; every paragraph must directly match the subsection topic."
                if english_repair
                else "补充要求：开头先定义本小节核心结论，再按要点展开，每段都要与本小节主题直接对应。"
            )
            if missing_anchor_terms:
                fallback_lines.append(
                    "Additional requirement: cover these keywords naturally: " + ", ".join(missing_anchor_terms) + "."
                    if english_repair
                    else "补充要求：必须覆盖关键词：" + "、".join(missing_anchor_terms) + "。"
                )
        if red_score > red_threshold:
            fallback_lines.append(
                "Additional requirement: avoid restating prior content; replace repeated material with new facts, cases, mechanisms, or analytical angles."
                if english_repair
                else "补充要求：避免复述前文已有信息，改写为新的事实、案例或角度。"
            )
            if repeated_terms:
                fallback_lines.append(
                    "Additional requirement: avoid repeating these already-used terms: " + ", ".join(repeated_terms) + "."
                    if english_repair
                    else "补充要求：避免重复这些已出现词：" + "、".join(repeated_terms) + "。"
                )
        if missing_terms:
            fallback_lines.append(
                "Targeted Expansion Plan: turn these missing topic terms into concrete claims: " + ", ".join(missing_terms) + "."
                if english_repair
                else "Targeted Expansion Plan：必须把这些缺失主题词转化为具体论点：" + "、".join(missing_terms) + "。"
            )
        if missing_aspects:
            fallback_lines.append(
                "Targeted Expansion Plan: complete these missing content aspects: " + ", ".join(missing_aspects) + "."
                if english_repair
                else "Targeted Expansion Plan：必须补齐这些内容面向：" + "、".join(missing_aspects) + "。"
            )
        if missing_evidence_types:
            fallback_lines.append(
                "Evidence Plan: add these missing evidence types: " + ", ".join(missing_evidence_types) + "."
                if english_repair
                else "Evidence Plan：必须补齐这些证据类型：" + "、".join(missing_evidence_types) + "。"
            )
        if "偏离主题" in feedback_text or rel_score < 0.5:
            fallback_lines.append(
                "Additional requirement: remove generic background and keep only content directly tied to the current outline points."
                if english_repair
                else "补充要求：删除泛泛背景描述，只保留与当前大纲要点直接相关的内容。"
            )
        rule_outline = _sanitize_outline_text("\n".join([line for line in fallback_lines if line and line.strip()]))

        if english_repair:
            structured_blocks = [
                "[Revision Version] Structured revision round {}".format(iteration),
                "[Core Topic] " + (original_outline[:240] if original_outline else "Stay focused on the current subsection topic."),
                "[Writing Structure]",
                "1) Start with the subsection's core definition or conclusion in one to two sentences.",
                "2) Develop three to five points; each point must directly support the topic.",
                "3) End with one paragraph summarizing the subsection's new information without repeating prior text.",
            ]
            if missing_anchor_terms:
                structured_blocks.append("[Required Keywords] " + ", ".join(missing_anchor_terms))
            if missing_terms:
                structured_blocks.append("[Missing Topic Terms to Expand] " + ", ".join(missing_terms))
            if missing_aspects:
                structured_blocks.append("[Missing Content Aspects] " + ", ".join(missing_aspects))
            if repeated_terms:
                structured_blocks.append("[Avoid Repeating] " + ", ".join(repeated_terms))
            if red_score > red_threshold:
                structured_blocks.append("[Differentiation Requirement] Each point must add a new fact, case, datum, mechanism, criterion, or boundary condition.")
            structured_blocks.append("[Professional Editing Requirement] Do not merely change formatting; every edit must add topic information, evidence, or reasoning.")
            if feedback_text:
                structured_blocks.append("[Verifier Feedback Constraint] " + feedback_text[:180])
            structured_blocks.append("[Quality Thresholds] relevancy >= {:.2f}, redundancy <= {:.2f}".format(rel_threshold, red_threshold))
        else:
            structured_blocks = [
                "【改纲版本】第{}轮结构化修订".format(iteration),
                "【核心主题】" + (original_outline[:240] if original_outline else "请紧扣当前小节主题"),
                "【写作结构】",
                "1) 先给出本小节的核心定义/结论（1-2句）",
                "2) 再按 3-5 个要点展开，每个要点必须直接支撑主题",
                "3) 结尾用 1 段总结该小节新增信息，不复述前文",
            ]
            if missing_anchor_terms:
                structured_blocks.append("【必写关键词】" + "、".join(missing_anchor_terms))
            if missing_terms:
                structured_blocks.append("【缺失主题词扩写】" + "、".join(missing_terms))
            if missing_aspects:
                structured_blocks.append("【必须补齐的内容面向】" + "、".join(missing_aspects))
            if repeated_terms:
                structured_blocks.append("【禁止重复词】" + "、".join(repeated_terms))
            if red_score > red_threshold:
                structured_blocks.append("【差异化要求】每个要点至少包含一个新的事实、案例、数据或机制说明。")
            structured_blocks.append("【专业编辑要求】不要只增加格式；每一处修改都必须带来新的主题信息、证据或推理。")
            if feedback_text:
                structured_blocks.append("【Verifier反馈约束】" + feedback_text[:180])
            structured_blocks.append("【质量阈值】relevancy >= {:.2f}, redundancy <= {:.2f}".format(rel_threshold, red_threshold))

        structured_rule_outline = _sanitize_outline_text("\n".join([blk for blk in structured_blocks if blk.strip()]))

        defect_graph = _build_defect_graph(
            feedback=req.feedback,
            rel_score=float(rel_score),
            red_score=float(red_score),
            rel_threshold=float(rel_threshold),
            red_threshold=float(red_threshold),
        )

        if english_repair:
            defect_topic_lines = [
                working_outline or original_outline,
                "",
                "[Topic Recovery] Prioritize topic_alignment and coverage_completeness.",
                "[Original Topic Lock] " + (original_outline[:260] if original_outline else "Preserve the current subsection topic."),
                "- Core claim: the first paragraph must directly answer the original subsection heading, not become generic background.",
                "- Topic anchors: every paragraph must contain at least one original topic keyword and build a conclusion around it.",
                "- Coverage checklist: complete definition/mechanism -> representative methods -> application scenarios -> evaluation metrics -> risk boundaries -> future directions.",
                "- Missing topic terms: " + (", ".join(missing_terms) if missing_terms else "extract at least five concrete terms from the original outline."),
                "- Missing content aspects: " + (", ".join(missing_aspects) if missing_aspects else "infer missing aspects from the outline."),
                "- Evidence constraint: insert evidence only after a topic sentence is established; evidence must not replace the subsection topic.",
                "- Anti-drift constraint: do not shift to unrelated finance, psychology, or generic software-reliability cases.",
            ]
        else:
            defect_topic_lines = [
                working_outline or original_outline,
                "",
                "【主题恢复 / Topic Recovery】优先修复 topic_alignment 与 coverage_completeness。",
                "【原始主题锁定】" + (original_outline[:260] if original_outline else "保持当前小节主题"),
                "- 核心论点：第一段必须直接回答原始小节标题，不得改写成泛泛背景介绍。",
                "- 主题锚点：每个段落至少包含 1 个原始主题关键词，并围绕该关键词给出结论句。",
                "- 覆盖清单：按“定义/机制 -> 代表方法 -> 应用场景 -> 评价指标 -> 风险边界 -> 未来方向”补齐缺失点。",
                "- 缺失主题词：" + ("、".join(missing_terms) if missing_terms else "从原始大纲中提取至少 5 个具体术语"),
                "- 缺失内容面向：" + ("、".join(missing_aspects) if missing_aspects else "根据大纲自行判断缺失的内容面向"),
                "- 证据约束：只有在主题句完成后才插入证据，不允许证据主题替代本小节主题。",
                "- 反漂移约束：禁止转向与原始小节无关的金融、心理、软件可靠性泛化案例。",
            ]
        defect_topic_outline = _sanitize_outline_text("\n".join(defect_topic_lines), strip_repair_blocks=False)
        evidence_anchor_tokens: List[str] = []
        for _token in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9_-]{2,}", (original_outline + "\n" + working_outline)):
            if _token not in evidence_anchor_tokens:
                evidence_anchor_tokens.append(_token)
            if len(evidence_anchor_tokens) >= 10:
                break
        if english_repair:
            defect_evidence_lines = [
                working_outline or original_outline,
                "",
                "[Topic Lock] This evidence repair must not change the subsection topic: " + (original_outline[:220] if original_outline else "Preserve the current subsection topic."),
                "[Evidence Plan] Prioritize evidence_grounding; do not write only abstract claims.",
                "- Claim Slot A: state the subsection's most important evidence-supported claim in one sentence and bind it to at least two topic anchors.",
                "- Evidence Slot 1: use available retrieved sources such as papers, reports, datasets, benchmarks, or reliable cases; insert [Source] at the supported sentence.",
                "- Evidence Slot 2: add mechanistic evidence or an engineering case explaining how and under what conditions the claim holds.",
                "- Evidence Slot 3: add one limitation, counterexample, or boundary condition to avoid overgeneralizing local evidence.",
                "- Reasoning Slot: after each evidence item, add one sentence explaining how the evidence supports the claim.",
                "- Same-sentence citation constraint: sentences using indicate, suggest, show, support, demonstrate, or reveal must include a valid [n] marker in the same sentence.",
                "- Claim-citation template: 'The benchmark evidence shows the limitation [n].' Claim sentences of this kind require same-sentence citation.",
                "- Missing evidence types: " + (", ".join(missing_evidence_types) if missing_evidence_types else "include at least two of method/model evidence, empirical or benchmark evidence, application case, and risk boundary."),
                "- Retrieved source topic anchors: " + (", ".join(source_terms) if source_terms else "use the terms most relevant to the current topic from available sources."),
                "- Anti-hallucination constraint: do not invent authors, years, DOIs, numbers, or nonexistent papers; if evidence is missing, use cautious wording and mark the claim for further verification.",
                "- Citation placement: reserve at least one citation marker for each key paragraph; do not concentrate all citations at paragraph ends.",
                "- If novelty/redundancy also fails, replace repeated background with source-bound mechanisms, metrics, and boundary conditions; do not merely add citations.",
                "- Score-preservation constraint: new evidence should naturally preserve core source/reference terminology to protect ROUGE/BERTScore semantic and lexical alignment.",
                "- Topic anchors: " + (", ".join(evidence_anchor_tokens) if evidence_anchor_tokens else "preserve the original topic keywords."),
            ]
        else:
            defect_evidence_lines = [
                working_outline or original_outline,
                "",
                "【主题锁定】本轮证据修复不得改变原小节主题：" + (original_outline[:220] if original_outline else "保持当前小节主题"),
                "【证据计划 / Evidence Plan】优先修复 evidence_grounding，不得只写抽象观点。",
                "- 主张槽 Claim A：用 1 句话写出本小节最核心、可被证据支持的论点，并绑定至少 2 个主题锚点。",
                "- 证据槽 Evidence Slot 1：从已检索/可用来源中寻找论文、报告、数据集、基准实验或可靠案例；在正文对应句后插入引用位置 [Source]。",
                "- 证据槽 Evidence Slot 2：补充一个机制性证据或工程案例，说明该主张如何发生、在哪些条件下成立。",
                "- 证据槽 Evidence Slot 3：加入一个限制/反例/边界条件，避免把局部证据扩大成普遍结论。",
                "- 推理槽 Reasoning：每个证据后必须写 1 句“证据如何支撑主张”的解释，而不是只堆引用。",
                "- 同句引用约束：凡使用 indicate/suggest/show/support/demonstrate/reveal/说明/表明/显示/支持 等主张动词的句子，必须在同一句包含有效引用标记 [n]；不能只把引用放在上一句、下一句或段末。",
                "- Claim-citation template: 'The benchmark evidence shows the limitation [n].' 这类 claim sentence 必须同句引用。",
                "- 缺失证据类型：" + ("、".join(missing_evidence_types) if missing_evidence_types else "至少包含方法/模型、实证或基准、应用案例、风险边界中的两类"),
                "- 检索来源主题锚点：" + ("、".join(source_terms) if source_terms else "使用当前来源中与主题最相关的术语"),
                "- 防幻觉约束：不得编造作者、年份、DOI、数值或不存在的论文；缺少来源时必须改写为谨慎表述并标注需要进一步验证。",
                "- 引用位置：每个关键段落至少预留 1 个引用标记位置，不把所有引用集中到段末。",
                "- 若同时存在 novelty/redundancy 问题：删除重复背景句，改为来源绑定的新机制、新评价指标、新边界条件；不要只增加引用。",
                "- 保分约束：新增证据必须自然包含当前 source/reference 的核心术语，避免牺牲 ROUGE/BERTScore 的语义与词面对齐。",
                "- 主题锚点：" + ("、".join(evidence_anchor_tokens) if evidence_anchor_tokens else "保持原主题关键词"),
            ]
        defect_evidence_outline = _sanitize_outline_text("\n".join(defect_evidence_lines), strip_repair_blocks=False)
        novelty_anchor_tokens: List[str] = []
        novelty_exclusive_terms: List[str] = []
        novelty_stop = {
            "build", "beyond", "explain", "write", "section", "subsection",
            "content", "introduce", "technical", "current", "prior", "previous",
            "use", "using", "used", "include", "including", "from", "into",
            "that", "this", "with", "without", "their", "these", "those",
        }
        history_lower = history_text.lower()
        for _token in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9_-]{2,}", original_outline):
            normalized_token = _token.lower()
            if normalized_token in novelty_stop:
                continue
            if _token not in novelty_anchor_tokens:
                novelty_anchor_tokens.append(_token)
            if normalized_token not in history_lower and _token not in novelty_exclusive_terms:
                novelty_exclusive_terms.append(_token)
            if len(novelty_anchor_tokens) >= 12 and len(novelty_exclusive_terms) >= 10:
                break
        redundancy_details = ((req.feedback.get("raw_data") or {}).get("redundancy") or {}) if isinstance(req.feedback.get("raw_data"), dict) else {}
        if not isinstance(redundancy_details, dict):
            redundancy_details = {}
        raw_feedback = req.feedback.get("raw_data") if isinstance(req.feedback.get("raw_data"), dict) else {}
        novelty_details = req.feedback.get("novelty_diagnostics") if isinstance(req.feedback.get("novelty_diagnostics"), dict) else {}
        if not novelty_details and isinstance(raw_feedback, dict):
            novelty_details = raw_feedback.get("novelty_diagnostics") if isinstance(raw_feedback.get("novelty_diagnostics"), dict) else {}
        overlap_terms = [
            str(x)
            for x in redundancy_details.get("overlap_terms", [])
            if str(x).strip()
        ][:12]
        overlap_bigrams = [
            str(x)
            for x in redundancy_details.get("overlap_bigrams", [])
            if str(x).strip()
        ][:8]
        novelty_new_terms = [str(x) for x in novelty_details.get("new_terms", []) if str(x).strip()][:10]
        novelty_new_phrases = [str(x) for x in novelty_details.get("new_phrases", []) if str(x).strip()][:8]
        novelty_source_terms = [str(x) for x in novelty_details.get("new_source_terms", []) if str(x).strip()][:8]
        max_history_index = redundancy_details.get("max_history_index")
        if english_repair:
            novelty_diagnostic_line = ", ".join(
                [
                    ("highest-overlap history index=" + str(max_history_index)) if max_history_index is not None else "",
                    ("overlap term count=" + str(len(overlap_terms))) if overlap_terms else "",
                    ("overlap phrase count=" + str(len(overlap_bigrams))) if overlap_bigrams else "",
                    ("new information term count=" + str(len(novelty_new_terms))) if novelty_new_terms else "",
                    ("new phrase count=" + str(len(novelty_new_phrases))) if novelty_new_phrases else "",
                    ("new source term count=" + str(len(novelty_source_terms))) if novelty_source_terms else "",
                ]
            ).strip(", ")
            novelty_lines = [
                working_outline or original_outline,
                "",
                "[Novelty Repair] Prioritize novelty and redundancy without changing the subsection topic.",
                "[Detection Principle] Novelty is not 1 - redundancy; it checks information gain, new terms/phrases, new analytical aspects, and source-specific claims relative to prior text.",
                "[Diagnosis] " + (novelty_diagnostic_line if novelty_diagnostic_line else "No specific repeated terms were provided; still add source-bound information gain."),
                "- Preserve the current subsection mission and valid citation markers; do not restate prior definitions, background, or examples.",
                "- Mandatory subsection-exclusive concepts: " + (", ".join(novelty_exclusive_terms) if novelty_exclusive_terms else "derive at least four concepts absent from the prior text."),
                "- Use exclusive concepts as paragraph-level slots; each slot must add a distinct mechanism, case, evaluation criterion, or boundary condition.",
                "- Replace repeated background at equal length; do not append new material after retaining repeated passages.",
                "- Mention the prior subsection only once in a short transition, then spend the remaining paragraphs on this subsection's unique contribution.",
                "- Prefer source concepts not used previously and form at least two source-bound claims with explicit evidence-to-claim reasoning.",
                "- Preserve-score constraint: keep indispensable topic anchors while changing paragraph openings, mechanisms, examples, and analytical dimensions.",
                "- Topic anchors: " + (", ".join(novelty_anchor_tokens) if novelty_anchor_tokens else "preserve the original subsection terms."),
            ]
        else:
            novelty_diagnostic_line = "、".join(
                [
                    ("最高重叠历史段 index=" + str(max_history_index)) if max_history_index is not None else "",
                    ("检测到重复词数量=" + str(len(overlap_terms))) if overlap_terms else "",
                    ("检测到重复短语数量=" + str(len(overlap_bigrams))) if overlap_bigrams else "",
                    ("已有信息增量词数量=" + str(len(novelty_new_terms))) if novelty_new_terms else "",
                    ("已有新短语数量=" + str(len(novelty_new_phrases))) if novelty_new_phrases else "",
                    ("新增来源术语数量=" + str(len(novelty_source_terms))) if novelty_source_terms else "",
                ]
            ).strip("、")
            novelty_lines = [
                working_outline or original_outline,
                "",
                "【新颖性修复 / Novelty Repair】优先修复 novelty 与 redundancy，不改变原小节主题。",
                "【检测原理】novelty 不再等同于 1 - redundancy；它同时检查相对历史的小节信息增量、新内容词/短语、新分析面向和 source-specific claim。",
                "【诊断】" + (novelty_diagnostic_line if novelty_diagnostic_line else "未提供具体重复词；仍需主动增加来源绑定的信息增量。"),
                "- Preserve the current subsection mission and valid citation markers; do not restate prior definitions, background, or examples.",
                "- Mandatory subsection-exclusive concepts: " + (", ".join(novelty_exclusive_terms) if novelty_exclusive_terms else "derive at least four concepts absent from the prior text"),
                "- Use the exclusive concepts as paragraph-level slots. Each slot must add a distinct mechanism, case, evaluation criterion, or boundary condition.",
                "- Replace repeated background at equal length; do not append new material after retaining the repeated passage.",
                "- Mention the prior subsection only once in a short transition, then spend all remaining paragraphs on the current subsection's unique contribution.",
                "- Prefer source concepts not used previously and form at least two source-bound claims with an explicit evidence-to-claim explanation.",
                "- Preserve-score constraint: keep indispensable topic anchors while changing paragraph openings, mechanisms, examples, and analytical dimensions.",
                "- Topic anchors: " + (", ".join(novelty_anchor_tokens) if novelty_anchor_tokens else "preserve the original subsection terms"),
            ]
        defect_novelty_outline = _sanitize_outline_text("\n".join(novelty_lines), strip_repair_blocks=False)
        baseline_outline = working_outline or original_outline
        topic_anchor_tokens: List[str] = []
        for _token in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9_-]{2,}", baseline_outline or original_outline or ""):
            if _token not in topic_anchor_tokens:
                topic_anchor_tokens.append(_token)
            if len(topic_anchor_tokens) >= 8:
                break
        if english_repair:
            structure_lines = [
                baseline_outline,
                "",
                "[Structure Repair]",
                "- Preserve and strengthen the subsection topic above; do not rewrite it into a generic template.",
                "- The first paragraph must state the subsection's core claim and directly answer the original outline heading.",
                "- Middle paragraphs should follow concept/mechanism -> evidence or case -> analytical reasoning -> limitation or boundary.",
                "- Every paragraph must include at least one keyword, fact, or verifiable argument strongly tied to the original topic.",
                "- End with a subsection-level synthesis only, explaining the information gain relative to neighboring sections.",
                "- Topic anchors: " + (", ".join(topic_anchor_tokens) if topic_anchor_tokens else "preserve the original topic keywords."),
            ]
        else:
            structure_lines = [
                baseline_outline,
                "",
                "【结构化修复约束】",
                "- 保留并强化上方原小节主题，不得改写为通用模板。",
                "- 第一段给出本小节的核心论点，并直接回应原大纲标题。",
                "- 中间段按“概念/机制 -> 证据或案例 -> 分析推理 -> 局限或边界”展开。",
                "- 每个段落至少包含一个与原主题强相关的关键词、事实或可验证论据。",
                "- 结尾只做本小节范围内的小结，并说明与前后小节的信息增量。",
                "- 主题锚点：" + ("、".join(topic_anchor_tokens) if topic_anchor_tokens else "保持原主题关键词"),
            ]
        defect_structure_outline = _sanitize_outline_text("\n".join(structure_lines), strip_repair_blocks=False)
        baseline_score = _score_outline_candidate(
            candidate_outline=baseline_outline,
            original_outline=original_outline,
            working_outline=baseline_outline,
            failed_draft=req.failed_draft,
            history=req.history,
            rel_score=rel_score,
            red_score=red_score,
            rel_threshold=rel_threshold,
            red_threshold=red_threshold,
            feedback=req.feedback,
            defect_graph=defect_graph,
        )

        candidates: List[Dict[str, Any]] = []
        if llm_outline:
            candidates.append({"source": "llm", "outline": llm_outline})
        if rule_outline:
            candidates.append({"source": "rule", "outline": rule_outline})
        if structured_rule_outline:
            candidates.append({"source": "rule_structured", "outline": structured_rule_outline})
        if defect_topic_outline:
            candidates.append({"source": "defect_topic", "outline": defect_topic_outline})
        if defect_evidence_outline:
            candidates.append({"source": "defect_evidence", "outline": defect_evidence_outline})
        if defect_novelty_outline:
            candidates.append({"source": "defect_novelty", "outline": defect_novelty_outline})
        if defect_structure_outline:
            candidates.append({"source": "defect_structure", "outline": defect_structure_outline})
            candidates.append({"source": "structure_readability_repair", "outline": defect_structure_outline})
        if defect_novelty_outline:
            candidates.append({"source": "novelty_repair", "outline": defect_novelty_outline})
        if defect_evidence_outline:
            claim_evidence_outline = defect_evidence_outline + (
                "\n[Claim-Evidence Repair] Bind every major claim to source-grounded evidence and add one reasoning sentence after each evidence cue."
                if english_repair
                else "\n【Claim-Evidence Repair】每个主要 claim 必须绑定 source-grounded evidence，并在证据后补一句 reasoning。"
            )
            citation_grounding_outline = defect_evidence_outline + (
                "\n[Citation Grounding Repair] Every citation marker must support the same-sentence claim; do not invent sources, authors, years, URLs, or DOIs."
                if english_repair
                else "\n【Citation Grounding Repair】每个引用标记必须支撑同句 claim；不得编造来源、作者、年份、URL 或 DOI。"
            )
            external_metric_outline = defect_evidence_outline + (
                "\n[External Metric Repair] Preserve source/reference terminology naturally while improving claim specificity; no mechanical keyword stuffing."
                if english_repair
                else "\n【External Metric Repair】自然保留 source/reference 关键术语并提高 claim 具体性；禁止机械堆词。"
            )
            reproducibility_outline = defect_evidence_outline + (
                "\n[Reproducibility Repair] Add supported dataset, protocol, parameter, implementation, audit-trail, or replication constraints; unsupported details must be framed as requirements."
                if english_repair
                else "\n【Reproducibility Repair】补充有来源支持的 dataset/protocol/parameter/implementation/audit-trail/replication 约束；无来源细节只能写成要求。"
            )
            reviewer_risk_outline = defect_evidence_outline + (
                "\n[Reviewer-Risk Repair] Address likely reviewer concerns: unsupported claim, weak novelty, missing limitation, missing evaluation, and reproducibility risk."
                if english_repair
                else "\n【Reviewer-Risk Repair】回应 reviewer 可能质疑：无支撑 claim、新颖性弱、缺少 limitation、缺少 evaluation、可复现风险。"
            )
            candidates.extend([
                {"source": "claim_evidence_repair", "outline": claim_evidence_outline},
                {"source": "citation_grounding_repair", "outline": citation_grounding_outline},
                {"source": "external_metric_repair", "outline": external_metric_outline},
                {"source": "reproducibility_repair", "outline": reproducibility_outline},
                {"source": "reviewer_risk_repair", "outline": reviewer_risk_outline},
            ])

        seen = set()
        dedup_candidates: List[Dict[str, Any]] = []
        for candidate in candidates:
            outline_text = candidate["outline"].strip()
            if not outline_text:
                continue
            key = outline_text[:2000]
            if key in seen:
                continue
            seen.add(key)
            score_detail = _score_outline_candidate(
                candidate_outline=outline_text,
                original_outline=original_outline,
                working_outline=baseline_outline,
                failed_draft=req.failed_draft,
                history=req.history,
                rel_score=rel_score,
                red_score=red_score,
                rel_threshold=rel_threshold,
                red_threshold=red_threshold,
                feedback=req.feedback,
                defect_graph=defect_graph,
            )
            dedup_candidates.append(
                {
                    "source": candidate["source"],
                    "outline": outline_text,
                    "score": score_detail,
                }
            )

        min_gain = float(os.getenv("CONTROLLER_MIN_SCORE_GAIN", "0.001"))
        min_rel_anchor_gain = float(os.getenv("CONTROLLER_MIN_REL_ANCHOR_GAIN", "0.005"))
        min_novelty_gain = float(os.getenv("CONTROLLER_MIN_NOVELTY_GAIN", "0.005"))
        min_structure_gain = float(os.getenv("CONTROLLER_MIN_STRUCTURE_GAIN", "0.05"))
        min_coherence_gain = float(os.getenv("CONTROLLER_MIN_COHERENCE_GAIN", "0.03"))
        min_evidence_gain = float(os.getenv("CONTROLLER_MIN_EVIDENCE_GAIN", "0.05"))

        bandit_enabled = (
            bool(req.controller_bandit_enabled)
            if req.controller_bandit_enabled is not None
            else os.getenv("CONTROLLER_BANDIT_ENABLED", "true").lower() == "true"
        )
        feature_vector = _build_bandit_context_features(
            rel_score=rel_score,
            red_score=red_score,
            rel_threshold=rel_threshold,
            red_threshold=red_threshold,
            iteration=iteration,
            history=req.history,
            feedback=req.feedback,
            defect_graph=defect_graph,
            section_id=req.section_id,
            subsection_id=req.subsection_id,
        )
        chosen = None
        chosen_source = "baseline"
        bandit_debug: Dict[str, Any] = {
            "enabled": bandit_enabled,
            "feature_vector": [round(v, 4) for v in feature_vector],
            "defect_graph": defect_graph,
            "selection": {},
            "state_path": _bandit_state_path(),
        }
        fixed_arm = (
            str(req.controller_fixed_arm or "").strip()
            if req.controller_fixed_arm is not None
            else os.getenv("CONTROLLER_FIXED_ARM", "").strip()
        )

        if dedup_candidates:
            best_candidate_by_source: Dict[str, Dict[str, Any]] = {}
            for cand in dedup_candidates:
                src = cand["source"]
                if src not in best_candidate_by_source or cand["score"]["total"] > best_candidate_by_source[src]["score"]["total"]:
                    best_candidate_by_source[src] = cand
            best_candidate_by_source, exclusion_debug = _apply_request_local_arm_exclusions(
                best_candidate_by_source,
                req.controller_excluded_arms,
            )
            bandit_debug.update(exclusion_debug)
            failed_dims_for_compatibility = (
                req.feedback.get("quality_dimensions_failed")
                if isinstance(req.feedback.get("quality_dimensions_failed"), list)
                else []
            )
            best_candidate_by_source, compatibility_debug = _filter_defect_compatible_candidates(
                best_candidate_by_source,
                defect_graph,
                failed_dims_for_compatibility,
            )
            bandit_debug.update(compatibility_debug)
            if bandit_enabled:
                available_arms = [
                    arm
                    for arm in [
                        "llm", "rule", "rule_structured", "defect_topic", "defect_evidence",
                        "defect_novelty", "defect_structure", "novelty_repair",
                        "claim_evidence_repair", "citation_grounding_repair",
                        "reviewer_risk_repair", "external_metric_repair",
                        "structure_readability_repair", "reproducibility_repair",
                    ]
                    if arm in best_candidate_by_source
                ]
                if available_arms:
                    with BANDIT_LOCK:
                        state = _load_bandit_state(len(feature_vector))
                        selected_arm, selection_debug = _bandit_choose_arm(
                            state=state,
                            available_arms=available_arms,
                            features=feature_vector,
                            defect_graph=defect_graph,
                        )
                    chosen = best_candidate_by_source[selected_arm]
                    chosen_source = selected_arm
                    bandit_debug["selection"] = selection_debug
                    bandit_debug["selected_arm"] = selected_arm
                else:
                    chosen = max(dedup_candidates, key=lambda x: x["score"]["total"])
                    chosen_source = chosen["source"]
            else:
                allow_no_bandit_llm = (
                    bool(req.controller_no_bandit_allow_llm)
                    if req.controller_no_bandit_allow_llm is not None
                    else os.getenv("CONTROLLER_NO_BANDIT_ALLOW_LLM", "false").lower() == "true"
                )
                if not allow_no_bandit_llm and "llm" in best_candidate_by_source:
                    best_candidate_by_source = {
                        arm: cand for arm, cand in best_candidate_by_source.items() if arm != "llm"
                    }
                if fixed_arm and fixed_arm in best_candidate_by_source:
                    chosen = best_candidate_by_source[fixed_arm]
                    chosen_source = fixed_arm
                    bandit_debug["selection"] = {"mode": "fixed_arm_no_bandit", "fixed_arm": fixed_arm}
                else:
                    candidate_pool = list(best_candidate_by_source.values()) or dedup_candidates
                    chosen = max(candidate_pool, key=lambda x: x["score"]["total"])
                    chosen_source = chosen["source"]
                    bandit_debug["selection"] = {"mode": "best_score_no_bandit"}

            # Risk-sensitive override: when evidence grounding is the dominant
            # failure mode, do not let historical bandit inertia pick a weaker
            # generic structure repair over a substantially better evidence plan.
            failed_dims_for_override = req.feedback.get("quality_dimensions_failed") if isinstance(req.feedback.get("quality_dimensions_failed"), list) else []
            evidence_override_margin = float(os.getenv("CONTROLLER_EVIDENCE_OVERRIDE_MARGIN", "0.08"))
            evidence_candidate = best_candidate_by_source.get("defect_evidence")
            chosen_total = float((chosen or {}).get("score", {}).get("total", 0.0)) if chosen else 0.0
            evidence_usage_for_override = float(evidence_diag.get("source_usage_coverage", 0.0) or 0.0) if isinstance(evidence_diag, dict) else 0.0
            claim_alignment_for_override = float(evidence_diag.get("claim_evidence_alignment", 0.0) or 0.0) if isinstance(evidence_diag, dict) else 0.0
            source_failures_for_override = evidence_diag.get("source_failures") if isinstance(evidence_diag, dict) else []
            if not isinstance(source_failures_for_override, list):
                source_failures_for_override = []
            severe_source_failures_for_override = {
                str(item)
                for item in source_failures_for_override
                if str(item) not in {"insufficient_citations", "missing_evidence_type:application_or_case"}
            }
            soft_citation_only_for_override = bool(source_failures_for_override) and not severe_source_failures_for_override
            novelty_primary_with_soft_citation = bool(
                "novelty" in failed_dims_for_override
                and "evidence_grounding" not in failed_dims_for_override
                and soft_citation_only_for_override
                and claim_alignment_for_override >= 0.62
            )
            if (
                bandit_enabled
                and
                evidence_candidate
                and not novelty_primary_with_soft_citation
                and not _coherence_repair_blocks_evidence_override(
                    chosen_source=chosen_source,
                    failed_dims=failed_dims_for_override,
                    defect_graph=defect_graph,
                    evidence_diag=evidence_diag if isinstance(evidence_diag, dict) else {},
                )
                and (
                    "evidence_grounding" in failed_dims_for_override
                    or float(defect_graph.get("evidence", 0.0)) >= 0.55
                    or evidence_usage_for_override < 0.92
                    or claim_alignment_for_override < 0.62
                )
                and (
                    float(defect_graph.get("evidence", 0.0)) >= float(defect_graph.get("topic", 0.0)) + 0.05
                    or rel_score >= rel_threshold * 0.85
                )
                and float(evidence_candidate["score"].get("total", 0.0)) >= chosen_total + evidence_override_margin
            ):
                chosen = evidence_candidate
                chosen_source = "defect_evidence"
                bandit_debug["override"] = {
                    "reason": "dominant_evidence_defect",
                    "previous_arm": bandit_debug.get("selected_arm", ""),
                    "previous_total": round(chosen_total, 4),
                    "override_total": evidence_candidate["score"].get("total", 0.0),
                    "margin": evidence_override_margin,
                }
                bandit_debug["selected_arm"] = "defect_evidence"

            topic_candidate = best_candidate_by_source.get("defect_topic")
            topic_override_margin = float(os.getenv("CONTROLLER_TOPIC_OVERRIDE_MARGIN", "0.10"))
            chosen_total = float((chosen or {}).get("score", {}).get("total", 0.0)) if chosen else 0.0
            topic_dominant = bool(
                topic_candidate
                and (
                    "topic_alignment" in failed_dims_for_override
                    or rel_score < rel_threshold * 0.82
                    or float(defect_graph.get("topic", 0.0)) >= float(defect_graph.get("evidence", 0.0)) + 0.10
                )
            )
            force_topic_recovery = bool(
                topic_candidate
                and (
                    rel_score < rel_threshold * 0.70
                    or "topic_alignment" in failed_dims_for_override
                    or float(defect_graph.get("topic", 0.0)) >= float(defect_graph.get("evidence", 0.0)) + 0.05
                )
            )
            if (
                bandit_enabled
                and
                topic_candidate
                and topic_dominant
                and (
                    force_topic_recovery
                    or float(topic_candidate["score"].get("total", 0.0)) >= chosen_total - topic_override_margin
                )
            ):
                chosen = topic_candidate
                chosen_source = "defect_topic"
                bandit_debug["override"] = {
                    "reason": "dominant_topic_defect",
                    "previous_arm": bandit_debug.get("selected_arm", ""),
                    "previous_total": round(chosen_total, 4),
                    "override_total": topic_candidate["score"].get("total", 0.0),
                    "margin": topic_override_margin,
                }
                bandit_debug["selected_arm"] = "defect_topic"

            novelty_candidate = best_candidate_by_source.get("defect_novelty")
            novelty_override_margin = float(os.getenv("CONTROLLER_NOVELTY_OVERRIDE_MARGIN", "0.10"))
            chosen_total = float((chosen or {}).get("score", {}).get("total", 0.0)) if chosen else 0.0
            novelty_dominant = bool(
                novelty_candidate
                and (
                    "novelty" in failed_dims_for_override
                    or red_score > red_threshold
                    or float(defect_graph.get("novelty", 0.0)) >= 0.02
                    or float(defect_graph.get("redundancy", 0.0)) >= 0.02
                )
                and "evidence_grounding" not in failed_dims_for_override
                and float(defect_graph.get("evidence", 0.0)) < 0.25
                and rel_score >= rel_threshold * 0.85
            )
            if (
                bandit_enabled
                and
                novelty_candidate
                and novelty_dominant
                and float(novelty_candidate["score"].get("total", 0.0)) >= chosen_total - novelty_override_margin
            ):
                current_novelty_score = float((chosen or {}).get("score", {}).get("novelty", 0.0)) if chosen else 0.0
                evidence_active_for_novelty = _evidence_active_for_novelty_repair(
                    evidence_diag=evidence_diag if isinstance(evidence_diag, dict) else {},
                    failed_dims=failed_dims_for_override,
                    defect_graph=defect_graph,
                )
                # A strong evidence score means the novelty repair should preserve
                # the existing evidence plan, not switch to an evidence-repair arm.
                evidence_guard_active = evidence_active_for_novelty
                evidence_preserving_novelty = _select_evidence_preserving_novelty_candidate(
                    chosen=chosen,
                    evidence_candidate=evidence_candidate,
                    evidence_guard_active=evidence_guard_active,
                )
                if evidence_preserving_novelty:
                    chosen = evidence_preserving_novelty
                    chosen_source = str(evidence_preserving_novelty.get("source", "defect_evidence"))
                    bandit_debug["override"] = {
                        "reason": "evidence_preserving_novelty_repair",
                        "previous_arm": bandit_debug.get("selected_arm", ""),
                        "previous_total": round(chosen_total, 4),
                        "override_total": evidence_preserving_novelty["score"].get("total", 0.0),
                        "override_novelty": evidence_preserving_novelty["score"].get("novelty", 0.0),
                        "override_evidence": evidence_preserving_novelty["score"].get("evidence_signal", 0.0),
                        "evidence_active_for_novelty": evidence_active_for_novelty,
                        "evidence_guard_active": evidence_guard_active,
                        "margin": novelty_override_margin,
                    }
                    bandit_debug["selected_arm"] = chosen_source
                    continue_novelty_safe_selection = False
                else:
                    continue_novelty_safe_selection = True
                current_evidence_score = float((chosen or {}).get("score", {}).get("evidence_signal", 0.0)) if chosen else 0.0
                novelty_safe_candidates = [
                    cand
                    for cand in best_candidate_by_source.values()
                    if (
                        float(cand.get("score", {}).get("relevance_anchor", 0.0)) >= 0.95
                        and float(cand.get("score", {}).get("novelty", 0.0)) >= current_novelty_score + 0.006
                        and (
                            not evidence_active_for_novelty
                            or float(cand.get("score", {}).get("evidence_signal", 0.0)) >= current_evidence_score - 0.05
                            or float(cand.get("score", {}).get("evidence_signal", 0.0)) >= 0.75
                        )
                        and (
                            evidence_active_for_novelty
                            or str(cand.get("source", "")) != "defect_evidence"
                            or float(cand.get("score", {}).get("novelty", 0.0)) >= current_novelty_score + 0.018
                        )
                    )
                ]
                if continue_novelty_safe_selection and novelty_safe_candidates:
                    def _novelty_safe_score(cand: Dict[str, Any]) -> float:
                        score = cand.get("score", {}) if isinstance(cand.get("score"), dict) else {}
                        evidence_part = (
                            0.18 * float(score.get("evidence_signal", 0.0))
                            if evidence_active_for_novelty
                            else 0.0
                        )
                        return (
                            0.57 * float(score.get("novelty", 0.0))
                            + 0.25 * float(score.get("total", 0.0))
                            + 0.10 * float(score.get("coherence_signal", 0.0))
                            + evidence_part
                        )

                    novelty_choice = max(novelty_safe_candidates, key=_novelty_safe_score)
                elif continue_novelty_safe_selection:
                    novelty_choice = novelty_candidate
                if continue_novelty_safe_selection:
                    chosen = novelty_choice
                    chosen_source = str(novelty_choice.get("source", "defect_novelty"))
                    bandit_debug["override"] = {
                        "reason": "dominant_novelty_defect_safe_candidate",
                        "previous_arm": bandit_debug.get("selected_arm", ""),
                        "previous_total": round(chosen_total, 4),
                        "override_total": novelty_choice["score"].get("total", 0.0),
                        "override_novelty": novelty_choice["score"].get("novelty", 0.0),
                        "evidence_active_for_novelty": evidence_active_for_novelty,
                        "margin": novelty_override_margin,
                    }
                    bandit_debug["selected_arm"] = chosen_source

            # Final guard: overrides can still keep selecting an arm that has
            # just failed repeatedly. Shift to the best viable alternative so
            # controller repairs do not spiral into the same low-yield outline.
            if bandit_enabled and chosen and best_candidate_by_source:
                try:
                    with BANDIT_LOCK:
                        cooldown_state = _load_bandit_state(len(feature_vector))
                    cooldown_streak = max(1, int(os.getenv("CONTROLLER_ARM_COOLDOWN_STREAK", "2")))
                    min_alt_ratio = max(0.0, min(1.0, float(os.getenv("CONTROLLER_COOLDOWN_MIN_ALT_SCORE_RATIO", "0.90"))))
                    arm_state = (cooldown_state.get("arms") or {}).get(chosen_source, {})
                    ineffective_streak = int(arm_state.get("ineffective_streak", 0) or 0) if isinstance(arm_state, dict) else 0
                    if ineffective_streak >= cooldown_streak and len(best_candidate_by_source) > 1:
                        current_total = max(1e-6, float((chosen or {}).get("score", {}).get("total", 0.0)))
                        alternatives = sorted(
                            [
                                cand for arm, cand in best_candidate_by_source.items()
                                if arm != chosen_source
                            ],
                            key=lambda cand: float(cand.get("score", {}).get("total", 0.0)),
                            reverse=True,
                        )
                        if novelty_dominant and chosen_source == "defect_novelty":
                            current_novelty = float((chosen or {}).get("score", {}).get("novelty", 0.0))
                            alternatives = [
                                alt
                                for alt in alternatives
                                if (
                                    float(alt.get("score", {}).get("novelty", 0.0)) >= current_novelty - 0.001
                                    and (
                                        str(alt.get("source", "")) != "defect_evidence"
                                        or float(defect_graph.get("evidence", 0.0)) >= 0.25
                                    )
                                )
                            ]
                        for alt in alternatives:
                            alt_source = str(alt.get("source", ""))
                            alt_state = (cooldown_state.get("arms") or {}).get(alt_source, {})
                            alt_streak = int(alt_state.get("ineffective_streak", 0) or 0) if isinstance(alt_state, dict) else 0
                            if alt_streak < cooldown_streak and float(alt.get("score", {}).get("total", 0.0)) >= current_total * min_alt_ratio:
                                bandit_debug["cooldown_override"] = {
                                    "reason": "selected_arm_ineffective_streak",
                                    "previous_arm": chosen_source,
                                    "previous_ineffective_streak": ineffective_streak,
                                    "new_arm": alt_source,
                                    "new_total": alt.get("score", {}).get("total", 0.0),
                                }
                                chosen = alt
                                chosen_source = alt_source
                                bandit_debug["selected_arm"] = alt_source
                                bandit_debug.setdefault("selection", {})["mode"] = "cooldown_shift"
                                break
                except Exception as _cooldown_error:
                    bandit_debug["cooldown_error"] = str(_cooldown_error)[:180]

            if bandit_enabled and chosen and best_candidate_by_source:
                aligned_choice, alignment_override = _select_defect_aligned_candidate(
                    chosen=chosen,
                    best_candidate_by_source=best_candidate_by_source,
                    defect_graph=defect_graph,
                    failed_dims=failed_dims_for_override,
                )
                if alignment_override:
                    chosen = aligned_choice
                    chosen_source = str(aligned_choice.get("source", chosen_source))
                    bandit_debug["defect_alignment_override"] = alignment_override
                    bandit_debug["selected_arm"] = chosen_source
                    bandit_debug.setdefault("selection", {})["mode"] = "defect_alignment_guard"

            # No-harm guard before generation: if the failed draft already has
            # acceptable evidence/claim alignment, avoid a novelty or cooldown
            # choice whose outline loses the explicit evidence plan. This keeps
            # Controller repairs targeted instead of trading one dimension for
            # a regression in another.
            if bandit_enabled and chosen and best_candidate_by_source:
                chosen_score = chosen.get("score", {}) if isinstance(chosen.get("score"), dict) else {}
                chosen_evidence_signal = float(chosen_score.get("evidence_signal", 0.0) or 0.0)
                chosen_total = float(chosen_score.get("total", 0.0) or 0.0)
                preserve_claim_alignment = bool(
                    claim_alignment_for_override >= float(os.getenv("CONTROLLER_PRESERVE_CLAIM_ALIGNMENT_MIN", "0.80"))
                    and evidence_usage_for_override >= float(os.getenv("CONTROLLER_PRESERVE_SOURCE_USAGE_MIN", "0.90"))
                )
                evidence_defect_active = bool(
                    "evidence_grounding" in failed_dims_for_override
                    or float(defect_graph.get("evidence", 0.0) or 0.0) >= 0.25
                )
                evidence_preserving_margin = float(os.getenv("CONTROLLER_EVIDENCE_PRESERVE_MARGIN", "0.025"))
                evidence_preserving_candidates = [
                    cand
                    for cand in best_candidate_by_source.values()
                    if (
                        float(cand.get("score", {}).get("evidence_signal", 0.0) or 0.0)
                        >= max(0.75, chosen_evidence_signal + 0.20)
                        and float(cand.get("score", {}).get("relevance_anchor", 0.0) or 0.0) >= 0.92
                        and float(cand.get("score", {}).get("total", 0.0) or 0.0)
                        >= chosen_total - evidence_preserving_margin
                        and (
                            not novelty_dominant
                            or float(cand.get("score", {}).get("novelty", 0.0) or 0.0)
                            >= float(chosen_score.get("novelty", 0.0) or 0.0) - 0.012
                        )
                    )
                ]
                if evidence_defect_active and preserve_claim_alignment and evidence_preserving_candidates:
                    preserved = max(
                        evidence_preserving_candidates,
                        key=lambda cand: (
                            float(cand.get("score", {}).get("total", 0.0) or 0.0),
                            float(cand.get("score", {}).get("evidence_signal", 0.0) or 0.0),
                            float(cand.get("score", {}).get("novelty", 0.0) or 0.0),
                        ),
                    )
                    preserved_source = str(preserved.get("source", ""))
                    if preserved_source and preserved_source != chosen_source:
                        bandit_debug["evidence_preserving_override"] = {
                            "reason": "preserve_claim_evidence_alignment",
                            "previous_arm": chosen_source,
                            "previous_total": round(chosen_total, 4),
                            "previous_evidence_signal": round(chosen_evidence_signal, 4),
                            "new_arm": preserved_source,
                            "new_total": preserved.get("score", {}).get("total", 0.0),
                            "new_evidence_signal": preserved.get("score", {}).get("evidence_signal", 0.0),
                            "margin": evidence_preserving_margin,
                        }
                        chosen = preserved
                        chosen_source = preserved_source
                        bandit_debug["selected_arm"] = preserved_source
                        bandit_debug.setdefault("selection", {})["mode"] = "evidence_preserving_guard"

            hard_gate = (bandit_debug.get("selection") or {}).get("hard_gate")
            hard_gate_arm = str((hard_gate or {}).get("selected") or "").strip() if isinstance(hard_gate, dict) else ""
            source_audit_evidence_lock = False
            source_audit_active = bool(
                req.feedback.get("source_alignment_low_pass_audit")
                or "source_alignment_low_pass" in str(req.feedback.get("feedback", "")).lower()
            )
            if (
                bandit_enabled
                and source_audit_active
                and "topic_alignment" not in failed_dims_for_override
                and best_candidate_by_source.get("defect_evidence")
            ):
                chosen = best_candidate_by_source["defect_evidence"]
                chosen_source = "defect_evidence"
                source_audit_evidence_lock = True
                bandit_debug["source_audit_arm_lock"] = {
                    "reason": "source_alignment_audit_requires_evidence_repair",
                    "previous_arm": bandit_debug.get("selected_arm", ""),
                    "locked_arm": "defect_evidence",
                }
                bandit_debug["selected_arm"] = "defect_evidence"
                bandit_debug.setdefault("selection", {})["mode"] = "source_alignment_evidence_lock"
            topic_complement_choice = None
            topic_complement_reason: Dict[str, Any] = {}
            source_anchored_topic_choice = None
            source_anchored_topic_reason: Dict[str, Any] = {}
            if bandit_enabled and hard_gate_arm == "defect_topic" and hard_gate_arm in best_candidate_by_source:
                source_anchored_topic_choice, source_anchored_topic_reason = _select_source_anchored_topic_repair_candidate(
                    hard_gate_arm=hard_gate_arm,
                    chosen=best_candidate_by_source[hard_gate_arm],
                    best_candidate_by_source=best_candidate_by_source,
                    feedback=req.feedback if isinstance(req.feedback, dict) else {},
                    rel_score=rel_score,
                    rel_threshold=rel_threshold,
                )
                if source_anchored_topic_choice:
                    chosen = source_anchored_topic_choice
                    chosen_source = str(source_anchored_topic_choice.get("source", chosen_source))
                    bandit_debug["hard_gate_softened"] = source_anchored_topic_reason
                    bandit_debug["selected_arm"] = chosen_source
                    bandit_debug.setdefault("selection", {})["mode"] = "hard_gate_source_anchored_topic_repair"
                topic_complement_choice, topic_complement_reason = _select_stalled_topic_complement_candidate(
                    hard_gate_arm=hard_gate_arm,
                    chosen=best_candidate_by_source[hard_gate_arm],
                    best_candidate_by_source=best_candidate_by_source,
                    feedback=req.feedback if isinstance(req.feedback, dict) else {},
                    rel_score=rel_score,
                    rel_threshold=rel_threshold,
                    iteration=iteration,
                )
                if topic_complement_choice and not source_anchored_topic_choice:
                    chosen = topic_complement_choice
                    chosen_source = str(topic_complement_choice.get("source", chosen_source))
                    bandit_debug["hard_gate_softened"] = topic_complement_reason
                    bandit_debug["selected_arm"] = chosen_source
                    bandit_debug.setdefault("selection", {})["mode"] = "hard_gate_topic_complement"
            if bandit_enabled and hard_gate_arm and hard_gate_arm in best_candidate_by_source:
                no_harm_evidence_choice, no_harm_evidence_reason = _select_topic_hard_gate_no_harm_evidence_candidate(
                    hard_gate_arm=hard_gate_arm,
                    topic_candidate=best_candidate_by_source.get(hard_gate_arm),
                    evidence_candidate=best_candidate_by_source.get("defect_evidence"),
                    defect_graph=defect_graph,
                    feedback=req.feedback if isinstance(req.feedback, dict) else {},
                )
                if no_harm_evidence_choice:
                    chosen = no_harm_evidence_choice
                    chosen_source = str(no_harm_evidence_choice.get("source", chosen_source))
                    bandit_debug["hard_gate_softened"] = no_harm_evidence_reason
                    bandit_debug["selected_arm"] = chosen_source
                    bandit_debug.setdefault("selection", {})["mode"] = "hard_gate_no_harm_evidence"
                aligned_hard_gate_softened = _should_soften_hard_gate_for_aligned_candidate(
                    hard_gate_arm=hard_gate_arm,
                    chosen_source=chosen_source,
                    chosen=chosen or {},
                    defect_graph=defect_graph,
                    alignment_override=bandit_debug.get("defect_alignment_override", {}),
                )
                if aligned_hard_gate_softened:
                    bandit_debug["hard_gate_softened"] = {
                        "reason": "multi_defect_pareto_hard_gate_soften",
                        "hard_gate_arm": hard_gate_arm,
                        "selected_arm": chosen_source,
                        "alignment_override": bandit_debug.get("defect_alignment_override", {}),
                    }
                    bandit_debug.setdefault("selection", {})["mode"] = "hard_gate_multi_defect_pareto"
                if source_audit_evidence_lock or source_anchored_topic_choice or topic_complement_choice or no_harm_evidence_choice or aligned_hard_gate_softened:
                    pass
                elif chosen_source != hard_gate_arm:
                    bandit_debug["hard_gate_lock"] = {
                        "reason": "preserve_hard_verifier_gate_arm",
                        "previous_arm": chosen_source,
                        "locked_arm": hard_gate_arm,
                    }
                if not source_audit_evidence_lock and not source_anchored_topic_choice and not topic_complement_choice and not no_harm_evidence_choice and not aligned_hard_gate_softened:
                    chosen = best_candidate_by_source[hard_gate_arm]
                    chosen_source = hard_gate_arm
                    bandit_debug["selected_arm"] = hard_gate_arm
                    bandit_debug.setdefault("selection", {})["mode"] = "hard_defect_gate"

            if (not bandit_enabled) and fixed_arm and fixed_arm in best_candidate_by_source:
                if chosen_source != fixed_arm:
                    bandit_debug["fixed_arm_override"] = {
                        "reason": "no_bandit_fixed_strategy",
                        "previous_arm": chosen_source,
                        "fixed_arm": fixed_arm,
                    }
                chosen = best_candidate_by_source[fixed_arm]
                chosen_source = fixed_arm
                bandit_debug["selected_arm"] = fixed_arm

            debug_selected_arm = str(bandit_debug.get("selected_arm") or "").strip()
            if debug_selected_arm and debug_selected_arm in best_candidate_by_source:
                actual_source = str((chosen or {}).get("source") or "").strip()
                if actual_source != debug_selected_arm:
                    bandit_debug["selection_consistency_fix"] = {
                        "previous_candidate_source": actual_source,
                        "debug_selected_arm": debug_selected_arm,
                    }
                    chosen = best_candidate_by_source[debug_selected_arm]
                    chosen_source = debug_selected_arm

        changed = False
        score_gain = (chosen["score"]["total"] - baseline_score["total"]) if chosen else 0.0
        rel_anchor_gain = (chosen["score"].get("relevance_anchor", 0.0) - baseline_score.get("relevance_anchor", 0.0)) if chosen else 0.0
        novelty_gain = (chosen["score"].get("novelty", 0.0) - baseline_score.get("novelty", 0.0)) if chosen else 0.0
        structure_gain = (chosen["score"].get("structure", 0.0) - baseline_score.get("structure", 0.0)) if chosen else 0.0
        coherence_gain = (chosen["score"].get("coherence_signal", 0.0) - baseline_score.get("coherence_signal", 0.0)) if chosen else 0.0
        evidence_gain = (chosen["score"].get("evidence_signal", 0.0) - baseline_score.get("evidence_signal", 0.0)) if chosen else 0.0

        rel_needed = rel_score < rel_threshold
        failed_dims_for_accept = req.feedback.get("quality_dimensions_failed") if isinstance(req.feedback.get("quality_dimensions_failed"), list) else []
        red_needed = bool(
            red_score > red_threshold
            or "novelty" in failed_dims_for_accept
            or float(defect_graph.get("novelty", 0.0)) >= 0.02
        )
        coherence_needed = bool(defect_graph.get("coherence", 0.0) >= 0.25 or "logical_coherence" in (req.feedback.get("quality_dimensions_failed") if isinstance(req.feedback.get("quality_dimensions_failed"), list) else []))
        evidence_needed = bool(defect_graph.get("evidence", 0.0) >= 0.25 or "evidence_grounding" in (req.feedback.get("quality_dimensions_failed") if isinstance(req.feedback.get("quality_dimensions_failed"), list) else []))
        rel_gain_ok = (
            (not rel_needed)
            or (rel_anchor_gain >= min_rel_anchor_gain)
            or (structure_gain >= min_structure_gain)
            or (
                chosen_source == "defect_topic"
                and float(defect_graph.get("hard_relevance_gate", 0.0) or 0.0) > 0.0
            )
        )
        novelty_gain_ok = (
            (not red_needed)
            or (novelty_gain >= min_novelty_gain)
            or (
                chosen_source == "defect_novelty"
                and float(defect_graph.get("hard_redundancy_gate", 0.0) or 0.0) > 0.0
            )
        )
        coherence_gain_ok = (not coherence_needed) or (coherence_gain >= min_coherence_gain) or (chosen_source in ("rule_structured", "defect_structure"))
        evidence_gain_ok = (not evidence_needed) or (evidence_gain >= min_evidence_gain) or (chosen_source == "defect_evidence" and chosen and chosen["score"].get("evidence_signal", 0.0) >= 0.75)

        candidate_changed = bool(chosen and _normalize_outline_for_compare(chosen["outline"]) != _normalize_outline_for_compare(baseline_outline))
        if chosen and candidate_changed:
            improved_outline = chosen["outline"]
            chosen_source = chosen["source"]
            changed = True
        else:
            improved_outline = baseline_outline
        if chosen_source and not bandit_debug.get("selected_arm"):
            bandit_debug["selected_arm"] = chosen_source

        # 线上稳定性优先：只要输出确实发生变化，且不是明显退化，就允许进入下一轮。
        # 这样避免 Controller 因阈值过严而反复返回原纲，导致 Generator/Controller 死循环。
        effective = bool(
            chosen
            and changed
            and (
                (
                    score_gain >= min_gain
                    and rel_gain_ok
                    and novelty_gain_ok
                    and coherence_gain_ok
                    and evidence_gain_ok
                )
                or (chosen_source == "rule_structured" and rel_gain_ok and coherence_gain_ok)
            )
        )
        if require_llm_source and chosen_source != "llm":
            effective = False

        if bandit_enabled and chosen:
            selection_scores = (bandit_debug.get("selection") or {}).get("scores", {})
            predicted_exploit = float((selection_scores.get(chosen_source) or {}).get("exploit", 0.0))
            reward_quality = _controller_proposal_reward_quality(
                chosen_source=chosen_source,
                score_gain=score_gain,
                rel_anchor_gain=rel_anchor_gain,
                novelty_gain=novelty_gain,
                structure_gain=structure_gain,
                evidence_gain=evidence_gain,
                defect_graph=defect_graph,
            )

            observed_cost, observed_latency = _estimate_arm_cost_latency(
                source=chosen_source,
                prompt_len=len(improvement_prompt),
                output_len=len(chosen.get("outline", "")),
                llm_elapsed=llm_elapsed_sec,
            )

            target_latency = max(0.05, float(os.getenv("CONTROLLER_CONSTRAINT_TARGET_LATENCY", "2.0")))
            target_cost = max(1.0, float(os.getenv("CONTROLLER_CONSTRAINT_TARGET_COST", "600")))
            latency_over = max(0.0, (observed_latency - target_latency) / target_latency)
            cost_over = max(0.0, (observed_cost - target_cost) / target_cost)
            penalty = _clip01(0.5 * latency_over + 0.5 * cost_over)

            uncertainty_pressure = float(defect_graph.get("uncertainty_pressure", 0.0))
            risk_bonus = 0.06 * uncertainty_pressure if chosen_source.startswith("defect_") else 0.0
            reward = _clip01(max(0.0, reward_quality - penalty) + risk_bonus)

            if not effective:
                reward *= 0.25

            with BANDIT_LOCK:
                state = _load_bandit_state(len(feature_vector))
                state["total_rounds"] = int(state.get("total_rounds", 0)) + 1
                _bandit_update(
                    state=state,
                    arm=chosen_source,
                    features=feature_vector,
                    reward=reward,
                    predicted_exploit=predicted_exploit,
                    observed_latency=observed_latency,
                    observed_cost=observed_cost,
                    uncertainty_pressure=uncertainty_pressure,
                    effective=effective,
                )
                constraint_debug = _update_constraints(
                    state=state,
                    observed_latency=observed_latency,
                    observed_cost=observed_cost,
                )
                drift_debug = _update_drift_and_maybe_reset(state=state, reward=reward)
                _save_bandit_state(state)

            bandit_debug["reward"] = round(reward, 4)
            bandit_debug["reward_quality"] = round(reward_quality, 4)
            bandit_debug["penalty"] = round(penalty, 4)
            bandit_debug["risk_bonus"] = round(risk_bonus, 4)
            bandit_debug["predicted_exploit"] = round(predicted_exploit, 4)
            bandit_debug["observed_latency"] = round(observed_latency, 4)
            bandit_debug["observed_cost"] = round(observed_cost, 4)
            bandit_debug["constraints"] = constraint_debug
            bandit_debug["drift"] = drift_debug

            propensity_map = (bandit_debug.get("selection") or {}).get("propensity") or {}
            chosen_propensity = float(propensity_map.get(chosen_source, 0.0))
            _append_ope_event(
                {
                    "timestamp": time.time(),
                    "event_type": "controller_proposal",
                    "document_id": req.document_id,
                    "section_id": req.section_id,
                    "subsection_id": req.subsection_id,
                    "state_path": _bandit_state_path(),
                    "chosen_arm": chosen_source,
                    "propensity": round(chosen_propensity, 6),
                    "reward": round(float(reward), 6),
                    "reward_quality": round(float(reward_quality), 6),
                    "penalty": round(float(penalty), 6),
                    "risk_bonus": round(float(risk_bonus), 6),
                    "effective": bool(effective),
                    "score_gain": round(float(score_gain), 6),
                    "rel_anchor_gain": round(float(rel_anchor_gain), 6),
                    "novelty_gain": round(float(novelty_gain), 6),
                    "structure_gain": round(float(structure_gain), 6),
                    "observed_latency": round(float(observed_latency), 6),
                    "observed_cost": round(float(observed_cost), 6),
                    "feature_vector": [round(float(v), 6) for v in feature_vector],
                    "defect_graph": defect_graph,
                    "policy_scores": selection_scores,
                    "constraint_debug": constraint_debug,
                    "drift_debug": drift_debug,
                }
            )

        if not effective:
            return {
                "success": True,
                "error": "controller_outline_not_effective",
                "improved_outline": baseline_outline,
                "source": chosen_source,
                "selected_arm": chosen_source,
                "changed": False,
                "effective": False,
                "baseline_score": baseline_score,
                "selected_score": (chosen["score"] if chosen else baseline_score),
                "selection_min_gain": min_gain,
                "selection_rel_anchor_gain": round(rel_anchor_gain, 4),
                "selection_novelty_gain": round(novelty_gain, 4),
                "selection_structure_gain": round(structure_gain, 4),
                "selection_coherence_gain": round(coherence_gain, 4),
                "selection_evidence_gain": round(evidence_gain, 4),
                "selection_rel_gain_ok": rel_gain_ok,
                "selection_novelty_gain_ok": novelty_gain_ok,
                "selection_coherence_gain_ok": coherence_gain_ok,
                "selection_evidence_gain_ok": evidence_gain_ok,
                "llm_error": llm_error,
                "defect_graph": defect_graph,
                "bandit": bandit_debug,
                "candidate_scores": [
                    {
                        "source": c["source"],
                        "total": c["score"]["total"],
                        "relevance_anchor": c["score"]["relevance_anchor"],
                        "novelty": c["score"]["novelty"],
                        "structure": c["score"]["structure"],
                        "coherence_signal": c["score"].get("coherence_signal", 0.0),
                        "evidence_signal": c["score"].get("evidence_signal", 0.0),
                    }
                    for c in dedup_candidates
                ],
            }

        # 改进成功后写回数据库
        _save_improved_outline_to_db(
            document_id=req.document_id,
            section_id=req.section_id,
            subsection_id=req.subsection_id,
            improved_outline=improved_outline,
            iteration_count=iteration,
        )

        return {
            "success": True,
            "improved_outline": improved_outline,
            "source": chosen_source,
            "selected_arm": chosen_source,
            "changed": changed,
            "effective": True,
            "baseline_score": baseline_score,
            "selected_score": (chosen["score"] if chosen else baseline_score),
            "selection_min_gain": min_gain,
            "selection_rel_anchor_gain": round(rel_anchor_gain, 4),
            "selection_novelty_gain": round(novelty_gain, 4),
            "selection_structure_gain": round(structure_gain, 4),
            "selection_coherence_gain": round(coherence_gain, 4),
            "selection_evidence_gain": round(evidence_gain, 4),
            "selection_rel_gain_ok": rel_gain_ok,
            "selection_novelty_gain_ok": novelty_gain_ok,
            "selection_coherence_gain_ok": coherence_gain_ok,
            "selection_evidence_gain_ok": evidence_gain_ok,
            "llm_error": llm_error,
            "defect_graph": defect_graph,
            "bandit": bandit_debug,
            "candidate_scores": [
                {
                    "source": c["source"],
                    "total": c["score"]["total"],
                    "relevance_anchor": c["score"]["relevance_anchor"],
                    "novelty": c["score"]["novelty"],
                    "structure": c["score"]["structure"],
                    "coherence_signal": c["score"].get("coherence_signal", 0.0),
                    "evidence_signal": c["score"].get("evidence_signal", 0.0),
                }
                for c in dedup_candidates
            ],
            "recommendations": [
                f"相关性分数: {rel_score:.4f}",
                f"冗余度分数: {red_score:.4f}",
                f"反馈: {feedback_text}"
            ]
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {
            "success": False,
            "error": str(e)
        }


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv('PORT', 8001))
    print(f"\n🚀 FlowerNet Controller 启动在 http://0.0.0.0:{port}")
    print(f"📖 API 文档: http://localhost:{port}/docs")
    uvicorn.run(app, host="0.0.0.0", port=port)
