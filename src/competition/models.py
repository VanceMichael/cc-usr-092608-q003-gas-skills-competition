"""燃气技能竞赛现场运行服务的领域模型。

现场竞赛运行服务管理报名冻结、赛程资源、评分证据、回避与申诉，
所有实体状态都由事件日志重放得到，本模块只描述内存中的形态。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CompetitionError(Exception):
    """现场竞赛运行服务的业务约束被违反。"""


class SchedulingError(CompetitionError):
    """现有工种、设备、时段或裁判资源无法排出可执行轮次。"""


class RecusalError(CompetitionError):
    """裁判与选手存在回避关系，评分任务不得下发。"""


class Trade(str, Enum):
    """竞赛设置的两个工种。"""

    INSTALLATION_REPAIR = "燃气用户安装检修工"
    PIPELINE_OPERATION = "燃气管网运行工"

    @classmethod
    def coerce(cls, value: str) -> "Trade":
        try:
            return cls(value)
        except ValueError:
            raise CompetitionError(f"未知工种: {value}") from None


class ContestantStatus(str, Enum):
    ACTIVE = "active"  # 在赛
    REPLACED = "replaced"  # 已被伤病替补替换，原始材料保留


class ScoreStatus(str, Enum):
    RECORDED = "recorded"  # 已录入，待复核
    VERIFIED = "verified"  # 已复核，参与排名
    PENDING_ADJUDICATION = "pending_adjudication"  # 设备故障，待赛事负责人裁定
    SUPERSEDED = "superseded"  # 被重赛成绩取代，原始记录保留备查


class AdjudicationDecision(str, Enum):
    RESUME = "resume"  # 续赛：成绩恢复到处置前状态，选手继续
    REMATCH = "rematch"  # 重赛：原成绩封存，另行安排补赛轮次
    KEEP = "keep"  # 保留：现有成绩直接生效

    @classmethod
    def coerce(cls, value: str) -> "AdjudicationDecision":
        try:
            return cls(value)
        except ValueError:
            raise CompetitionError(f"未知裁定方式: {value}") from None


class AppealStatus(str, Enum):
    PENDING = "pending"  # 待审，相关名次冻结
    UPHELD = "upheld"  # 申诉成立，已改判
    REJECTED = "rejected"  # 申诉驳回


# 选手资格三项材料：地方选拔、岗位经历、培训资格
QUALIFICATION_KEYS = ("local_selection", "work_experience", "training_certificate")


@dataclass(frozen=True)
class ScoreComponents:
    """分项成绩：理论题、实际操作、安全违规扣分、应急处置，分别计算。"""

    theory: float
    practical: float
    safety_deduction: float
    emergency: float

    def __post_init__(self) -> None:
        for name in ("theory", "practical", "safety_deduction", "emergency"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise CompetitionError(f"分项成绩无效: {name}")

    @property
    def total(self) -> float:
        return self.theory + self.practical + self.emergency - self.safety_deduction

    def to_dict(self) -> dict[str, float]:
        return {
            "theory": self.theory,
            "practical": self.practical,
            "safety_deduction": self.safety_deduction,
            "emergency": self.emergency,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScoreComponents":
        try:
            return cls(
                theory=data["theory"],
                practical=data["practical"],
                safety_deduction=data["safety_deduction"],
                emergency=data["emergency"],
            )
        except KeyError as exc:
            raise CompetitionError(f"分项成绩缺少字段: {exc.args[0]}") from None


@dataclass
class TeamState:
    team_id: str
    name: str
    quotas: dict[str, int]  # 工种 -> 冻结名额


@dataclass
class ContestantState:
    contestant_id: str
    name: str
    team_id: str
    trade: str
    slot_id: str  # 名额槽位，伤病替补沿用原槽位
    qualifications: dict[str, Any]  # 当前资格材料
    original_qualifications: dict[str, Any]  # 最初材料，任何更正都不得抹掉
    available_slots: list[str]
    status: ContestantStatus = ContestantStatus.ACTIVE
    checked_in: bool = False
    corrections: list[dict[str, Any]] = field(default_factory=list)  # 逐次更正记录
    replaced_by: str | None = None
    substitution: dict[str, Any] | None = None  # 替补来源说明（替补者持有）


@dataclass
class JudgeState:
    judge_id: str
    name: str
    affiliation: str  # 所属单位（与代表队同口径，用于回避）
    trades: list[str]


@dataclass
class StationState:
    station_id: str
    trade: str
    calibrated: bool = True
    active: bool = True
    disabled_reason: str | None = None


@dataclass
class RoundState:
    round_id: str
    trade: str
    slot: str
    assignment_ids: list[str] = field(default_factory=list)


@dataclass
class AssignmentState:
    assignment_id: str
    round_id: str
    contestant_id: str
    station_id: str
    judge_id: str
    trade: str
    slot: str
    blocked: bool = False  # 设备故障阻断，等待裁定
    superseded: bool = False  # 已被重赛安排取代


@dataclass
class ScoreRecord:
    assignment_id: str
    contestant_id: str
    judge_id: str
    components: ScoreComponents
    recorded_by: str
    status: ScoreStatus = ScoreStatus.RECORDED
    verified_by: str | None = None
    previous_status: ScoreStatus | None = None  # 故障前的状态，供续赛恢复
    revisions: list[dict[str, Any]] = field(default_factory=list)  # 改判历史，原件保留


@dataclass
class FaultRecord:
    fault_id: str
    station_id: str
    reason: str
    at: str
    affected_assignment_ids: list[str]
    decided_assignment_ids: list[str] = field(default_factory=list)
    open: bool = True


@dataclass
class AppealRecord:
    appeal_id: str
    contestant_id: str
    grounds: str
    at: str
    frozen_contestant_ids: list[str]  # 被冻结名次的选手
    status: AppealStatus = AppealStatus.PENDING
    rationale: str | None = None
    affected_candidate_ids: list[str] = field(default_factory=list)


@dataclass
class TieResolution:
    trade: str
    ordered_candidate_ids: list[str]  # 并列处理必须说明影响哪些候选人
    rationale: str
    at: str
