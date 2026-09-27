"""跨计划共享的容量账册：印刷额度与合格原料的预留/确认。

容量池是主数据（总额度由运营维护），事件只表达占用：
- CAPACITY_RESERVED：某计划预留数量，可用额度随之扣减；
- CAPACITY_CONFIRMED：预留转为承诺，承诺量不得超过该计划的预留量。

多个计划共享同一池时，任何时刻 ``sum(预留) <= 总额``，因此确认阶段
不可能出现两个计划对同一额度的重复承诺。纯函数投影，不修改事件。
"""

from __future__ import annotations

from dataclasses import dataclass, field


class CapacityError(ValueError):
    """容量不足或确认超过预留。"""


@dataclass(frozen=True)
class PoolStatus:
    pool: str
    total: float
    reserved: float
    committed: float

    @property
    def available(self) -> float:
        return self.total - self.reserved


@dataclass
class CapacityLedger:
    totals: dict[str, float]
    _reserved: dict[tuple[str, str], float] = field(default_factory=dict)
    _committed: dict[tuple[str, str], float] = field(default_factory=dict)

    def apply(self, event: dict) -> None:
        event_type = event["event_type"]
        body = event["payload"]
        if event_type == "CAPACITY_RESERVED":
            key = (body["pool"], body["plan_id"])
            self._reserved[key] = self._reserved.get(key, 0.0) + float(body["quantity"])
        elif event_type == "CAPACITY_CONFIRMED":
            key = (body["pool"], body["plan_id"])
            self._committed[key] = self._committed.get(key, 0.0) + float(body["quantity"])

    def assert_reservable(self, pool: str, quantity: float) -> None:
        if pool not in self.totals:
            raise CapacityError(f"capacity_pool_unknown: {pool}")
        if quantity < 0:
            raise CapacityError("capacity_quantity_negative: 预留数量不能为负")
        already = sum(v for (p, _plan), v in self._reserved.items() if p == pool)
        if already + quantity > self.totals[pool] + 1e-9:
            raise CapacityError(
                f"capacity_oversold: {pool} 总额 {self.totals[pool]}，已预留 {already}，"
                f"本次再承诺 {quantity} 将超出 {already + quantity - self.totals[pool]:g}"
            )

    def assert_confirmed(self, pool: str, plan_id: str, quantity: float) -> None:
        reserved = self._reserved.get((pool, plan_id), 0.0)
        committed = self._committed.get((pool, plan_id), 0.0)
        if reserved <= 0:
            raise CapacityError(f"capacity_without_reservation: {pool} / {plan_id} 尚未预留")
        if committed + quantity > reserved + 1e-9:
            raise CapacityError(
                f"capacity_double_commit: {pool} / {plan_id} 预留 {reserved}，"
                f"已确认 {committed}，不能再确认 {quantity}"
            )

    def committed(self, pool: str, plan_id: str) -> float:
        return self._committed.get((pool, plan_id), 0.0)

    def status(self, pool: str) -> PoolStatus:
        if pool not in self.totals:
            raise CapacityError(f"capacity_pool_unknown: {pool}")
        reserved = sum(v for (p, _plan), v in self._reserved.items() if p == pool)
        committed = sum(v for (p, _plan), v in self._committed.items() if p == pool)
        return PoolStatus(pool, self.totals[pool], reserved, committed)
