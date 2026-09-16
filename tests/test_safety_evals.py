import json
from pathlib import Path

import pytest

from app.services.policy import (
    evaluate_generated_content,
    evaluate_knowledge_content,
    evaluate_user_content,
)

CASES = json.loads(
    (Path(__file__).parents[1] / "evals" / "safety_cases.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", CASES, ids=lambda case: f"{case['policy']}-{case['expected']}")
def test_safety_evaluation_case(case):
    evaluators = {
        "generated": evaluate_generated_content,
        "knowledge": evaluate_knowledge_content,
        "user": evaluate_user_content,
    }
    assert evaluators[case["policy"]](case["text"]).status == case["expected"]
