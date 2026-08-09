import importlib.util
from pathlib import Path


PATH = Path(__file__).parent / "flowernet-generator" / "evidence_drift_policy.py"
SPEC = importlib.util.spec_from_file_location("evidence_drift_policy", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def test_bayes_commitment_changes_with_claim_validity():
    assert MODULE.bayes_decision(0.95)["decision"] == "retain"
    assert MODULE.bayes_decision(0.50)["decision"] == "weaken"
    assert MODULE.bayes_decision(0.05)["decision"] == "retract"


def test_voi_prefers_decisive_low_cost_rerun():
    claims = [{"claim_id": "C1", "probability_valid": 0.5, "impact": 1.0}]
    actions = [
        {
            "action_id": "reread",
            "action_type": "reread_literature",
            "target_claim_ids": ["C1"],
            "cost": 1.0,
            "outcomes": [
                {"probability": 0.5, "posteriors": {"C1": 0.60}},
                {"probability": 0.5, "posteriors": {"C1": 0.40}},
            ],
        },
        {
            "action_id": "rerun",
            "action_type": "rerun_experiment",
            "target_claim_ids": ["C1"],
            "cost": 1.0,
            "outcomes": [
                {"probability": 0.5, "posteriors": {"C1": 0.98}},
                {"probability": 0.5, "posteriors": {"C1": 0.02}},
            ],
        },
    ]
    result = MODULE.select_drift_actions(claims, actions, budget=1.0, cost_weight=0.01)
    assert result["selected_actions"][0]["action_id"] == "rerun"
    assert result["selected_actions"][0]["expected_regret_reduction"] > 0


def test_dependency_impact_can_prioritize_upstream_claim():
    claims = [
        {"claim_id": "method", "probability_valid": 0.5, "impact": 1.0},
        {"claim_id": "conclusion", "probability_valid": 0.5, "impact": 3.0},
    ]
    dependencies = [{"source": "method", "target": "conclusion", "strength": 1.0}]
    actions = [
        {
            "action_id": "check-method",
            "type": "reimplement",
            "claim_id": "method",
            "cost": 1.0,
            "outcomes": [
                {"probability": 0.5, "posteriors": {"method": 0.99}},
                {"probability": 0.5, "posteriors": {"method": 0.01}},
            ],
        }
    ]
    result = MODULE.select_drift_actions(claims, actions, dependencies=dependencies, budget=1.0, cost_weight=0.0)
    assert result["effective_claim_impacts"]["method"] > result["effective_claim_impacts"]["conclusion"]
    assert result["selected_actions"]


def test_policy_abstains_when_information_is_not_worth_cost():
    result = MODULE.select_drift_actions(
        [{"claim_id": "C1", "probability_valid": 0.95}],
        [{
            "action_id": "expensive-rerun",
            "type": "rerun",
            "claim_id": "C1",
            "cost": 100.0,
            "outcomes": [{"probability": 1.0, "posteriors": {"C1": 0.96}}],
        }],
        budget=100.0,
        cost_weight=0.05,
    )
    assert result["abstain"] is True
    assert result["selected_actions"] == []
