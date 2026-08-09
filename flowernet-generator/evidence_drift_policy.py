"""Decision-theoretic action selection for scientific evidence drift.

RDO-VOI separates *finding an inconsistency* from deciding whether the best
next step is to gather information (reread/rerun/reimplement/ablate) or change
the report commitment (weaken/retract/patch).  It is deterministic and expects
calibrated probabilities from an upstream model or empirical estimator.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Sequence, Set


DEFAULT_LOSSES: Dict[str, Dict[str, float]] = {
    # Loss if the latent claim is valid / invalid.
    "retain": {"valid": 0.0, "invalid": 1.0},
    "weaken": {"valid": 0.20, "invalid": 0.35},
    "retract": {"valid": 0.75, "invalid": 0.0},
}


def _clip_probability(value: Any, default: float = 0.5) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _claim_probability(claim: Mapping[str, Any]) -> float:
    return _clip_probability(
        claim.get("probability_valid", claim.get("confidence", claim.get("support_probability", 0.5)))
    )


def decision_risk(
    probability_valid: float,
    decision: str,
    *,
    losses: Mapping[str, Mapping[str, float]] | None = None,
) -> float:
    """Expected regret for one report commitment under a binary latent truth."""
    table = losses or DEFAULT_LOSSES
    row = table[decision]
    p = _clip_probability(probability_valid)
    return p * float(row["valid"]) + (1.0 - p) * float(row["invalid"])


def bayes_decision(
    probability_valid: float,
    *,
    losses: Mapping[str, Mapping[str, float]] | None = None,
) -> Dict[str, Any]:
    """Return the minimum-regret retain/weaken/retract commitment."""
    table = losses or DEFAULT_LOSSES
    risks = {
        decision: decision_risk(probability_valid, decision, losses=table)
        for decision in table
    }
    selected = min(risks, key=risks.get)
    return {
        "decision": selected,
        "risk": round(risks[selected], 6),
        "risks": {key: round(value, 6) for key, value in risks.items()},
    }


def _effective_impacts(
    claims: Sequence[Mapping[str, Any]],
    dependencies: Sequence[Mapping[str, Any]],
) -> Dict[str, float]:
    """Propagate claim impact through directed dependency edges."""
    base = {
        str(claim.get("claim_id")): max(0.0, float(claim.get("impact", 1.0) or 1.0))
        for claim in claims
        if str(claim.get("claim_id") or "")
    }
    outgoing: Dict[str, List[tuple[str, float]]] = {}
    for edge in dependencies:
        source = str(edge.get("source") or edge.get("from") or "")
        target = str(edge.get("target") or edge.get("to") or "")
        if source and target:
            outgoing.setdefault(source, []).append((target, _clip_probability(edge.get("strength", 1.0), 1.0)))

    def downstream(claim_id: str, path: Set[str]) -> float:
        if claim_id in path:
            return 0.0
        path = set(path) | {claim_id}
        return sum(
            strength * (base.get(target, 0.0) + downstream(target, path))
            for target, strength in outgoing.get(claim_id, [])
        )

    return {claim_id: impact + downstream(claim_id, set()) for claim_id, impact in base.items()}


def _portfolio_risk(
    probabilities: Mapping[str, float],
    impacts: Mapping[str, float],
    *,
    losses: Mapping[str, Mapping[str, float]] | None = None,
) -> float:
    return sum(
        impacts.get(claim_id, 1.0) * float(bayes_decision(probability, losses=losses)["risk"])
        for claim_id, probability in probabilities.items()
    )


def score_information_action(
    action: Mapping[str, Any],
    *,
    probabilities: Mapping[str, float],
    impacts: Mapping[str, float],
    losses: Mapping[str, Mapping[str, float]] | None = None,
    cost_weight: float = 0.05,
) -> Dict[str, Any]:
    """Compute expected decision-regret reduction for a calibrated action model."""
    targets = [str(item) for item in action.get("target_claim_ids", []) if str(item)]
    if not targets and action.get("claim_id"):
        targets = [str(action["claim_id"])]
    current = {key: value for key, value in probabilities.items() if key in targets}
    current_risk = _portfolio_risk(current, impacts, losses=losses)
    outcomes = [item for item in action.get("outcomes", []) if isinstance(item, Mapping)]
    probability_mass = sum(_clip_probability(item.get("probability"), 0.0) for item in outcomes)
    expected_risk = current_risk
    if outcomes and probability_mass > 0:
        expected_risk = 0.0
        for outcome in outcomes:
            outcome_probability = _clip_probability(outcome.get("probability"), 0.0) / probability_mass
            posteriors = dict(current)
            raw_posteriors = outcome.get("posteriors", {})
            if isinstance(raw_posteriors, Mapping):
                for claim_id, value in raw_posteriors.items():
                    if str(claim_id) in posteriors:
                        posteriors[str(claim_id)] = _clip_probability(value)
            expected_risk += outcome_probability * _portfolio_risk(posteriors, impacts, losses=losses)
    information_value = max(0.0, current_risk - expected_risk)
    cost = max(0.0, float(action.get("cost", 0.0) or 0.0))
    utility = information_value - max(0.0, float(cost_weight)) * cost
    return {
        "action_id": str(action.get("action_id") or ""),
        "action_type": str(action.get("action_type") or action.get("type") or ""),
        "target_claim_ids": targets,
        "cost": round(cost, 6),
        "current_decision_risk": round(current_risk, 6),
        "expected_post_action_risk": round(expected_risk, 6),
        "expected_regret_reduction": round(information_value, 6),
        "utility": round(utility, 6),
        "outcome_model_available": bool(outcomes and probability_mass > 0),
    }


def select_drift_actions(
    claims: Sequence[Mapping[str, Any]],
    actions: Sequence[Mapping[str, Any]],
    *,
    dependencies: Sequence[Mapping[str, Any]] | None = None,
    budget: float,
    losses: Mapping[str, Mapping[str, float]] | None = None,
    cost_weight: float = 0.05,
) -> Dict[str, Any]:
    """Select a budget-feasible set of heterogeneous epistemic actions.

    The current implementation uses a deterministic utility-per-cost greedy
    policy.  It is an explicit baseline and deployable safety policy, not a
    claim that greedy selection solves the general adaptive VOI problem.
    """
    probabilities = {
        str(claim.get("claim_id")): _claim_probability(claim)
        for claim in claims
        if str(claim.get("claim_id") or "")
    }
    impacts = _effective_impacts(claims, dependencies or [])
    scores = [
        score_information_action(
            action,
            probabilities=probabilities,
            impacts=impacts,
            losses=losses,
            cost_weight=cost_weight,
        )
        for action in actions
    ]
    scores.sort(
        key=lambda item: (
            item["utility"] / max(item["cost"], 1e-9),
            item["expected_regret_reduction"],
        ),
        reverse=True,
    )
    remaining = max(0.0, float(budget))
    selected: List[Dict[str, Any]] = []
    covered_claims: Set[str] = set()
    for score in scores:
        targets = set(score["target_claim_ids"])
        # Scores are one-step VOI estimates. Selecting two actions for the same
        # claim would double-count information unless outcomes are observed and
        # the posterior is updated, so this non-adaptive baseline permits only
        # one action per target claim.
        if targets & covered_claims or score["utility"] <= 0 or score["cost"] > remaining:
            continue
        selected.append(score)
        covered_claims.update(targets)
        remaining -= score["cost"]

    commitments = {
        claim_id: bayes_decision(probability, losses=losses)
        for claim_id, probability in probabilities.items()
    }
    return {
        "version": "rdo_voi_v1",
        "budget": round(float(budget), 6),
        "spent": round(float(budget) - remaining, 6),
        "remaining": round(remaining, 6),
        "effective_claim_impacts": {key: round(value, 6) for key, value in impacts.items()},
        "current_commitments": commitments,
        "selected_actions": selected,
        "all_action_scores": scores,
        "abstain": not selected,
        "policy": "greedy_expected_decision_regret_reduction_per_cost",
    }
