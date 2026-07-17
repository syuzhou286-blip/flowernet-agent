import importlib.util
import pathlib


ROOT = pathlib.Path(__file__).resolve().parent


def load_module(name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


memory = load_module("research_memory", "flowernet-generator/research_memory.py")
optimizer = load_module("harness_optimizer", "flowernet-generator/harness_optimizer.py")


def test_research_reviewer_failure_memory_builds_topic_strategy():
    store = memory.ResearchReviewerFailureMemory()
    store.record_research(
        topic="AI in K-12 education",
        domain="education",
        methods=["learning analytics", "teacher-in-the-loop feedback"],
        benchmarks=["ASSISTments", "EdNet"],
        datasets=["student interaction logs"],
        literature_patterns=["intervention study", "classroom deployment"],
    )
    store.record_reviewer(
        domain="education",
        standards=["claims must distinguish learning gain from engagement"],
        rejection_reasons=["weak causal evidence", "missing classroom validity"],
    )
    store.record_failure(
        topic="AI in K-12 education",
        failure_kind="citation_unsupported",
        repair_arm="citation_grounding_repair",
        effective=True,
        harmful=False,
        metrics_delta={"citation_faithfulness": 0.10, "bertscore_f1": 0.01},
    )
    store.record_failure(
        topic="AI in K-12 education",
        failure_kind="novelty_weak",
        repair_arm="novelty_repair",
        effective=False,
        harmful=True,
        metrics_delta={"claim_support": -0.08},
    )

    strategy = store.topic_strategy("AI in K-12 formative feedback", domain="education")

    assert strategy["domain"] == "education"
    assert "ASSISTments" in strategy["recommended_benchmarks"]
    assert "weak causal evidence" in strategy["reviewer_risks"]
    assert strategy["repair_policy_hints"]["prefer_arms"] == ["citation_grounding_repair"]
    assert "novelty_repair" in strategy["repair_policy_hints"]["cooldown_arms"]
    assert strategy["novelty_strategy"]


def test_harness_optimizer_proposes_bounded_held_out_validated_update():
    memories = {
        "topic_strategy": {
            "domain": "agentic_research",
            "recommended_methods": ["claim-evidence graph", "citation audit"],
            "recommended_benchmarks": ["PaperBench", "RE-Bench"],
            "reviewer_risks": ["unsupported contribution claim"],
            "repair_policy_hints": {
                "prefer_arms": ["claim_evidence_repair"],
                "cooldown_arms": ["novelty_repair"],
            },
        }
    }
    trace = {
        "topic": "research-writing agents",
        "status": "failed",
        "weaknesses": [{"kind": "claim_evidence_weak"}, {"kind": "external_metrics_low"}],
        "reviewer_assessment": {
            "external_alignment": {"available": True, "aligned_with_external_metrics": False},
            "reviewer_dimensions": {"claim_support": {"score": 0.44}},
        },
    }
    proposal = optimizer.propose_harness_optimization(trace=trace, memories=memories)

    assert proposal["enabled"] is True
    assert proposal["bounded"] is True
    assert proposal["recommended_flow_order"][0] in {"research_scout", "rag_first"}
    assert proposal["held_out_required"] is True
    assert proposal["must_not_change"]

    accepted = optimizer.validate_harness_optimization(
        proposal,
        held_in_before={"external_mean": 0.50, "reviewer_score": 0.60, "no_harm_pass_rate": 0.80},
        held_in_after={"external_mean": 0.54, "reviewer_score": 0.64, "no_harm_pass_rate": 0.84},
        held_out_before={"external_mean": 0.49, "reviewer_score": 0.58, "no_harm_pass_rate": 0.78},
        held_out_after={"external_mean": 0.52, "reviewer_score": 0.61, "no_harm_pass_rate": 0.80},
    )
    assert accepted["status"] == "validated_retain"

    rejected = optimizer.validate_harness_optimization(
        proposal,
        held_in_before={"external_mean": 0.50, "reviewer_score": 0.60, "no_harm_pass_rate": 0.80},
        held_in_after={"external_mean": 0.55, "reviewer_score": 0.66, "no_harm_pass_rate": 0.82},
        held_out_before={"external_mean": 0.49, "reviewer_score": 0.58, "no_harm_pass_rate": 0.78},
        held_out_after={"external_mean": 0.47, "reviewer_score": 0.57, "no_harm_pass_rate": 0.76},
    )
    assert rejected["status"] == "validated_reject"
    assert rejected["validation"]["held_out_ok"] is False


def test_recommended_eight_layer_architecture_is_explicit_and_ordered():
    layers = optimizer.recommended_flowernet_full_architecture()
    names = [layer["name"] for layer in layers]

    assert names == [
        "Research Scout",
        "Literature Map Builder",
        "Novelty Miner",
        "Claim-Evidence Graph Builder",
        "Research Paper Writer",
        "Evolvable Reviewer / Verifier",
        "Controller + Bandit Repair",
        "Self-Improving Harness Optimizer",
    ]
    assert all(layer["role"] and layer["paper_value"] for layer in layers)


def test_orchestrator_builds_memory_and_optimizer_record():
    orch_mod = load_module("orch_memory_optimizer", "flowernet-generator/flowernet_orchestrator_impl.py")
    orch = orch_mod.DocumentGenerationOrchestrator(history_manager=None)
    record = orch._build_memory_optimizer_record(
        document_title="Research-writing agents for evidence-grounded manuscripts",
        section_title="System Design",
        subsection_title="Claim-evidence verification",
        trace={
            "topic": "research-writing agents",
            "status": "failed",
            "weaknesses": [{"kind": "claim_evidence_weak"}],
            "reviewer_assessment": {
                "reviewer_dimensions": {"claim_support": {"score": 0.42}},
                "external_alignment": {"available": True, "aligned_with_external_metrics": False},
            },
        },
        source_results=[{"title": "PaperBench evaluates AI research replication", "snippet": "benchmark dataset method"}],
    )

    assert record["enabled"] is True
    assert record["memory"]["topic_strategy"]["domain"]
    assert record["harness_optimizer"]["held_out_required"] is True
    assert len(record["architecture_layers"]) == 8
