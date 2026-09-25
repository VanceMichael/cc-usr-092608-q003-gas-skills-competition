"""终评现场断电重启后的状态恢复。"""

import tempfile
import unittest
from pathlib import Path

try:
    import support
except ImportError:  # pragma: no cover
    from tests import support

from src.competition import CompetitionService, ScoreStatus


class PersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "events.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_restart_recovers_scene_in_original_order(self) -> None:
        service = support.build_service(self.path)
        service.check_in("C3")
        service.check_in("C1")
        service.check_in("C5")
        service.seal_questions(support.TRADE, "v2026-备用", "秘书处")
        service.disable_station("Z3", "临时停用")
        support.score_all(service, {"C1": 100, "C2": 90, "C3": 80, "C4": 70, "C5": 60, "C6": 50})
        appeal_id = service.file_appeal("C2", "评分异议")
        service.file_appeal("C4", "设备异响")
        before_results = service.public_results()
        before_events = service.audit_events()

        # 模拟断电重启：新实例从同一事件日志恢复
        recovered = CompetitionService.open(self.path)
        self.assertEqual(recovered.checked_in_contestants(), ["C3", "C1", "C5"])
        self.assertEqual(
            [q["version"] for q in recovered.sealed_question_list()],
            ["v2026", "v2026-备用"],
        )
        self.assertEqual(recovered.disabled_stations(), ["Z3"])
        self.assertEqual(
            [a.appeal_id for a in recovered.pending_appeals()], [appeal_id, "AP2"]
        )
        self.assertEqual(recovered.public_results(), before_results)
        self.assertEqual(recovered.audit_events(), before_events)

    def test_restart_recovers_substitution_corrections_and_faults(self) -> None:
        service = support.build_service(self.path)
        service.substitute_contestant(
            "C1",
            "C1B",
            "选手01替",
            dict(support.QUALIFICATIONS),
            reason="伤病",
            region_note="赛区说明",
        )
        service.correct_qualification("C2", {"work_experience": "一线岗位四年"}, "补章")
        support.score_all(service, {"C1B": 100, "C2": 90, "C3": 80, "C4": 70, "C5": 60, "C6": 50})
        service.report_equipment_fault("Z1", "检测仪漂移")

        recovered = CompetitionService.open(self.path)
        audit = recovered.audit_report("C1B")
        self.assertEqual(audit["substitution"]["replaced_id"], "C1")
        self.assertEqual(audit["slot_id"], recovered.contestants["C1"].slot_id)
        self.assertEqual(
            recovered.audit_report("C2")["qualifications"]["corrections"][0]["reason"], "补章"
        )
        # 待裁定状态在重启后仍然保持
        pending = {
            s.contestant_id
            for s in recovered.scores.values()
            if s.status == ScoreStatus.PENDING_ADJUDICATION
        }
        self.assertEqual(pending, {"C1B", "C4"})
        # 重启后仍可继续裁定
        assignment = recovered.contestant_assignments("C1B")[0]
        recovered.decide_adjudication(
            assignment.assignment_id, "keep", "故障发生在成绩确认之后", "赛事负责人甲"
        )
        self.assertEqual(
            recovered.scores[assignment.assignment_id].status, ScoreStatus.VERIFIED
        )

    def test_event_log_is_append_only(self) -> None:
        service = support.build_service(self.path)
        service.correct_qualification("C2", {"work_experience": "一线岗位四年"}, "补章")
        first_size = len(service.audit_events())
        recovered = CompetitionService.open(self.path)
        self.assertEqual(len(recovered.audit_events()), first_size)
        # 最初材料事件仍在日志中，更正只是追加
        types = [e["type"] for e in recovered.audit_events()]
        self.assertEqual(types.count("contestant_registered"), 6)
        self.assertEqual(types.count("qualification_corrected"), 1)


if __name__ == "__main__":
    unittest.main()
