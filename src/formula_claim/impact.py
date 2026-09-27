"""后台影响传播：检测过期、原料变更、召回后的批次定位与断点续跑。

每个作业按批次确定性顺序推进，每处理一批即写入检查点；
中断后用同一作业标识恢复，从下一批继续，不会重复落事件。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Iterable

from .model import DomainError
from .service import ReleaseService


@dataclass
class ImpactJob:
    job_id: str
    kind: str  # evidence_expired / ingredient_changed / recall
    target: str  # evidence_id / ingredient_id / 召回说明
    started_at: str
    pending: list[str]
    processed: list[str] = field(default_factory=list)
    notifications: list[dict] = field(default_factory=list)
    status: str = "running"  # running / completed

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ImpactJob":
        return cls(
            job_id=data["job_id"],
            kind=data["kind"],
            target=data["target"],
            started_at=data["started_at"],
            pending=list(data["pending"]),
            processed=list(data["processed"]),
            notifications=list(data["notifications"]),
            status=data["status"],
        )


class InMemoryCheckpointStore:
    """以字典持久化作业进度，模拟可恢复的存储。"""

    def __init__(self) -> None:
        self._rows: dict[str, dict] = {}

    def save(self, job: ImpactJob) -> None:
        self._rows[job.job_id] = job.to_dict()

    def load(self, job_id: str) -> ImpactJob | None:
        row = self._rows.get(job_id)
        return ImpactJob.from_dict(row) if row is not None else None


class ImpactPropagator:
    """在放行服务上执行可恢复的影响传播作业。"""

    def __init__(self, service: ReleaseService, store: InMemoryCheckpointStore) -> None:
        self._service = service
        self._store = store
        self._seq = 0

    def start_evidence_expiry(self, *, evidence_id: str, occurred_at: datetime) -> ImpactJob:
        """检测过期：定位依赖该证据的声明所覆盖的批次。"""
        lots = self._service.find_lots_using_evidence(evidence_id)
        job = self._new_job("evidence_expired", evidence_id, lots, occurred_at)
        self._service.mark_evidence_expired(evidence_id=evidence_id, occurred_at=occurred_at, job_id=job.job_id)
        return job

    def start_ingredient_change(self, *, ingredient_id: str, occurred_at: datetime) -> ImpactJob:
        """原料变更：按批次各自锁定的配方版本定位受影响批次。"""
        lots = self._service.find_lots_using_ingredient(ingredient_id)
        return self._new_job("ingredient_changed", ingredient_id, lots, occurred_at)

    def start_recall(self, *, lot_ids: Iterable[str], reason: str, occurred_at: datetime) -> ImpactJob:
        """召回：对指定批次逐批落实。"""
        lots = sorted(lot_ids)
        for lot_id in lots:
            self._service.lot(lot_id)
        return self._new_job("recall", reason, lots, occurred_at)

    def job(self, job_id: str) -> ImpactJob:
        job = self._store.load(job_id)
        if job is None:
            raise DomainError("not_found", f"影响作业不存在：{job_id}")
        return job

    def run(self, job_id: str, *, max_steps: int | None = None) -> ImpactJob:
        """推进作业；max_steps 用于限量执行，模拟中断点。"""
        job = self.job(job_id)
        if job.status == "completed":
            return job
        occurred_at = datetime.fromisoformat(job.started_at)
        steps = 0
        while job.pending and (max_steps is None or steps < max_steps):
            lot_id = job.pending.pop(0)
            self._apply(job, lot_id, occurred_at)
            job.processed.append(lot_id)
            steps += 1
            if not job.pending:
                job.status = "completed"
            self._store.save(job)
        return job

    def resume(self, job_id: str, *, max_steps: int | None = None) -> ImpactJob:
        """从检查点继续；与 run 等价，语义上强调断点续跑。"""
        return self.run(job_id, max_steps=max_steps)

    def _new_job(self, kind: str, target: str, lots: list[str], occurred_at: datetime) -> ImpactJob:
        self._seq += 1
        job = ImpactJob(
            job_id=f"job-{self._seq:04d}",
            kind=kind,
            target=target,
            started_at=occurred_at.isoformat(),
            pending=list(lots),
        )
        self._store.save(job)
        return job

    def _apply(self, job: ImpactJob, lot_id: str, occurred_at: datetime) -> None:
        if job.kind == "evidence_expired":
            _, notifications = self._service.apply_evidence_expiry(
                lot_id=lot_id,
                evidence_id=job.target,
                occurred_at=occurred_at,
                job_id=job.job_id,
            )
            job.notifications.extend(asdict(note) for note in notifications)
        elif job.kind == "ingredient_changed":
            self._service.apply_ingredient_change(
                lot_id=lot_id,
                ingredient_id=job.target,
                occurred_at=occurred_at,
                job_id=job.job_id,
            )
        elif job.kind == "recall":
            self._service.apply_recall(
                lot_id=lot_id,
                reason=job.target,
                occurred_at=occurred_at,
                job_id=job.job_id,
            )
        else:
            raise DomainError("unsupported_value", f"未知影响作业类型：{job.kind}")
