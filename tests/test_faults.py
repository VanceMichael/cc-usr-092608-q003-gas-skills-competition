"""设备故障的局部化影响与赛事负责人逐条裁定。"""

import tempfile
import unittest
from pathlib import Path

try:
    import support
except ImportError:  # pragma: no cover
    from tests import support

from src.competition import AdjudicationDecision, CompetitionError, ScoreStatus


class EquipmentFaultTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "events.jsonl"
        self.service = support.build_service(self.path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _assignment_of(self, contestant_id: str):
        return self.service.contestant_assignments(contestant_id)[0]

    def test_fault_only_suspends_scores_at_that_station(self) -> None:
        support.score_all(self.service)
        # Z1工位承载C1与C4的出场
        affected = {
            a.contestant_id
            for a in self.service.assignments.values()
            if a.station_id == "Z1"
        }
        self.assertEqual(affected, {"C1", "C4"})
        fault_id = self.service.report_equipment_fault("Z1", "燃气检测仪漂移")
        for contestant_id in ("C1", "C4"):
            assignment = self._assignment_of(contestant_id)
            self.assertEqual(
                self.service.scores[assignment.assignment_id].status,
                ScoreStatus.PENDING_ADJUDICATION,
            )
        # 无关工位的成绩不受牵连，整轮数据不被清空
        for contestant_id in ("C2", "C3", "C5", "C6"):
            assignment = self._assignment_of(contestant_id)
            self.assertEqual(
                self.service.scores[assignment.assignment_id].status, ScoreStatus.VERIFIED
            )
        self.assertFalse(self.service.stations["Z1"].active)
        self.assertTrue(self.service.faults[fault_id].open)

    def test_fault_blocks_new_recording_until_decided(self) -> None:
        assignment = self._assignment_of("C1")
        self.service.report_equipment_fault("Z1", "工位停电")
        with self.assertRaisesRegex(CompetitionError, "待裁定"):
            self.service.record_score(
                assignment.assignment_id,
                assignment.judge_id,
                {"theory": 1, "practical": 1, "safety_deduction": 0, "emergency": 1},
                "clerk-1",
            )

    def test_resume_restores_previous_status(self) -> None:
        assignment = self._assignment_of("C1")
        self.service.record_score(
            assignment.assignment_id,
            assignment.judge_id,
            {"theory": 88, "practical": 0, "safety_deduction": 0, "emergency": 0},
            "clerk-1",
        )
        self.service.report_equipment_fault("Z1", "阀门卡涩")
        score = self.service.scores[assignment.assignment_id]
        self.assertEqual(score.status, ScoreStatus.PENDING_ADJUDICATION)
        self.service.decide_adjudication(
            assignment.assignment_id, AdjudicationDecision.RESUME, "故障排除，继续比赛", "赛事负责人甲"
        )
        self.assertEqual(score.status, ScoreStatus.RECORDED)  # 恢复到处置前状态
        self.service.verify_score(assignment.assignment_id, "clerk-2")

    def test_keep_finalizes_existing_score(self) -> None:
        support.score_all(self.service)
        assignment = self._assignment_of("C4")
        self.service.report_equipment_fault("Z1", "气源波动")
        self.service.decide_adjudication(
            assignment.assignment_id, AdjudicationDecision.KEEP, "故障发生在成绩确认之后", "赛事负责人甲"
        )
        self.assertEqual(
            self.service.scores[assignment.assignment_id].status, ScoreStatus.VERIFIED
        )

    def test_rematch_supersedes_score_and_creates_makeup_round(self) -> None:
        support.score_all(self.service)
        assignment = self._assignment_of("C1")
        old_assignment_id = assignment.assignment_id
        self.service.report_equipment_fault("Z1", "管路泄漏")
        self.service.decide_adjudication(
            old_assignment_id,
            AdjudicationDecision.REMATCH,
            "故障影响操作过程，安排重赛",
            "赛事负责人甲",
            slot="S3",
        )
        # 原成绩封存保留，不参与排名
        old_score = self.service.scores[old_assignment_id]
        self.assertEqual(old_score.status, ScoreStatus.SUPERSEDED)
        self.assertTrue(self.service.assignments[old_assignment_id].superseded)
        # 补赛安排落在S3时段，且避开回避关系
        makeup = self.service.contestant_assignments("C1")
        self.assertEqual(len(makeup), 2)
        new_assignment = next(a for a in makeup if not a.superseded)
        self.assertEqual(new_assignment.slot, "S3")
        self.assertNotEqual(new_assignment.judge_id, "J1")  # J1与T1回避
        # 重赛成绩重新录入并参与排名
        self.service.record_score(
            new_assignment.assignment_id,
            new_assignment.judge_id,
            {"theory": 95, "practical": 0, "safety_deduction": 0, "emergency": 0},
            "clerk-1",
        )
        self.service.verify_score(new_assignment.assignment_id, "clerk-2")
        totals = self.service._verified_totals(support.TRADE)
        self.assertEqual(totals["C1"], 95)
        # 审计仍能看到被封存的原成绩
        audit = self.service.audit_report("C1")
        self.assertEqual(len(audit["scores"]), 2)
        self.assertEqual(len(audit["adjudications"]), 1)
        self.assertEqual(audit["adjudications"][0]["decision"], "rematch")

    def test_fault_closes_only_when_all_affected_decided(self) -> None:
        support.score_all(self.service)
        fault_id = self.service.report_equipment_fault("Z1", "台架松动")
        self.service.decide_adjudication(
            self._assignment_of("C1").assignment_id,
            AdjudicationDecision.KEEP,
            "成绩有效",
            "赛事负责人甲",
        )
        self.assertTrue(self.service.faults[fault_id].open)  # C4尚未裁定
        self.service.decide_adjudication(
            self._assignment_of("C4").assignment_id,
            AdjudicationDecision.KEEP,
            "成绩有效",
            "赛事负责人甲",
        )
        self.assertFalse(self.service.faults[fault_id].open)
        self.service.restore_station("Z1")
        self.assertTrue(self.service.stations["Z1"].active)

    def test_decision_requires_rationale_and_director(self) -> None:
        support.score_all(self.service)
        assignment = self._assignment_of("C1")
        self.service.report_equipment_fault("Z1", "仪表故障")
        with self.assertRaisesRegex(CompetitionError, "理由"):
            self.service.decide_adjudication(
                assignment.assignment_id, AdjudicationDecision.KEEP, "", "赛事负责人甲"
            )
        with self.assertRaisesRegex(CompetitionError, "负责人"):
            self.service.decide_adjudication(
                assignment.assignment_id, AdjudicationDecision.KEEP, "理由", ""
            )


if __name__ == "__main__":
    unittest.main()
