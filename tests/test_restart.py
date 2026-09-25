"""断电重启：签到、封存赛题、设备停用、待审申诉按原顺序恢复。"""

import tempfile
import unittest

from src.competition import (
    AppealState,
    CompetitionService,
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
    score_components,
)


class RestartTest(unittest.TestCase):
    def test_state_survives_restart_in_original_order(self) -> None:
        data_dir = tempfile.mkdtemp()
        service = make_service(data_dir)
        close_and_schedule(service)
        # 现场签到（按到达顺序）
        for cid in ("C2", "C1", "D3", "D1"):
            service.check_in(cid, at())
        # 设备停用与待审申诉
        entries = score_components(service, "C1", full_scores(80, 90, 10, 70))
        service.report_equipment_failure("S1", "终评现场设备故障", at())
        score_components(service, "D1", full_scores(70, 80, 5, 60))
        service.publish_results(Trade.NETWORK_OPERATION, at(30))
        appeal_id = service.file_appeal("D1", "安全违规扣分异议", at(60))
        assignment_order = [a.id for a in service.state.assignments]
        event_count = len(service.journal.events())

        # 断电重启
        reopened = CompetitionService.open(data_dir)
        state = reopened.state
        self.assertEqual(len(reopened.journal.events()), event_count)
        # 签到按原顺序存在
        self.assertEqual(
            [c["contestant_id"] for c in state.check_ins],
            ["C2", "C1", "D3", "D1"],
        )
        # 封存赛题保持封存
        self.assertTrue(all(p.sealed for p in state.packages.values()))
        # 设备停用与待裁定成绩保持
        self.assertEqual(
            state.stations["S1"].status, EquipmentStatus.OUT_OF_SERVICE
        )
        for entry_id in entries.values():
            self.assertEqual(state.scores[entry_id].state, ScoreState.PENDING)
        # 待审申诉保持
        self.assertEqual(state.appeals[appeal_id].state, AppealState.PENDING)
        # 轮次顺序保持
        self.assertEqual([a.id for a in state.assignments], assignment_order)
        # 重启后业务可继续：裁定与申诉处理照常落盘
        reopened.adjudicate("赛事负责人", "S1", Decision.KEEP, "成绩有效", at(90))
        reopened.resolve_appeal(appeal_id, False, "维持原判", at(100))
        again = CompetitionService.open(data_dir)
        self.assertEqual(
            again.state.appeals[appeal_id].state, AppealState.DISMISSED
        )
        self.assertEqual(
            again.state.scores[entries[Component.THEORY]].state,
            ScoreState.CONFIRMED,
        )

    def test_open_empty_directory_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuleViolation, "没有赛事记录"):
            CompetitionService.open(tempfile.mkdtemp())


if __name__ == "__main__":
    unittest.main()
