"""报名截止固定名额与资格、伤病替补沿用原名额、更正不抹掉最初材料。"""

import tempfile
import unittest

from src.competition import CompetitionService, RuleViolation, Trade
from tests.helpers import at, make_service, qualification


class RegistrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.service = make_service(self.dir.name)

    def test_close_registration_fixes_quota_and_qualification(self) -> None:
        self.service.close_registration(at())
        slots = self.service.state.slots
        self.assertEqual(len(slots), 6)
        self.assertEqual(slots["T1-IR-1"].contestant_id, "C1")
        self.assertEqual(self.service.state.contestants["C1"].slot_id, "T1-IR-1")
        with self.assertRaisesRegex(RuleViolation, "报名已截止"):
            self.service.register_contestant(
                "C9", "选手九", "T1", Trade.INSTALL_REPAIR, qualification("C9"), [1]
            )
        with self.assertRaisesRegex(RuleViolation, "报名已截止"):
            self.service.register_team("T9", "丁队", {Trade.INSTALL_REPAIR: 1})

    def test_quota_is_enforced_before_close(self) -> None:
        with self.assertRaisesRegex(RuleViolation, "名额已满"):
            self.service.register_contestant(
                "C8", "选手八", "T1", Trade.INSTALL_REPAIR, qualification("C8"), [1]
            )

    def test_substitute_reuses_original_slot_with_region_note(self) -> None:
        self.service.close_registration(at())
        with self.assertRaisesRegex(RuleViolation, "赛区说明不能为空"):
            self.service.substitute(
                "C1", "C1B", "替补一", qualification("C1B"), [1, 2, 3],
                reason="赛前训练受伤", region_note="", at=at(),
            )
        self.service.substitute(
            "C1", "C1B", "替补一", qualification("C1B"), [1, 2, 3],
            reason="赛前训练受伤", region_note="甲赛区医疗证明已备案", at=at(),
        )
        state = self.service.state
        self.assertFalse(state.contestants["C1"].active)
        self.assertTrue(state.contestants["C1B"].active)
        self.assertEqual(state.contestants["C1B"].slot_id, "T1-IR-1")
        self.assertEqual(state.slots["T1-IR-1"].contestant_id, "C1B")
        record = state.substitutions[0]
        self.assertEqual(record.out_contestant, "C1")
        self.assertEqual(record.region_note, "甲赛区医疗证明已备案")

    def test_substitute_requires_closed_registration(self) -> None:
        with self.assertRaisesRegex(RuleViolation, "报名截止后"):
            self.service.substitute(
                "C1", "C1B", "替补一", qualification("C1B"), [1],
                reason="伤病", region_note="赛区说明", at=at(),
            )

    def test_correction_never_erases_original_material(self) -> None:
        self.service.close_registration(at())
        original = self.service.state.contestants["C1"].qualification.experience
        self.service.correct_qualification(
            "C1", "experience", "一线岗位六年", "岗位年限核定更正", at()
        )
        contestant = self.service.state.contestants["C1"]
        self.assertEqual(contestant.qualification.experience, "一线岗位六年")
        audit = self.service.audit_report()
        history = next(q for q in audit["选手资格"] if q["选手"] == "C1")["更正历史"]
        self.assertEqual(history[0]["原值"], original)
        self.assertEqual(history[0]["新值"], "一线岗位六年")
        # 重放日志后最初材料仍在
        reopened = CompetitionService.open(self.dir.name)
        registered = next(
            event
            for event in reopened.journal.events()
            if event["kind"] == "contestant_registered"
            and event["payload"]["id"] == "C1"
        )
        self.assertEqual(
            registered["payload"]["qualification"]["experience"], original
        )


if __name__ == "__main__":
    unittest.main()
