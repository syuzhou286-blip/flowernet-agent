import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parent
MODULE_PATH = ROOT / "flowernet-generator" / "self_improving_harness.py"
spec = importlib.util.spec_from_file_location("self_improving_harness", MODULE_PATH)
sih = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(sih)


def test_weakness_mining_detects_research_writing_failures():
    trace = {
        "system": "flowernet_full",
        "topic_id": "dev_topic",
        "success": False,
        "status": "failed",
        "error": "subsection_verification_failed",
        "research_novelty": {
            "novelty_confidence": 0.31,
            "reviewer_risk": "high",
            "literature_gaps": [{"axis": "future_gap"}],
        },
        "claim_evidence_graph": {
            "claim_support_rate": 0.25,
            "claims": [
                {"claim": "This important system improves research.", "reviewer_risk_tag": "insufficient-evidence"},
                {"claim": "The approach is significant for many studies.", "reviewer_risk_tag": "needs-caveat"},
            ],
        },
        "verification": {
            "source_check": {"passed": False, "reason": "unsupported citation", "reference_count": 1},
        },
        "bandit": {
            "events": [
                {
                    "selected_arm": "defect_evidence",
                    "realized_reward": 0.0,
                    "realized_regressed": True,
                    "realized_effective": False,
                }
            ]
        },
        "external_metrics": {"rouge1": 0.31, "rouge2": 0.04, "rougeL": 0.10, "bertscore_f1": 0.83},
        "baseline_external_metrics": {"rouge1": 0.34, "rouge2": 0.05, "rougeL": 0.11, "bertscore_f1": 0.84},
    }
    kinds = {item["kind"] for item in sih.mine_weaknesses(trace)}
    assert {
        "novelty_weak",
        "claim_evidence_weak",
        "claim_generic",
        "citation_unsupported",
        "controller_harmful",
        "external_metrics_low",
        "run_failed",
    }.issubset(kinds)


def test_harness_proposals_are_bounded_and_validation_ready():
    weaknesses = [
        {
            "weakness_id": "weak_1",
            "kind": "controller_harmful",
            "severity": 0.9,
            "component": "controller",
            "evidence": {"selected_arm": "defect_evidence"},
        },
        {
            "weakness_id": "weak_2",
            "kind": "novelty_weak",
            "severity": 0.8,
            "component": "novelty_miner",
            "evidence": {"novelty_confidence": 0.2},
        },
    ]
    proposals = sih.propose_harness_updates(weaknesses)
    assert proposals
    assert proposals[0]["bounded"] is True
    assert proposals[0]["status"] == "proposed_unvalidated"
    assert "held_out_references" in proposals[0]["must_not_change"]
    assert "acceptance_rule" in proposals[0]["validation_plan"]


def test_validation_requires_held_in_held_out_and_no_harm():
    proposal = {"proposal_id": "prop_1"}
    before = {
        "quality_score": 0.70,
        "source_alignment_score": 0.60,
        "claim_support_rate": 0.55,
        "novelty_confidence": 0.50,
        "rouge1": 0.35,
        "rouge2": 0.06,
        "rougeL": 0.12,
        "bertscore_f1": 0.84,
    }
    after_good = {
        "quality_score": 0.72,
        "source_alignment_score": 0.61,
        "claim_support_rate": 0.60,
        "novelty_confidence": 0.54,
        "rouge1": 0.355,
        "rouge2": 0.062,
        "rougeL": 0.121,
        "bertscore_f1": 0.841,
    }
    accepted = sih.validate_harness_proposal(
        proposal,
        held_in_before=before,
        held_in_after=after_good,
        held_out_before=before,
        held_out_after=after_good,
    )
    assert accepted["retained"] is True
    assert accepted["status"] == "validated_retain"

    after_regressed = dict(after_good)
    after_regressed["rouge2"] = 0.050
    rejected = sih.validate_harness_proposal(
        proposal,
        held_in_before=before,
        held_in_after=after_good,
        held_out_before=before,
        held_out_after=after_regressed,
    )
    assert rejected["retained"] is False
    assert rejected["status"] == "validated_reject"
    assert rejected["no_harm_passed"] is False


def test_build_self_improving_harness_record_returns_weaknesses_and_proposals():
    record = sih.build_self_improving_harness_record(
        {
            "success": False,
            "status": "failed",
            "verification": {"source_check": {"passed": False, "reason": "invalid_source_url"}},
            "claim_evidence_graph": {"claim_support_rate": 0.2, "claims": [{"claim": "Generic claim"}]},
        }
    )
    assert record["enabled"] is True
    assert record["weakness_count"] >= 1
    assert record["proposal_count"] >= 1
    assert record["status"] == "proposal_ready"

