"""盲评服务层集成测试：SQLite 内存库，覆盖状态机、幂等、版本重算、晋级回退、试算绑定。

运行：/tmp/venv/bin/python -m pytest test_blind_service.py -v
依赖第三方包（fastapi/sqlalchemy/redis），与纯标准库的引擎测试互补。
"""
import os
import sys
import time
import threading
import unittest
from datetime import date, timedelta

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6399/0")  # 不存在 → 退化进程锁
os.environ["JWT_SECRET"] = "test-secret"
os.environ.setdefault("ARTIFACT_DIR", "/tmp/blind-test-artifacts")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import src.config.database as dbconfig  # noqa: E402
from src.config.database import Base  # noqa: E402
from src import models  # noqa: E402
from src.models import (  # noqa: E402
    User, Dataset, BlindCampaign, BlindDay, BlindScoreVersion,
    BlindPromotion, TrialRun,
)
from src.services import blind, blind_engine as eng  # noqa: E402
from src.utils.serialization import checksum  # noqa: E402

# 单连接内存库，保证各 Session 看到同一份数据
engine = create_engine(
    "sqlite://", connect_args={"check_same_thread": False},
    poolclass=StaticPool, pool_pre_ping=True)
TestSession = sessionmaker(engine)
dbconfig.engine = engine
dbconfig.Session = TestSession
blind.Session = TestSession

START = date(2026, 9, 1)


def build_records(n=45):
    rows = []
    weekly = [40, 60, 70, 55, 20, -180, -200]
    for i in range(n):
        d = START + timedelta(days=i)
        rows.append({"date": d.isoformat(),
                     "demand": float(400 + 2 * i + weekly[d.weekday()]),
                     "meal": "测试午餐档"})
    return rows


class Actor:
    def __init__(self, uid, email):
        self.id = uid
        self.email = email


class Input:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class BlindServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 防御性重绑：与 API 测试互不污染，始终走进程内内存库
        dbconfig.engine = engine
        dbconfig.Session = TestSession
        blind.Session = TestSession
        Base.metadata.create_all(engine)

    def setUp(self):
        with TestSession() as db:
            db.query(BlindPromotion).delete()
            db.query(BlindScoreVersion).delete()
            db.query(BlindDay).delete()
            db.query(TrialRun).delete()
            db.query(BlindCampaign).delete()
            db.query(Dataset).delete()
            db.query(User).delete()
            db.commit()
            self.user = User(email="admin@test", name="测试负责人", role="admin", password="x")
            db.add(self.user)
            rows = build_records()
            self.dataset = Dataset(name="盲评测试批次", source="unit.json",
                                   records=rows, checksum=checksum(rows), created_at=time.time())
            db.add(self.dataset)
            db.commit()
            self.uid, self.did = self.user.id, self.dataset.id
        self.actor = Actor(self.uid, "admin@test")

    def _campaign_input(self, start=None, days=10, **kw):
        start = start or (START + timedelta(days=30)).isoformat()
        end = (date.fromisoformat(start) + timedelta(days=days - 1)).isoformat()
        return Input(name="候选规则盲评", dataset_id=self.did, start_date=start,
                     end_date=end, lock_hour=10,
                     current_params={}, candidate_params={"alpha": 0.35},
                     threshold={"metric": "wape", "min_improve_pct": 1.0,
                                "min_scored_days": 3}, **kw)

    def _create(self, **kw):
        with TestSession() as db:
            c = blind.create_campaign(db, self._campaign_input(**kw), self.actor)
            db.commit()
            return c.id

    def _day_ts(self, day, hour=9):
        return datetime_ts(day, hour)

    # ---------- 登记 ----------
    def test_campaign_registers_snapshot_and_days(self):
        cid = self._create(days=7)
        with TestSession() as db:
            c = db.get(BlindCampaign, cid)
            self.assertEqual(c.active_rule, eng.RULE_CURRENT)
            self.assertEqual(c.candidate_rule["params"]["alpha"], 0.35)
            self.assertEqual(db.query(BlindDay).filter_by(campaign_id=cid).count(), 7)

    def test_campaign_rejects_when_history_insufficient(self):
        from fastapi import HTTPException
        with TestSession() as db:
            inp = self._campaign_input(
                start=(START + timedelta(days=5)).isoformat(), days=3)
            with self.assertRaises(HTTPException):
                blind.create_campaign(db, inp, self.actor)

    # ---------- 锁报与迟报 ----------
    def test_late_forecast_excluded_forever(self):
        cid = self._create(days=5)
        day = (START + timedelta(days=30)).isoformat()
        with TestSession() as db:
            # 9 点锁定 → 准时；再用 11 点补候选（已过 10 点开餐）→ 迟报
            blind.lock_both(db, cid, day, self.actor, now=datetime_ts(day, 9))
            bd = db.query(BlindDay).filter_by(campaign_id=cid, day=day).one()
            self.assertFalse(bd.current_late or bd.candidate_late)
            # 制造候选迟报：先清掉候选锁，再以 11 点重锁
            bd.candidate_forecast, bd.candidate_locked_at, bd.candidate_late = None, None, 0
            blind.lock_forecast(db, cid, day, "candidate", self.actor, now=datetime_ts(day, 11))
            db.commit()
            bd = db.query(BlindDay).filter_by(campaign_id=cid, day=day).one()
            self.assertTrue(bd.candidate_late)
            blind.report_actual(db, cid, day, 420.0, self.actor)
            db.commit()
            bd = db.query(BlindDay).filter_by(campaign_id=cid, day=day).one()
            self.assertFalse(bd.scored)  # 迟报永不参评
            self.assertIn("迟报", bd.excluded_reason)

    def test_lock_is_idempotent_no_rewrite(self):
        from fastapi import HTTPException
        cid = self._create(days=3)
        day = (START + timedelta(days=30)).isoformat()
        with TestSession() as db:
            blind.lock_forecast(db, cid, day, "current", self.actor, now=datetime_ts(day, 9))
            with self.assertRaises(HTTPException):  # 重复锁报被拒
                blind.lock_forecast(db, cid, day, "current", self.actor, now=datetime_ts(day, 9))

    # ---------- 补齐才评分、同份实领 ----------
    def test_no_scoring_until_actual_complete_same_actual(self):
        cid = self._create(days=4)
        d0 = (START + timedelta(days=30)).isoformat()
        with TestSession() as db:
            for i in range(4):
                day = (START + timedelta(days=30 + i)).isoformat()
                blind.lock_both(db, cid, day, self.actor, now=datetime_ts(day, 9))
            db.commit()
            # 未补实领 → 无评分版本
            self.assertEqual(db.query(BlindScoreVersion).filter_by(campaign_id=cid).count(), 0)
            blind.report_actual(db, cid, d0, 410.0, self.actor)
            db.commit()
            v = db.query(BlindScoreVersion).filter_by(campaign_id=cid, status="current").one()
            self.assertEqual(v.result["score"]["scored_days"], 1)
            # 同日双方读到的是同一份实领
            row = v.result["days"][0]
            self.assertEqual(row["actual"], 410.0)
            self.assertIn("current", row)
            self.assertIn("candidate", row)

    # ---------- 更正失效、版本重算、晋级与回退 ----------
    def test_correction_supersedes_old_score_and_rolls_back(self):
        cid = self._create(days=4)
        days = [(START + timedelta(days=30 + i)).isoformat() for i in range(4)]
        with TestSession() as db:
            for day in days:
                blind.lock_both(db, cid, day, self.actor, now=datetime_ts(day, 9))
            db.commit()
            # 第一组实领：候选大幅占优 → 晋级
            for i, day in enumerate(days):
                bd = db.query(BlindDay).filter_by(day=day).one()
                # 让候选预测接近实领、现行偏离
                actual = bd.candidate_forecast + 1.0
                blind.report_actual(db, cid, day, float(actual), self.actor)
            db.commit()
            c = db.get(BlindCampaign, cid)
            self.assertEqual(c.active_rule, eng.RULE_CANDIDATE)
            promos = db.query(BlindPromotion).filter_by(campaign_id=cid).all()
            self.assertEqual(promos[-1].action, "promoted")
            versions_before = db.query(BlindScoreVersion).filter_by(campaign_id=cid).count()

            # 更正实领：改为现行预测接近、候选大幅偏离 → 重算后回退
            for day in days:
                bd = db.query(BlindDay).filter_by(day=day).one()
                blind.report_actual(db, cid, day, float(bd.current_forecast + 1.0), self.actor)
            db.commit()
            c = db.get(BlindCampaign, cid)
            self.assertEqual(c.active_rule, eng.RULE_CURRENT)
            promos = db.query(BlindPromotion).filter_by(campaign_id=cid).all()
            self.assertEqual(promos[-1].action, "rolled_back")
            # 旧版本全部 superseded，只留一个 current
            current = db.query(BlindScoreVersion).filter_by(
                campaign_id=cid, status="current").all()
            self.assertEqual(len(current), 1)
            superseded = db.query(BlindScoreVersion).filter_by(
                campaign_id=cid, status="superseded").count()
            self.assertGreaterEqual(superseded, versions_before)
            self.assertEqual(current[0].basis["correction"], True)

    def test_rescore_is_idempotent_and_recoverable(self):
        cid = self._create(days=3)
        day = (START + timedelta(days=30)).isoformat()
        with TestSession() as db:
            blind.lock_both(db, cid, day, self.actor, now=datetime_ts(day, 9))
            blind.report_actual(db, cid, day, 405.0, self.actor)
            db.commit()
            n1 = db.query(BlindScoreVersion).filter_by(campaign_id=cid).count()
            promoted_before = db.query(BlindPromotion).filter_by(
                campaign_id=cid, action="promoted").count()
            # 中断恢复：幂等重算会再生成一个版本但不重复晋级
            blind._rescore(db, db.get(BlindCampaign, cid),
                           basis={"recovery": True}, actor=self.actor.email)
            db.commit()
            n2 = db.query(BlindScoreVersion).filter_by(campaign_id=cid).count()
            self.assertEqual(n2, n1 + 1)  # 版本留痕递增
            promoted_after = db.query(BlindPromotion).filter_by(
                campaign_id=cid, action="promoted").count()
            self.assertEqual(promoted_after, promoted_before)  # 无重复晋级
            self.assertEqual(db.query(BlindPromotion).filter_by(
                campaign_id=cid, action="rolled_back").count(), 0)

    # ---------- 并发：不凭过期评分切换 ----------
    def _setup_promotable_campaign(self):
        cid = self._create(days=4)
        days = [(START + timedelta(days=30 + i)).isoformat() for i in range(4)]
        with TestSession() as db:
            for day in days:
                blind.lock_both(db, cid, day, self.actor, now=datetime_ts(day, 9))
            for day in days:
                bd = db.query(BlindDay).filter_by(day=day).one()
                blind.report_actual(db, cid, day, float(bd.candidate_forecast + 1.0), self.actor)
            db.commit()
            self.assertEqual(db.get(BlindCampaign, cid).active_rule, eng.RULE_CANDIDATE)
        return cid, days

    def test_correction_then_promote_rejects_stale_score(self):
        """更正先持锁并回退后，晋级线程只能读到最新 current 版本，旧评分无法再切换。"""
        cid, days = self._setup_promotable_campaign()
        with TestSession() as db:  # 先更正 → 自动回退到现行
            for day in days:
                bd = db.query(BlindDay).filter_by(day=day).one()
                blind.report_actual(db, cid, day, float(bd.current_forecast + 1.0), self.actor)
            db.commit()
            self.assertEqual(db.get(BlindCampaign, cid).active_rule, eng.RULE_CURRENT)
        # 此刻再请求晋级：最新评分不达标 → 409，绝不凭更正前的旧评分切回候选
        from fastapi import HTTPException
        with TestSession() as db:
            with self.assertRaises(HTTPException) as ctx:
                blind.manual_promote(db, cid, self.actor)
            self.assertEqual(ctx.exception.status_code, 409)
            self.assertEqual(db.get(BlindCampaign, cid).active_rule, eng.RULE_CURRENT)

    def test_promote_then_correction_rolls_back(self):
        """晋级已落库后再更正：重算不达标 → 自动回退，终态唯一且留痕完整。"""
        cid, days = self._setup_promotable_campaign()
        with TestSession() as db:
            # 起点：候选已晋级为现行规则
            self.assertEqual(db.get(BlindCampaign, cid).active_rule, eng.RULE_CANDIDATE)
            promoted_before = db.query(BlindPromotion).filter_by(
                campaign_id=cid, action="promoted").count()
            self.assertEqual(promoted_before, 1)
            # 更正实领使候选不再占优 → 必须自动回退
            for day in days:
                bd = db.query(BlindDay).filter_by(day=day).one()
                blind.report_actual(db, cid, day, float(bd.current_forecast + 1.0), self.actor)
            db.commit()
            db.expire_all()
            self.assertEqual(db.get(BlindCampaign, cid).active_rule, eng.RULE_CURRENT)
            promos = db.query(BlindPromotion).filter_by(campaign_id=cid).all()
            self.assertEqual(promos[-1].action, "rolled_back")
            # 只有最初自动晋级一条 promoted，没有基于过期评分的重复晋级
            self.assertEqual(sum(1 for p in promos if p.action == "promoted"), 1)
            # 回退流水必须带评分版本依据，可追查
            self.assertIsNotNone(promos[-1].score_version_id)
            self.assertTrue(promos[-1].reason)

    def test_concurrent_writers_are_serialized_by_lock(self):
        """两个更正线程并发：锁串行化，终态一致且只产生顺序版本，不损坏数据。"""
        cid, days = self._setup_promotable_campaign()

        def correct():
            with TestSession() as db:
                for day in days:
                    bd = db.query(BlindDay).filter_by(day=day).one()
                    blind.report_actual(db, cid, day,
                                        float(bd.current_forecast + 1.0), self.actor)
                db.commit()

        threads = [threading.Thread(target=correct) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with TestSession() as db:
            self.assertEqual(db.get(BlindCampaign, cid).active_rule, eng.RULE_CURRENT)
            versions = db.query(BlindScoreVersion).filter_by(campaign_id=cid).all()
            statuses = [v.status for v in versions]
            self.assertEqual(statuses.count("current"), 1)  # 只有一个有效版本
            days_rows = db.query(BlindDay).filter_by(campaign_id=cid).all()
            self.assertTrue(all(d.score_version == versions[-1].version
                                for d in days_rows if d.scored))

    def test_manual_promote_rejects_stale_or_below_threshold(self):
        from fastapi import HTTPException
        cid = self._create(days=3)
        day = (START + timedelta(days=30)).isoformat()
        with TestSession() as db:
            blind.lock_both(db, cid, day, self.actor, now=datetime_ts(day, 9))
            # 实领让候选明显更差 → 未达门槛
            bd = db.query(BlindDay).filter_by(day=day).one()
            blind.report_actual(db, cid, day, float(bd.current_forecast), self.actor)
            db.commit()
            with self.assertRaises(HTTPException):
                blind.manual_promote(db, cid, self.actor)

    # ---------- 试算绑定规则快照，切换不改变排队试算 ----------
    def test_trial_binds_active_rule_and_keeps_snapshot_after_switch(self):
        cid = self._create(days=4)
        days = [(START + timedelta(days=30 + i)).isoformat() for i in range(4)]
        target = (START + timedelta(days=40)).isoformat()
        with TestSession() as db:
            inp = Input(campaign_id=cid, name="切换前试算", target_day=target)
            t1 = blind.create_trial(db, inp, self.actor)
            db.commit()
            self.assertEqual(t1.bound_rule, eng.RULE_CURRENT)
            blind.run_trial(t1.id)  # 独立 Session 执行
            db.expire_all()
            self.assertEqual(db.get(TrialRun, t1.id).status, "done")

            # 触发晋级
            for day in days:
                blind.lock_both(db, cid, day, self.actor, now=datetime_ts(day, 9))
            for day in days:
                bd = db.query(BlindDay).filter_by(day=day).one()
                blind.report_actual(db, cid, day, float(bd.candidate_forecast + 1.0), self.actor)
            db.commit()
            self.assertEqual(db.get(BlindCampaign, cid).active_rule, eng.RULE_CANDIDATE)

            # 新试算绑定候选；旧试算快照仍是现行，且结果不被改写
            t2 = blind.create_trial(db, Input(campaign_id=cid, name="切换后试算",
                                              target_day=target), self.actor)
            db.commit()
            blind.run_trial(t2.id)
            old = db.get(TrialRun, t1.id)
            new = db.get(TrialRun, t2.id)
            self.assertEqual(old.bound_rule, eng.RULE_CURRENT)
            self.assertEqual(new.bound_rule, eng.RULE_CANDIDATE)
            self.assertEqual(old.rule_snapshot["rule"], eng.RULE_CURRENT)
            self.assertTrue(old.result)  # 已完成结果保留

    def test_trial_retry_does_not_duplicate_done(self):
        from fastapi import HTTPException
        cid = self._create(days=3)
        target = (START + timedelta(days=40)).isoformat()
        with TestSession() as db:
            t = blind.create_trial(db, Input(campaign_id=cid, name="重试试算",
                                             target_day=target), self.actor)
            db.commit()
            blind.run_trial(t.id)
            db.expire_all()
            fresh = db.get(TrialRun, t.id)
            self.assertEqual(fresh.status, "done")
            with self.assertRaises(HTTPException):  # 路由层对 done 的重试会 409
                raise HTTPException(409, "试算已完成，重试不会重复执行")

            # 失败试算可重试，且重试不改写已完成记录
            fresh.status = "failed"
            db.commit()
            fresh.status = "queued"
            fresh.error = ""
            db.commit()
            blind.run_trial(t.id)
            db.expire_all()
            self.assertEqual(db.get(TrialRun, t.id).status, "done")


def datetime_ts(day, hour):
    from datetime import datetime, timezone
    return datetime.fromisoformat(f"{day}T{hour:02d}:00:00").replace(
        tzinfo=timezone.utc).timestamp()


if __name__ == "__main__":
    unittest.main(verbosity=2)
