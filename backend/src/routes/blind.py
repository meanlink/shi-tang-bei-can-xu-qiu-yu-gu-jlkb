from fastapi import APIRouter, Depends
from sqlalchemy import select

from src.config.database import get_db
from src.middlewares.auth import current_user, can_write, admin_only
from src.models import (
    BlindCampaign, BlindDay, BlindScoreVersion, BlindPromotion, TrialRun,
)
from src.schemas.blind import CampaignInput, ForecastLockInput, ActualInput, TrialInput
from src.services import blind
from src.services.audit import record
from src.utils.blind_serialization import (
    campaign_dict, day_dict, version_dict, promotion_dict, trial_dict,
)

router = APIRouter(prefix="/blind")


# ---------------- 考核登记 ----------------

@router.get("/campaigns")
def list_campaigns(db=Depends(get_db), u=Depends(current_user)):
    rows = db.scalars(select(BlindCampaign).order_by(BlindCampaign.id.desc()).limit(50))
    return [campaign_dict(c) for c in rows]


@router.post("/campaigns", status_code=201)
def create_campaign(data: CampaignInput, db=Depends(get_db), u=Depends(can_write)):
    c = blind.create_campaign(db, data, u)
    db.commit()
    return campaign_dict(c)


@router.get("/campaigns/{cid}")
def campaign_detail(cid: int, db=Depends(get_db), u=Depends(current_user)):
    c = db.get(BlindCampaign, cid)
    if not c:
        from fastapi import HTTPException
        raise HTTPException(404, "考核单不存在")
    days = [day_dict(d) for d in db.scalars(select(BlindDay).where(
        BlindDay.campaign_id == cid).order_by(BlindDay.day))]
    versions = [version_dict(v) for v in db.scalars(select(BlindScoreVersion).where(
        BlindScoreVersion.campaign_id == cid).order_by(BlindScoreVersion.version))]
    promotions = [promotion_dict(p) for p in db.scalars(select(BlindPromotion).where(
        BlindPromotion.campaign_id == cid).order_by(BlindPromotion.id))]
    trials = [trial_dict(t) for t in db.scalars(select(TrialRun).where(
        TrialRun.campaign_id == cid).order_by(TrialRun.id.desc()))]
    pending = [d.day for d in db.scalars(select(BlindDay).where(
        BlindDay.campaign_id == cid, BlindDay.actual.is_(None))).all()]
    return {**campaign_dict(c), "days": days, "score_versions": versions,
            "promotions": promotions, "trials": trials, "pending_actual_days": pending}


@router.post("/campaigns/{cid}/close")
def close_campaign(cid: int, db=Depends(get_db), u=Depends(admin_only)):
    c = blind.close_campaign(db, cid, u)
    db.commit()
    return campaign_dict(c)


# ---------------- 开餐前锁报 ----------------

@router.post("/campaigns/{cid}/lock-forecast")
def lock_forecast(cid: int, data: ForecastLockInput, db=Depends(get_db), u=Depends(can_write)):
    if data.rule:  # 锁单一方
        d = blind.lock_forecast(db, cid, data.day, data.rule, u)
    else:         # 双方同刻锁定
        d = blind.lock_both(db, cid, data.day, u)
    db.commit()
    return day_dict(d)


@router.post("/campaigns/{cid}/prelock-window")
def prelock_window(cid: int, db=Depends(get_db), u=Depends(can_write)):
    """开餐前一次性预锁整个考核窗口的双方盲预测（盲评主入口，幂等可恢复）。"""
    days = blind.prelock_window(db, cid, u)
    db.commit()
    return [day_dict(d) for d in days]


# ---------------- 实领补齐 / 更正 ----------------

@router.post("/campaigns/{cid}/actual")
def report_actual(cid: int, data: ActualInput, db=Depends(get_db), u=Depends(can_write)):
    d, version = blind.report_actual(db, cid, data.day, data.value, u)
    db.commit()
    return {"day": day_dict(d), "score_version": version_dict(version)}


@router.post("/campaigns/{cid}/rescore")
def rescore(cid: int, db=Depends(get_db), u=Depends(can_write)):
    """盲评中断后的恢复入口：幂等重算，不重复计分或晋级。"""
    version = blind.recover_rescore(db, cid, u)
    db.commit()
    return version_dict(version)


# ---------------- 晋级 / 回退（依据追查） ----------------

@router.post("/campaigns/{cid}/promote")
def promote(cid: int, db=Depends(get_db), u=Depends(admin_only)):
    c = blind.manual_promote(db, cid, u)
    db.commit()
    return campaign_dict(c)


@router.get("/campaigns/{cid}/promotions")
def promotions(cid: int, db=Depends(get_db), u=Depends(current_user)):
    return [promotion_dict(p) for p in db.scalars(select(BlindPromotion).where(
        BlindPromotion.campaign_id == cid).order_by(BlindPromotion.id))]


# ---------------- 试算：绑定当时现行规则 ----------------

@router.post("/trials", status_code=201)
def create_trial(data: TrialInput, db=Depends(get_db), u=Depends(can_write)):
    t = blind.create_trial(db, data, u)
    db.commit()
    blind.run_trial(t.id)  # 独立 Session 同步执行；排队语义由 status 与冻结快照保证
    db.expire_all()
    return trial_dict(db.get(TrialRun, t.id))


@router.get("/trials")
def list_trials(db=Depends(get_db), u=Depends(current_user)):
    return [trial_dict(t) for t in db.scalars(select(TrialRun).order_by(TrialRun.id.desc()).limit(100))]


@router.post("/trials/{tid}/retry")
def retry_trial(tid: int, db=Depends(get_db), u=Depends(can_write)):
    """失败试算重试：重新排队执行；已完成的拒绝，保证不重复计分/晋级。"""
    from fastapi import HTTPException
    t = db.get(TrialRun, tid)
    if not t:
        raise HTTPException(404, "试算不存在")
    if t.status == "done":
        raise HTTPException(409, "试算已完成，重试不会重复执行")
    t.status = "queued"
    t.error = ""
    record(db, u.email, "blind.trial.retry", {"id": tid})
    db.commit()
    blind.run_trial(tid)
    return trial_dict(db.get(TrialRun, tid))
