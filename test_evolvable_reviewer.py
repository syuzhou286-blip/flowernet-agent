import importlib.util
import pathlib


ROOT = pathlib.Path(__file__).resolve().parent
MODULE_PATH = ROOT / "flowernet-generator" / "evolvable_reviewer.py"
spec = importlib.util.spec_from_file_location("evolvable_reviewer", MODULE_PATH)
reviewer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reviewer)


def test_reviewer_returns_auditable_mini_review_dimensions():
    result = reviewer.build_reviewer_assessment(
        draft=(
            "Retrieval-augmented research writers need claim-evidence graphs because "
            "citations must support concrete claims [1]. The system should evaluate "
            "citation faithfulness, novelty, logical coherence, and reproducibility risk."
        ),
        outline="Write a research section about evidence-grounded research-writing agents.",
        verification={
            "source_check": {"passed": True, "matched_source_count": 2},
            "quality_dimensions": {
                "logical_coherence": 0.78,
                "evidence_grounding": 0.74,
                "novelty": 0.68,
                "coverage_completeness": 0.72,
            },
            "quality_dimensions_uncertainty": {"novelty": 0.10, "evidence_grounding": 0.12},
        },
        research_intelligence={
            "novelty": {"novelty_confidence": 0.66, "reviewer_risk": "medium"},
            "claim_evidence_graph": {
                "claim_support_rate": 0.75,
                "claims": [
                    {
                        "claim": "Claim-evidence graphs improve auditability.",
                        "verifier_status": "supported_in_draft",
                        "reviewer_risk_tag": "low-risk",
                    }
                ],
            },
        },
        external_metrics={"rouge1": 0.51, "rouge2": 0.24, "rougeL": 0.39, "bertscore_f1": 0.86},
    )

    dimensions = result["reviewer_dimensions"]
    assert set(dimensions) == {
        "citation_faithfulness",
        "claim_support",
        "novelty_strength",
        "logical_coherence",
        "experimental_completeness",
        "reviewer_concern_prediction",
        "reproducibility_risk",
    }
    assert result["auditability"]["source_bound"] is True
    assert result["external_alignment"]["available"] is True
    assert result["overall_reviewer_score"] > 0.60
    assert result["reviewer_decision"] in {"accept_with_minor_revision", "borderline_revision"}


def test_external_metric_mismatch_raises_reviewer_concern():
    result = reviewer.build_reviewer_assessment(
        draft="Generic discussion without enough source-specific details or reproducible experiment design.",
        outline="Write an evidence-grounded research paper section.",
        verification={
            "source_check": {"passed": True, "matched_source_count": 1},
            "quality_score": 0.78,
            "quality_dimensions": {
                "logical_coherence": 0.82,
                "evidence_grounding": 0.76,
                "novelty": 0.74,
            },
        },
        research_intelligence={
            "novelty": {"novelty_confidence": 0.72, "reviewer_risk": "low"},
            "claim_evidence_graph": {"claim_support_rate": 0.65, "claims": []},
        },
        external_metrics={"rouge1": 0.22, "rouge2": 0.05, "rougeL": 0.18, "bertscore_f1": 0.71},
    )

    assert result["external_alignment"]["aligned_with_external_metrics"] is False
    assert result["reviewer_dimensions"]["reviewer_concern_prediction"]["score"] < 0.60
    assert any("external" in concern.lower() for concern in result["reviewer_concerns"])


def test_reviewer_calibration_requires_held_out_no_harm():
    proposal = reviewer.propose_reviewer_calibration(
        reviewer_record={
            "reviewer_dimensions": {
                "citation_faithfulness": {"score": 0.42},
                "claim_support": {"score": 0.48},
            },
            "external_alignment": {"aligned_with_external_metrics": False},
        },
        before={"reviewer_score": 0.61, "external_mean": 0.55, "claim_support": 0.50},
        after_held_in={"reviewer_score": 0.66, "external_mean": 0.59, "claim_support": 0.56},
        after_held_out={"reviewer_score": 0.65, "external_mean": 0.58, "claim_support": 0.55},
    )

    assert proposal["status"] == "validated_retain"
    assert proposal["bounded"] is True
    assert "no_harm_gate" in proposal["validation"]

    rejected = reviewer.propose_reviewer_calibration(
        reviewer_record={"external_alignment": {"aligned_with_external_metrics": False}},
        before={"reviewer_score": 0.61, "external_mean": 0.55, "claim_support": 0.50},
        after_held_in={"reviewer_score": 0.67, "external_mean": 0.60, "claim_support": 0.57},
        after_held_out={"reviewer_score": 0.63, "external_mean": 0.52, "claim_support": 0.55},
    )
    assert rejected["status"] == "validated_reject"
    assert rejected["validation"]["no_harm_gate"] is False


def test_flowernet_verifier_returns_reviewer_assessment(monkeypatch):
    verifier_path = ROOT / "flowernet-verifier" / "main.py"
    verifier_spec = importlib.util.spec_from_file_location("flowernet_verifier_main", verifier_path)
    verifier_mod = importlib.util.module_from_spec(verifier_spec)
    verifier_spec.loader.exec_module(verifier_mod)
    verifier = verifier_mod.FlowerNetVerifier()

    monkeypatch.setenv("REQUIRE_MULTIDIM_QUALITY", "false")
    result = verifier.verify(
        draft=(
            "Evidence-grounded research-writing agents need claim-evidence graphs, "
            "citation audits, benchmark metrics, and reproducible evaluation protocol [1]."
        ),
        outline="Write about evidence-grounded research-writing agents and reviewer assessment.",
        history_list=[],
        rel_threshold=0.05,
        red_threshold=0.95,
        require_source_citations=False,
        research_intelligence={
            "novelty": {"novelty_confidence": 0.62, "reviewer_risk": "medium"},
            "claim_evidence_graph": {
                "claim_support_rate": 0.70,
                "claims": [{"claim": "Claim evidence graphs improve auditability."}],
            },
        },
        external_metrics={"rouge1": 0.50, "rouge2": 0.23, "rougeL": 0.38, "bertscore_f1": 0.85},
    )

    assessment = result["reviewer_assessment"]
    assert assessment["enabled"] is True
    assert assessment["auditability"]["source_bound"] is True
    assert assessment["external_alignment"]["available"] is True
    assert "reproducibility_risk" in assessment["reviewer_dimensions"]
