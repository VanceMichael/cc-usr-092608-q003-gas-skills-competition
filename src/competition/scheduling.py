"""按工种、设备校准状态、选手时段与裁判回避关系排出可执行轮次。"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable

from .models import (
    Assignment,
    Contestant,
    EquipmentStatus,
    Judge,
    Recusal,
    Station,
    Trade,
)


def plan_rounds(
    contestants: Iterable[Contestant],
    stations: Iterable[Station],
    judges: Iterable[Judge],
    declared_recusals: Iterable[Recusal],
    judges_per_station: int,
) -> tuple[list[Assignment], list[Recusal], list[str]]:
    """排出可执行轮次。

    只使用已校准的工位；裁判须具备对应工种资格、与选手不同单位且未申报回避；
    同一时段内一名裁判只上一个工位。返回（轮次安排, 排程回避记录, 未能安排的选手）。
    """
    active = [c for c in contestants if c.active]
    declared = {(r.judge_id, r.contestant_id) for r in declared_recusals}
    judge_list = list(judges)

    def recusal_reason(judge: Judge, contestant: Contestant) -> str | None:
        if judge.affiliation == contestant.team_id:
            return "本单位回避"
        if (judge.id, contestant.id) in declared:
            return "申报回避"
        return None

    stations_by_trade: dict[Trade, list[Station]] = defaultdict(list)
    for station in stations:
        if station.status == EquipmentStatus.CALIBRATED:
            stations_by_trade[station.trade].append(station)

    by_trade: dict[Trade, list[Contestant]] = defaultdict(list)
    for contestant in active:
        by_trade[contestant.trade].append(contestant)

    all_slots = sorted({slot for c in active for slot in c.slots})
    assignments: list[Assignment] = []
    recusals: list[Recusal] = []
    recorded_pairs: set[tuple[str, str]] = set()
    unscheduled: list[str] = []

    for trade, group in by_trade.items():
        remaining = deque(group)
        trade_stations = stations_by_trade.get(trade, [])
        if not trade_stations:
            unscheduled.extend(c.id for c in remaining)
            continue
        for slot in all_slots:
            if not remaining:
                break
            used_judges: set[str] = set()
            for station in trade_stations:
                chosen: tuple[Contestant, list[Judge]] | None = None
                for contestant in remaining:
                    if slot not in contestant.slots:
                        continue
                    eligible: list[Judge] = []
                    for judge in judge_list:
                        if trade not in judge.trades or judge.id in used_judges:
                            continue
                        reason = recusal_reason(judge, contestant)
                        if reason is not None:
                            pair = (judge.id, contestant.id)
                            if pair not in recorded_pairs:
                                recorded_pairs.add(pair)
                                recusals.append(
                                    Recusal(judge.id, contestant.id, reason, "排程")
                                )
                            continue
                        eligible.append(judge)
                    if len(eligible) >= judges_per_station:
                        chosen = (contestant, eligible[:judges_per_station])
                        break
                if chosen is None:
                    break
                contestant, panel = chosen
                remaining.remove(contestant)
                used_judges.update(j.id for j in panel)
                assignments.append(
                    Assignment(
                        id=f"R{slot}-{station.id}-{contestant.id}",
                        round_no=slot,
                        station_id=station.id,
                        contestant_id=contestant.id,
                        judge_ids=tuple(j.id for j in panel),
                    )
                )
        unscheduled.extend(c.id for c in remaining)

    return assignments, recusals, unscheduled
