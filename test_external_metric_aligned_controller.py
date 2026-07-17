import asyncio
import importlib.util
import pathlib


ROOT = pathlib.Path(__file__).resolve().parent
MODULE_PATH = ROOT / "flowernet-controler" / "main.py"
spec = importlib.util.spec_from_file_location("flowernet_controller_main", MODULE_PATH)
controller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controller)


def test_reviewer_and_external_defects_map_to_research_quality_arms():
    feedback = {
        "relevancy_index": 0.80,
        "redundancy_index": 0.18,
        "quality_dimensions": {
            "topic_alignment": 0.82,
            "evidence_grounding": 0.78,
            "novelty": 0.75,
            "logical_coherence": 0.80,
            "structure_clarity": 0.79,
        },
        "dimension_thresholds": {},
        "reviewer_assessment": {
            "reviewer_dimensions": {
                "citation_faithfulness": {"score": 0.42, "risk": "high"},
                "claim_support": {"score": 0.49, "risk": "high"},
                "novelty_strength": {"score": 0.43, "risk": "high"},
                "reviewer_concern_prediction": {"score": 0.40, "risk": "high"},
                "reproducibility_risk": {"score": 0.44, "risk": "high"},
            },
            "external_alignment": {
                "available": True,
                "aligned_with_external_metrics": False,
                "external_mean": 0.42,
                "internal_mean": 0.73,
            },
        },
    }

    graph = controller._build_defect_graph(feedback, 0.80, 0.18, 0.70, 0.30)

    assert graph["citation_grounding"] >= 0.20
    assert graph["claim_evidence"] >= 0.20
    assert graph["novelty_repair"] >= 0.20
    assert graph["reviewer_risk"] >= 0.20
    assert graph["external_metric"] >= 0.20
    assert graph["reproducibility"] >= 0.20


def test_research_quality_arm_compatibility_keeps_only_relevant_arms():
    candidates = {
        "novelty_repair": {"source": "novelty_repair"},
        "citation_grounding_repair": {"source": "citation_grounding_repair"},
        "reproducibility_repair": {"source": "reproducibility_repair"},
        "structure_readability_repair": {"source": "structure_readability_repair"},
    }
    compatible, debug = controller._filter_defect_compatible_candidates(
        candidates,
        defect_graph={"citation_grounding": 0.45, "claim_evidence": 0.39},
        failed_dims=[],
    )

    assert set(compatible) == {"citation_grounding_repair"}
    assert debug["defect_compatibility_applied"] is True


def test_research_defect_gate_prioritizes_external_metric_repair(monkeypatch):
    monkeypatch.setenv("CONTROLLER_TRAINED_POLICY_ENABLED", "false")
    state = controller._default_bandit_state(22)
    arm, debug = controller._bandit_choose_arm(
        state,
        available_arms=["citation_grounding_repair", "external_metric_repair", "structure_readability_repair"],
        features=[0.0] * 22,
        defect_graph={
            "citation_grounding": 0.34,
            "external_metric": 0.82,
            "structure_readability": 0.31,
        },
    )

    assert arm == "external_metric_repair"
    assert debug["mode"] == "research_defect_gate"


def test_no_harm_gate_rejects_reviewer_external_and_readability_regressions():
    before = {
        "relevancy_index": 0.78,
        "redundancy_index": 0.20,
        "quality_score": 0.72,
        "quality_dimensions": {"evidence_grounding": 0.74, "novelty": 0.70},
        "source_check": {"passed": True},
        "source_alignment": {"source_count": 3, "score": 0.70, "topic_phrase_coverage": 0.66},
        "reviewer_assessment": {
            "reviewer_dimensions": {
                "citation_faithfulness": {"score": 0.78},
                "claim_support": {"score": 0.74},
                "logical_coherence": {"score": 0.73},
            },
            "external_alignment": {"available": True, "external_mean": 0.62},
        },
    }
    after = {
        "relevancy_index": 0.79,
        "redundancy_index": 0.19,
        "quality_score": 0.73,
        "quality_dimensions": {"evidence_grounding": 0.75, "novelty": 0.72},
        "source_check": {"passed": True},
        "source_alignment": {"source_count": 3, "score": 0.69, "topic_phrase_coverage": 0.65},
        "reviewer_assessment": {
            "reviewer_dimensions": {
                "citation_faithfulness": {"score": 0.69},
                "claim_support": {"score": 0.64},
                "logical_coherence": {"score": 0.72},
            },
            "external_alignment": {"available": True, "external_mean": 0.54},
        },
    }

    gate = controller._controller_no_harm_gate(before, after)

    assert gate["passed"] is False
    assert "citation_faithfulness" in gate["reviewer_regressions"]
    assert "claim_support" in gate["reviewer_regressions"]
    assert gate["external_metric_regression"] is True


def test_bandit_outcome_cools_down_arm_when_no_harm_fails(tmp_path, monkeypatch):
    state_path = tmp_path / "bandit.json"
    events_path = tmp_path / "events.jsonl"
    monkeypatch.setenv("CONTROLLER_BANDIT_STATE_PATH", str(state_path))
    monkeypatch.setenv("CONTROLLER_BANDIT_EVENTS_PATH", str(events_path))
    feature_vector = [0.0] * 22
    state = controller._default_bandit_state(len(feature_vector))
    controller._save_bandit_state(state)

    before = {
        "quality_dimensions": {"evidence_grounding": 0.74},
        "source_check": {"passed": True},
        "reviewer_assessment": {
            "reviewer_dimensions": {"citation_faithfulness": {"score": 0.80}},
            "external_alignment": {"available": True, "external_mean": 0.64},
        },
    }
    after = {
        "quality_dimensions": {"evidence_grounding": 0.76},
        "source_check": {"passed": True},
        "reviewer_assessment": {
            "reviewer_dimensions": {"citation_faithfulness": {"score": 0.68}},
            "external_alignment": {"available": True, "external_mean": 0.50},
        },
    }
    req = controller.BanditOutcomeRequest(
        selected_arm="citation_grounding_repair",
        feature_vector=feature_vector,
        before_verification=before,
        after_verification=after,
        application_accepted=True,
    )
    result = asyncio.run(controller.record_bandit_outcome(req))
    saved = controller._load_bandit_state(len(feature_vector))

    assert result["no_harm_gate"]["passed"] is False
    assert result["effective"] is False
    assert saved["arms"]["citation_grounding_repair"]["ineffective_streak"] >= 1
