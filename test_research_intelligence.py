import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parent
MODULE_PATH = ROOT / "flowernet-generator" / "research_intelligence.py"
spec = importlib.util.spec_from_file_location("research_intelligence", MODULE_PATH)
ri = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(ri)


SOURCES = [
    {
        "title": "Vision-Language Foundation Models and Multimodal Large Language Models: A Survey",
        "body": (
            "This survey reviews multimodal architectures, benchmark evaluation, "
            "cross-modal alignment, open challenges, limitations, deployment cost, and future directions."
        ),
        "href": "https://doi.org/10.0000/example1",
        "quality_score": 0.82,
    },
    {
        "title": "Evaluation Metrics for Multimodal Systems in Real-World Products",
        "body": (
            "The paper discusses product deployment, user-facing applications, "
            "benchmark metrics, privacy risk, inference cost, and failure modes."
        ),
        "href": "https://doi.org/10.0000/example2",
        "quality_score": 0.78,
    },
]


def test_literature_anchored_novelty_outputs_required_fields():
    novelty = ri.analyze_literature_anchored_novelty(
        topic="Multimodal large language models in real-world products",
        section_title="Evaluation and limitations",
        subsection_title="Benchmarking and deployment risks",
        outline="Compare evaluation metrics, unresolved deployment challenges, privacy risks, and future research gaps.",
        sources=SOURCES,
    )
    assert novelty["source_bound"] is True
    assert novelty["source_count"] == 2
    assert novelty["literature_gaps"]
    gap = novelty["literature_gaps"][0]
    for key in [
        "literature_gap",
        "already_solved",
        "unresolved_challenge",
        "possible_contribution",
        "evidence_support",
        "reviewer_risk",
        "novelty_confidence",
    ]:
        assert key in gap
    assert 0.0 <= novelty["novelty_confidence"] <= 1.0


def test_claim_evidence_graph_is_source_bound_and_bindable():
    novelty = ri.analyze_literature_anchored_novelty(
        topic="Multimodal large language models in real-world products",
        section_title="Evaluation",
        subsection_title="Benchmarking",
        outline="Discuss benchmark metrics, deployment cost, privacy risk, and future directions.",
        sources=SOURCES,
    )
    graph = ri.build_claim_evidence_graph(
        topic="Multimodal large language models in real-world products",
        section_title="Evaluation",
        subsection_title="Benchmarking",
        outline="Discuss benchmark metrics, deployment cost, privacy risk, and future directions.",
        sources=SOURCES,
        novelty=novelty,
    )
    assert graph["source_bound"] is True
    assert graph["claims"]
    claim = graph["claims"][0]
    for key in [
        "claim",
        "supporting_evidence",
        "citation_source",
        "counter_evidence_limitation",
        "novelty_type",
        "verifier_status",
        "reviewer_risk_tag",
    ]:
        assert key in claim

    draft = (
        "Benchmarking multimodal systems requires evaluation metrics that connect cross-modal alignment "
        "with real-world product deployment [1].\n\n"
        "Deployment also introduces privacy risk and inference cost, so claims should be framed as bounded "
        "syntheses rather than definitive conclusions [2]."
    )
    bound = ri.bind_draft_to_claim_evidence_graph(draft, graph)
    assert bound["graph_status"] == "draft_bound"
    assert bound["paragraph_bindings"]
    assert 0.0 <= bound["claim_support_rate"] <= 1.0


def test_prompt_block_mentions_novelty_and_claim_evidence_plan():
    novelty = ri.analyze_literature_anchored_novelty(
        topic="AI research writing agents",
        section_title="Method",
        subsection_title="Claim-evidence planning",
        outline="Build claim evidence graph and reviewer risk analysis.",
        sources=SOURCES,
    )
    graph = ri.build_claim_evidence_graph(
        topic="AI research writing agents",
        section_title="Method",
        subsection_title="Claim-evidence planning",
        outline="Build claim evidence graph and reviewer risk analysis.",
        sources=SOURCES,
        novelty=novelty,
    )
    prompt = ri.format_research_intelligence_prompt(novelty, graph)
    assert "Literature-anchored novelty" in prompt
    assert "Claim-evidence graph" in prompt
    assert "Do not invent sources" in prompt

