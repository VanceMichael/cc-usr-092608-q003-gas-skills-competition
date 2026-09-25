"""评分：裁评分离、分项计算、更正留痕。"""

import tempfile
import unittest

from src.competition import Component, RuleViolation, ScoreState, Trade
from tests.helpers import (
    at,
    close_and_schedule,
    full_scores,
    make_service,
    panel,
    score_components,
)


class ScoringTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.service = make_service(self.dir.name)
        close_and_schedule(self.service)

    def test_components_are_scored_and_totalled_separately(self) -> None:
        score_components(self.service, "C1", full_scores(80, 90, 10, 70))
        entries = [
            e for e in self.service.state.scores.values() if e.contestant_id == "C1"
        ]
        self.assertEqual(len(entries), 4)
        by_component = {e.component: e for e in entries}
        self.assertEqual(by_component[Component.SAFETY].value, 10)
        self.assertTrue(all(e.state == ScoreState.REVIEWED for e in entries))
        # 总分 = 理论 + 实操 + 应急 - 安全违规扣分
        ranking = self.service.ranking(Trade.INSTALL_REPAIR)
        self.assertEqual(ranking[0]["总分"], 80 + 90 + 70 - 10)

    def test_recorder_cannot_review_own_entry(self) -> None:
        recorder, _ = panel(self.service, "C1")[:2]
        entry_id = self.service.record_score(
            recorder, "C1", Component.THEORY, 80, at()
        )
        with self.assertRaisesRegex(RuleViolation, "录分者不能复核自己的结果"):
            self.service.review_score(entry_id, recorder, at())

    def test_unassigned_judge_cannot_record(self) -> None:
        assigned = set(panel(self.service, "C1"))
        outsider = next(j for j in ("J1", "J2", "J3") if j not in assigned)
        with self.assertRaisesRegex(RuleViolation, "裁判未分配至该工位"):
            self.service.record_score(outsider, "C1", Component.THEORY, 80, at())

    def test_judge_tasks_hide_own_unit_contestants(self) -> None:
        for judge_id in ("J1", "J2", "J3"):
            affiliation = self.service.state.judges[judge_id].affiliation
            tasks = self.service.judge_tasks(judge_id)
            self.assertTrue(tasks)
            for task in tasks:
                contestant = self.service.state.contestants[task["contestant_id"]]
                self.assertNotEqual(contestant.team_id, affiliation)

    def test_duplicate_component_requires_correction_or_rerun(self) -> None:
        recorder, reviewer = panel(self.service, "C1")[:2]
        self.service.record_score(recorder, "C1", Component.THEORY, 80, at())
        with self.assertRaisesRegex(RuleViolation, "已录入"):
            self.service.record_score(recorder, "C1", Component.THEORY, 85, at())

    def test_score_bounds_are_enforced(self) -> None:
        recorder, _ = panel(self.service, "C1")[:2]
        with self.assertRaisesRegex(RuleViolation, "超出范围"):
            self.service.record_score(recorder, "C1", Component.THEORY, 120, at())

    def test_correction_keeps_original_value(self) -> None:
        recorder, reviewer = panel(self.service, "C1")[:2]
        entry_id = self.service.record_score(
            recorder, "C1", Component.THEORY, 80, at()
        )
        self.service.review_score(entry_id, reviewer, at())
        self.service.correct_score(entry_id, 82, "录分笔误更正", at())
        entry = self.service.state.scores[entry_id]
        self.assertEqual(entry.value, 82)
        self.assertEqual(entry.corrections[0].old, 80)
        self.assertEqual(entry.corrections[0].new, 82)
        audit = self.service.audit_report()
        record = next(s for s in audit["评分分项"] if s["编号"] == entry_id)
        self.assertEqual(record["更正历史"][0]["原值"], 80)


if __name__ == "__main__":
    unittest.main()
