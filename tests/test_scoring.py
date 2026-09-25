"""分项成绩、录分与复核分离的约束。"""

import tempfile
import unittest
from pathlib import Path

try:
    import support
except ImportError:  # pragma: no cover
    from tests import support

from src.competition import CompetitionError, ScoreComponents, ScoreStatus


class ScoringTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "events.jsonl"
        self.service = support.build_service(self.path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _assignment_of(self, contestant_id: str):
        return self.service.contestant_assignments(contestant_id)[0]

    def test_components_are_computed_separately(self) -> None:
        assignment = self._assignment_of("C1")
        components = ScoreComponents(theory=30, practical=50, safety_deduction=5, emergency=15)
        self.service.record_score(assignment.assignment_id, assignment.judge_id, components, "clerk-1")
        self.service.verify_score(assignment.assignment_id, "clerk-2")
        audit = self.service.audit_report("C1")
        score = audit["scores"][0]
        self.assertEqual(
            score["components"],
            {"theory": 30, "practical": 50, "safety_deduction": 5, "emergency": 15},
        )
        self.assertEqual(score["total"], 90)  # 30+50+15-5
        self.assertEqual(score["recorded_by"], "clerk-1")
        self.assertEqual(score["verified_by"], "clerk-2")

    def test_recorder_cannot_verify_own_entry(self) -> None:
        assignment = self._assignment_of("C1")
        self.service.record_score(
            assignment.assignment_id,
            assignment.judge_id,
            {"theory": 80, "practical": 10, "safety_deduction": 0, "emergency": 5},
            "clerk-1",
        )
        with self.assertRaisesRegex(CompetitionError, "录分者不能复核自己的结果"):
            self.service.verify_score(assignment.assignment_id, "clerk-1")
        self.service.verify_score(assignment.assignment_id, "clerk-2")
        self.assertEqual(
            self.service.scores[assignment.assignment_id].status, ScoreStatus.VERIFIED
        )

    def test_duplicate_recording_is_rejected(self) -> None:
        assignment = self._assignment_of("C1")
        components = {"theory": 80, "practical": 10, "safety_deduction": 0, "emergency": 5}
        self.service.record_score(assignment.assignment_id, assignment.judge_id, components, "clerk-1")
        with self.assertRaisesRegex(CompetitionError, "成绩已录入"):
            self.service.record_score(
                assignment.assignment_id, assignment.judge_id, components, "clerk-1"
            )

    def test_score_must_come_from_assigned_judge(self) -> None:
        assignment = self._assignment_of("C1")
        with self.assertRaisesRegex(CompetitionError, "评分裁判与排定裁判不一致"):
            self.service.record_score(
                assignment.assignment_id,
                "J4",
                {"theory": 80, "practical": 10, "safety_deduction": 0, "emergency": 5},
                "clerk-1",
            )

    def test_negative_component_is_rejected(self) -> None:
        with self.assertRaisesRegex(CompetitionError, "分项成绩无效"):
            ScoreComponents(theory=-1, practical=0, safety_deduction=0, emergency=0)


if __name__ == "__main__":
    unittest.main()
