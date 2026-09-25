"""报名冻结、伤病替补与资格更正的约束。"""

import tempfile
import unittest
from pathlib import Path

try:
    import support
except ImportError:  # pragma: no cover
    from tests import support

from src.competition import CompetitionError, CompetitionService, ContestantStatus, Trade


class RegistrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "events.jsonl"
        self.service = support.build_service(self.path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_freeze_locks_registration_and_quotas(self) -> None:
        with self.assertRaisesRegex(CompetitionError, "报名截止后不得新增选手"):
            self.service.register_contestant(
                "C9", "选手09", "T1", support.TRADE, dict(support.QUALIFICATIONS), ["S1"]
            )
        with self.assertRaisesRegex(CompetitionError, "报名截止后不得新增代表队"):
            self.service.register_team("T9", "代表队9", {support.TRADE: 1})
        snapshot = self.service.freeze_snapshot
        self.assertEqual(snapshot["quotas"]["T1"][support.TRADE], 2)
        self.assertEqual(
            snapshot["qualifications"]["C1"], support.QUALIFICATIONS
        )

    def test_quota_is_enforced_before_freeze(self) -> None:
        path = Path(self._tmp.name) / "quota.jsonl"
        service = CompetitionService.open(path)
        service.register_team("TA", "代表队A", {support.TRADE: 1})
        service.register_contestant(
            "CA1", "选手A1", "TA", support.TRADE, dict(support.QUALIFICATIONS), ["S1"]
        )
        with self.assertRaisesRegex(CompetitionError, "名额已用完"):
            service.register_contestant(
                "CA2", "选手A2", "TA", support.TRADE, dict(support.QUALIFICATIONS), ["S1"]
            )

    def test_substitute_reuses_slot_and_keeps_original_record(self) -> None:
        self.service.substitute_contestant(
            "C1",
            "C1B",
            "选手01替",
            dict(support.QUALIFICATIONS),
            reason="赛前训练手臂受伤",
            region_note="赛区医疗组确认，准予替补",
        )
        replaced = self.service.contestants["C1"]
        substitute = self.service.contestants["C1B"]
        self.assertEqual(replaced.status, ContestantStatus.REPLACED)
        self.assertEqual(replaced.replaced_by, "C1B")
        self.assertEqual(substitute.slot_id, replaced.slot_id)  # 沿用原名额
        audit = self.service.audit_report("C1B")
        self.assertEqual(audit["substitution"]["replaced_id"], "C1")
        self.assertEqual(audit["substitution"]["reason"], "赛前训练手臂受伤")
        self.assertEqual(audit["substitution"]["region_note"], "赛区医疗组确认，准予替补")
        # 被替换者的原始资格材料仍然可查
        original = self.service.audit_report("C1")
        self.assertEqual(original["qualifications"]["original"], support.QUALIFICATIONS)

    def test_substitute_requires_reason_and_region_note(self) -> None:
        with self.assertRaisesRegex(CompetitionError, "伤病原因"):
            self.service.substitute_contestant(
                "C1", "C1B", "选手01替", dict(support.QUALIFICATIONS), "", "赛区说明"
            )
        with self.assertRaisesRegex(CompetitionError, "赛区"):
            self.service.substitute_contestant(
                "C1", "C1B", "选手01替", dict(support.QUALIFICATIONS), "伤病", " "
            )

    def test_correction_is_append_only(self) -> None:
        self.service.correct_qualification(
            "C2", {"work_experience": "一线岗位四年"}, "岗位经历证明补章"
        )
        audit = self.service.audit_report("C2")
        self.assertEqual(
            audit["qualifications"]["original"], support.QUALIFICATIONS  # 最初材料未抹掉
        )
        self.assertEqual(audit["qualifications"]["current"]["work_experience"], "一线岗位四年")
        self.assertEqual(len(audit["qualifications"]["corrections"]), 1)
        self.assertEqual(
            audit["qualifications"]["corrections"][0]["reason"], "岗位经历证明补章"
        )
        # 冻结快照同样保留最初材料
        self.assertEqual(
            self.service.freeze_snapshot["qualifications"]["C2"], support.QUALIFICATIONS
        )

    def test_correction_rejects_unknown_or_empty_content(self) -> None:
        with self.assertRaisesRegex(CompetitionError, "未知资格材料项"):
            self.service.correct_qualification("C2", {"unknown": "x"}, "原因")
        with self.assertRaisesRegex(CompetitionError, "更正必须说明原因"):
            self.service.correct_qualification("C2", {"work_experience": "五年"}, "")


if __name__ == "__main__":
    unittest.main()
