"""排程：按工种、设备校准状态、选手时段与裁判回避排出可执行轮次。"""

import tempfile
import unittest

from src.competition import CompetitionService, EquipmentStatus, RuleViolation, Trade
from tests.helpers import at, make_service, qualification


class SchedulingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.service = make_service(self.dir.name)

    def test_rounds_respect_trade_slots_and_recusal(self) -> None:
        self.service.close_registration(at())
        result = self.service.schedule(at())
        self.assertEqual(result["scheduled"], 6)
        self.assertEqual(result["unscheduled"], [])
        state = self.service.state
        for assignment in state.assignments:
            contestant = state.contestants[assignment.contestant_id]
            station = state.stations[assignment.station_id]
            # 工种匹配、时段在选手可用范围内
            self.assertEqual(station.trade, contestant.trade)
            self.assertIn(assignment.round_no, contestant.slots)
            # 裁判不与选手同单位
            for judge_id in assignment.judge_ids:
                judge = state.judges[judge_id]
                self.assertNotEqual(judge.affiliation, contestant.team_id)
                self.assertIn(contestant.trade, judge.trades)
        # 同单位回避已记录
        pairs = {(r.judge_id, r.contestant_id) for r in state.schedule_recusals}
        self.assertIn(("J1", "C1"), pairs)
        self.assertIn(("J2", "D2"), pairs)

    def test_out_of_service_station_is_not_used(self) -> None:
        self.service.register_station(
            "S9", Trade.INSTALL_REPAIR, EquipmentStatus.OUT_OF_SERVICE
        )
        self.service.close_registration(at())
        self.service.schedule(at())
        used = {a.station_id for a in self.service.state.assignments}
        self.assertNotIn("S9", used)

    def test_declared_recusal_is_respected(self) -> None:
        # 增加一名外聘裁判，保证回避后仍能组成裁判组
        self.service.register_judge(
            "J4", "裁判四", "外聘中心", [Trade.INSTALL_REPAIR, Trade.NETWORK_OPERATION]
        )
        self.service.declare_recusal("J2", "C1", "亲属关系申报", at())
        self.service.close_registration(at())
        self.service.schedule(at())
        assignment = next(
            a for a in self.service.state.assignments if a.contestant_id == "C1"
        )
        self.assertNotIn("J2", assignment.judge_ids)
        declared = self.service.state.declared_recusals[0]
        self.assertEqual((declared.judge_id, declared.contestant_id), ("J2", "C1"))

    def test_contestant_without_full_panel_stays_unscheduled(self) -> None:
        service = CompetitionService.create(tempfile.mkdtemp())
        service.register_team("T1", "甲队", {Trade.INSTALL_REPAIR: 1})
        service.register_team("T2", "乙队", {Trade.INSTALL_REPAIR: 1})
        service.register_contestant(
            "C1", "选手一", "T1", Trade.INSTALL_REPAIR, qualification("C1"), [1]
        )
        service.register_judge("J1", "裁判一", "T1", [Trade.INSTALL_REPAIR])
        service.register_judge("J2", "裁判二", "T2", [Trade.INSTALL_REPAIR])
        service.register_station("S1", Trade.INSTALL_REPAIR)
        service.close_registration(at())
        result = service.schedule(at())
        # 两名裁判中一人须回避，凑不齐裁判组，选手无法安排
        self.assertEqual(result["unscheduled"], ["C1"])

    def test_schedule_requires_closed_registration(self) -> None:
        with self.assertRaisesRegex(RuleViolation, "报名未截止"):
            self.service.schedule(at())


if __name__ == "__main__":
    unittest.main()
