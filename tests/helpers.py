"""测试共用的赛事场景。"""

from __future__ import annotations

from datetime import datetime, timedelta

from src.competition import CompetitionService, Component, Qualification, Trade

T0 = datetime(2026, 9, 25, 8, 0, 0)


def at(minutes: int = 0) -> datetime:
    return T0 + timedelta(minutes=minutes)


def qualification(tag: str = "") -> Qualification:
    return Qualification(f"地方选拔{tag}", f"一线岗位{tag}", f"培训合格{tag}")


def make_service(path) -> CompetitionService:
    """三支代表队、两个工种各三名选手、三名分属不同单位的裁判、两个工位。"""
    service = CompetitionService.create(path)
    for team_id, name in (("T1", "甲队"), ("T2", "乙队"), ("T3", "丙队")):
        service.register_team(
            team_id, name, {Trade.INSTALL_REPAIR: 1, Trade.NETWORK_OPERATION: 1}
        )
    contestants = [
        ("C1", "选手一", "T1", Trade.INSTALL_REPAIR),
        ("C2", "选手二", "T2", Trade.INSTALL_REPAIR),
        ("C3", "选手三", "T3", Trade.INSTALL_REPAIR),
        ("D1", "选手四", "T1", Trade.NETWORK_OPERATION),
        ("D2", "选手五", "T2", Trade.NETWORK_OPERATION),
        ("D3", "选手六", "T3", Trade.NETWORK_OPERATION),
    ]
    for cid, name, team, trade in contestants:
        service.register_contestant(cid, name, team, trade, qualification(cid), [1, 2, 3])
    for index, affiliation in enumerate(("T1", "T2", "T3"), start=1):
        service.register_judge(
            f"J{index}", f"裁判{index}", affiliation,
            [Trade.INSTALL_REPAIR, Trade.NETWORK_OPERATION],
        )
    service.register_station("S1", Trade.INSTALL_REPAIR)
    service.register_station("S2", Trade.NETWORK_OPERATION)
    service.register_question_package("Q1", Trade.INSTALL_REPAIR, 1)
    service.register_question_package("Q2", Trade.NETWORK_OPERATION, 1)
    return service


def close_and_schedule(service: CompetitionService) -> None:
    service.close_registration(at())
    service.schedule(at())


def panel(service: CompetitionService, contestant_id: str) -> tuple[str, ...]:
    assignment = next(
        a for a in service.state.assignments if a.contestant_id == contestant_id
    )
    return assignment.judge_ids


def score_components(
    service: CompetitionService,
    contestant_id: str,
    scores: dict[Component, float],
) -> dict[Component, str]:
    """按该选手工位的裁判组逐项录入并复核，返回分项成绩编号。"""
    recorder, reviewer = panel(service, contestant_id)[:2]
    entry_ids = {}
    for component, value in scores.items():
        entry_id = service.record_score(recorder, contestant_id, component, value, at())
        service.review_score(entry_id, reviewer, at())
        entry_ids[component] = entry_id
    return entry_ids


def full_scores(theory: float, practical: float, safety: float, emergency: float):
    return {
        Component.THEORY: theory,
        Component.PRACTICAL: practical,
        Component.SAFETY: safety,
        Component.EMERGENCY: emergency,
    }
