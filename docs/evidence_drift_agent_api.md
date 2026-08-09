# Running the RDO-VOI Auto Research Agent

FlowerNet now exposes an executable plan→observe→update→re-plan loop. It does
not fabricate research results: in `live` mode, a real literature/experiment
executor must submit calibrated posteriors and evidence after completing the
selected action. `replay` mode is only for frozen benchmark outcomes.

## 1. Start the generator service

```bash
cd flowernet-generator
uvicorn main:app --host 0.0.0.0 --port 8002
```

## 2. Create a live evidence-drift episode

```bash
curl -X POST http://localhost:8002/research/evidence-drift/episodes \
  -H 'Content-Type: application/json' \
  -d '{
    "episode_id": "replication-001",
    "mode": "live",
    "research_question": "Does the reported improvement replicate?",
    "report": "The method improves test accuracy.",
    "budget": 3.0,
    "claims": [
      {"claim_id": "C1", "probability_valid": 0.5, "impact": 2.0}
    ],
    "actions": [
      {
        "action_id": "reread-1",
        "type": "retrieve_counterevidence",
        "claim_id": "C1",
        "cost": 1.0,
        "outcomes": [
          {"probability": 0.7, "posteriors": {"C1": 0.55}},
          {"probability": 0.3, "posteriors": {"C1": 0.20}}
        ]
      },
      {
        "action_id": "rerun-1",
        "type": "rerun_experiment",
        "claim_id": "C1",
        "cost": 2.0,
        "outcomes": [
          {"probability": 0.5, "posteriors": {"C1": 0.95}},
          {"probability": 0.5, "posteriors": {"C1": 0.05}}
        ]
      }
    ]
  }'
```

`outcomes` are the action outcome model used for planning, not claimed results.

## 3. Ask the agent for the next research action

```bash
curl -X POST \
  http://localhost:8002/research/evidence-drift/episodes/replication-001/plan
```

The response contains every action's expected decision-regret reduction,
cost-adjusted utility, and the selected next action.

## 4. Execute the selected action and submit the real observation

After the external experiment executor reruns the study:

```bash
curl -X POST \
  http://localhost:8002/research/evidence-drift/episodes/replication-001/observe \
  -H 'Content-Type: application/json' \
  -d '{
    "action_id": "rerun-1",
    "posteriors": {"C1": 0.08},
    "cost": 2.0,
    "evidence": {
      "run_id": "run-2026-08-09-01",
      "config_hash": "sha256:...",
      "result": "failed_to_replicate"
    }
  }'
```

The agent updates claim validity, changes the Bayes commitment if necessary,
charges the budget, persists the evidence, and can then be asked to plan again.

## 5. Inspect or finalize

```bash
curl http://localhost:8002/research/evidence-drift/episodes/replication-001
curl -X POST http://localhost:8002/research/evidence-drift/episodes/replication-001/finalize
```

Every plan, observation, cost, posterior update, and final commitment remains in
the episode history and checkpoint store.

## Replay benchmark mode

`POST /research/evidence-drift/episodes/{id}/replay-step` consumes a frozen
`realized_outcome` from an action. The endpoint rejects live episodes, preventing
simulated benchmark outcomes from being mistaken for real research evidence.
