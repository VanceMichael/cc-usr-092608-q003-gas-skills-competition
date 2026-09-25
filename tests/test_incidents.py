"""设备故障：仅关联工位成绩待裁定，负责人逐工位裁定，不得批量清空整轮数据。"""

import tempfile
import unittest

from src.competition import (
    AssignmentStatus,
    Component,
    Decision,
    EquipmentStatus,
    RuleViolation,
    ScoreState,
    Trade,
)
from tests.helpers import (
    at,
    close_and_schedule,
    full_scores,
    make_service,
    panel,
    score_components,
)


class IncidentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.service = make_service(self.dir.name)
        close_and_schedule(self.service)

    def test_failure_only_pending_related_station(self) -> None:
        c1_entries = score_components(self.service, "C1", full_scores(80, 90, 10, 70))
        d1_entries = score_components(self.service, "D1", full_scores(70, 80, 5, 60))
        self.service.report_equipment_failure("S1", "供气模拟装置故障", at())
        state = self.service.state
        self.assertEqual(state.stations["S1"].status, EquipmentStatus.OUT_OF_SERVICE)
        # 关联工位成绩进入待裁定
        for entry_id in c1_entries.values():
            self.assertEqual(state.scores[entry_id].state, ScoreState.PENDING)
        # 无关工位成绩与轮次不受影响
        for entry_id in d1_entries.values():
            self.assertEqual(state.scores[entry_id].state, ScoreState.REVIEWED)
        blocked = {
            a.station_id: a.status
            for a in state.assignments
            if a.contestant_id in ("C1", "D1")
        }
        self.assertEqual(blocked["S1"], AssignmentStatus.BLOCKED)
        self.assertEqual(blocked["S2"], AssignmentStatus.SCHEDULED)

    def test_keep_decision_confirms_scores_without_clearing_round(self) -> None:
        entries = score_components(self.service, "C1", full_scores(80, 90, 10, 70))
        before = dict(self.service.state.scores)
        self.service.report_equipment_failure("S1", "故障", at())
        self.service.adjudicate("赛事负责人", "S1", Decision.KEEP, "成绩有效", at())
        state = self.service.state
        for entry_id in entries.values():
            self.assertEqual(state.scores[entry_id].state, ScoreState.CONFIRMED)
        # 整轮数据未被清空
        self.assertEqual(set(state.scores), set(before))
        assignment = next(
            a for a in state.assignments if a.contestant_id == "C1"
        )
        self.assertEqual(assignment.status, AssignmentStatus.COMPLETED)

    def test_rerun_supersedes_and_records_new_attempt(self) -> None:
        entries = score_components(self.service, "C1", full_scores(80, 90, 10, 70))
        self.service.report_equipment_failure("S1", "故障", at())
        self.service.adjudicate("赛事负责人", "S1", Decision.RERUN, "重新操作", at())
        state = self.service.state
        for entry_id in entries.values():
            self.assertEqual(state.scores[entry_id].state, ScoreState.SUPERSEDED)
        recorder, reviewer = panel(self.service, "C1")[:2]
        new_id = self.service.record_score(
            recorder, "C1", Component.PRACTICAL, 95, at()
        )
        self.service.review_score(new_id, reviewer, at())
        self.assertEqual(state.scores[new_id].attempt, 2)
        # 原成绩仍保留可审计
        self.assertIn(entries[Component.PRACTICAL], state.scores)
        ranking = self.service.ranking(Trade.INSTALL_REPAIR)
        self.assertEqual(ranking[0]["总分"], 95)

    def test_continue_restores_previous_state(self) -> None:
        entries = score_components(self.service, "C1", full_scores(80, 90, 10, 70))
        self.service.report_equipment_failure("S1", "故障", at())
        self.service.adjudicate("赛事负责人", "S1", Decision.CONTINUE, "排除故障后续赛", at())
        state = self.service.state
        for entry_id in entries.values():
            self.assertEqual(state.scores[entry_id].state, ScoreState.REVIEWED)
        assignment = next(
            a for a in state.assignments if a.contestant_id == "C1"
        )
        self.assertEqual(assignment.status, AssignmentStatus.SCHEDULED)

    def test_adjudicate_requires_pending_scores(self) -> None:
        with self.assertRaisesRegex(RuleViolation, "无待裁定成绩"):
            self.service.adjudicate("赛事负责人", "S1", Decision.KEEP, "无理由", at())

    def test_publish_blocked_while_pending(self) -> None:
        score_components(self.service, "C1", full_scores(80, 90, 10, 70))
        self.service.report_equipment_failure("S1", "故障", at())
        with self.assertRaisesRegex(RuleViolation, "待裁定"):
            self.service.publish_results(Trade.INSTALL_REPAIR, at())


if __name__ == "__main__":
    unittest.main()
