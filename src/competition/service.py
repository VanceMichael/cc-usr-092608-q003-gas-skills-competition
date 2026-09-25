"""燃气技能竞赛现场竞赛运行服务。

职责边界：

- 报名截止时冻结代表队名额与选手资格；伤病替补沿用原名额并由赛区说明
  原因；任何更正只追加新材料，最初材料永不抹除。
- 依据工种、设备校准状态、选手时段与裁判回避关系排出可执行轮次；
  裁判看不到本单位选手的评分任务，录分者不能复核自己的结果。
- 理论题、实际操作、安全违规、应急处置分别计算；设备故障只使关联工位
  成绩进入待裁定，由赛事负责人逐条决定续赛、重赛或保留，系统不提供
  批量清空整轮数据的操作。
- 申诉期间冻结相关名次但不阻塞无关奖项；并列处理与改判必须说明影响了
  哪些候选人。
- 所有处置写入只可追加的事件日志，断电重启后按原顺序重放恢复。
- 对外成绩单只呈现获奖所需信息；内部审计可还原资格、工位、分项、回避
  与每次裁定。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .events import Event, EventStore
from .models import (
    QUALIFICATION_KEYS,
    AdjudicationDecision,
    AppealRecord,
    AppealStatus,
    AssignmentState,
    CompetitionError,
    ContestantState,
    ContestantStatus,
    FaultRecord,
    JudgeState,
    RecusalError,
    RoundState,
    SchedulingError,
    ScoreComponents,
    ScoreRecord,
    ScoreStatus,
    StationState,
    TeamState,
    TieResolution,
    Trade,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CompetitionService:
    """现场竞赛运行服务门面：命令校验约束后追加事件，查询只读内存状态。"""

    def __init__(
        self,
        store: EventStore,
        clock: Callable[[], str] = _utc_now,
    ) -> None:
        self._store = store
        self._clock = clock
        self._reset_state()
        for event in store.load():
            self._apply(event)

    @classmethod
    def open(cls, path: Path | str, clock: Callable[[], str] = _utc_now) -> "CompetitionService":
        """打开（或创建）事件日志并重放，用于现场断电重启后的恢复。"""
        return cls(EventStore(path), clock)

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    def _reset_state(self) -> None:
        self.teams: dict[str, TeamState] = {}
        self.contestants: dict[str, ContestantState] = {}
        self.judges: dict[str, JudgeState] = {}
        self.stations: dict[str, StationState] = {}
        self.recusals: list[dict[str, str]] = []  # 明示回避记录
        self.sealed_questions: list[dict[str, str]] = []
        self.award_quotas: dict[str, dict[str, int]] = {}
        self.frozen: bool = False
        self.freeze_snapshot: dict[str, Any] | None = None
        self.rounds: dict[str, RoundState] = {}
        self.assignments: dict[str, AssignmentState] = {}
        self.scores: dict[str, ScoreRecord] = {}  # assignment_id -> 成绩
        self.faults: dict[str, FaultRecord] = {}
        self.appeals: dict[str, AppealRecord] = {}
        self.tie_resolutions: list[TieResolution] = []
        self.adjudications: list[dict[str, Any]] = []
        self.schedule_slots: list[str] = []
        self._check_in_order: list[str] = []
        self._disable_order: list[str] = []
        self._round_order: list[str] = []
        self._assignment_seq = 0
        self._round_seq = 0
        self._makeup_seq = 0
        self._fault_seq = 0
        self._appeal_seq = 0

    def _apply(self, event: Event) -> None:
        handler = getattr(self, f"_on_{event.type}", None)
        if handler is None:
            raise CompetitionError(f"未知事件类型: {event.type}")
        handler(event.payload, event.at)

    def _emit(self, event_type: str, payload: dict[str, Any]) -> Event:
        event = self._store.append(event_type, payload, self._clock())
        self._apply(event)
        return event

    # ------------------------------------------------------------------
    # 报名与资格
    # ------------------------------------------------------------------

    def register_team(self, team_id: str, name: str, quotas: dict[str, int]) -> None:
        self._require_not_frozen("报名截止后不得新增代表队")
        if not team_id.strip() or not name.strip():
            raise CompetitionError("代表队信息不完整")
        if team_id in self.teams:
            raise CompetitionError(f"代表队已存在: {team_id}")
        if not quotas:
            raise CompetitionError("代表队名额不能为空")
        for trade, count in quotas.items():
            Trade.coerce(trade)
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                raise CompetitionError("代表队名额必须为正整数")
        self._emit("team_registered", {"team_id": team_id, "name": name, "quotas": dict(quotas)})

    def register_contestant(
        self,
        contestant_id: str,
        name: str,
        team_id: str,
        trade: str,
        qualifications: dict[str, Any],
        available_slots: list[str],
    ) -> None:
        self._require_not_frozen("报名截止后不得新增选手，伤病替补须走替补流程")
        team = self._team(team_id)
        Trade.coerce(trade)
        self._validate_qualifications(qualifications)
        if not contestant_id.strip() or not name.strip():
            raise CompetitionError("选手信息不完整")
        if contestant_id in self.contestants:
            raise CompetitionError(f"选手已存在: {contestant_id}")
        if not available_slots:
            raise CompetitionError("选手时段不能为空")
        used = sum(
            1
            for c in self.contestants.values()
            if c.team_id == team_id and c.trade == trade and c.status == ContestantStatus.ACTIVE
        )
        quota = team.quotas.get(trade, 0)
        if used + 1 > quota:
            raise CompetitionError(f"代表队{team_id}在{trade}的名额已用完")
        slot_id = f"{team_id}:{trade}:{used + 1}"
        self._emit(
            "contestant_registered",
            {
                "contestant_id": contestant_id,
                "name": name,
                "team_id": team_id,
                "trade": trade,
                "slot_id": slot_id,
                "qualifications": dict(qualifications),
                "available_slots": list(available_slots),
            },
        )

    def freeze_registration(self) -> None:
        """报名截止：固定代表队名额与选手资格，此后只可追加更正与替补。"""
        if self.frozen:
            raise CompetitionError("报名已截止")
        if not self.teams or not self.contestants:
            raise CompetitionError("报名数据为空，不能截止")
        snapshot = {
            "quotas": {tid: dict(team.quotas) for tid, team in self.teams.items()},
            "qualifications": {
                cid: dict(c.qualifications) for cid, c in self.contestants.items()
            },
        }
        self._emit("registration_frozen", {"snapshot": snapshot})

    def substitute_contestant(
        self,
        replaced_id: str,
        new_id: str,
        name: str,
        qualifications: dict[str, Any],
        reason: str,
        region_note: str,
        available_slots: list[str] | None = None,
    ) -> None:
        """伤病替补：沿用原名额槽位，赛区必须说明原因，被替换者材料保留。"""
        self._require_frozen("报名截止前可直接改报，无需替补")
        replaced = self._contestant(replaced_id)
        if replaced.status != ContestantStatus.ACTIVE:
            raise CompetitionError(f"选手不在赛，不能替补: {replaced_id}")
        if not reason.strip():
            raise CompetitionError("替补必须说明伤病原因")
        if not region_note.strip():
            raise CompetitionError("替补必须由赛区出具说明")
        self._validate_qualifications(qualifications)
        if not new_id.strip() or not name.strip():
            raise CompetitionError("替补选手信息不完整")
        if new_id in self.contestants:
            raise CompetitionError(f"选手已存在: {new_id}")
        scored = [
            a.assignment_id
            for a in self.assignments.values()
            if a.contestant_id == replaced_id
            and not a.superseded
            and self.scores.get(a.assignment_id) is not None
            and self.scores[a.assignment_id].status != ScoreStatus.SUPERSEDED
        ]
        if scored:
            raise CompetitionError("被替换者已产生成绩，不能替补")
        self._emit(
            "contestant_substituted",
            {
                "replaced_id": replaced_id,
                "new_id": new_id,
                "name": name,
                "team_id": replaced.team_id,
                "trade": replaced.trade,
                "slot_id": replaced.slot_id,
                "qualifications": dict(qualifications),
                "available_slots": list(available_slots or replaced.available_slots),
                "reason": reason,
                "region_note": region_note,
            },
        )

    def correct_qualification(self, contestant_id: str, changes: dict[str, Any], reason: str) -> None:
        """更正资格材料：只追加更正记录，最初材料保留在原始快照中。"""
        contestant = self._contestant(contestant_id)
        if not changes:
            raise CompetitionError("更正内容不能为空")
        unknown = set(changes) - set(QUALIFICATION_KEYS)
        if unknown:
            raise CompetitionError(f"未知资格材料项: {sorted(unknown)}")
        if any(not str(value).strip() for value in changes.values()):
            raise CompetitionError("更正后的材料不能为空")
        if not reason.strip():
            raise CompetitionError("更正必须说明原因")
        self._emit(
            "qualification_corrected",
            {"contestant_id": contestant.contestant_id, "changes": dict(changes), "reason": reason},
        )

    # ------------------------------------------------------------------
    # 资源：裁判、工位、赛题、回避、奖项
    # ------------------------------------------------------------------

    def register_judge(self, judge_id: str, name: str, affiliation: str, trades: list[str]) -> None:
        self._require_no_schedule("赛程生成后不得新增裁判")
        if not judge_id.strip() or not name.strip() or not affiliation.strip():
            raise CompetitionError("裁判信息不完整")
        if judge_id in self.judges:
            raise CompetitionError(f"裁判已存在: {judge_id}")
        if not trades:
            raise CompetitionError("裁判执业工种不能为空")
        for trade in trades:
            Trade.coerce(trade)
        self._emit(
            "judge_registered",
            {"judge_id": judge_id, "name": name, "affiliation": affiliation, "trades": list(trades)},
        )

    def register_station(self, station_id: str, trade: str, calibrated: bool = True) -> None:
        self._require_no_schedule("赛程生成后不得新增工位")
        Trade.coerce(trade)
        if not station_id.strip():
            raise CompetitionError("工位编号不能为空")
        if station_id in self.stations:
            raise CompetitionError(f"工位已存在: {station_id}")
        self._emit(
            "station_registered",
            {"station_id": station_id, "trade": trade, "calibrated": bool(calibrated)},
        )

    def set_calibration(self, station_id: str, calibrated: bool) -> None:
        self._station(station_id)
        self._emit("station_calibration_set", {"station_id": station_id, "calibrated": bool(calibrated)})

    def disable_station(self, station_id: str, reason: str) -> None:
        self._station(station_id)
        if not reason.strip():
            raise CompetitionError("设备停用必须说明原因")
        self._emit("station_disabled", {"station_id": station_id, "reason": reason})

    def restore_station(self, station_id: str) -> None:
        station = self._station(station_id)
        if any(f.station_id == station_id and f.open for f in self.faults.values()):
            raise CompetitionError("工位故障尚未裁定完毕，不能恢复使用")
        if station.active:
            raise CompetitionError(f"工位未停用: {station_id}")
        self._emit("station_restored", {"station_id": station_id})

    def seal_questions(self, trade: str, version: str, sealed_by: str) -> None:
        Trade.coerce(trade)
        if not version.strip() or not sealed_by.strip():
            raise CompetitionError("赛题封存信息不完整")
        if any(q["trade"] == trade and q["version"] == version for q in self.sealed_questions):
            raise CompetitionError(f"赛题版本已封存: {trade}/{version}")
        self._emit(
            "questions_sealed", {"trade": trade, "version": version, "sealed_by": sealed_by}
        )

    def declare_recusal(self, judge_id: str, team_id: str, reason: str) -> None:
        self._judge(judge_id)
        self._team(team_id)
        if not reason.strip():
            raise CompetitionError("回避声明必须说明原因")
        if any(r["judge_id"] == judge_id and r["team_id"] == team_id for r in self.recusals):
            raise CompetitionError("回避关系已声明")
        self._emit(
            "recusal_declared", {"judge_id": judge_id, "team_id": team_id, "reason": reason}
        )

    def configure_awards(self, trade: str, quotas: dict[str, int]) -> None:
        Trade.coerce(trade)
        if not quotas:
            raise CompetitionError("奖项名额不能为空")
        for award, count in quotas.items():
            if not award.strip():
                raise CompetitionError("奖项名称不能为空")
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                raise CompetitionError("奖项名额必须为正整数")
        self._emit("awards_configured", {"trade": trade, "quotas": dict(quotas)})

    # ------------------------------------------------------------------
    # 赛程
    # ------------------------------------------------------------------

    def generate_schedule(self, slots: list[str]) -> dict[str, Any]:
        """按工种、设备校准状态、选手时段与裁判回避排出可执行轮次。

        任何选手排不进去都整体失败，不产生半个赛程；赛程一旦生成，
        后续调整只能通过设备故障裁定与裁判更换进行。
        """
        self._require_frozen("报名未截止，不能生成赛程")
        if self.rounds:
            raise CompetitionError("赛程已生成，调整须通过裁定与裁判更换进行")
        if not slots or len(set(slots)) != len(slots):
            raise CompetitionError("时段列表无效")
        busy_stations: set[tuple[str, str]] = set()
        busy_judges: set[tuple[str, str]] = set()
        planned: list[dict[str, Any]] = []
        unassigned: list[str] = []
        for trade in Trade:
            stations = sorted(
                (s for s in self.stations.values() if s.trade == trade.value and s.active and s.calibrated),
                key=lambda s: s.station_id,
            )
            judges = sorted(
                (j for j in self.judges.values() if trade.value in j.trades),
                key=lambda j: j.judge_id,
            )
            contestants = sorted(
                (
                    c
                    for c in self.contestants.values()
                    if c.trade == trade.value and c.status == ContestantStatus.ACTIVE
                ),
                key=lambda c: c.contestant_id,
            )
            for contestant in contestants:
                placed = False
                for slot in slots:
                    if slot not in contestant.available_slots:
                        continue
                    station = next(
                        (s for s in stations if (s.station_id, slot) not in busy_stations), None
                    )
                    if station is None:
                        continue
                    judge = next(
                        (
                            j
                            for j in judges
                            if (j.judge_id, slot) not in busy_judges
                            and not self._is_recused(j.judge_id, contestant.team_id)
                        ),
                        None,
                    )
                    if judge is None:
                        continue
                    busy_stations.add((station.station_id, slot))
                    busy_judges.add((judge.judge_id, slot))
                    planned.append(
                        {
                            "contestant_id": contestant.contestant_id,
                            "station_id": station.station_id,
                            "judge_id": judge.judge_id,
                            "trade": trade.value,
                            "slot": slot,
                        }
                    )
                    placed = True
                    break
                if not placed:
                    unassigned.append(contestant.contestant_id)
        if unassigned:
            raise SchedulingError(f"以下选手无法排入可执行轮次: {unassigned}")
        rounds: list[dict[str, Any]] = []
        assignments: list[dict[str, Any]] = []
        round_ids: dict[tuple[str, str], str] = {}
        for slot in slots:
            for trade in Trade:
                key = (slot, trade.value)
                members = [p for p in planned if p["slot"] == slot and p["trade"] == trade.value]
                if not members:
                    continue
                self._round_seq += 1
                round_id = f"R{self._round_seq}"
                round_ids[key] = round_id
                rounds.append({"round_id": round_id, "trade": trade.value, "slot": slot})
                for plan in members:
                    self._assignment_seq += 1
                    assignments.append(
                        {"assignment_id": f"A{self._assignment_seq}", "round_id": round_id, **plan}
                    )
        self._emit(
            "schedule_generated",
            {"slots": list(slots), "rounds": rounds, "assignments": assignments},
        )
        return {"rounds": rounds, "assignments": assignments}

    def reassign_judge(self, assignment_id: str, new_judge_id: str, reason: str) -> None:
        """更换排定裁判（例如回避关系后补声明），须满足回避与时段约束。"""
        assignment = self._assignment(assignment_id)
        if assignment.superseded:
            raise CompetitionError("该出场安排已被重赛取代")
        judge = self._judge(new_judge_id)
        contestant = self._contestant(assignment.contestant_id)
        if assignment.trade not in judge.trades:
            raise CompetitionError("裁判执业工种不符")
        if self._is_recused(new_judge_id, contestant.team_id):
            raise RecusalError(f"裁判{new_judge_id}与该选手单位存在回避关系")
        if not reason.strip():
            raise CompetitionError("更换裁判必须说明原因")
        for other in self.assignments.values():
            if (
                other.judge_id == new_judge_id
                and other.slot == assignment.slot
                and not other.superseded
            ):
                raise SchedulingError(f"裁判{new_judge_id}在该时段已有任务")
        self._emit(
            "judge_reassigned",
            {"assignment_id": assignment_id, "judge_id": new_judge_id, "reason": reason},
        )

    def check_in(self, contestant_id: str) -> None:
        contestant = self._contestant(contestant_id)
        if contestant.status != ContestantStatus.ACTIVE:
            raise CompetitionError(f"选手不在赛，不能签到: {contestant_id}")
        if contestant.checked_in:
            raise CompetitionError(f"选手已签到: {contestant_id}")
        self._emit("contestant_checked_in", {"contestant_id": contestant_id})

    # ------------------------------------------------------------------
    # 评分
    # ------------------------------------------------------------------

    def record_score(
        self,
        assignment_id: str,
        judge_id: str,
        components: ScoreComponents | dict[str, Any],
        recorded_by: str,
    ) -> None:
        """录入分项成绩；评分裁判须为排定裁判且与选手单位无回避关系。"""
        assignment = self._assignment(assignment_id)
        if assignment.superseded:
            raise CompetitionError("该出场安排已被重赛取代")
        if assignment.blocked:
            raise CompetitionError("该工位设备故障待裁定，不能录入成绩")
        if assignment.judge_id != judge_id:
            raise CompetitionError("评分裁判与排定裁判不一致")
        contestant = self._contestant(assignment.contestant_id)
        if self._is_recused(judge_id, contestant.team_id):
            raise RecusalError(f"裁判{judge_id}与选手单位存在回避关系，不得评分")
        if not recorded_by.strip():
            raise CompetitionError("录分者不能为空")
        existing = self.scores.get(assignment_id)
        if existing is not None and existing.status != ScoreStatus.SUPERSEDED:
            raise CompetitionError("成绩已录入，更正须通过申诉改判或负责人裁定")
        if not isinstance(components, ScoreComponents):
            components = ScoreComponents.from_dict(components)
        self._emit(
            "score_recorded",
            {
                "assignment_id": assignment_id,
                "contestant_id": assignment.contestant_id,
                "judge_id": judge_id,
                "components": components.to_dict(),
                "recorded_by": recorded_by,
            },
        )

    def verify_score(self, assignment_id: str, verified_by: str) -> None:
        """复核成绩：录分者不能复核自己的结果。"""
        score = self._score(assignment_id)
        if score.status != ScoreStatus.RECORDED:
            raise CompetitionError("只有已录入未复核的成绩可以复核")
        if not verified_by.strip():
            raise CompetitionError("复核者不能为空")
        if verified_by == score.recorded_by:
            raise CompetitionError("录分者不能复核自己的结果")
        self._emit("score_verified", {"assignment_id": assignment_id, "verified_by": verified_by})

    # ------------------------------------------------------------------
    # 设备故障与裁定
    # ------------------------------------------------------------------

    def report_equipment_fault(self, station_id: str, reason: str) -> str:
        """设备故障：仅关联工位的成绩进入待裁定，其余轮次数据不受影响。"""
        station = self._station(station_id)
        if not reason.strip():
            raise CompetitionError("设备故障必须说明原因")
        if any(f.station_id == station_id and f.open for f in self.faults.values()):
            raise CompetitionError(f"工位已有未结故障: {station_id}")
        affected = [
            a.assignment_id
            for a in self.assignments.values()
            if a.station_id == station_id and not a.superseded
        ]
        self._fault_seq += 1
        fault_id = f"F{self._fault_seq}"
        self._emit(
            "equipment_fault_reported",
            {
                "fault_id": fault_id,
                "station_id": station_id,
                "reason": reason,
                "affected_assignment_ids": affected,
            },
        )
        return fault_id

    def decide_adjudication(
        self,
        assignment_id: str,
        decision: str | AdjudicationDecision,
        rationale: str,
        decided_by: str,
        slot: str | None = None,
    ) -> None:
        """赛事负责人逐条裁定受故障影响的出场：续赛、重赛或保留。"""
        assignment = self._assignment(assignment_id)
        if not isinstance(decision, AdjudicationDecision):
            decision = AdjudicationDecision.coerce(decision)
        fault = self._open_fault_for(assignment_id)
        if fault is None:
            raise CompetitionError("该出场安排不在待裁定状态")
        if not rationale.strip():
            raise CompetitionError("裁定必须说明理由")
        if not decided_by.strip():
            raise CompetitionError("裁定必须注明赛事负责人")
        score = self.scores.get(assignment_id)
        if decision == AdjudicationDecision.KEEP and score is None:
            raise CompetitionError("该出场尚无成绩，不能裁定保留")
        makeup: dict[str, Any] | None = None
        if decision == AdjudicationDecision.REMATCH:
            makeup = self._plan_rematch(assignment, slot)
        self._emit(
            "adjudication_decided",
            {
                "assignment_id": assignment_id,
                "fault_id": fault.fault_id,
                "decision": decision.value,
                "rationale": rationale,
                "decided_by": decided_by,
                "makeup": makeup,
            },
        )

    # ------------------------------------------------------------------
    # 申诉与并列
    # ------------------------------------------------------------------

    def file_appeal(self, contestant_id: str, grounds: str) -> str:
        """申诉：冻结相关名次（本人及同工种同分者），不阻塞无关奖项。"""
        contestant = self._contestant(contestant_id)
        if not grounds.strip():
            raise CompetitionError("申诉理由不能为空")
        if any(
            a.contestant_id == contestant_id and a.status == AppealStatus.PENDING
            for a in self.appeals.values()
        ):
            raise CompetitionError(f"选手已有待审申诉: {contestant_id}")
        totals = self._verified_totals(contestant.trade)
        frozen_ids = [contestant_id]
        if contestant_id in totals:
            frozen_ids += sorted(
                cid for cid, total in totals.items() if total == totals[contestant_id] and cid != contestant_id
            )
        self._appeal_seq += 1
        appeal_id = f"AP{self._appeal_seq}"
        self._emit(
            "appeal_filed",
            {
                "appeal_id": appeal_id,
                "contestant_id": contestant_id,
                "grounds": grounds,
                "frozen_contestant_ids": frozen_ids,
            },
        )
        return appeal_id

    def rule_appeal(
        self,
        appeal_id: str,
        upheld: bool,
        rationale: str,
        affected_candidate_ids: list[str],
        adjustments: dict[str, ScoreComponents | dict[str, Any]] | None = None,
    ) -> None:
        """裁定申诉：改判必须说明影响了哪些候选人，成绩原件保留为历史版本。"""
        appeal = self._appeal(appeal_id)
        if appeal.status != AppealStatus.PENDING:
            raise CompetitionError("申诉已裁定")
        if not rationale.strip():
            raise CompetitionError("裁定必须说明理由")
        if upheld and not affected_candidate_ids:
            raise CompetitionError("改判必须说明影响了哪些候选人")
        normalized: dict[str, dict[str, float]] = {}
        for cid, components in (adjustments or {}).items():
            if cid not in affected_candidate_ids:
                raise CompetitionError(f"改判对象未列入影响候选人: {cid}")
            score = self._score_of_contestant(cid)
            if score is None or score.status != ScoreStatus.VERIFIED:
                raise CompetitionError(f"选手没有可改判的已复核成绩: {cid}")
            if not isinstance(components, ScoreComponents):
                components = ScoreComponents.from_dict(components)
            normalized[cid] = components.to_dict()
        self._emit(
            "appeal_ruled",
            {
                "appeal_id": appeal_id,
                "upheld": bool(upheld),
                "rationale": rationale,
                "affected_candidate_ids": list(affected_candidate_ids),
                "adjustments": normalized,
            },
        )

    def resolve_tie(self, trade: str, ordered_candidate_ids: list[str], rationale: str) -> None:
        """并列处理：必须给出受影响候选人的先后次序与理由。"""
        Trade.coerce(trade)
        if not rationale.strip():
            raise CompetitionError("并列处理必须说明理由")
        if len(ordered_candidate_ids) < 2:
            raise CompetitionError("并列处理至少涉及两名候选人")
        totals = self._verified_totals(trade)
        missing = [cid for cid in ordered_candidate_ids if cid not in totals]
        if missing:
            raise CompetitionError(f"候选人没有已复核成绩: {missing}")
        values = {totals[cid] for cid in ordered_candidate_ids}
        if len(values) != 1:
            raise CompetitionError("候选人总分并不并列")
        tied_group = {cid for cid, total in totals.items() if total in values}
        if set(ordered_candidate_ids) != tied_group:
            raise CompetitionError("并列处理必须完整列出该名次的全部候选人")
        self._emit(
            "tie_resolved",
            {
                "trade": trade,
                "ordered_candidate_ids": list(ordered_candidate_ids),
                "rationale": rationale,
            },
        )

    # ------------------------------------------------------------------
    # 查询：裁判任务、对外成绩单、内部审计
    # ------------------------------------------------------------------

    def judge_task_list(self, judge_id: str) -> list[dict[str, Any]]:
        """裁判的评分任务：本单位选手的任务不下发，回避关系在此再次过滤。"""
        self._judge(judge_id)
        tasks = []
        for assignment in self._ordered_assignments():
            if assignment.judge_id != judge_id or assignment.superseded:
                continue
            contestant = self._contestant(assignment.contestant_id)
            if self._is_recused(judge_id, contestant.team_id):
                continue
            tasks.append(
                {
                    "assignment_id": assignment.assignment_id,
                    "round_id": assignment.round_id,
                    "slot": assignment.slot,
                    "station_id": assignment.station_id,
                    "contestant_id": assignment.contestant_id,
                    "trade": assignment.trade,
                }
            )
        return tasks

    def public_results(self) -> dict[str, Any]:
        """对外成绩单：只呈现获奖所需信息，不含分项、裁判与裁定细节。"""
        trades: dict[str, Any] = {}
        for trade, quotas in self.award_quotas.items():
            entries = self._ranked_entries(trade)
            frozen_ranks = self._frozen_ranks(trade, entries)
            awards: list[dict[str, Any]] = []
            withheld: list[dict[str, Any]] = []
            low = 1
            for award, count in quotas.items():
                high = low + count - 1
                tier = [e for e in entries if low <= e["rank"] <= high]
                if self._tier_blocked_by_tie(entries, low, high):
                    withheld.append({"award": award, "reason": "tie_unresolved"})
                else:
                    for entry in tier:
                        if entry["rank"] in frozen_ranks:
                            withheld.append(
                                {"award": award, "rank": entry["rank"], "reason": "appeal_pending"}
                            )
                            continue
                        contestant = self._contestant(entry["contestant_id"])
                        awards.append(
                            {
                                "award": award,
                                "rank": entry["rank"],
                                "contestant": contestant.name,
                                "team": self.teams[contestant.team_id].name,
                            }
                        )
                low = high + 1
            trades[trade] = {"awards": awards, "withheld": withheld}
        return {"trades": trades}

    def audit_report(self, contestant_id: str) -> dict[str, Any]:
        """内部审计：还原选手资格、工位安排、评分分项、回避记录与每次裁定。"""
        contestant = self._contestant(contestant_id)
        assignments = [a for a in self._ordered_assignments() if a.contestant_id == contestant_id]
        assignment_ids = {a.assignment_id for a in assignments}
        return {
            "contestant_id": contestant_id,
            "name": contestant.name,
            "team": self.teams[contestant.team_id].name,
            "trade": contestant.trade,
            "slot_id": contestant.slot_id,
            "status": contestant.status.value,
            "checked_in": contestant.checked_in,
            "qualifications": {
                "original": dict(contestant.original_qualifications),
                "current": dict(contestant.qualifications),
                "corrections": [dict(c) for c in contestant.corrections],
            },
            "substitution": dict(contestant.substitution) if contestant.substitution else None,
            "assignments": [
                {
                    "assignment_id": a.assignment_id,
                    "round_id": a.round_id,
                    "slot": a.slot,
                    "station_id": a.station_id,
                    "judge_id": a.judge_id,
                    "blocked": a.blocked,
                    "superseded": a.superseded,
                }
                for a in assignments
            ],
            "scores": [
                {
                    "assignment_id": score.assignment_id,
                    "components": score.components.to_dict(),
                    "total": score.components.total,
                    "status": score.status.value,
                    "recorded_by": score.recorded_by,
                    "verified_by": score.verified_by,
                    "revisions": [dict(r) for r in score.revisions],
                }
                for score in (self.scores.get(aid) for aid in assignment_ids)
                if score is not None
            ],
            "adjudications": [
                dict(record)
                for record in self.adjudications
                if record["assignment_id"] in assignment_ids
            ],
            "appeals": [
                {
                    "appeal_id": appeal.appeal_id,
                    "grounds": appeal.grounds,
                    "status": appeal.status.value,
                    "rationale": appeal.rationale,
                    "affected_candidate_ids": list(appeal.affected_candidate_ids),
                }
                for appeal in self.appeals.values()
                if appeal.contestant_id == contestant_id
            ],
            "recusals": self.recusal_records(contestant.team_id),
        }

    def recusal_records(self, team_id: str | None = None) -> list[dict[str, str]]:
        """回避记录：单位隶属自动回避 + 明示声明回避。"""
        records: list[dict[str, str]] = []
        for judge in self.judges.values():
            if team_id is None or judge.affiliation == team_id:
                records.append(
                    {
                        "judge_id": judge.judge_id,
                        "team_id": judge.affiliation,
                        "reason": "本单位隶属",
                        "source": "affiliation",
                    }
                )
        for recusal in self.recusals:
            if team_id is None or recusal["team_id"] == team_id:
                records.append({**recusal, "source": "declared"})
        return records

    def audit_events(self) -> list[dict[str, Any]]:
        """完整事件日志，供内部审计逐条还原现场处置顺序。"""
        return [event.to_dict() for event in self._store.load()]

    # 持久化状态查询：断电重启后按原顺序存在

    def checked_in_contestants(self) -> list[str]:
        return list(self._check_in_order)

    def sealed_question_list(self) -> list[dict[str, str]]:
        return [dict(q) for q in self.sealed_questions]

    def disabled_stations(self) -> list[str]:
        return list(self._disable_order)

    def pending_appeals(self) -> list[AppealRecord]:
        return [a for a in self.appeals.values() if a.status == AppealStatus.PENDING]

    def contestant_assignments(self, contestant_id: str) -> list[AssignmentState]:
        self._contestant(contestant_id)
        return [a for a in self._ordered_assignments() if a.contestant_id == contestant_id]

    # ------------------------------------------------------------------
    # 内部：排名与奖项
    # ------------------------------------------------------------------

    def _verified_totals(self, trade: str) -> dict[str, float]:
        totals: dict[str, float] = {}
        for assignment in self.assignments.values():
            if assignment.trade != trade or assignment.superseded:
                continue
            score = self.scores.get(assignment.assignment_id)
            if score is not None and score.status == ScoreStatus.VERIFIED:
                totals[assignment.contestant_id] = score.components.total
        return totals

    def _tie_order(self, trade: str, members: Iterable[str]) -> dict[str, int] | None:
        wanted = set(members)
        for resolution in reversed(self.tie_resolutions):
            if resolution.trade == trade and set(resolution.ordered_candidate_ids) == wanted:
                return {cid: index for index, cid in enumerate(resolution.ordered_candidate_ids)}
        return None

    def _ranked_entries(self, trade: str) -> list[dict[str, Any]]:
        totals = self._verified_totals(trade)
        entries: list[dict[str, Any]] = []
        position = 1
        for total in sorted(set(totals.values()), reverse=True):
            members = sorted(cid for cid, value in totals.items() if value == total)
            order = self._tie_order(trade, members) if len(members) > 1 else None
            if order is not None:
                for cid in sorted(members, key=lambda c: order[c]):
                    entries.append({"rank": position, "contestant_id": cid, "total": total, "tied": False})
                    position += 1
            else:
                for cid in members:
                    entries.append(
                        {"rank": position, "contestant_id": cid, "total": total, "tied": len(members) > 1}
                    )
                position += len(members)
        return entries

    def _tier_blocked_by_tie(self, entries: list[dict[str, Any]], low: int, high: int) -> bool:
        """未处理的并列横跨奖项边界时，该档奖项挂起等待并列处理。"""
        groups: dict[int, list[dict[str, Any]]] = {}
        for entry in entries:
            if entry["tied"]:
                groups.setdefault(entry["rank"], []).append(entry)
        for rank, members in groups.items():
            size = len(members)
            if low <= rank <= high < rank + size - 1:
                return True
        return False

    def _frozen_ranks(self, trade: str, entries: list[dict[str, Any]]) -> set[int]:
        frozen_ids: set[str] = set()
        for appeal in self.pending_appeals():
            if self._contestant(appeal.contestant_id).trade == trade:
                frozen_ids.update(appeal.frozen_contestant_ids)
        return {e["rank"] for e in entries if e["contestant_id"] in frozen_ids}

    # ------------------------------------------------------------------
    # 内部：工具
    # ------------------------------------------------------------------

    def _ordered_assignments(self) -> list[AssignmentState]:
        order = {rid: index for index, rid in enumerate(self._round_order)}
        return sorted(
            self.assignments.values(),
            key=lambda a: (order.get(a.round_id, len(order)), int(a.assignment_id[1:])),
        )

    def _is_recused(self, judge_id: str, team_id: str) -> bool:
        judge = self.judges.get(judge_id)
        if judge is not None and judge.affiliation == team_id:
            return True
        return any(r["judge_id"] == judge_id and r["team_id"] == team_id for r in self.recusals)

    def _open_fault_for(self, assignment_id: str) -> FaultRecord | None:
        for fault in self.faults.values():
            if fault.open and assignment_id in fault.affected_assignment_ids:
                if assignment_id not in fault.decided_assignment_ids:
                    return fault
        return None

    def _plan_rematch(self, assignment: AssignmentState, slot: str | None) -> dict[str, Any]:
        if slot is None:
            raise CompetitionError("裁定重赛必须指定补赛时段")
        contestant = self._contestant(assignment.contestant_id)
        station = next(
            (
                s
                for s in sorted(self.stations.values(), key=lambda x: x.station_id)
                if s.trade == assignment.trade
                and s.active
                and s.calibrated
                and not any(
                    a.station_id == s.station_id and a.slot == slot and not a.superseded
                    for a in self.assignments.values()
                )
            ),
            None,
        )
        if station is None:
            raise SchedulingError("补赛时段没有可用工位")
        judge = next(
            (
                j
                for j in sorted(self.judges.values(), key=lambda x: x.judge_id)
                if assignment.trade in j.trades
                and not self._is_recused(j.judge_id, contestant.team_id)
                and not any(
                    a.judge_id == j.judge_id and a.slot == slot and not a.superseded
                    for a in self.assignments.values()
                )
            ),
            None,
        )
        if judge is None:
            raise SchedulingError("补赛时段没有可回避冲突的裁判")
        self._assignment_seq += 1
        makeup_assignment_id = f"A{self._assignment_seq}"
        round_id = next(
            (
                r.round_id
                for r in self.rounds.values()
                if r.trade == assignment.trade and r.slot == slot
            ),
            None,
        )
        if round_id is None:
            self._makeup_seq += 1
            round_id = f"RM{self._makeup_seq}"
        return {
            "assignment_id": makeup_assignment_id,
            "round_id": round_id,
            "contestant_id": assignment.contestant_id,
            "station_id": station.station_id,
            "judge_id": judge.judge_id,
            "trade": assignment.trade,
            "slot": slot,
        }

    def _validate_qualifications(self, qualifications: dict[str, Any]) -> None:
        if not isinstance(qualifications, dict):
            raise CompetitionError("资格材料无效")
        missing = [key for key in QUALIFICATION_KEYS if not str(qualifications.get(key, "")).strip()]
        if missing:
            raise CompetitionError(f"资格材料缺项: {missing}")

    def _require_not_frozen(self, message: str) -> None:
        if self.frozen:
            raise CompetitionError(message)

    def _require_frozen(self, message: str) -> None:
        if not self.frozen:
            raise CompetitionError(message)

    def _require_no_schedule(self, message: str) -> None:
        if self.rounds:
            raise CompetitionError(message)

    def _team(self, team_id: str) -> TeamState:
        try:
            return self.teams[team_id]
        except KeyError:
            raise CompetitionError(f"未知代表队: {team_id}") from None

    def _contestant(self, contestant_id: str) -> ContestantState:
        try:
            return self.contestants[contestant_id]
        except KeyError:
            raise CompetitionError(f"未知选手: {contestant_id}") from None

    def _judge(self, judge_id: str) -> JudgeState:
        try:
            return self.judges[judge_id]
        except KeyError:
            raise CompetitionError(f"未知裁判: {judge_id}") from None

    def _station(self, station_id: str) -> StationState:
        try:
            return self.stations[station_id]
        except KeyError:
            raise CompetitionError(f"未知工位: {station_id}") from None

    def _assignment(self, assignment_id: str) -> AssignmentState:
        try:
            return self.assignments[assignment_id]
        except KeyError:
            raise CompetitionError(f"未知出场安排: {assignment_id}") from None

    def _score(self, assignment_id: str) -> ScoreRecord:
        try:
            return self.scores[assignment_id]
        except KeyError:
            raise CompetitionError(f"该出场安排尚无成绩: {assignment_id}") from None

    def _score_of_contestant(self, contestant_id: str) -> ScoreRecord | None:
        for assignment in self.assignments.values():
            if assignment.contestant_id != contestant_id or assignment.superseded:
                continue
            score = self.scores.get(assignment.assignment_id)
            if score is not None and score.status != ScoreStatus.SUPERSEDED:
                return score
        return None

    def _appeal(self, appeal_id: str) -> AppealRecord:
        try:
            return self.appeals[appeal_id]
        except KeyError:
            raise CompetitionError(f"未知申诉: {appeal_id}") from None

    # ------------------------------------------------------------------
    # 事件应用：重放即恢复，顺序即事实
    # ------------------------------------------------------------------

    def _on_team_registered(self, payload: dict[str, Any], at: str) -> None:
        self.teams[payload["team_id"]] = TeamState(
            team_id=payload["team_id"], name=payload["name"], quotas=dict(payload["quotas"])
        )

    def _on_contestant_registered(self, payload: dict[str, Any], at: str) -> None:
        self.contestants[payload["contestant_id"]] = ContestantState(
            contestant_id=payload["contestant_id"],
            name=payload["name"],
            team_id=payload["team_id"],
            trade=payload["trade"],
            slot_id=payload["slot_id"],
            qualifications=dict(payload["qualifications"]),
            original_qualifications=dict(payload["qualifications"]),
            available_slots=list(payload["available_slots"]),
        )

    def _on_registration_frozen(self, payload: dict[str, Any], at: str) -> None:
        self.frozen = True
        self.freeze_snapshot = payload["snapshot"]

    def _on_contestant_substituted(self, payload: dict[str, Any], at: str) -> None:
        replaced = self.contestants[payload["replaced_id"]]
        replaced.status = ContestantStatus.REPLACED
        replaced.replaced_by = payload["new_id"]
        self.contestants[payload["new_id"]] = ContestantState(
            contestant_id=payload["new_id"],
            name=payload["name"],
            team_id=payload["team_id"],
            trade=payload["trade"],
            slot_id=payload["slot_id"],
            qualifications=dict(payload["qualifications"]),
            original_qualifications=dict(payload["qualifications"]),
            available_slots=list(payload["available_slots"]),
            substitution={
                "replaced_id": payload["replaced_id"],
                "reason": payload["reason"],
                "region_note": payload["region_note"],
                "at": at,
            },
        )
        # 替补沿用原名额：被替换者尚未进行的出场安排一并移交
        for assignment in self.assignments.values():
            if assignment.contestant_id == payload["replaced_id"] and not assignment.superseded:
                assignment.contestant_id = payload["new_id"]

    def _on_qualification_corrected(self, payload: dict[str, Any], at: str) -> None:
        contestant = self.contestants[payload["contestant_id"]]
        contestant.corrections.append(
            {"changes": dict(payload["changes"]), "reason": payload["reason"], "at": at}
        )
        contestant.qualifications.update(payload["changes"])

    def _on_judge_registered(self, payload: dict[str, Any], at: str) -> None:
        self.judges[payload["judge_id"]] = JudgeState(
            judge_id=payload["judge_id"],
            name=payload["name"],
            affiliation=payload["affiliation"],
            trades=list(payload["trades"]),
        )

    def _on_station_registered(self, payload: dict[str, Any], at: str) -> None:
        self.stations[payload["station_id"]] = StationState(
            station_id=payload["station_id"],
            trade=payload["trade"],
            calibrated=payload["calibrated"],
        )

    def _on_station_calibration_set(self, payload: dict[str, Any], at: str) -> None:
        self.stations[payload["station_id"]].calibrated = payload["calibrated"]

    def _on_station_disabled(self, payload: dict[str, Any], at: str) -> None:
        station = self.stations[payload["station_id"]]
        station.active = False
        station.disabled_reason = payload["reason"]
        if payload["station_id"] not in self._disable_order:
            self._disable_order.append(payload["station_id"])

    def _on_station_restored(self, payload: dict[str, Any], at: str) -> None:
        station = self.stations[payload["station_id"]]
        station.active = True
        station.disabled_reason = None
        if payload["station_id"] in self._disable_order:
            self._disable_order.remove(payload["station_id"])

    def _on_questions_sealed(self, payload: dict[str, Any], at: str) -> None:
        self.sealed_questions.append(
            {
                "trade": payload["trade"],
                "version": payload["version"],
                "sealed_by": payload["sealed_by"],
                "at": at,
            }
        )

    def _on_recusal_declared(self, payload: dict[str, Any], at: str) -> None:
        self.recusals.append(
            {
                "judge_id": payload["judge_id"],
                "team_id": payload["team_id"],
                "reason": payload["reason"],
            }
        )

    def _on_awards_configured(self, payload: dict[str, Any], at: str) -> None:
        self.award_quotas[payload["trade"]] = dict(payload["quotas"])

    def _on_schedule_generated(self, payload: dict[str, Any], at: str) -> None:
        self.schedule_slots = list(payload["slots"])
        for round_info in payload["rounds"]:
            self.rounds[round_info["round_id"]] = RoundState(
                round_id=round_info["round_id"],
                trade=round_info["trade"],
                slot=round_info["slot"],
            )
            self._round_order.append(round_info["round_id"])
        for plan in payload["assignments"]:
            assignment = AssignmentState(
                assignment_id=plan["assignment_id"],
                round_id=plan["round_id"],
                contestant_id=plan["contestant_id"],
                station_id=plan["station_id"],
                judge_id=plan["judge_id"],
                trade=plan["trade"],
                slot=plan["slot"],
            )
            self.assignments[assignment.assignment_id] = assignment
            self.rounds[assignment.round_id].assignment_ids.append(assignment.assignment_id)
        self._round_seq = max(
            (int(r[1:]) for r in self.rounds if r.startswith("R") and r[1:].isdigit()),
            default=0,
        )
        self._assignment_seq = max((int(a[1:]) for a in self.assignments), default=0)

    def _on_judge_reassigned(self, payload: dict[str, Any], at: str) -> None:
        self.assignments[payload["assignment_id"]].judge_id = payload["judge_id"]

    def _on_contestant_checked_in(self, payload: dict[str, Any], at: str) -> None:
        self.contestants[payload["contestant_id"]].checked_in = True
        self._check_in_order.append(payload["contestant_id"])

    def _on_score_recorded(self, payload: dict[str, Any], at: str) -> None:
        self.scores[payload["assignment_id"]] = ScoreRecord(
            assignment_id=payload["assignment_id"],
            contestant_id=payload["contestant_id"],
            judge_id=payload["judge_id"],
            components=ScoreComponents.from_dict(payload["components"]),
            recorded_by=payload["recorded_by"],
        )

    def _on_score_verified(self, payload: dict[str, Any], at: str) -> None:
        score = self.scores[payload["assignment_id"]]
        score.status = ScoreStatus.VERIFIED
        score.verified_by = payload["verified_by"]

    def _on_equipment_fault_reported(self, payload: dict[str, Any], at: str) -> None:
        station = self.stations[payload["station_id"]]
        station.active = False
        station.disabled_reason = payload["reason"]
        if payload["station_id"] not in self._disable_order:
            self._disable_order.append(payload["station_id"])
        for assignment_id in payload["affected_assignment_ids"]:
            assignment = self.assignments[assignment_id]
            assignment.blocked = True
            score = self.scores.get(assignment_id)
            if score is not None and score.status in (ScoreStatus.RECORDED, ScoreStatus.VERIFIED):
                score.previous_status = score.status
                score.status = ScoreStatus.PENDING_ADJUDICATION
        self.faults[payload["fault_id"]] = FaultRecord(
            fault_id=payload["fault_id"],
            station_id=payload["station_id"],
            reason=payload["reason"],
            at=at,
            affected_assignment_ids=list(payload["affected_assignment_ids"]),
        )

    def _on_adjudication_decided(self, payload: dict[str, Any], at: str) -> None:
        assignment = self.assignments[payload["assignment_id"]]
        fault = self.faults[payload["fault_id"]]
        decision = AdjudicationDecision(payload["decision"])
        score = self.scores.get(assignment.assignment_id)
        if decision == AdjudicationDecision.RESUME:
            assignment.blocked = False
            if score is not None and score.status == ScoreStatus.PENDING_ADJUDICATION:
                score.status = score.previous_status or ScoreStatus.RECORDED
                score.previous_status = None
        elif decision == AdjudicationDecision.KEEP:
            assignment.blocked = False
            if score is not None:
                score.status = ScoreStatus.VERIFIED
                score.previous_status = None
        elif decision == AdjudicationDecision.REMATCH:
            assignment.blocked = False
            assignment.superseded = True
            if score is not None:
                score.status = ScoreStatus.SUPERSEDED
                score.previous_status = None
            makeup = payload["makeup"]
            if makeup["round_id"] not in self.rounds:
                self.rounds[makeup["round_id"]] = RoundState(
                    round_id=makeup["round_id"], trade=makeup["trade"], slot=makeup["slot"]
                )
                self._round_order.append(makeup["round_id"])
            new_assignment = AssignmentState(
                assignment_id=makeup["assignment_id"],
                round_id=makeup["round_id"],
                contestant_id=makeup["contestant_id"],
                station_id=makeup["station_id"],
                judge_id=makeup["judge_id"],
                trade=makeup["trade"],
                slot=makeup["slot"],
            )
            self.assignments[new_assignment.assignment_id] = new_assignment
            self.rounds[new_assignment.round_id].assignment_ids.append(new_assignment.assignment_id)
            self._assignment_seq = max(self._assignment_seq, int(new_assignment.assignment_id[1:]))
            if new_assignment.round_id.startswith("RM"):
                self._makeup_seq = max(self._makeup_seq, int(new_assignment.round_id[2:]))
        fault.decided_assignment_ids.append(assignment.assignment_id)
        if set(fault.affected_assignment_ids) <= set(fault.decided_assignment_ids):
            fault.open = False
        self.adjudications.append(
            {
                "assignment_id": assignment.assignment_id,
                "fault_id": fault.fault_id,
                "decision": decision.value,
                "rationale": payload["rationale"],
                "decided_by": payload["decided_by"],
                "at": at,
            }
        )

    def _on_appeal_filed(self, payload: dict[str, Any], at: str) -> None:
        self.appeals[payload["appeal_id"]] = AppealRecord(
            appeal_id=payload["appeal_id"],
            contestant_id=payload["contestant_id"],
            grounds=payload["grounds"],
            at=at,
            frozen_contestant_ids=list(payload["frozen_contestant_ids"]),
        )
        self._appeal_seq = max(self._appeal_seq, int(payload["appeal_id"][2:]))

    def _on_appeal_ruled(self, payload: dict[str, Any], at: str) -> None:
        appeal = self.appeals[payload["appeal_id"]]
        appeal.status = AppealStatus.UPHELD if payload["upheld"] else AppealStatus.REJECTED
        appeal.rationale = payload["rationale"]
        appeal.affected_candidate_ids = list(payload["affected_candidate_ids"])
        for contestant_id, components in payload["adjustments"].items():
            score = self._score_of_contestant(contestant_id)
            if score is None:
                continue
            score.revisions.append(
                {
                    "components": score.components.to_dict(),
                    "reason": payload["rationale"],
                    "source": payload["appeal_id"],
                    "at": at,
                }
            )
            score.components = ScoreComponents.from_dict(components)

    def _on_tie_resolved(self, payload: dict[str, Any], at: str) -> None:
        self.tie_resolutions.append(
            TieResolution(
                trade=payload["trade"],
                ordered_candidate_ids=list(payload["ordered_candidate_ids"]),
                rationale=payload["rationale"],
                at=at,
            )
        )
