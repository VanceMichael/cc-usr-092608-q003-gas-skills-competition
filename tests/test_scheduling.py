"""排程、裁判回避与评分任务下发的约束。"""

import tempfile
import unittest
from pathlib import Path

try:
    import support
except ImportError:  # pragma: no cover
    from tests import support

from src.competition import (
    CompetitionError,
    CompetitionService,
    RecusalError,
    SchedulingError,
    Trade,
)


class SchedulingTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "events.jsonl"
        self.service = support.build_service(self.path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_schedule_never_assigns_recused_judge(self) -> None:
        # 裁判甲隶属T1，不得出现在T1选手（C1、C2）的出场安排中
        for contestant_id in ("C1", "C2"):
            for assignment in self.service.contestant_assignments(contestant_id):
                self.assertNotEqual(assignment.judge_id, "J1")
        tasks = self.service.judge_task_list("J1")
        self.assertTrue(tasks)
        for task in tasks:
            contestant = self.service.contestants[task["contestant_id"]]
            self.assertNotEqual(contestant.team_id, "T1")

    def test_declared_recusal_hides_tasks_and_blocks_scoring(self) -> None:
        # C3的排定裁判是J1；补声明J1与T2的回避关系后，任务消失且录分被拒
        assignment = self.service.contestant_assignments("C3")[0]
        self.assertEqual(assignment.judge_id, "J1")
        self.service.declare_recusal("J1", "T2", "裁判甲与T2存在亲属关系")
        task_ids = [t["assignment_id"] for t in self.service.judge_task_list("J1")]
        self.assertNotIn(assignment.assignment_id, task_ids)
        with self.assertRaises(RecusalError):
            self.service.record_score(
                assignment.assignment_id,
                "J1",
                {"theory": 80, "practical": 0, "safety_deduction": 0, "emergency": 0},
                "clerk-1",
            )
        # 更换为无回避关系的机动裁判后可以录分
        self.service.reassign_judge(assignment.assignment_id, "J4", "回避关系补声明")
        self.service.record_score(
            assignment.assignment_id,
            "J4",
            {"theory": 80, "practical": 0, "safety_deduction": 0, "emergency": 0},
            "clerk-1",
        )

    def test_uncalibrated_or_disabled_station_is_not_scheduled(self) -> None:
        path = Path(self._tmp.name) / "sparse.jsonl"
        service = CompetitionService.open(path)
        service.register_team("TA", "代表队A", {support.TRADE: 2})
        for cid, name in (("CA1", "选手A1"), ("CA2", "选手A2")):
            service.register_contestant(
                cid, name, "TA", support.TRADE, dict(support.QUALIFICATIONS), ["S1", "S2"]
            )
        service.register_judge("JA", "裁判A", "裁判组", [support.TRADE])
        service.register_station("ZA", support.TRADE, calibrated=False)  # 未校准
        service.register_station("ZB", support.TRADE)
        service.disable_station("ZB", "临时停用")  # 已停用
        service.register_station("ZC", support.TRADE)
        service.freeze_registration()
        plan = service.generate_schedule(["S1", "S2"])
        used = {a["station_id"] for a in plan["assignments"]}
        self.assertEqual(used, {"ZC"})

    def test_infeasible_schedule_raises_without_partial_plan(self) -> None:
        path = Path(self._tmp.name) / "infeasible.jsonl"
        service = CompetitionService.open(path)
        service.register_team("TA", "代表队A", {support.TRADE: 2})
        for cid, name in (("CA1", "选手A1"), ("CA2", "选手A2")):
            service.register_contestant(
                cid, name, "TA", support.TRADE, dict(support.QUALIFICATIONS), ["S1"]
            )
        service.register_judge("JA", "裁判A", "裁判组", [support.TRADE])
        service.register_station("ZA", support.TRADE)
        service.freeze_registration()
        with self.assertRaises(SchedulingError):
            service.generate_schedule(["S1"])  # 一个时段一个工位排不下两人
        self.assertEqual(service.rounds, {})  # 不产生半个赛程

    def test_schedule_cannot_be_regenerated(self) -> None:
        with self.assertRaisesRegex(CompetitionError, "赛程已生成"):
            self.service.generate_schedule(["S1", "S2", "S3"])

    def test_reassigned_judge_must_be_free_and_unrelated(self) -> None:
        assignment = self.service.contestant_assignments("C3")[0]
        with self.assertRaises(SchedulingError):
            # J2在S1时段已有任务
            self.service.reassign_judge(assignment.assignment_id, "J2", "测试")
        with self.assertRaises(RecusalError):
            self.service.declare_recusal("J4", "T2", "利益关系")
            self.service.reassign_judge(assignment.assignment_id, "J4", "测试")


if __name__ == "__main__":
    unittest.main()
