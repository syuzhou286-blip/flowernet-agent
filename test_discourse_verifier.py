import importlib.util
from pathlib import Path


PATH = Path(__file__).parent / "flowernet-verifier" / "discourse_verifier.py"
SPEC = importlib.util.spec_from_file_location("discourse_verifier", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def test_connected_novel_progression_scores_above_disconnected_repetition():
    good = (
        "Retrieval agents first construct an evidence graph for every claim.\n\n"
        "Building on that graph, the verifier tests whether each citation entails its claim.\n\n"
        "Therefore, failed edges trigger targeted retrieval rather than a complete rewrite."
    )
    bad = (
        "Evidence graphs are useful for research agents.\n\n"
        "Bananas are cultivated in tropical regions and exported globally.\n\n"
        "Evidence graphs are useful for research agents."
    )
    good_score = MODULE.analyze_discourse(good, "evidence graph verification and repair")
    bad_score = MODULE.analyze_discourse(bad, "evidence graph verification and repair")
    assert good_score["discourse_delta"] > bad_score["discourse_delta"]
    assert bad_score["duplicate_pairs"]


def test_history_repetition_is_separate_from_boundary_continuity():
    history = ["The system constructs a claim evidence graph before drafting the report."]
    repeated = MODULE.analyze_discourse(
        "The system constructs a claim evidence graph before drafting the report.",
        "claim evidence graph",
        history,
    )
    novel = MODULE.analyze_discourse(
        "Building on the graph, counterevidence is retrieved for the weakest causal edge.",
        "claim evidence graph counterevidence",
        history,
    )
    assert repeated["document_redundancy"] > novel["document_redundancy"]
    assert repeated["information_gain"] < novel["information_gain"]


def test_empty_draft_cannot_receive_default_high_score():
    result = MODULE.analyze_discourse("", "claim evidence graph")
    assert result["discourse_delta"] == 0.0
    assert result["repair_targets"] == ["generate_section_content"]
