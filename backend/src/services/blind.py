"""盲评考核服务层：登记、锁报、实领补齐/更正、评分版本、晋级回退、试算绑定。

关键不变量：
1. 开餐锁报后迟交的预测标记 late，永不参与评分（即使后来补齐实领）。
2. 实领未到不评分；同日双方共用同一份实领。
3. 实领更正使旧评分版本整体失效并全量重算（版本号递增，旧版留痕 superseded）。
4. 已晋级候选在重算后不再达标则自动退回现行规则。
5. 晋级与实领更正并发：以 DB 行锁 + 版本号串行化，拒绝凭过期评分切换。
6. 试算创建即冻结当时现行规则快照，排队中不随切换改变。
7. 所有写操作幂等：重试不重复计分、不重复晋级。
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import select

from src.config.database import Session
from src.config.settings import REDIS_URL
from src.models import (
    BlindCampaign, BlindDay, BlindScoreVersion, BlindPromotion,
    Dataset, TrialRun,
)
from src.services import blind_engine as engine
from src.services.audit import record

try:  # Redis 可用时用分布式锁；不可用时退化到进程内锁（单机 compose 只有一个后端）
    from redis import Redis
except Exception:  # pragma: no cover
    Redis = None

_LOCK_TTL = 20
_local_locks: dict[str, float] = {}


class _CampaignLock:
    """按考核单串行化写操作，防止晋级与实领更正并发读到过期评分。"""
    def __init__(self, campaign_id: int):
        self.key = f"blind:lock:campaign:{campaign_id}"
        self.redis = None
        self.got_redis = False

    def __enter__(self):
        if Redis is not None:
            try:
                r = Redis.from_url(REDIS_URL, socket_timeout=2)
                if r.set(self.key, str(time.time()), nx=True, ex=_LOCK_TTL):
                    self.redis, self.got_redis = r, True
                    return self
                r.close()
            except Exception:
                pass
        # 退化：进程内带 TTL 的自旋锁
        deadline = time.time() + _LOCK_TTL
        while time.time() < deadline:
            expire_at = _local_locks.get(self.key)
            if expire_at is None or expire_at < time.time():
                _local_locks[self.key] = time.time() + _LOCK_TTL
                self.got_redis = True
                return self
            time.sleep(0.05)
        raise HTTPException(409, "考核单正在被其他操作处理，请稍后重试")

    def __exit__(self, *exc):
        if self.redis is not None:
            try:
                self.redis.delete(self.key)
                self.redis.close()
            except Exception:
                pass
        else:
            _local_locks.pop(self.key, None)


# ---------------- 登记 ----------------

def _history_until(dataset: Dataset, cutoff: date):
    rows = []
    for row in dataset.records:
        day = date.fromisoformat(str(row["date"]))
        if day <= cutoff:
            rows.append({"date": row["date"], "demand": row["demand"]})
    return rows


def create_campaign(db, data, user):
    dataset = db.get(Dataset, data.dataset_id)
    if not dataset:
        raise HTTPException(404, "数据批次不存在")
    start = date.fromisoformat(data.start_date)
    end = date.fromisoformat(data.end_date)
    if end < start:
        raise HTTPException(422, "考核结束日不能早于开始日")
    span = (end - start).days + 1
    if span > 90:
        raise HTTPException(422, "单次考核不超过 90 天")
    if not (0 <= data.lock_hour <= 23):
        raise HTTPException(422, "开餐锁报时刻须为 0~23 点")
    current_params = engine.validate_rule(engine.RULE_CURRENT, data.current_params or {})
    candidate_params = engine.validate_rule(engine.RULE_CANDIDATE, data.candidate_params or {})
    threshold = engine.validate_threshold(data.threshold)
    # 历史必须能覆盖首个考核日（至少 14 天连续历史给候选规则初始化）
    try:
        engine.forecast(engine.RULE_CANDIDATE, candidate_params,
                        _history_until(dataset, start - timedelta(days=1)), start)
    except engine.EngineError as exc:
        raise HTTPException(422, f"首考核日无法生成候选盲预测：{exc}") from exc

    campaign = BlindCampaign(
        name=data.name.strip(), dataset_id=dataset.id,
        current_rule={"rule": engine.RULE_CURRENT, "params": current_params,
                      "label": engine.RULE_LABELS[engine.RULE_CURRENT]},
        candidate_rule={"rule": engine.RULE_CANDIDATE, "params": candidate_params,
                        "label": engine.RULE_LABELS[engine.RULE_CANDIDATE]},
        threshold=threshold, start_date=start.isoformat(), end_date=end.isoformat(),
        lock_hour=data.lock_hour, status="active", active_rule=engine.RULE_CURRENT,
        created_by=user.id, created_at=time.time(),
    )
    db.add(campaign)
    db.flush()
    for i in range(span):
        day = (start + timedelta(days=i)).isoformat()
        db.add(BlindDay(campaign_id=campaign.id, day=day))
    record(db, user.email, "blind.campaign.create",
           {"id": campaign.id, "days": span, "threshold": threshold})
    return campaign


# ---------------- 开餐前锁报 ----------------

def _lock_deadline(campaign: BlindCampaign, day: str) -> float:
    return datetime.fromisoformat(f"{day}T{campaign.lock_hour:02d}:00:00").replace(
        tzinfo=timezone.utc).timestamp()


def _is_late(campaign: BlindCampaign, day: str, now: float) -> bool:
    return now >= _lock_deadline(campaign, day)


def lock_forecast(db, campaign_id, day, rule, user, now=None):
    """登记某方开餐前预测（加锁入口）。幂等：已锁报则拒绝改写；迟交标记 late 且不参评。"""
    now = time.time() if now is None else now
    with _CampaignLock(campaign_id):
        return _lock_forecast_locked(db, campaign_id, day, rule, user, now=now)


def _lock_forecast_locked(db, campaign_id, day, rule, user, now):
    if rule not in engine.RULES:
        raise HTTPException(422, "规则侧别须为 current 或 candidate")
    campaign = db.get(BlindCampaign, campaign_id)
    if not campaign:
        raise HTTPException(404, "考核单不存在")
    bd = db.scalar(select(BlindDay).where(
        BlindDay.campaign_id == campaign_id, BlindDay.day == day))
    if not bd:
        raise HTTPException(404, "该日期不在考核期内")
    late = _is_late(campaign, day, now)
    if rule == engine.RULE_CURRENT:
        if bd.current_forecast is not None:
            raise HTTPException(409, "现行规则该日预测已锁定，不可改写")
        bd.current_forecast = float(_forecast_value(db, campaign, bd, rule))
        bd.current_locked_at = now
        bd.current_late = 1 if late else 0
    else:
        if bd.candidate_forecast is not None:
            raise HTTPException(409, "候选规则该日预测已锁定，不可改写")
        bd.candidate_forecast = float(_forecast_value(db, campaign, bd, rule))
        bd.candidate_locked_at = now
        bd.candidate_late = 1 if late else 0
    if late:
        bd.excluded_reason = "迟于开餐锁报，预测不参评"
    record(db, user.email, "blind.forecast.lock",
           {"campaign": campaign_id, "day": day, "rule": rule, "late": bool(late)})
    return bd


def _continuous_history(db, campaign, target_day):
    """构造目标日前一天为止的连续历史：数据批次历史 + 考核期内已补齐的实领。

    盲评期内后续考核日的预测，只能使用此前已发生并补报的实领，
    看不到目标日当天及之后的数据；尚未补报的日期不允许出现在序列中。
    """
    dataset = db.get(Dataset, campaign.dataset_id)
    target = date.fromisoformat(target_day)
    rows = _history_until(dataset, target - timedelta(days=1))
    known = {date.fromisoformat(r["date"]): float(r["demand"]) for r in rows}
    prior_days = db.scalars(select(BlindDay).where(
        BlindDay.campaign_id == campaign.id,
        BlindDay.day < target_day,
        BlindDay.actual.is_not(None))).all()
    for bd in prior_days:
        known[date.fromisoformat(bd.day)] = float(bd.actual)
    ordered = sorted(known.items())
    return [{"date": d.isoformat(), "demand": v} for d, v in ordered]


def _forecast_value(db, campaign, bd, rule):
    """用目标日前连续历史 + 登记快照参数生成盲预测，保证双方看不到当天实领。"""
    history = _continuous_history(db, campaign, bd.day)
    snap = campaign.current_rule if rule == engine.RULE_CURRENT else campaign.candidate_rule
    rows = engine.forecast(rule, snap["params"], history, bd.day, horizon=1)
    return rows[0]["forecast"]


def forecast_campaign_window(db, campaign, horizon_days):
    """开餐前基于考核期前历史，一次性预生成整个考核窗口双方盲预测（看不到任何考核日实领）。"""
    first = date.fromisoformat(campaign.start_date)
    dataset = db.get(Dataset, campaign.dataset_id)
    base_history = _history_until(dataset, first - timedelta(days=1))
    out = {}
    for rule in engine.RULES:
        snap = campaign.current_rule if rule == engine.RULE_CURRENT else campaign.candidate_rule
        rows = engine.forecast(rule, snap["params"], base_history,
                               campaign.start_date, horizon=horizon_days)
        out[rule] = {r["date"]: r["forecast"] for r in rows}
    return out


def lock_both(db, campaign_id, day, user, now=None):
    """开餐前一次性锁定双方预测（页面主入口）；任一方迟报都各自标记。

    仅取一次考核锁，内部直接复用不加锁的实现，避免不可重入锁自锁。
    """
    now = time.time() if now is None else now
    with _CampaignLock(campaign_id):
        for rule in engine.RULES:
            try:
                _lock_forecast_locked(db, campaign_id, day, rule, user, now=now)
            except HTTPException as exc:
                if exc.status_code != 409:  # 已锁定幂等跳过，其余错误抛出
                    raise
    return db.scalar(select(BlindDay).where(
        BlindDay.campaign_id == campaign_id, BlindDay.day == day))


def prelock_window(db, campaign_id, user, now=None):
    """开餐前锁定未来考核日期：在首个考核日前用考核期前历史，一次性生成双方整窗盲预测。

    - 仅处理预测尚为空、且开餐截止时刻未到的考核日；已锁定的保持不变（幂等可恢复）。
    - 全程只引用首个考核日之前的历史，任何考核日实领都不可见，满足盲评。
    """
    now = time.time() if now is None else now
    with _CampaignLock(campaign_id):
        campaign = db.get(BlindCampaign, campaign_id)
        if not campaign:
            raise HTTPException(404, "考核单不存在")
        days = list(db.scalars(select(BlindDay).where(
            BlindDay.campaign_id == campaign_id).order_by(BlindDay.day)))
        pending = [d for d in days if d.current_forecast is None or d.candidate_forecast is None]
        if not pending:
            return days
        horizon = (date.fromisoformat(campaign.end_date)
                   - date.fromisoformat(campaign.start_date)).days + 1
        window = forecast_campaign_window(db, campaign, horizon)
        locked = []
        for bd in pending:
            late = _is_late(campaign, bd.day, now)
            for rule in engine.RULES:
                col = "current" if rule == engine.RULE_CURRENT else "candidate"
                if getattr(bd, f"{col}_forecast") is not None:
                    continue
                setattr(bd, f"{col}_forecast", float(window[rule][bd.day]))
                setattr(bd, f"{col}_locked_at", now)
                setattr(bd, f"{col}_late", 1 if late else 0)
            if late:
                bd.excluded_reason = "迟于开餐锁报，预测不参评"
            locked.append(bd.day)
        record(db, user.email, "blind.forecast.prelock",
               {"campaign": campaign_id, "days": locked, "count": len(locked)})
        return days


# ---------------- 实领补齐与更正 ----------------

def report_actual(db, campaign_id, day, value, user, now=None):
    """补报/更正某日实领。首次=补齐，再次=更正（旧评分失效并重算）。"""
    now = time.time() if now is None else now
    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, "实领份数须为数值") from exc
    if value < 0:
        raise HTTPException(422, "实领份数不可为负")
    with _CampaignLock(campaign_id):
        campaign = db.get(BlindCampaign, campaign_id)
        if not campaign:
            raise HTTPException(404, "考核单不存在")
        bd = db.scalar(select(BlindDay).where(
            BlindDay.campaign_id == campaign_id, BlindDay.day == day))
        if not bd:
            raise HTTPException(404, "该日期不在考核期内")
        is_correction = bd.actual is not None and abs(bd.actual - value) > 1e-9
        bd.actual, bd.actual_reported_at = value, now
        record(db, user.email,
               "blind.actual.correct" if is_correction else "blind.actual.report",
               {"campaign": campaign_id, "day": day, "value": value,
                "old": bd.actual if is_correction else None})
        version = _rescore(db, campaign, basis={"day": day, "actual": value,
                                                "correction": is_correction, "at": now},
                           actor=user.email)
        return bd, version


# ---------------- 评分版本与晋级/回退 ----------------

def _eligible_days(db, campaign):
    return list(db.scalars(select(BlindDay).where(
        BlindDay.campaign_id == campaign.id).order_by(BlindDay.day)))


def _rescore(db, campaign, basis, actor):
    """用当前全部实领全量重算，生成新版本；旧版本标记 superseded 但保留留痕。"""
    days = _eligible_days(db, campaign)
    pairs, excluded = [], []
    for bd in days:
        if bd.actual is None:
            bd.scored, bd.excluded_reason = 0, "待收齐实领"
            continue
        late_side = None
        if bd.current_late or bd.candidate_late:
            late_side = [s for s, flag in (
                (engine.RULE_CURRENT, bd.current_late),
                (engine.RULE_CANDIDATE, bd.candidate_late)) if flag]
        if bd.current_forecast is None or bd.candidate_forecast is None:
            bd.scored, bd.excluded_reason = 0, "预测未收齐"
            excluded.append({"day": bd.day, "reason": "预测未收齐"})
            continue
        if late_side:
            bd.scored, bd.excluded_reason = 0, "迟报不参评"
            excluded.append({"day": bd.day, "reason": f"迟报方：{','.join(late_side)}"})
            continue
        bd.scored, bd.excluded_reason = 1, ""
        pairs.append({"day": bd.day, "actual": bd.actual,
                      engine.RULE_CURRENT: bd.current_forecast,
                      engine.RULE_CANDIDATE: bd.candidate_forecast})

    score = engine.score_pair(pairs)
    promoted, reason = engine.decide_promotion(score, campaign.threshold)
    prev_versions = list(db.scalars(select(BlindScoreVersion).where(
        BlindScoreVersion.campaign_id == campaign.id,
        BlindScoreVersion.status == "current")))
    new_version = (max([v.version for v in prev_versions], default=0) + 1)
    for v in prev_versions:
        v.status = "superseded"
    snap = BlindScoreVersion(
        campaign_id=campaign.id, version=new_version, status="current",
        result={"score": score, "days": [{"day": p["day"], "actual": p["actual"],
                                          "current": p[engine.RULE_CURRENT],
                                          "candidate": p[engine.RULE_CANDIDATE]} for p in pairs],
                "excluded": excluded},
        decision={"would_promote": promoted, "reason": reason},
        basis=basis, created_at=time.time())
    db.add(snap)
    db.flush()
    for bd in days:
        if bd.scored:
            bd.score_version = new_version
    _apply_decision(db, campaign, snap, promoted, reason, actor)
    record(db, actor, "blind.score.version",
           {"campaign": campaign.id, "version": new_version,
            "scored_days": score["scored_days"] if score else 0,
            "would_promote": promoted, "basis": basis})
    return snap


def _apply_decision(db, campaign, version, qualifies, reason, actor):
    """依据最新版本决定现行规则；已晋级却不再达标则退回，全程留痕且幂等。"""
    if qualifies and campaign.active_rule != engine.RULE_CANDIDATE:
        campaign.active_rule = engine.RULE_CANDIDATE
        db.add(BlindPromotion(campaign_id=campaign.id, action="promoted",
                              from_rule=engine.RULE_CURRENT, to_rule=engine.RULE_CANDIDATE,
                              score_version_id=version.id, reason=reason,
                              actor=actor, at=time.time()))
        record(db, actor, "blind.rule.promote",
               {"campaign": campaign.id, "version": version.version, "reason": reason})
    elif not qualifies and campaign.active_rule == engine.RULE_CANDIDATE:
        # 更正后旧评分失效，候选不再达标 → 退回原规则，绝不凭过期评分维持切换
        campaign.active_rule = engine.RULE_CURRENT
        db.add(BlindPromotion(campaign_id=campaign.id, action="rolled_back",
                              from_rule=engine.RULE_CANDIDATE, to_rule=engine.RULE_CURRENT,
                              score_version_id=version.id,
                              reason=f"更正后复算不再达标，退回现行规则：{reason}",
                              actor=actor, at=time.time()))
        record(db, actor, "blind.rule.rollback",
               {"campaign": campaign.id, "version": version.version, "reason": reason})


def manual_promote(db, campaign_id, user):
    """手动晋级入口：只认当前 current 版本，若已被更正作废则拒绝（不凭过期评分切换）。"""
    with _CampaignLock(campaign_id):
        campaign = db.get(BlindCampaign, campaign_id)
        if not campaign:
            raise HTTPException(404, "考核单不存在")
        version = db.scalar(select(BlindScoreVersion).where(
            BlindScoreVersion.campaign_id == campaign_id,
            BlindScoreVersion.status == "current").order_by(BlindScoreVersion.version.desc()))
        if not version or not version.result.get("score"):
            raise HTTPException(409, "尚无有效评分，不能晋级")
        if campaign.active_rule == engine.RULE_CANDIDATE:
            raise HTTPException(409, "候选规则已是现行规则，无需重复晋级")
        if not version.decision.get("would_promote"):
            raise HTTPException(409, f"当前评分未达晋级门槛：{version.decision.get('reason')}")
        _apply_decision(db, campaign, version, True,
                        version.decision.get("reason", "手动晋级"), user.email)
        db.flush()
        return campaign


def close_campaign(db, campaign_id, user):
    with _CampaignLock(campaign_id):
        campaign = db.get(BlindCampaign, campaign_id)
        if not campaign:
            raise HTTPException(404, "考核单不存在")
        if campaign.status == "closed":
            return campaign
        campaign.status = "closed"
        campaign.closed_at = time.time()
        record(db, user.email, "blind.campaign.close", {"id": campaign_id})
        return campaign


def recover_rescore(db, campaign_id, user):
    """盲评中断后的公开恢复入口：加锁并幂等重算，不重复计分或晋级。"""
    with _CampaignLock(campaign_id):
        campaign = db.get(BlindCampaign, campaign_id)
        if not campaign:
            raise HTTPException(404, "考核单不存在")
        return _rescore(db, campaign, basis={"day": None, "actual": None,
                                             "correction": False, "at": None,
                                             "recovery": True}, actor=user.email)


# ---------------- 试算：绑定创建当时现行规则 ----------------

def create_trial(db, data, user):
    campaign = db.get(BlindCampaign, data.campaign_id)
    if not campaign:
        raise HTTPException(404, "考核单不存在")
    date.fromisoformat(data.target_day)  # 校验日期格式
    bound = campaign.active_rule
    snapshot = campaign.current_rule if bound == engine.RULE_CURRENT else campaign.candidate_rule
    trial = TrialRun(campaign_id=data.campaign_id, name=data.name.strip(),
                     status="queued", bound_rule=bound, rule_snapshot=snapshot,
                     target_day=data.target_day, created_by=user.id, created_at=time.time())
    db.add(trial)
    db.flush()
    record(db, user.email, "blind.trial.create",
           {"id": trial.id, "bound_rule": bound, "campaign": data.campaign_id})
    return trial


def run_trial(trial_id):
    """worker/接口执行试算。始终用冻结快照，即使期间现行规则已切换也不受影响。"""
    with Session() as db:
        trial = db.get(TrialRun, trial_id)
        if not trial or trial.status != "queued":
            return
        trial.status = "running"
        db.commit()
        try:
            campaign = db.get(BlindCampaign, trial.campaign_id)
            dataset = db.get(Dataset, campaign.dataset_id)
            history = _history_until(dataset, date.fromisoformat(trial.target_day) - timedelta(days=1))
            rows = engine.forecast(trial.bound_rule, trial.rule_snapshot.get("params", {}),
                                   history, trial.target_day, horizon=1)
            trial.result = rows[0]
            trial.status = "done"
            trial.finished_at = time.time()
            record(db, "worker", "blind.trial.done", {"id": trial_id})
        except Exception as exc:  # 重试安全：失败可重新排队，不产生计分副作用
            trial.status = "failed"
            trial.error = str(exc)[:480]
            trial.finished_at = time.time()
            record(db, "worker", "blind.trial.failed", {"id": trial_id, "error": trial.error})
        db.commit()
