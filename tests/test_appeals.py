"""申诉：冻结相关名次不阻塞无关奖项，并列处理与改判说明影响的候选人。"""

import tempfile
import unittest

from src.competition import AppealState, Component, RuleViolation, Trade
from tests.helpers import (
    at,
    close_and_schedule,
    full_scores,
    make_service,
    score_components,
)


class AppealTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.service = make_service(self.dir.name)
        close_and_schedule(self.service)
        # 安装检修：C3 最高(270)，C2 次之(235)，C1 最低(190)
        self.c1_entries = score_components(
            self.service, "C1", full_scores(70, 70, 10, 60)
        )
        self.c2_entries = score_components(
            self.service, "C2", full_scores(80, 80, 5, 80)
        )
        score_components(self.service, "C3", full_scores(90, 90, 0, 90))
        # 管网运行：三人同分
        for cid in ("D1", "D2", "D3"):
            score_components(self.service, cid, full_scores(80, 80, 0, 80))
        self.service.publish_results(Trade.INSTALL_REPAIR, at(30))
        self.service.publish_results(Trade.NETWORK_OPERATION, at(30))

    def test_appeal_window_is_enforced(self) -> None:
        with self.assertRaisesRegex(RuleViolation, "申诉期已过"):
            self.service.file_appeal("C1", "超时申诉", at(30 + 121))
        appeal_id = self.service.file_appeal("C1", "实操评分异议", at(60))
        self.assertEqual(
            self.service.state.appeals[appeal_id].state, AppealState.PENDING
        )

    def test_appeal_requires_published_results(self) -> None:
        service = make_service(tempfile.mkdtemp())
        close_and_schedule(service)
        with self.assertRaisesRegex(RuleViolation, "申诉期未开始"):
            service.file_appeal("C1", "尚未公布", at())

    def test_pending_appeal_freezes_own_trade_only(self) -> None:
        self.service.file_appeal("C1", "实操评分异议", at(60))
        self.assertTrue(self.service.award_frozen(Trade.INSTALL_REPAIR))
        self.assertFalse(self.service.award_frozen(Trade.NETWORK_OPERATION))
        with self.assertRaisesRegex(RuleViolation, "名次冻结"):
            self.service.finalize_awards(Trade.INSTALL_REPAIR, at(90))
        # 无关工种奖项照常终评
        self.service.finalize_awards(Trade.NETWORK_OPERATION, at(90))
        rows = self.service.public_transcript(Trade.NETWORK_OPERATION)
        self.assertEqual(len(rows), 3)

    def test_changed_judgment_reports_affected_candidates(self) -> None:
        appeal_id = self.service.file_appeal("C1", "理论与实操评分异议", at(60))
        # 改判：C1 理论 70 -> 100、实操 70 -> 95，总分 190 -> 245，反超 C2
        self.service.resolve_appeal(
            appeal_id, True, "复核后调整两项成绩", at(90),
            score_changes={
                self.c1_entries[Component.THEORY]: 100,
                self.c1_entries[Component.PRACTICAL]: 95,
            },
        )
        appeal = self.service.state.appeals[appeal_id]
        self.assertEqual(appeal.state, AppealState.UPHELD)
        # 影响面：C1 升至第二，C2 降至第三，C3 不受影响
        self.assertEqual(set(appeal.affected_candidates), {"C1", "C2"})
        standing = {
            row["选手"]: row["名次"]
            for row in self.service.ranking(Trade.INSTALL_REPAIR)
        }
        self.assertEqual(standing["C1"], 2)
        self.assertEqual(standing["C2"], 3)
        # 改判留痕
        entry = self.service.state.scores[self.c1_entries[Component.THEORY]]
        self.assertEqual(entry.corrections[-1].old, 70)
        self.assertIn("申诉改判", entry.corrections[-1].reason)

    def test_dismissed_appeal_affects_nobody(self) -> None:
        appeal_id = self.service.file_appeal("C1", "无理由异议", at(60))
        self.service.resolve_appeal(appeal_id, False, "维持原评分", at(90))
        appeal = self.service.state.appeals[appeal_id]
        self.assertEqual(appeal.state, AppealState.DISMISSED)
        self.assertEqual(appeal.affected_candidates, ())
        self.service.finalize_awards(Trade.INSTALL_REPAIR, at(100))

    def test_tie_handling_reports_affected_candidates(self) -> None:
        self.service.finalize_awards(Trade.NETWORK_OPERATION, at(90))
        finalization = self.service.state.awards[Trade.NETWORK_OPERATION]
        # 三人同分，一等奖并列扩额
        self.assertEqual(
            set(finalization.affected_candidates), {"D1", "D2", "D3"}
        )
        self.assertEqual(len(finalization.ties), 1)
        rows = self.service.public_transcript(Trade.NETWORK_OPERATION)
        self.assertTrue(all(row["奖项"] == "一等奖" for row in rows))
        self.assertTrue(all(row["名次"] == 1 for row in rows))

    def test_no_change_after_finalization(self) -> None:
        self.service.finalize_awards(Trade.INSTALL_REPAIR, at(90))
        appeal_id = self.service.file_appeal("C1", "终评后异议", at(100))
        with self.assertRaisesRegex(RuleViolation, "奖项已终评"):
            self.service.resolve_appeal(
                appeal_id, True, "尝试改判", at(110),
                score_changes={self.c1_entries[Component.THEORY]: 100},
            )


if __name__ == "__main__":
    unittest.main()
