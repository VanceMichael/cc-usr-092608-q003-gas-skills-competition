"""测试共用的现场搭建：三支代表队、六名选手、四名裁判、三个工位。

所有姓名均为占位符，不含真实个人信息。
"""

from __future__ import annotations

from pathlib import Path

from src.competition import CompetitionService, Trade

TRADE = Trade.INSTALLATION_REPAIR.value
SLOTS = ["S1", "S2", "S3"]
DEFAULT_TOTALS = {"C1": 100, "C2": 90, "C3": 80, "C4": 70, "C5": 60, "C6": 50}

QUALIFICATIONS = {
    "local_selection": "地方选拔通过",
    "work_experience": "一线岗位三年",
    "training_certificate": "赛前培训合格",
}


def build_service(path: Path) -> CompetitionService:
    """搭建已完成报名冻结与排程的现场。"""
    service = CompetitionService.open(path, clock=lambda: "2026-09-25T08:00:00+00:00")
    for index in (1, 2, 3):
        service.register_team(f"T{index}", f"代表队{index}", {TRADE: 2})
    for index in range(1, 7):
        team_id = f"T{(index - 1) // 2 + 1}"
        service.register_contestant(
            f"C{index}",
            f"选手{index:02d}",
            team_id,
            TRADE,
            dict(QUALIFICATIONS),
            list(SLOTS),
        )
    # 裁判甲隶属代表队T1，自动回避C1、C2；裁判丁作为机动
    service.register_judge("J1", "裁判甲", "T1", [TRADE])
    service.register_judge("J2", "裁判乙", "裁判组", [TRADE])
    service.register_judge("J3", "裁判丙", "裁判组", [TRADE])
    service.register_judge("J4", "裁判丁", "裁判组", [TRADE])
    for index in (1, 2, 3):
        service.register_station(f"Z{index}", TRADE)
    service.seal_questions(TRADE, "v2026", "秘书处")
    service.configure_awards(TRADE, {"一等奖": 1, "二等奖": 2})
    service.freeze_registration()
    service.generate_schedule(list(SLOTS))
    return service


def score_all(
    service: CompetitionService,
    totals: dict[str, float] | None = None,
    recorded_by: str = "clerk-1",
    verified_by: str = "clerk-2",
) -> None:
    """按给定总分录分并复核（理论分项承载总分，其余分项为零）。"""
    for contestant_id, total in (totals or DEFAULT_TOTALS).items():
        assignment = service.contestant_assignments(contestant_id)[0]
        service.record_score(
            assignment.assignment_id,
            assignment.judge_id,
            {"theory": total, "practical": 0, "safety_deduction": 0, "emergency": 0},
            recorded_by,
        )
        service.verify_score(assignment.assignment_id, verified_by)
