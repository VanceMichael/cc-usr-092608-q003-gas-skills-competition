"""现场竞赛运行服务的领域模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Trade(str, Enum):
    """竞赛工种。"""

    INSTALL_REPAIR = "燃气用户安装检修工"
    NETWORK_OPERATION = "燃气管网运行工"


# 名额编号使用的工种简称
TRADE_CODES = {
    Trade.INSTALL_REPAIR: "IR",
    Trade.NETWORK_OPERATION: "NO",
}


class Component(str, Enum):
    """分项成绩：理论题、实际操作、安全违规（扣分项）、应急处置。"""

    THEORY = "理论题"
    PRACTICAL = "实际操作"
    SAFETY = "安全违规"
    EMERGENCY = "应急处置"


class EquipmentStatus(str, Enum):
    """设备校准与停用状态。"""

    CALIBRATED = "已校准"
    OUT_OF_SERVICE = "停用"


class AssignmentStatus(str, Enum):
    """轮次安排的执行状态。"""

    SCHEDULED = "待执行"
    BLOCKED = "受阻"
    COMPLETED = "已完成"


class ScoreState(str, Enum):
    """成绩分项的流转状态。"""

    RECORDED = "已录入"
    REVIEWED = "已复核"
    PENDING = "待裁定"
    CONFIRMED = "已确认"
    SUPERSEDED = "已替代"


class Decision(str, Enum):
    """赛事负责人对设备故障关联成绩的裁定。"""

    CONTINUE = "续赛"
    RERUN = "重赛"
    KEEP = "保留"


class AppealState(str, Enum):
    """申诉处理状态。"""

    PENDING = "待审"
    UPHELD = "改判"
    DISMISSED = "驳回"


@dataclass
class Qualification:
    """报名截止时固定的选手资格材料：地方选拔、岗位经历、培训资格。"""

    selection: str
    experience: str
    training: str


@dataclass
class Team:
    id: str
    name: str
    quotas: dict[Trade, int]


@dataclass
class Contestant:
    id: str
    name: str
    team_id: str
    trade: Trade
    qualification: Qualification
    slots: tuple[int, ...]
    slot_id: str | None = None
    active: bool = True


@dataclass
class Judge:
    id: str
    name: str
    affiliation: str
    trades: tuple[Trade, ...]


@dataclass
class Station:
    id: str
    trade: Trade
    status: EquipmentStatus = EquipmentStatus.CALIBRATED


@dataclass
class QuestionPackage:
    id: str
    trade: Trade
    version: int
    sealed: bool = True


@dataclass
class QuotaSlot:
    id: str
    team_id: str
    trade: Trade
    contestant_id: str


@dataclass
class Substitution:
    """伤病替补记录：沿用原名额，并留存赛区说明。"""

    slot_id: str
    out_contestant: str
    in_contestant: str
    reason: str
    region_note: str
    at: str


@dataclass
class Correction:
    """更正记录：只追加，不抹掉最初材料。"""

    target: str
    field: str
    old: object
    new: object
    reason: str
    at: str


@dataclass
class Assignment:
    id: str
    round_no: int
    station_id: str
    contestant_id: str
    judge_ids: tuple[str, ...]
    status: AssignmentStatus = AssignmentStatus.SCHEDULED


@dataclass
class Recusal:
    """回避记录：申报回避或排程时按单位推导的回避。"""

    judge_id: str
    contestant_id: str
    reason: str
    source: str


@dataclass
class ScoreEntry:
    id: str
    contestant_id: str
    station_id: str
    round_no: int
    component: Component
    value: float
    recorder: str
    reviewer: str | None = None
    state: ScoreState = ScoreState.RECORDED
    attempt: int = 1
    before_pending: ScoreState | None = None
    corrections: list[Correction] = field(default_factory=list)


@dataclass
class Adjudication:
    """赛事负责人对关联工位待裁定成绩的逐条裁定。"""

    id: str
    station_id: str
    decision: Decision
    reason: str
    director: str
    affected_entries: tuple[str, ...]
    at: str


@dataclass
class Appeal:
    id: str
    contestant_id: str
    trade: Trade
    reason: str
    filed_at: str
    state: AppealState = AppealState.PENDING
    resolution: str | None = None
    affected_candidates: tuple[str, ...] = ()
    resolved_at: str | None = None


@dataclass
class AwardFinalization:
    """奖项终评结果：并列处理必须说明影响的候选人。"""

    trade: Trade
    awards: dict[str, str]
    ties: tuple[tuple[str, ...], ...]
    affected_candidates: tuple[str, ...]
    at: str
