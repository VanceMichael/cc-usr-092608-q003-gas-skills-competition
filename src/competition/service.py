"""现场竞赛运行服务。

以只增事件日志为事实来源：报名冻结、伤病替补、排程、评分、设备故障裁定、
申诉改判全部落为顺序事件，断电重启后按原顺序重放恢复；任何更正只追加，
不抹掉最初材料。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .journal import Journal
from .models import (
    TRADE_CODES,
    Adjudication,
    Appeal,
    AppealState,
    Assignment,
    AssignmentStatus,
    AwardFinalization,
    Component,
    Contestant,
    Correction,
    Decision,
    EquipmentStatus,
    Judge,
    Qualification,
    QuestionPackage,
    QuotaSlot,
    Recusal,
    ScoreEntry,
    ScoreState,
    Station,
    Substitution,
    Team,
    Trade,
)
from .ranking import allocate_awards, rank
from .scheduling import plan_rounds


class RuleViolation(Exception):
    """违反竞赛运行规则。"""


# 各分项取值范围；安全违规为扣分项
COMPONENT_LIMITS = {
    Component.THEORY: (0.0, 100.0),
    Component.PRACTICAL: (0.0, 100.0),
    Component.EMERGENCY: (0.0, 100.0),
    Component.SAFETY: (0.0, 50.0),
}

# 计入名次的成绩状态
FINALIZED_STATES = (ScoreState.REVIEWED, ScoreState.CONFIRMED)

DEFAULT_AWARD_QUOTAS = {"一等奖": 1, "二等奖": 2, "三等奖": 3}


class State:
    """由事件日志重放得到的内存状态。"""

    def __init__(self) -> None:
        self.config: dict[str, Any] = {}
        self.teams: dict[str, Team] = {}
        self.contestants: dict[str, Contestant] = {}
        self.judges: dict[str, Judge] = {}
        self.stations: dict[str, Station] = {}
        self.packages: dict[str, QuestionPackage] = {}
        self.closed = False
        self.slots: dict[str, QuotaSlot] = {}
        self.substitutions: list[Substitution] = []
        self.qualification_corrections: list[Correction] = []
        self.declared_recusals: list[Recusal] = []
        self.schedule_recusals: list[Recusal] = []
        self.assignments: list[Assignment] = []
        self.unscheduled: list[str] = []
        self.check_ins: list[dict[str, str]] = []
        self.scores: dict[str, ScoreEntry] = {}
        self.adjudications: list[Adjudication] = []
        self.incidents: list[dict[str, str]] = []
        self.published: dict[Trade, datetime] = {}
        self.appeals: dict[str, Appeal] = {}
        self.awards: dict[Trade, AwardFinalization] = {}


class CompetitionService:
    """燃气技能竞赛现场运行服务。"""

    def __init__(self, journal: Journal, state: State) -> None:
        self.journal = journal
        self.state = state

    # ------------------------------------------------------------------
    # 打开与恢复
    # ------------------------------------------------------------------
    @classmethod
    def create(
        cls,
        data_dir: str | Path,
        *,
        appeal_window: timedelta = timedelta(hours=2),
        judges_per_station: int = 2,
        award_quotas: dict[str, int] | None = None,
    ) -> "CompetitionService":
        """在空目录中创建赛事；目录已有记录时拒绝覆盖。"""
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        journal = Journal(data_dir / "journal.jsonl")
        if journal.events():
            raise RuleViolation("数据目录已存在赛事记录")
        service = cls(journal, State())
        quotas = award_quotas or DEFAULT_AWARD_QUOTAS
        if not quotas or any(count < 1 for count in quotas.values()):
            raise RuleViolation("奖项名额无效")
        service._append(
            "config",
            {
                "appeal_window_seconds": appeal_window.total_seconds(),
                "judges_per_station": judges_per_station,
                "award_quotas": dict(quotas),
            },
        )
        return service

    @classmethod
    def open(cls, data_dir: str | Path) -> "CompetitionService":
        """断电重启后重开：按原顺序重放事件恢复现场。"""
        journal = Journal(Path(data_dir) / "journal.jsonl")
        service = cls(journal, State())
        for event in journal.events():
            service._apply(event)
        if not service.state.config:
            raise RuleViolation("数据目录中没有赛事记录")
        return service

    # ------------------------------------------------------------------
    # 事件落盘与重放
    # ------------------------------------------------------------------
    def _append(self, kind: str, payload: dict[str, Any]) -> None:
        self._apply(self.journal.append(kind, payload))

    def _apply(self, event: dict[str, Any]) -> None:
        handler = getattr(self, f"_on_{event['kind']}")
        handler(event["payload"])

    @staticmethod
    def _require(condition: bool, message: str) -> None:
        if not condition:
            raise RuleViolation(message)

    # ------------------------------------------------------------------
    # 报名与资格
    # ------------------------------------------------------------------
    def register_team(self, team_id: str, name: str, quotas: dict[Trade, int]) -> None:
        self._require(not self.state.closed, "报名已截止")
        self._require(team_id not in self.state.teams, "代表队已存在")
        self._require(bool(name.strip()), "代表队名称不能为空")
        self._require(bool(quotas) and all(n > 0 for n in quotas.values()), "名额配置无效")
        self._append(
            "team_registered",
            {"id": team_id, "name": name, "quotas": {t.value: n for t, n in quotas.items()}},
        )

    def register_contestant(
        self,
        contestant_id: str,
        name: str,
        team_id: str,
        trade: Trade,
        qualification: Qualification,
        slots: Iterable[int],
    ) -> None:
        self._require(not self.state.closed, "报名已截止，名额与资格已固定")
        self._require(contestant_id not in self.state.contestants, "选手已存在")
        team = self._team(team_id)
        self._require(trade in team.quotas, "代表队未配置该工种名额")
        taken = sum(
            1
            for c in self.state.contestants.values()
            if c.team_id == team_id and c.trade == trade
        )
        self._require(taken < team.quotas[trade], "该工种名额已满")
        self._require(bool(name.strip()), "选手姓名不能为空")
        self._require(
            all(isinstance(v, str) and v.strip() for v in vars(qualification).values()),
            "资格材料不完整",
        )
        slot_tuple = tuple(sorted({int(s) for s in slots}))
        self._require(bool(slot_tuple), "选手时段不能为空")
        self._append(
            "contestant_registered",
            {
                "id": contestant_id,
                "name": name,
                "team_id": team_id,
                "trade": trade.value,
                "qualification": vars(qualification),
                "slots": list(slot_tuple),
            },
        )

    def register_judge(
        self, judge_id: str, name: str, affiliation: str, trades: Iterable[Trade]
    ) -> None:
        self._require(judge_id not in self.state.judges, "裁判已存在")
        trade_tuple = tuple(dict.fromkeys(trades))
        self._require(bool(trade_tuple), "裁判资格工种不能为空")
        self._require(bool(affiliation.strip()), "裁判单位不能为空")
        self._append(
            "judge_registered",
            {
                "id": judge_id,
                "name": name,
                "affiliation": affiliation,
                "trades": [t.value for t in trade_tuple],
            },
        )

    def register_station(
        self,
        station_id: str,
        trade: Trade,
        status: EquipmentStatus = EquipmentStatus.CALIBRATED,
    ) -> None:
        self._require(station_id not in self.state.stations, "工位已存在")
        self._append(
            "station_registered",
            {"id": station_id, "trade": trade.value, "status": status.value},
        )

    def register_question_package(self, package_id: str, trade: Trade, version: int) -> None:
        self._require(package_id not in self.state.packages, "赛题包已存在")
        self._require(isinstance(version, int) and version >= 1, "赛题版本无效")
        self._append(
            "package_registered",
            {"id": package_id, "trade": trade.value, "version": version, "sealed": True},
        )

    def close_registration(self, at: datetime) -> None:
        """报名截止：固定代表队名额与选手资格，生成名额编号。"""
        self._require(not self.state.closed, "报名已截止")
        slots: list[dict[str, str]] = []
        for team in self.state.teams.values():
            for trade in Trade:
                members = [
                    c
                    for c in self.state.contestants.values()
                    if c.team_id == team.id and c.trade == trade
                ]
                for index, contestant in enumerate(members, start=1):
                    slots.append(
                        {
                            "id": f"{team.id}-{TRADE_CODES[trade]}-{index}",
                            "team_id": team.id,
                            "trade": trade.value,
                            "contestant_id": contestant.id,
                        }
                    )
        self._append("registration_closed", {"slots": slots, "at": at.isoformat()})

    def substitute(
        self,
        out_contestant_id: str,
        in_contestant_id: str,
        name: str,
        qualification: Qualification,
        slots: Iterable[int],
        reason: str,
        region_note: str,
        at: datetime,
    ) -> None:
        """伤病替补：沿用原名额，赛区须说明原因，最初材料保留。"""
        self._require(self.state.closed, "报名截止后才能办理伤病替补")
        out = self._contestant(out_contestant_id)
        self._require(out.active, "原选手已不在赛")
        self._require(in_contestant_id not in self.state.contestants, "替补选手已存在")
        self._require(bool(reason.strip()), "替补原因不能为空")
        self._require(bool(region_note.strip()), "赛区说明不能为空")
        self._require(
            all(isinstance(v, str) and v.strip() for v in vars(qualification).values()),
            "资格材料不完整",
        )
        slot_tuple = tuple(sorted({int(s) for s in slots}))
        self._require(bool(slot_tuple), "选手时段不能为空")
        self._append(
            "substituted",
            {
                "slot_id": out.slot_id,
                "out": out_contestant_id,
                "in_contestant": {
                    "id": in_contestant_id,
                    "name": name,
                    "team_id": out.team_id,
                    "trade": out.trade.value,
                    "qualification": vars(qualification),
                    "slots": list(slot_tuple),
                    "slot_id": out.slot_id,
                },
                "reason": reason,
                "region_note": region_note,
                "at": at.isoformat(),
            },
        )

    def correct_qualification(
        self,
        contestant_id: str,
        field: str,
        new_value: str,
        reason: str,
        at: datetime,
    ) -> None:
        """更正资格材料：只追加更正记录，最初材料保留在日志中。"""
        contestant = self._contestant(contestant_id)
        self._require(field in vars(contestant.qualification), "资格字段不存在")
        self._require(bool(new_value.strip()), "更正内容不能为空")
        self._require(bool(reason.strip()), "更正原因不能为空")
        old = getattr(contestant.qualification, field)
        self._append(
            "qualification_corrected",
            {
                "target": contestant_id,
                "field": field,
                "old": old,
                "new": new_value,
                "reason": reason,
                "at": at.isoformat(),
            },
        )

    def declare_recusal(self, judge_id: str, contestant_id: str, reason: str, at: datetime) -> None:
        """登记申报回避关系，供排程与评分共同遵守。"""
        self._judge(judge_id)
        self._contestant(contestant_id)
        self._require(bool(reason.strip()), "回避原因不能为空")
        self._append(
            "recusal_declared",
            {
                "judge_id": judge_id,
                "contestant_id": contestant_id,
                "reason": reason,
                "at": at.isoformat(),
            },
        )

    # ------------------------------------------------------------------
    # 排程
    # ------------------------------------------------------------------
    def schedule(self, at: datetime) -> dict[str, Any]:
        """按工种、设备校准状态、选手时段与裁判回避排出可执行轮次。"""
        self._require(self.state.closed, "报名未截止，不能排程")
        self._require(not self.state.assignments, "轮次已排出")
        assignments, recusals, unscheduled = plan_rounds(
            self.state.contestants.values(),
            self.state.stations.values(),
            self.state.judges.values(),
            self.state.declared_recusals,
            self.state.config["judges_per_station"],
        )
        self._append(
            "scheduled",
            {
                "assignments": [
                    {
                        "id": a.id,
                        "round_no": a.round_no,
                        "station_id": a.station_id,
                        "contestant_id": a.contestant_id,
                        "judge_ids": list(a.judge_ids),
                    }
                    for a in assignments
                ],
                "recusals": [
                    {
                        "judge_id": r.judge_id,
                        "contestant_id": r.contestant_id,
                        "reason": r.reason,
                    }
                    for r in recusals
                ],
                "unscheduled": list(unscheduled),
                "at": at.isoformat(),
            },
        )
        return {
            "scheduled": len(assignments),
            "unscheduled": list(unscheduled),
        }

    # ------------------------------------------------------------------
    # 签到与赛题
    # ------------------------------------------------------------------
    def check_in(self, contestant_id: str, at: datetime) -> None:
        contestant = self._contestant(contestant_id)
        self._require(contestant.active, "选手已不在赛")
        self._require(
            all(c["contestant_id"] != contestant_id for c in self.state.check_ins),
            "选手已签到",
        )
        self._append("checked_in", {"contestant_id": contestant_id, "at": at.isoformat()})

    def unseal_questions(self, package_id: str, at: datetime) -> None:
        package = self._package(package_id)
        self._require(package.sealed, "赛题已启封")
        self._append("questions_unsealed", {"package_id": package_id, "at": at.isoformat()})

    # ------------------------------------------------------------------
    # 评分：裁评分离
    # ------------------------------------------------------------------
    def judge_tasks(self, judge_id: str) -> list[dict[str, Any]]:
        """裁判可见的评分任务：不含本单位及已申报回避的选手。"""
        judge = self._judge(judge_id)
        tasks = []
        for assignment in self.state.assignments:
            if judge_id not in assignment.judge_ids:
                continue
            contestant = self.state.contestants[assignment.contestant_id]
            if self._is_recused(judge, contestant):
                continue
            tasks.append(
                {
                    "assignment_id": assignment.id,
                    "round_no": assignment.round_no,
                    "station_id": assignment.station_id,
                    "contestant_id": contestant.id,
                    "status": assignment.status.value,
                }
            )
        return sorted(tasks, key=lambda item: item["round_no"])

    def record_score(
        self,
        recorder_id: str,
        contestant_id: str,
        component: Component,
        value: float,
        at: datetime,
    ) -> str:
        """录入分项成绩：仅本工位未回避裁判可录，返回成绩编号。"""
        contestant = self._contestant(contestant_id)
        self._require(contestant.active, "选手已不在赛")
        judge = self._judge(recorder_id)
        assignment = next(
            (
                a
                for a in self.state.assignments
                if a.contestant_id == contestant_id
                and a.status == AssignmentStatus.SCHEDULED
            ),
            None,
        )
        self._require(assignment is not None, "该选手无待执行轮次")
        self._require(recorder_id in assignment.judge_ids, "裁判未分配至该工位")
        self._require(not self._is_recused(judge, contestant), "裁判须回避本单位选手")
        self._check_bounds(component, value)
        self._require(
            self._active_entry(contestant_id, component) is None,
            "该分项成绩已录入，需更正或经裁定重赛",
        )
        attempt = 1 + sum(
            1
            for e in self.state.scores.values()
            if e.contestant_id == contestant_id
            and e.component == component
            and e.state == ScoreState.SUPERSEDED
        )
        entry_id = f"{contestant_id}:{component.value}:{attempt}"
        self._append(
            "score_recorded",
            {
                "id": entry_id,
                "contestant_id": contestant_id,
                "station_id": assignment.station_id,
                "round_no": assignment.round_no,
                "component": component.value,
                "value": float(value),
                "recorder": recorder_id,
                "attempt": attempt,
                "at": at.isoformat(),
            },
        )
        return entry_id

    def review_score(self, entry_id: str, reviewer_id: str, at: datetime) -> None:
        """复核成绩：录分者不能复核自己的结果。"""
        entry = self._entry(entry_id)
        self._judge(reviewer_id)
        self._require(entry.state == ScoreState.RECORDED, "成绩状态不允许复核")
        self._require(entry.recorder != reviewer_id, "录分者不能复核自己的结果")
        self._append(
            "score_reviewed",
            {"id": entry_id, "reviewer": reviewer_id, "at": at.isoformat()},
        )

    def correct_score(
        self, entry_id: str, new_value: float, reason: str, at: datetime
    ) -> None:
        """更正成绩：原值保留在更正记录中。"""
        entry = self._entry(entry_id)
        self._require(
            entry.state in (ScoreState.RECORDED, ScoreState.REVIEWED),
            "当前状态不允许更正",
        )
        self._record_correction(entry, new_value, reason, at)

    # ------------------------------------------------------------------
    # 设备故障与裁定
    # ------------------------------------------------------------------
    def report_equipment_failure(self, station_id: str, note: str, at: datetime) -> None:
        """设备停用：仅关联工位的成绩进入待裁定，其余轮次数据不受影响。"""
        station = self._station(station_id)
        self._require(station.status == EquipmentStatus.CALIBRATED, "设备已停用")
        entries = [
            {"id": e.id, "before": e.state.value}
            for e in self.state.scores.values()
            if e.station_id == station_id
            and e.state in (ScoreState.RECORDED, ScoreState.REVIEWED)
        ]
        blocked = [
            a.id
            for a in self.state.assignments
            if a.station_id == station_id and a.status == AssignmentStatus.SCHEDULED
        ]
        self._append(
            "equipment_failed",
            {
                "station_id": station_id,
                "note": note,
                "entries": entries,
                "assignments": blocked,
                "at": at.isoformat(),
            },
        )

    def restore_equipment(self, station_id: str, at: datetime) -> None:
        station = self._station(station_id)
        self._require(station.status == EquipmentStatus.OUT_OF_SERVICE, "设备未停用")
        self._append(
            "equipment_restored", {"station_id": station_id, "at": at.isoformat()}
        )

    def adjudicate(
        self,
        director: str,
        station_id: str,
        decision: Decision,
        reason: str,
        at: datetime,
    ) -> str:
        """赛事负责人逐工位裁定续赛、重赛或保留；不得批量清空整轮数据。"""
        self._station(station_id)
        self._require(bool(director.strip()), "赛事负责人不能为空")
        self._require(bool(reason.strip()), "裁定理由不能为空")
        affected = [
            e
            for e in self.state.scores.values()
            if e.station_id == station_id and e.state == ScoreState.PENDING
        ]
        self._require(bool(affected), "该工位无待裁定成绩")
        blocked = [
            a
            for a in self.state.assignments
            if a.station_id == station_id and a.status == AssignmentStatus.BLOCKED
        ]
        if decision == Decision.CONTINUE:
            entry_states = {e.id: (e.before_pending or ScoreState.RECORDED) for e in affected}
            assignment_status = AssignmentStatus.SCHEDULED
        elif decision == Decision.RERUN:
            entry_states = {e.id: ScoreState.SUPERSEDED for e in affected}
            assignment_status = AssignmentStatus.SCHEDULED
        else:
            entry_states = {e.id: ScoreState.CONFIRMED for e in affected}
            assignment_status = AssignmentStatus.COMPLETED
        adjudication_id = f"AJ{len(self.state.adjudications) + 1:02d}"
        self._append(
            "adjudicated",
            {
                "id": adjudication_id,
                "station_id": station_id,
                "decision": decision.value,
                "reason": reason,
                "director": director,
                "entries": [
                    {"id": entry_id, "state": state.value}
                    for entry_id, state in entry_states.items()
                ],
                "assignments": [
                    {"id": a.id, "status": assignment_status.value} for a in blocked
                ],
                "at": at.isoformat(),
            },
        )
        return adjudication_id

    # ------------------------------------------------------------------
    # 成绩公布、申诉与奖项
    # ------------------------------------------------------------------
    def publish_results(self, trade: Trade, at: datetime) -> None:
        """公布工种成绩：存在待裁定成绩时不得公布，申诉期自公布起算。"""
        self._require(trade not in self.state.published, "该工种成绩已公布")
        self._require(not self._has_pending(trade), "存在待裁定成绩，不能公布")
        self._append("results_published", {"trade": trade.value, "at": at.isoformat()})

    def appeal_deadline(self, trade: Trade) -> datetime | None:
        published_at = self.state.published.get(trade)
        if published_at is None:
            return None
        return published_at + timedelta(
            seconds=self.state.config["appeal_window_seconds"]
        )

    def file_appeal(self, contestant_id: str, reason: str, at: datetime) -> str:
        """在申诉期内提出申诉；相关名次冻结，无关奖项不受影响。"""
        contestant = self._contestant(contestant_id)
        self._require(bool(reason.strip()), "申诉理由不能为空")
        deadline = self.appeal_deadline(contestant.trade)
        self._require(deadline is not None, "成绩尚未公布，申诉期未开始")
        self._require(at <= deadline, "申诉期已过")
        appeal_id = f"AP{len(self.state.appeals) + 1:02d}"
        self._append(
            "appeal_filed",
            {
                "id": appeal_id,
                "contestant_id": contestant_id,
                "trade": contestant.trade.value,
                "reason": reason,
                "at": at.isoformat(),
            },
        )
        return appeal_id

    def resolve_appeal(
        self,
        appeal_id: str,
        uphold: bool,
        note: str,
        at: datetime,
        score_changes: dict[str, float] | None = None,
    ) -> None:
        """处理申诉：改判须说明影响了哪些候选人。"""
        appeal = self._appeal(appeal_id)
        self._require(appeal.state == AppealState.PENDING, "申诉已处理")
        self._require(appeal.trade not in self.state.awards, "奖项已终评，不能改判")
        self._require(bool(note.strip()), "处理说明不能为空")
        changes = score_changes or {}
        self._require(uphold or not changes, "驳回申诉不得附带改分")
        before = self._standing(appeal.trade)
        for entry_id, new_value in changes.items():
            entry = self._entry(entry_id)
            owner = self.state.contestants[entry.contestant_id]
            self._require(owner.trade == appeal.trade, "改判成绩不属于该申诉工种")
            self._require(
                entry.state in FINALIZED_STATES + (ScoreState.RECORDED,),
                "当前状态不允许改判",
            )
            self._record_correction(entry, new_value, f"申诉改判：{note}", at)
        after = self._standing(appeal.trade)
        affected = sorted(
            cid
            for cid in set(before) | set(after)
            if before.get(cid) != after.get(cid)
        )
        self._append(
            "appeal_resolved",
            {
                "id": appeal_id,
                "state": AppealState.UPHELD.value if uphold else AppealState.DISMISSED.value,
                "resolution": note,
                "affected_candidates": affected,
                "at": at.isoformat(),
            },
        )

    def award_frozen(self, trade: Trade) -> bool:
        """申诉期间冻结相关名次。"""
        return any(
            a.trade == trade and a.state == AppealState.PENDING
            for a in self.state.appeals.values()
        )

    def ranking(self, trade: Trade) -> list[dict[str, Any]]:
        """当前名次：理论、实操、应急加分，安全违规扣分，只计已复核与已确认成绩。"""
        return [
            {"选手": row.contestant_id, "总分": row.total, "名次": row.rank}
            for row in rank(self._totals(trade))
        ]

    def finalize_awards(self, trade: Trade, at: datetime) -> None:
        """终评奖项：并列处理记录影响的候选人；申诉未结或成绩待裁定时不得终评。"""
        self._require(trade in self.state.published, "成绩尚未公布")
        self._require(trade not in self.state.awards, "奖项已终评")
        self._require(not self.award_frozen(trade), "存在待审申诉，名次冻结中")
        self._require(not self._has_pending(trade), "存在待裁定成绩，不能终评")
        rows = rank(self._totals(trade))
        awards, ties = allocate_awards(rows, self.state.config["award_quotas"])
        affected = sorted({cid for group in ties for cid in group})
        self._append(
            "awards_finalized",
            {
                "trade": trade.value,
                "awards": awards,
                "ties": [list(group) for group in ties],
                "affected_candidates": affected,
                "at": at.isoformat(),
            },
        )

    # ------------------------------------------------------------------
    # 对外成绩单与内部审计
    # ------------------------------------------------------------------
    def public_transcript(self, trade: Trade | None = None) -> list[dict[str, Any]]:
        """对外成绩单：只呈现获奖所需信息。"""
        trades = [trade] if trade is not None else list(Trade)
        level_order = list(self.state.config["award_quotas"])
        rows = []
        for item in trades:
            self._require(item in self.state.awards, f"{item.value}奖项未终评")
            self._require(not self.award_frozen(item), f"{item.value}奖项处于申诉冻结期")
            finalization = self.state.awards[item]
            standing = self._standing(item)
            for contestant_id, level in finalization.awards.items():
                contestant = self.state.contestants[contestant_id]
                rows.append(
                    {
                        "工种": item.value,
                        "奖项": level,
                        "名次": standing[contestant_id][1],
                        "选手": contestant.name,
                        "代表队": self.state.teams[contestant.team_id].name,
                    }
                )
        return sorted(
            rows,
            key=lambda row: (
                list(Trade).index(Trade(row["工种"])),
                level_order.index(row["奖项"]),
                row["名次"],
                row["选手"],
            ),
        )

    def audit_report(self) -> dict[str, Any]:
        """内部审计：还原资格、工位安排、评分分项、回避记录与每次裁定。"""
        qualifications = []
        for contestant in self.state.contestants.values():
            qualifications.append(
                {
                    "选手": contestant.id,
                    "代表队": contestant.team_id,
                    "工种": contestant.trade.value,
                    "名额": contestant.slot_id,
                    "在赛": contestant.active,
                    "资格现状": dict(vars(contestant.qualification)),
                    "更正历史": [
                        {
                            "字段": c.field,
                            "原值": c.old,
                            "新值": c.new,
                            "原因": c.reason,
                            "时间": c.at,
                        }
                        for c in self.state.qualification_corrections
                        if c.target == contestant.id
                    ],
                }
            )
        scores = []
        for entry in self.state.scores.values():
            scores.append(
                {
                    "编号": entry.id,
                    "选手": entry.contestant_id,
                    "工位": entry.station_id,
                    "轮次": entry.round_no,
                    "分项": entry.component.value,
                    "分值": entry.value,
                    "状态": entry.state.value,
                    "录分人": entry.recorder,
                    "复核人": entry.reviewer,
                    "次数": entry.attempt,
                    "更正历史": [
                        {"原值": c.old, "新值": c.new, "原因": c.reason, "时间": c.at}
                        for c in entry.corrections
                    ],
                }
            )
        return {
            "选手资格": qualifications,
            "名额与替补": {
                "名额": [
                    {"编号": s.id, "代表队": s.team_id, "工种": s.trade.value, "占用": s.contestant_id}
                    for s in self.state.slots.values()
                ],
                "替补": [
                    {
                        "名额": s.slot_id,
                        "下场": s.out_contestant,
                        "上场": s.in_contestant,
                        "原因": s.reason,
                        "赛区说明": s.region_note,
                        "时间": s.at,
                    }
                    for s in self.state.substitutions
                ],
            },
            "工位安排": [
                {
                    "编号": a.id,
                    "轮次": a.round_no,
                    "工位": a.station_id,
                    "选手": a.contestant_id,
                    "裁判": list(a.judge_ids),
                    "状态": a.status.value,
                }
                for a in self.state.assignments
            ],
            "评分分项": scores,
            "回避记录": [
                {"裁判": r.judge_id, "选手": r.contestant_id, "原因": r.reason, "来源": r.source}
                for r in self.state.declared_recusals + self.state.schedule_recusals
            ],
            "设备事件": list(self.state.incidents),
            "裁定": [
                {
                    "编号": a.id,
                    "工位": a.station_id,
                    "决定": a.decision.value,
                    "理由": a.reason,
                    "负责人": a.director,
                    "涉及成绩": list(a.affected_entries),
                    "时间": a.at,
                }
                for a in self.state.adjudications
            ],
            "申诉": [
                {
                    "编号": a.id,
                    "选手": a.contestant_id,
                    "工种": a.trade.value,
                    "理由": a.reason,
                    "状态": a.state.value,
                    "处理说明": a.resolution,
                    "影响候选人": list(a.affected_candidates),
                }
                for a in self.state.appeals.values()
            ],
            "签到": list(self.state.check_ins),
            "赛题": [
                {"编号": p.id, "工种": p.trade.value, "版本": p.version, "封存": p.sealed}
                for p in self.state.packages.values()
            ],
            "奖项": {
                trade.value: {
                    "获奖": dict(finalization.awards),
                    "并列组": [list(group) for group in finalization.ties],
                    "影响候选人": list(finalization.affected_candidates),
                }
                for trade, finalization in self.state.awards.items()
            },
        }

    # ------------------------------------------------------------------
    # 内部查询
    # ------------------------------------------------------------------
    def _team(self, team_id: str) -> Team:
        self._require(team_id in self.state.teams, "代表队不存在")
        return self.state.teams[team_id]

    def _contestant(self, contestant_id: str) -> Contestant:
        self._require(contestant_id in self.state.contestants, "选手不存在")
        return self.state.contestants[contestant_id]

    def _judge(self, judge_id: str) -> Judge:
        self._require(judge_id in self.state.judges, "裁判不存在")
        return self.state.judges[judge_id]

    def _station(self, station_id: str) -> Station:
        self._require(station_id in self.state.stations, "工位不存在")
        return self.state.stations[station_id]

    def _package(self, package_id: str) -> QuestionPackage:
        self._require(package_id in self.state.packages, "赛题包不存在")
        return self.state.packages[package_id]

    def _entry(self, entry_id: str) -> ScoreEntry:
        self._require(entry_id in self.state.scores, "成绩不存在")
        return self.state.scores[entry_id]

    def _appeal(self, appeal_id: str) -> Appeal:
        self._require(appeal_id in self.state.appeals, "申诉不存在")
        return self.state.appeals[appeal_id]

    def _is_recused(self, judge: Judge, contestant: Contestant) -> bool:
        if judge.affiliation == contestant.team_id:
            return True
        return any(
            r.judge_id == judge.id and r.contestant_id == contestant.id
            for r in self.state.declared_recusals
        )

    def _active_entry(self, contestant_id: str, component: Component) -> ScoreEntry | None:
        entries = [
            e
            for e in self.state.scores.values()
            if e.contestant_id == contestant_id
            and e.component == component
            and e.state != ScoreState.SUPERSEDED
        ]
        return max(entries, key=lambda e: e.attempt, default=None)

    def _has_pending(self, trade: Trade) -> bool:
        return any(
            e.state == ScoreState.PENDING
            and self.state.contestants[e.contestant_id].trade == trade
            for e in self.state.scores.values()
        )

    def _totals(self, trade: Trade) -> dict[str, float]:
        totals: dict[str, float] = {}
        for contestant in self.state.contestants.values():
            if contestant.trade != trade or not contestant.active:
                continue
            total = 0.0
            for component in Component:
                entry = self._active_entry(contestant.id, component)
                if entry is not None and entry.state in FINALIZED_STATES:
                    total += -entry.value if component == Component.SAFETY else entry.value
            totals[contestant.id] = round(total, 6)
        return totals

    def _standing(self, trade: Trade) -> dict[str, tuple[float, int, str | None]]:
        rows = rank(self._totals(trade))
        awards, _ = allocate_awards(rows, self.state.config["award_quotas"])
        return {
            row.contestant_id: (row.total, row.rank, awards.get(row.contestant_id))
            for row in rows
        }

    def _check_bounds(self, component: Component, value: float) -> None:
        low, high = COMPONENT_LIMITS[component]
        self._require(
            isinstance(value, (int, float)) and not isinstance(value, bool),
            "分值无效",
        )
        self._require(low <= float(value) <= high, f"{component.value}分值超出范围")

    def _record_correction(
        self, entry: ScoreEntry, new_value: float, reason: str, at: datetime
    ) -> None:
        self._check_bounds(entry.component, new_value)
        self._require(bool(reason.strip()), "更正原因不能为空")
        self._append(
            "score_corrected",
            {
                "id": entry.id,
                "old": entry.value,
                "new": float(new_value),
                "reason": reason,
                "at": at.isoformat(),
            },
        )

    # ------------------------------------------------------------------
    # 事件重放
    # ------------------------------------------------------------------
    def _on_config(self, payload: dict[str, Any]) -> None:
        self.state.config = payload

    def _on_team_registered(self, payload: dict[str, Any]) -> None:
        self.state.teams[payload["id"]] = Team(
            payload["id"],
            payload["name"],
            {Trade(k): v for k, v in payload["quotas"].items()},
        )

    @staticmethod
    def _contestant_from(payload: dict[str, Any]) -> Contestant:
        qualification = Qualification(**payload["qualification"])
        return Contestant(
            id=payload["id"],
            name=payload["name"],
            team_id=payload["team_id"],
            trade=Trade(payload["trade"]),
            qualification=qualification,
            slots=tuple(payload["slots"]),
            slot_id=payload.get("slot_id"),
        )

    def _on_contestant_registered(self, payload: dict[str, Any]) -> None:
        self.state.contestants[payload["id"]] = self._contestant_from(payload)

    def _on_judge_registered(self, payload: dict[str, Any]) -> None:
        self.state.judges[payload["id"]] = Judge(
            payload["id"],
            payload["name"],
            payload["affiliation"],
            tuple(Trade(t) for t in payload["trades"]),
        )

    def _on_station_registered(self, payload: dict[str, Any]) -> None:
        self.state.stations[payload["id"]] = Station(
            payload["id"], Trade(payload["trade"]), EquipmentStatus(payload["status"])
        )

    def _on_package_registered(self, payload: dict[str, Any]) -> None:
        self.state.packages[payload["id"]] = QuestionPackage(
            payload["id"], Trade(payload["trade"]), payload["version"], payload["sealed"]
        )

    def _on_registration_closed(self, payload: dict[str, Any]) -> None:
        self.state.closed = True
        for item in payload["slots"]:
            slot = QuotaSlot(
                item["id"], item["team_id"], Trade(item["trade"]), item["contestant_id"]
            )
            self.state.slots[slot.id] = slot
            self.state.contestants[slot.contestant_id].slot_id = slot.id

    def _on_substituted(self, payload: dict[str, Any]) -> None:
        out = self.state.contestants[payload["out"]]
        out.active = False
        newcomer = self._contestant_from(payload["in_contestant"])
        self.state.contestants[newcomer.id] = newcomer
        self.state.slots[payload["slot_id"]].contestant_id = newcomer.id
        for assignment in self.state.assignments:
            if assignment.contestant_id == out.id:
                assignment.contestant_id = newcomer.id
        self.state.substitutions.append(
            Substitution(
                payload["slot_id"],
                payload["out"],
                newcomer.id,
                payload["reason"],
                payload["region_note"],
                payload["at"],
            )
        )

    def _on_qualification_corrected(self, payload: dict[str, Any]) -> None:
        correction = Correction(
            payload["target"],
            payload["field"],
            payload["old"],
            payload["new"],
            payload["reason"],
            payload["at"],
        )
        self.state.qualification_corrections.append(correction)
        contestant = self.state.contestants[payload["target"]]
        setattr(contestant.qualification, payload["field"], payload["new"])

    def _on_recusal_declared(self, payload: dict[str, Any]) -> None:
        self.state.declared_recusals.append(
            Recusal(
                payload["judge_id"], payload["contestant_id"], payload["reason"], "申报"
            )
        )

    def _on_scheduled(self, payload: dict[str, Any]) -> None:
        self.state.assignments = [
            Assignment(
                id=item["id"],
                round_no=item["round_no"],
                station_id=item["station_id"],
                contestant_id=item["contestant_id"],
                judge_ids=tuple(item["judge_ids"]),
            )
            for item in payload["assignments"]
        ]
        self.state.schedule_recusals = [
            Recusal(item["judge_id"], item["contestant_id"], item["reason"], "排程")
            for item in payload["recusals"]
        ]
        self.state.unscheduled = list(payload["unscheduled"])

    def _on_checked_in(self, payload: dict[str, Any]) -> None:
        self.state.check_ins.append(
            {"contestant_id": payload["contestant_id"], "at": payload["at"]}
        )

    def _on_questions_unsealed(self, payload: dict[str, Any]) -> None:
        self.state.packages[payload["package_id"]].sealed = False

    def _on_score_recorded(self, payload: dict[str, Any]) -> None:
        self.state.scores[payload["id"]] = ScoreEntry(
            id=payload["id"],
            contestant_id=payload["contestant_id"],
            station_id=payload["station_id"],
            round_no=payload["round_no"],
            component=Component(payload["component"]),
            value=payload["value"],
            recorder=payload["recorder"],
            attempt=payload["attempt"],
        )

    def _on_score_reviewed(self, payload: dict[str, Any]) -> None:
        entry = self.state.scores[payload["id"]]
        entry.reviewer = payload["reviewer"]
        entry.state = ScoreState.REVIEWED

    def _on_score_corrected(self, payload: dict[str, Any]) -> None:
        entry = self.state.scores[payload["id"]]
        entry.corrections.append(
            Correction(
                payload["id"], "value", payload["old"], payload["new"],
                payload["reason"], payload["at"],
            )
        )
        entry.value = payload["new"]

    def _on_equipment_failed(self, payload: dict[str, Any]) -> None:
        self.state.stations[payload["station_id"]].status = EquipmentStatus.OUT_OF_SERVICE
        for item in payload["entries"]:
            entry = self.state.scores[item["id"]]
            entry.before_pending = ScoreState(item["before"])
            entry.state = ScoreState.PENDING
        for assignment_id in payload["assignments"]:
            self._assignment(assignment_id).status = AssignmentStatus.BLOCKED
        self.state.incidents.append(
            {
                "工位": payload["station_id"],
                "事件": "设备停用",
                "说明": payload["note"],
                "时间": payload["at"],
            }
        )

    def _on_equipment_restored(self, payload: dict[str, Any]) -> None:
        self.state.stations[payload["station_id"]].status = EquipmentStatus.CALIBRATED
        self.state.incidents.append(
            {"工位": payload["station_id"], "事件": "恢复校准", "说明": "", "时间": payload["at"]}
        )

    def _on_adjudicated(self, payload: dict[str, Any]) -> None:
        for item in payload["entries"]:
            entry = self.state.scores[item["id"]]
            entry.state = ScoreState(item["state"])
            entry.before_pending = None
        for item in payload["assignments"]:
            self._assignment(item["id"]).status = AssignmentStatus(item["status"])
        self.state.adjudications.append(
            Adjudication(
                id=payload["id"],
                station_id=payload["station_id"],
                decision=Decision(payload["decision"]),
                reason=payload["reason"],
                director=payload["director"],
                affected_entries=tuple(item["id"] for item in payload["entries"]),
                at=payload["at"],
            )
        )

    def _on_results_published(self, payload: dict[str, Any]) -> None:
        self.state.published[Trade(payload["trade"])] = datetime.fromisoformat(
            payload["at"]
        )

    def _on_appeal_filed(self, payload: dict[str, Any]) -> None:
        self.state.appeals[payload["id"]] = Appeal(
            id=payload["id"],
            contestant_id=payload["contestant_id"],
            trade=Trade(payload["trade"]),
            reason=payload["reason"],
            filed_at=payload["at"],
        )

    def _on_appeal_resolved(self, payload: dict[str, Any]) -> None:
        appeal = self.state.appeals[payload["id"]]
        appeal.state = AppealState(payload["state"])
        appeal.resolution = payload["resolution"]
        appeal.affected_candidates = tuple(payload["affected_candidates"])
        appeal.resolved_at = payload["at"]

    def _on_awards_finalized(self, payload: dict[str, Any]) -> None:
        trade = Trade(payload["trade"])
        self.state.awards[trade] = AwardFinalization(
            trade=trade,
            awards=dict(payload["awards"]),
            ties=tuple(tuple(group) for group in payload["ties"]),
            affected_candidates=tuple(payload["affected_candidates"]),
            at=payload["at"],
        )

    def _assignment(self, assignment_id: str) -> Assignment:
        for assignment in self.state.assignments:
            if assignment.id == assignment_id:
                return assignment
        raise RuleViolation("轮次安排不存在")
