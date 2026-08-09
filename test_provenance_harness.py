import importlib.util
from pathlib import Path


PATH = Path(__file__).parent / "flowernet-generator" / "provenance_harness.py"
SPEC = importlib.util.spec_from_file_location("provenance_harness", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def test_detects_numeric_and_split_trace_mismatch():
    audit = MODULE.validate_research_provenance({
        "claims": [{
            "claim_id": "C1",
            "text": "Accuracy reached 91.2 on the test split.",
            "artifact_refs": [{"run_id": "run-7", "split": "test", "value": 91.2}],
        }],
        "artifacts": [{"run_id": "run-7", "split": "validation", "value": 87.4, "status": "completed"}],
        "report": "Accuracy reached 91.2 on the test split (run-7).",
    })
    kinds = {item["kind"] for item in audit["defects"]}
    assert "numeric_trace_mismatch" in kinds
    assert "dataset_split_mismatch" in kinds
    assert audit["hard_gate_passed"] is False


def test_valid_claim_with_source_span_and_artifact_passes():
    audit = MODULE.validate_research_provenance({
        "claims": [
            {
                "claim_id": "C1",
                "text": "The ablation achieved 87.4 on the validation split [1].",
                "source_spans": [{"source_id": 1, "quote": "Ablation details", "relation": "supports"}],
                "artifact_refs": [{"run_id": "run-7", "split": "validation", "value": 87.4}],
            }
        ],
        "artifacts": [{"run_id": "run-7", "split": "validation", "value": 87.4, "status": "completed"}],
        "report": "The ablation achieved 87.4 on validation (run-7).",
    })
    assert audit["valid"] is True
    assert audit["supported_claim_rate"] == 1.0


def test_negative_result_omission_gets_bounded_disclosure_repair():
    audit = MODULE.validate_research_provenance({
        "claims": [],
        "artifacts": [{"run_id": "failed-2", "status": "regressed", "value": 0.41}],
        "report": "The successful configuration is discussed.",
    })
    repairs = MODULE.propose_selective_repairs(audit)
    assert audit["undisclosed_negative_runs"] == ["failed-2"]
    assert repairs[0]["action"] == "restore_negative_result"
    assert repairs[0]["full_rewrite_allowed"] is False


def test_repair_rolls_back_when_external_quality_regresses():
    before = {"critical_defect_count": 2, "defect_count": 3}
    after = {"critical_defect_count": 1, "defect_count": 1}
    decision = MODULE.accept_repair_candidate(
        before,
        after,
        before_no_harm={"artifact_success": 1.0, "question_coverage": 0.8, "long_document_quality": 0.7},
        after_no_harm={"artifact_success": 1.0, "question_coverage": 0.6, "long_document_quality": 0.72},
    )
    assert decision["decision"] == "rollback"
    assert "question_coverage" in decision["regressions"]
