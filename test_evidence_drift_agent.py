import importlib.util
import sys
from pathlib import Path


GENERATOR_DIR = Path(__file__).parent / "flowernet-generator"
sys.path.insert(0, str(GENERATOR_DIR))
SPEC = importlib.util.spec_from_file_location("evidence_drift_agent", GENERATOR_DIR / "evidence_drift_agent.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class MemoryCheckpoint:
    def __init__(self):
        self.data = {}

    def set(self, key, value, ttl_seconds=None):
        self.data[key] = value

    def get(self, key):
        return self.data.get(key)


def episode_payload(mode="live"):
    return {
        "episode_id": f"episode-{mode}",
        "mode": mode,
        "research_question": "Does the method replicate?",
        "budget": 2.0,
        "claims": [{"claim_id": "C1", "probability_valid": 0.5, "impact": 2.0}],
        "actions": [
            {
                "action_id": "rerun-1",
                "type": "rerun_experiment",
                "claim_id": "C1",
                "cost": 1.0,
                "outcomes": [
                    {"probability": 0.5, "posteriors": {"C1": 0.95}},
                    {"probability": 0.5, "posteriors": {"C1": 0.05}},
                ],
                "realized_outcome": {
                    "posteriors": {"C1": 0.05},
                    "cost": 1.0,
                    "evidence": {"run_id": "run-new", "status": "failed"},
                },
            }
        ],
    }


def test_live_agent_plans_observes_and_updates_commitment():
    store = MemoryCheckpoint()
    agent = MODULE.EvidenceDriftResearchAgent(store)
    created = agent.create_episode(episode_payload())
    assert created["claims"][0]["commitment"]["decision"] == "weaken"

    plan = agent.plan("episode-live")
    assert plan["next_action"]["action_id"] == "rerun-1"

    observed = agent.observe(
        "episode-live",
        "rerun-1",
        {"posteriors": {"C1": 0.05}, "cost": 1.0, "evidence": {"run_id": "run-live"}},
    )
    assert observed["claims"][0]["commitment"]["decision"] == "retract"
    assert observed["budget_spent"] == 1.0
    assert store.get("evidence_drift_episode:episode-live")


def test_live_agent_refuses_fabricated_replay_outcome():
    agent = MODULE.EvidenceDriftResearchAgent()
    agent.create_episode(episode_payload())
    try:
        agent.replay_step("episode-live")
        assert False, "live replay must fail"
    except ValueError as exc:
        assert "forbidden" in str(exc)


def test_replay_agent_executes_frozen_outcome_and_keeps_audit_history():
    agent = MODULE.EvidenceDriftResearchAgent()
    agent.create_episode(episode_payload(mode="replay"))
    result = agent.replay_step("episode-replay")
    state = result["state"]
    assert result["executed_action_id"] == "rerun-1"
    assert state["claims"][0]["probability_valid"] == 0.05
    assert [event["event"] for event in state["history"]] == ["plan", "observation"]


def test_observation_cannot_exceed_remaining_budget():
    agent = MODULE.EvidenceDriftResearchAgent()
    agent.create_episode(episode_payload())
    try:
        agent.observe("episode-live", "rerun-1", {"posteriors": {"C1": 0.1}, "cost": 3.0})
        assert False, "over-budget observation must fail"
    except ValueError as exc:
        assert "exceeds remaining budget" in str(exc)
