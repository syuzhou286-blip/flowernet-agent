"""Executable state machine for RDO-VOI evidence-drift research episodes."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

from evidence_drift_policy import bayes_decision, select_drift_actions


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _episode_id(payload: Mapping[str, Any]) -> str:
    explicit = str(payload.get("episode_id") or "").strip()
    if explicit:
        return explicit
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return f"drift_{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:12]}"


class EvidenceDriftResearchAgent:
    """Plan, observe, update, and re-plan epistemic research actions.

    Live mode never fabricates an action outcome: an executor or researcher must
    submit the actual observation. Replay mode may consume a frozen
    ``realized_outcome`` embedded in a benchmark action.
    """

    def __init__(self, checkpoint_store: Any = None):
        self.checkpoint_store = checkpoint_store
        self._episodes: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _key(episode_id: str) -> str:
        return f"evidence_drift_episode:{episode_id}"

    def _save(self, state: Dict[str, Any]) -> None:
        state["updated_at"] = _now()
        self._episodes[state["episode_id"]] = copy.deepcopy(state)
        if self.checkpoint_store is not None:
            self.checkpoint_store.set(self._key(state["episode_id"]), state, ttl_seconds=30 * 24 * 3600)

    def get_episode(self, episode_id: str) -> Dict[str, Any]:
        state = self._episodes.get(episode_id)
        if state is None and self.checkpoint_store is not None:
            state = self.checkpoint_store.get(self._key(episode_id))
        if not isinstance(state, dict):
            raise KeyError(f"unknown evidence-drift episode: {episode_id}")
        self._episodes[episode_id] = copy.deepcopy(state)
        return copy.deepcopy(state)

    def create_episode(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        claims = [dict(item) for item in payload.get("claims", []) if isinstance(item, Mapping)]
        actions = [dict(item) for item in payload.get("actions", []) if isinstance(item, Mapping)]
        claim_ids = [str(item.get("claim_id") or "") for item in claims]
        action_ids = [str(item.get("action_id") or "") for item in actions]
        if not claims or any(not item for item in claim_ids) or len(set(claim_ids)) != len(claim_ids):
            raise ValueError("claims require unique non-empty claim_id values")
        if not actions or any(not item for item in action_ids) or len(set(action_ids)) != len(action_ids):
            raise ValueError("actions require unique non-empty action_id values")
        budget = float(payload.get("budget", 0.0) or 0.0)
        if budget < 0:
            raise ValueError("budget must be non-negative")
        mode = str(payload.get("mode") or "live").lower()
        if mode not in {"live", "replay"}:
            raise ValueError("mode must be live or replay")
        for action in actions:
            action["status"] = "pending"
        state = {
            "version": "evidence_drift_agent_v1",
            "episode_id": _episode_id(payload),
            "mode": mode,
            "status": "active",
            "research_question": str(payload.get("research_question") or ""),
            "report": str(payload.get("report") or ""),
            "claims": claims,
            "actions": actions,
            "dependencies": [dict(item) for item in payload.get("dependencies", []) if isinstance(item, Mapping)],
            "budget_total": budget,
            "budget_spent": 0.0,
            "cost_weight": float(payload.get("cost_weight", 0.05) or 0.05),
            "history": [],
            "created_at": _now(),
        }
        self._refresh_commitments(state)
        self._save(state)
        return copy.deepcopy(state)

    @staticmethod
    def _refresh_commitments(state: Dict[str, Any]) -> None:
        for claim in state["claims"]:
            probability = claim.get("probability_valid", claim.get("confidence", 0.5))
            claim["commitment"] = bayes_decision(float(probability))

    def plan(self, episode_id: str) -> Dict[str, Any]:
        state = self.get_episode(episode_id)
        remaining = max(0.0, float(state["budget_total"]) - float(state["budget_spent"]))
        available = [item for item in state["actions"] if item.get("status") == "pending"]
        decision = select_drift_actions(
            state["claims"],
            available,
            dependencies=state.get("dependencies", []),
            budget=remaining,
            cost_weight=float(state.get("cost_weight", 0.05)),
        )
        next_action = decision["selected_actions"][0] if decision["selected_actions"] else None
        event = {
            "event": "plan",
            "at": _now(),
            "next_action_id": next_action.get("action_id") if next_action else None,
            "remaining_budget": remaining,
            "abstain": decision["abstain"],
        }
        state["history"].append(event)
        if next_action is None:
            state["status"] = "awaiting_commitment"
        self._save(state)
        return {"episode_id": episode_id, "next_action": next_action, "decision": decision, "state": state}

    def observe(self, episode_id: str, action_id: str, observation: Mapping[str, Any]) -> Dict[str, Any]:
        state = self.get_episode(episode_id)
        action = next((item for item in state["actions"] if item.get("action_id") == action_id), None)
        if action is None:
            raise KeyError(f"unknown action: {action_id}")
        if action.get("status") != "pending":
            raise ValueError(f"action is not pending: {action_id}")
        cost = float(observation.get("cost", action.get("cost", 0.0)) or 0.0)
        remaining = float(state["budget_total"]) - float(state["budget_spent"])
        if cost < 0 or cost > remaining + 1e-9:
            raise ValueError(f"action cost {cost} exceeds remaining budget {remaining}")
        posteriors = observation.get("posteriors", {})
        if not isinstance(posteriors, Mapping) or not posteriors:
            raise ValueError("observation requires non-empty calibrated posteriors")
        claim_by_id = {str(item["claim_id"]): item for item in state["claims"]}
        updated: Dict[str, float] = {}
        for claim_id, value in posteriors.items():
            claim = claim_by_id.get(str(claim_id))
            if claim is None:
                raise ValueError(f"posterior references unknown claim: {claim_id}")
            probability = max(0.0, min(1.0, float(value)))
            claim["probability_valid"] = probability
            updated[str(claim_id)] = probability
        action["status"] = "observed"
        action["actual_cost"] = cost
        action["observation"] = dict(observation)
        state["budget_spent"] = round(float(state["budget_spent"]) + cost, 6)
        state["history"].append({
            "event": "observation",
            "at": _now(),
            "action_id": action_id,
            "posteriors": updated,
            "cost": cost,
            "evidence": observation.get("evidence", {}),
        })
        state["status"] = "active"
        self._refresh_commitments(state)
        self._save(state)
        return copy.deepcopy(state)

    def replay_step(self, episode_id: str) -> Dict[str, Any]:
        """Execute one frozen benchmark action; unavailable in live mode."""
        state = self.get_episode(episode_id)
        if state.get("mode") != "replay":
            raise ValueError("replay_step is forbidden in live mode")
        plan = self.plan(episode_id)
        selected = plan.get("next_action")
        if not selected:
            return plan
        action = next(item for item in state["actions"] if item.get("action_id") == selected["action_id"])
        outcome = action.get("realized_outcome")
        if not isinstance(outcome, Mapping):
            raise ValueError(f"replay action lacks realized_outcome: {action['action_id']}")
        observed_state = self.observe(episode_id, str(action["action_id"]), outcome)
        return {"episode_id": episode_id, "executed_action_id": action["action_id"], "state": observed_state}

    def finalize(self, episode_id: str) -> Dict[str, Any]:
        state = self.get_episode(episode_id)
        self._refresh_commitments(state)
        state["status"] = "completed"
        state["history"].append({"event": "finalize", "at": _now()})
        self._save(state)
        return copy.deepcopy(state)
