"""对外成绩单只呈现获奖所需信息，内部审计还原全部过程。"""

import tempfile
import unittest

from src.competition import Component, Decision, RuleViolation, Trade
from tests.helpers import (
    at,
    close_and_schedule,
    full_scores,
    make_service,
    score_components,
)


class ReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.service = make_service(self.dir.name)
        close_and_schedule(self.service)
        score_components(self.service, "C1", full_scores(90, 95, 0, 90))
        score_components(self.service, "C2", full_scores(80, 85, 5, 80))
        score_components(self.service, "C3", full_scores(70, 75, 10, 70))
        score_components(self.service, "D1", full_scores(88, 88, 0, 88))
        self.service.publish_results(Trade.INSTALL_REPAIR, at(30))
        self.service.finalize_awards(Trade.INSTALL_REPAIR, at(40))

    def test_public_transcript_only_contains_award_essentials(self) -> None:
        rows = self.service.public_transcript(Trade.INSTALL_REPAIR)
        self.assertEqual(
            [row["奖项"] for row in rows], ["一等奖", "二等奖", "二等奖"]
        )
        self.assertEqual(rows[0]["选手"], "选手一")
        self.assertEqual(rows[0]["代表队"], "甲队")
        for row in rows:
            self.assertEqual(set(row), {"工种", "奖项", "名次", "选手", "代表队"})

    def test_public_transcript_requires_finalization(self) -> None:
        with self.assertRaisesRegex(RuleViolation, "未终评"):
            self.service.public_transcript(Trade.NETWORK_OPERATION)

    def test_audit_report_reconstructs_full_process(self) -> None:
        self.service.correct_qualification(
            "C1", "training", "培训优秀", "证书复核更正", at(50)
        )
        self.service.declare_recusal("J1", "D2", "既往共事申报", at(50))
        self.service.report_equipment_failure("S2", "管网模拟台故障", at(60))
        self.service.adjudicate("赛事负责人", "S2", Decision.KEEP, "成绩有效", at(70))
        audit = self.service.audit_report()
        # 选手资格与更正历史
        c1 = next(q for q in audit["选手资格"] if q["选手"] == "C1")
        self.assertEqual(c1["资格现状"]["training"], "培训优秀")
        self.assertEqual(c1["更正历史"][0]["原值"], "培训合格C1")
        # 工位安排
        self.assertEqual(len(audit["工位安排"]), 6)
        # 评分分项：录分人与复核人分离
        entry = next(
            s for s in audit["评分分项"]
            if s["选手"] == "C1" and s["分项"] == Component.THEORY.value
        )
        self.assertEqual(entry["分值"], 90)
        self.assertNotEqual(entry["录分人"], entry["复核人"])
        # 回避记录：排程推导 + 申报
        sources = {(r["裁判"], r["选手"], r["来源"]) for r in audit["回避记录"]}
        self.assertIn(("J1", "C1", "排程"), sources)
        self.assertIn(("J1", "D2", "申报"), sources)
        # 每次裁定
        self.assertEqual(audit["裁定"][0]["决定"], Decision.KEEP.value)
        self.assertEqual(audit["裁定"][0]["负责人"], "赛事负责人")
        self.assertTrue(audit["裁定"][0]["涉及成绩"])
        # 名额与奖项
        self.assertEqual(len(audit["名额与替补"]["名额"]), 6)
        self.assertIn(Trade.INSTALL_REPAIR.value, audit["奖项"])


if __name__ == "__main__":
    unittest.main()
