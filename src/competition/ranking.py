"""名次排列与奖项分配：并列时扩额并记录涉及的候选人。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RankRow:
    contestant_id: str
    total: float
    rank: int


def rank(totals: dict[str, float]) -> list[RankRow]:
    """按总分降序排出标准竞赛名次（并列同名次，后续名次跳号）。"""
    ordered = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    rows: list[RankRow] = []
    for contestant_id, total in ordered:
        position = 1 + sum(1 for _, other in ordered if other > total)
        rows.append(RankRow(contestant_id, total, position))
    return rows


def allocate_awards(
    rows: list[RankRow], quotas: dict[str, int]
) -> tuple[dict[str, str], list[tuple[str, ...]]]:
    """按奖项名额依次分配；边界处并列的选手一同获奖并记入并列组。

    返回（选手到奖项的映射, 并列组列表）。
    """
    awards: dict[str, str] = {}
    ties: list[tuple[str, ...]] = []
    index = 0
    for level, quota in quotas.items():
        if index >= len(rows):
            break
        group = list(rows[index : index + quota])
        boundary = group[-1].total
        tied = [row for row in group if row.total == boundary]
        cursor = index + len(group)
        while cursor < len(rows) and rows[cursor].total == boundary:
            tied.append(rows[cursor])
            group.append(rows[cursor])
            cursor += 1
        if len(tied) > 1 and cursor > index + quota:
            ties.append(tuple(row.contestant_id for row in tied))
        for row in group:
            awards[row.contestant_id] = level
        index = cursor
    return awards, ties
