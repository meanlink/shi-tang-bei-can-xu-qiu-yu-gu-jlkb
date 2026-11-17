from sqlalchemy import String, JSON, Double, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base


class BlindCampaign(Base):
    """盲评考核登记：双方规则参数快照、晋级门槛与考核期，一经登记不可变。"""
    __tablename__ = 'blind_campaigns'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'))
    # 登记时冻结的规则参数快照，后续评分/晋级只认快照，不随外部配置变化
    current_rule: Mapped[dict] = mapped_column(JSON, default=dict)
    candidate_rule: Mapped[dict] = mapped_column(JSON, default=dict)
    threshold: Mapped[dict] = mapped_column(JSON, default=dict)
    start_date: Mapped[str] = mapped_column(String(10))   # 首个考核日 YYYY-MM-DD
    end_date: Mapped[str] = mapped_column(String(10))     # 末个考核日
    lock_hour: Mapped[int] = mapped_column(Integer, default=10)  # 开餐前锁报时刻（小时）
    status: Mapped[str] = mapped_column(String(20), default='active')  # active/closed
    active_rule: Mapped[str] = mapped_column(String(20), default='current')  # current/candidate
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[float] = mapped_column(Double)
    closed_at: Mapped[float | None] = mapped_column(Double, nullable=True)


class BlindDay(Base):
    """考核日：每天双方各一条预测（开餐前锁定）与一份实领（补齐后才评分）。"""
    __tablename__ = 'blind_days'
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey('blind_campaigns.id'))
    day: Mapped[str] = mapped_column(String(10), index=True)
    current_forecast: Mapped[float | None] = mapped_column(Double, nullable=True)
    candidate_forecast: Mapped[float | None] = mapped_column(Double, nullable=True)
    # 预测提交时刻；迟于开餐锁报时刻则 late=True，不参评（预测列仍留痕但不进评分）
    current_locked_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    candidate_locked_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    current_late: Mapped[bool] = mapped_column(Integer, default=0)
    candidate_late: Mapped[bool] = mapped_column(Integer, default=0)
    actual: Mapped[float | None] = mapped_column(Double, nullable=True)
    actual_reported_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    # 评分版本：每次实领更正后 +1，旧版本整体失效；null=尚未评分
    score_version: Mapped[int] = mapped_column(Integer, default=0)
    scored: Mapped[bool] = mapped_column(Integer, default=0)
    excluded_reason: Mapped[str] = mapped_column(String(200), default='')


class BlindScoreVersion(Base):
    """评分版本快照：每个版本基于当时同份实领全量重算，更正后旧版本标记 superseded。"""
    __tablename__ = 'blind_score_versions'
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey('blind_campaigns.id'))
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default='current')  # current/superseded
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    decision: Mapped[dict] = mapped_column(JSON, default=dict)
    # 触发该版本的实领更正审计依据（日期 + 实领值指纹）
    basis: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[float] = mapped_column(Double)


class BlindPromotion(Base):
    """晋级与回退流水：每次现行规则切换都留一条，含采用依据，支持退回原规则。"""
    __tablename__ = 'blind_promotions'
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey('blind_campaigns.id'))
    action: Mapped[str] = mapped_column(String(20))  # promoted/rolled_back
    from_rule: Mapped[str] = mapped_column(String(20))
    to_rule: Mapped[str] = mapped_column(String(20))
    score_version_id: Mapped[int | None] = mapped_column(ForeignKey('blind_score_versions.id'), nullable=True)
    reason: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(150))
    at: Mapped[float] = mapped_column(Double)


class TrialRun(Base):
    """新试算：绑定创建当时的现行规则快照；规则切换不改变已排队试算。"""
    __tablename__ = 'blind_trial_runs'
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey('blind_campaigns.id'), nullable=True)
    name: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(20), default='queued')  # queued/running/done/failed
    bound_rule: Mapped[str] = mapped_column(String(20))       # 创建时现行规则
    rule_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)  # 冻结快照
    target_day: Mapped[str] = mapped_column(String(10))
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(String(500), default='')
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[float] = mapped_column(Double)
    finished_at: Mapped[float | None] = mapped_column(Double, nullable=True)
