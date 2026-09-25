"""申诉冻结、改判留痕与并列处理。"""

import tempfile
import unittest
from pathlib import Path

try:
    import support
except ImportError:  # pragma: no cover
    from tests import support

from src.competition import AppealStatus, CompetitionError


class AppealTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "events.jsonl"
        self.service = support.build_service(self.path)
        support.score_all(self.service)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _awards(self) -> dict:
        return self.service.public_results()["trades"][support.TRADE]

    def test_appeal_freezes_related_ranks_but_not_unrelated_awards(self) -> None:
        # 名次：C1(100)一等奖，C2(90)、C3(80)二等奖
        appeal_id = self.service.file_appeal("C2", "实际操作计时有误")
        self.assertEqual(self.service.appeals[appeal_id].status, AppealStatus.PENDING)
        result = self._awards()
        published = {(a["award"], a["rank"]): a["contestant"] for a in result["awards"]}
        self.assertEqual(published[("一等奖", 1)], "选手01")  # 无关奖项照常发布
        self.assertEqual(published[("二等奖", 3)], "选手03")
        self.assertNotIn(("二等奖", 2), published)  # 相关名次冻结
        self.assertIn(
            {"award": "二等奖", "rank": 2, "reason": "appeal_pending"}, result["withheld"]
        )

    def test_upheld_appeal_requires_affected_candidates(self) -> None:
        appeal_id = self.service.file_appeal("C2", "评分异议")
        with self.assertRaisesRegex(CompetitionError, "影响了哪些候选人"):
            self.service.rule_appeal(appeal_id, True, "经复核确属误判", [])

    def test_adjustment_keeps_original_score_as_revision(self) -> None:
        appeal_id = self.service.file_appeal("C2", "理论题漏改")
        self.service.rule_appeal(
            appeal_id,
            True,
            "理论题第12题漏判，补回15分",
            ["C2"],
            adjustments={
                "C2": {"theory": 105, "practical": 0, "safety_deduction": 0, "emergency": 0}
            },
        )
        audit = self.service.audit_report("C2")
        revisions = audit["scores"][0]["revisions"]
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0]["components"]["theory"], 90)  # 原件保留
        self.assertEqual(revisions[0]["source"], appeal_id)
        # 改判后C2升至第一，冻结解除，奖项正常发布
        result = self._awards()
        published = {(a["award"], a["rank"]): a["contestant"] for a in result["awards"]}
        self.assertEqual(published[("一等奖", 1)], "选手02")
        self.assertEqual(result["withheld"], [])

    def test_adjustment_target_must_be_listed_as_affected(self) -> None:
        appeal_id = self.service.file_appeal("C2", "评分异议")
        with self.assertRaisesRegex(CompetitionError, "未列入影响候选人"):
            self.service.rule_appeal(
                appeal_id,
                True,
                "改判",
                ["C2"],
                adjustments={
                    "C3": {"theory": 1, "practical": 0, "safety_deduction": 0, "emergency": 0}
                },
            )

    def test_rejected_appeal_unfreezes_ranks(self) -> None:
        appeal_id = self.service.file_appeal("C2", "评分异议")
        self.service.rule_appeal(appeal_id, False, "复核无误，维持原判", [])
        result = self._awards()
        published = {(a["award"], a["rank"]): a["contestant"] for a in result["awards"]}
        self.assertEqual(published[("二等奖", 2)], "选手02")

    def test_unresolved_tie_blocks_straddled_award_tier(self) -> None:
        path = Path(self._tmp.name) / "tie.jsonl"
        service = support.build_service(path)
        support.score_all(service, {"C1": 100, "C2": 90, "C3": 90, "C4": 80, "C5": 70, "C6": 60})
        service.configure_awards(support.TRADE, {"一等奖": 1, "二等奖": 1})
        result = service.public_results()["trades"][support.TRADE]
        # C2、C3并列第二，横跨二等奖边界，该档挂起
        self.assertIn({"award": "二等奖", "reason": "tie_unresolved"}, result["withheld"])
        published = {(a["award"], a["rank"]): a["contestant"] for a in result["awards"]}
        self.assertEqual(published[("一等奖", 1)], "选手01")
        # 并列处理必须说明影响的候选人，处理后按指定次序排名
        service.resolve_tie(support.TRADE, ["C3", "C2"], "应急处置环节用时较短者列前")
        result = service.public_results()["trades"][support.TRADE]
        published = {(a["award"], a["rank"]): a["contestant"] for a in result["awards"]}
        self.assertEqual(published[("二等奖", 2)], "选手03")

    def test_tie_resolution_validates_candidates(self) -> None:
        with self.assertRaisesRegex(CompetitionError, "并不并列"):
            self.service.resolve_tie(support.TRADE, ["C2", "C3"], "测试")
        with self.assertRaisesRegex(CompetitionError, "必须说明理由"):
            self.service.resolve_tie(support.TRADE, ["C2", "C3"], "")

    def test_tie_resolution_must_cover_whole_group(self) -> None:
        path = Path(self._tmp.name) / "tie3.jsonl"
        service = support.build_service(path)
        support.score_all(service, {"C1": 100, "C2": 90, "C3": 90, "C4": 90, "C5": 70, "C6": 60})
        with self.assertRaisesRegex(CompetitionError, "全部候选人"):
            service.resolve_tie(support.TRADE, ["C2", "C3"], "只处理两人")


if __name__ == "__main__":
    unittest.main()
