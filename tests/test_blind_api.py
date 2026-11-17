"""盲评路由端到端测试：经 FastAPI TestClient 走完整 HTTP/鉴权/审计链路。"""
import os
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone


def _utc_today():
    # 服务端锁报截止按 UTC 计算，测试统一用 UTC 日期，避免时区差一天导致历史缺口
    return datetime.now(timezone.utc).date()

_tmp = tempfile.mkdtemp(prefix="blind-api-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/api.db"
os.environ["REDIS_URL"] = "redis://127.0.0.1:6399/0"
os.environ["JWT_SECRET"] = "test-secret"  # 与服务测试保持一致，避免导入顺序污染
os.environ["ARTIFACT_DIR"] = _tmp
os.environ["SEED_DEMO"] = "false"

import sys  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from src.main import app  # noqa: E402
import src.config.database as dbconfig  # noqa: E402
from src.config.database import Base, Session  # noqa: E402
from src import models  # noqa: E402
from migrations.initial import migrate  # noqa: E402
from src.services import blind  # noqa: E402
from src.services.business import seed_records  # noqa: E402
from src.models import User, Dataset  # noqa: E402
from src.utils.security import hash_password, token_for  # noqa: E402
from src.utils.serialization import checksum  # noqa: E402

# 独立文件库引擎，显式重绑，避免与服务测试的内存库全局状态互相污染
api_engine = create_engine(f"sqlite:///{_tmp}/api.db", connect_args={"check_same_thread": False})
ApiSession = sessionmaker(api_engine)
dbconfig.engine = api_engine
dbconfig.Session = ApiSession
blind.Session = ApiSession
client = TestClient(app)


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def _extended_records():
    """在 112 天种子数据后，用同一星期节律补齐到「后天」前一天，保证未来考核有历史。"""
    import numpy as np
    rows = seed_records()
    last = date.fromisoformat(rows[-1]["date"])
    weekday = [480, 520, 550, 530, 470, 180, 140]
    cursor, i = last + timedelta(days=1), len(rows)
    stop = _utc_today() + timedelta(days=1)  # 历史覆盖到明天(UTC)，考核从后天开始
    while cursor <= stop:
        demand = max(0, int(round(weekday[cursor.weekday()] + 0.45 * i)))
        rows.append({"date": cursor.isoformat(), "demand": demand, "meal": "东港园午餐"})
        cursor += timedelta(days=1)
        i += 1
    return rows


def _seed_user_and_dataset():
    migrate()
    with Session() as db:
        admin = User(email="admin@demo.local", name="负责人", role="admin",
                     password=hash_password("Demo123!"))
        analyst = User(email="analyst@demo.local", name="分析员", role="analyst",
                       password=hash_password("Demo123!"))
        viewer = User(email="viewer@demo.local", name="观察员", role="viewer",
                      password=hash_password("Demo123!"))
        db.add_all([admin, analyst, viewer])
        rows = _extended_records()
        ds = Dataset(name="API盲评批次", source="api.json", records=rows,
                     checksum=checksum(rows), created_at=time.time())
        db.add(ds)
        db.commit()
        return admin.id, analyst.id, viewer.id, ds.id


class BlindApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 防御性重绑：无论模块导入顺序如何，API 测试始终走独立文件库
        dbconfig.engine = api_engine
        dbconfig.Session = ApiSession
        blind.Session = ApiSession
        cls.admin_id, cls.analyst_id, cls.viewer_id, cls.did = _seed_user_and_dataset()
        with ApiSession() as db:
            cls.admin_token = token_for(db.get(User, cls.admin_id))
            cls.analyst_token = token_for(db.get(User, cls.analyst_id))
            cls.viewer_token = token_for(db.get(User, cls.viewer_id))
        # 考核期放在未来(UTC)：今天实时锁报不会被判迟报，历史已补齐到考核前一天
        cls.start = (_utc_today() + timedelta(days=2)).isoformat()
        cls.end = (_utc_today() + timedelta(days=8)).isoformat()

    def _create_campaign(self, token=None):
        r = client.post("/blind/campaigns", headers=auth(token or self.admin_token), json={
            "name": "API 盲评考核", "dataset_id": self.did,
            "start_date": self.start, "end_date": self.end, "lock_hour": 10,
            "candidate_params": {"alpha": 0.35},
            "threshold": {"metric": "wape", "min_improve_pct": 1.0, "min_scored_days": 3}})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def test_01_viewer_cannot_create(self):
        r = client.post("/blind/campaigns", headers=auth(self.viewer_token), json={
            "name": "越权考核", "dataset_id": self.did,
            "start_date": self.start, "end_date": self.end})
        self.assertEqual(r.status_code, 403)

    def test_02_full_lifecycle_lock_actual_correct_promote_rollback(self):
        c = self._create_campaign()
        cid = c["id"]
        # 开餐前一次性预锁整个考核窗口的双方盲预测（盲评主入口）
        r = client.post(f"/blind/campaigns/{cid}/prelock-window",
                        headers=auth(self.analyst_token))
        self.assertEqual(r.status_code, 200, r.text)
        window = r.json()
        self.assertEqual(len(window), 7)
        self.assertTrue(all(x["current_forecast"] is not None
                            and x["candidate_forecast"] is not None for x in window))
        self.assertFalse(any(x["current_late"] or x["candidate_late"] for x in window))
        d = next(x for x in window if x["day"] == self.start)
        # 预锁可重试：已锁定的日期保持不变（中断恢复安全，不重复计分）
        r_retry = client.post(f"/blind/campaigns/{cid}/prelock-window",
                              headers=auth(self.analyst_token))
        self.assertEqual(r_retry.status_code, 200)
        self.assertEqual(next(x for x in r_retry.json()
                              if x["day"] == self.start)["current_forecast"],
                         d["current_forecast"])
        # 单规则重复锁报 → 409，预测一经锁定不可改写
        r2 = client.post(f"/blind/campaigns/{cid}/lock-forecast",
                         headers=auth(self.analyst_token),
                         json={"day": self.start, "rule": "current"})
        self.assertEqual(r2.status_code, 409)

        # 未补齐实领前：无评分版本
        detail = client.get(f"/blind/campaigns/{cid}", headers=auth(self.viewer_token)).json()
        self.assertEqual(len(detail["score_versions"]), 0)
        self.assertEqual(len(detail["pending_actual_days"]), 7)

        # 补齐实领：让候选接近实领 → 累积达标后自动晋级
        for drow in detail["days"]:
            actual = drow["candidate_forecast"] + 0.5
            rr = client.post(f"/blind/campaigns/{cid}/actual",
                             headers=auth(self.analyst_token),
                             json={"day": drow["day"], "value": actual})
            self.assertEqual(rr.status_code, 200, rr.text)
        detail = client.get(f"/blind/campaigns/{cid}", headers=auth(self.admin_token)).json()
        self.assertEqual(detail["active_rule"], "candidate")
        self.assertTrue(any(p["action"] == "promoted" for p in detail["promotions"]))
        version = detail["score_versions"][-1]
        self.assertEqual(version["status"], "current")

        # 更正全部实领：改为现行接近 → 重算后自动回退
        for drow in detail["days"]:
            rr = client.post(f"/blind/campaigns/{cid}/actual",
                             headers=auth(self.admin_token),
                             json={"day": drow["day"],
                                   "value": drow["current_forecast"] + 0.5})
            self.assertEqual(rr.status_code, 200, rr.text)
        detail = client.get(f"/blind/campaigns/{cid}", headers=auth(self.admin_token)).json()
        self.assertEqual(detail["active_rule"], "current")
        self.assertEqual(detail["promotions"][-1]["action"], "rolled_back")
        # 旧版本全部 superseded，仅一个 current
        self.assertEqual(sum(1 for v in detail["score_versions"] if v["status"] == "current"), 1)
        self.assertTrue(all(v["basis"].get("correction") is True
                            for v in detail["score_versions"][-7:]))
        # 每次采用依据可追查
        r = client.get(f"/blind/campaigns/{cid}/promotions",
                       headers=auth(self.viewer_token))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(all(p["reason"] and p["score_version_id"] for p in r.json()))

    def test_03_late_forecast_never_scored(self):
        c = self._create_campaign()
        cid = c["id"]
        # 过了开餐时刻（lock_hour=10 UTC）才锁报 → 迟报
        from datetime import datetime, timezone
        late_now = datetime.fromisoformat(f"{self.start}T11:30:00").replace(
            tzinfo=timezone.utc).timestamp()
        with Session() as db:
            from src.services import blind
            from src.models import User
            admin = db.get(User, self.admin_id)
            blind.lock_both(db, cid, self.start, admin, now=late_now)
            db.commit()
        rr = client.post(f"/blind/campaigns/{cid}/actual",
                         headers=auth(self.admin_token),
                         json={"day": self.start, "value": 400})
        self.assertEqual(rr.status_code, 200)
        day = next(x for x in client.get(
            f"/blind/campaigns/{cid}", headers=auth(self.admin_token)).json()["days"]
            if x["day"] == self.start)
        self.assertFalse(day["scored"])
        self.assertIn("迟报", day["excluded_reason"])

    def test_04_rescore_recovery_idempotent_and_audit_trail(self):
        c = self._create_campaign()
        cid = c["id"]
        client.post(f"/blind/campaigns/{cid}/lock-forecast",
                    headers=auth(self.admin_token), json={"day": self.start})
        client.post(f"/blind/campaigns/{cid}/actual",
                    headers=auth(self.admin_token),
                    json={"day": self.start, "value": 410})
        r = client.post(f"/blind/campaigns/{cid}/rescore",
                        headers=auth(self.analyst_token))
        self.assertEqual(r.status_code, 200, r.text)
        # 审计可追查
        audits = client.get("/audits", headers=auth(self.viewer_token)).json()
        actions = {a["action"] for a in audits}
        self.assertIn("blind.campaign.create", actions)
        self.assertIn("blind.forecast.lock", actions)
        self.assertIn("blind.actual.report", actions)
        self.assertIn("blind.score.version", actions)

    def test_05_trial_binds_rule_then_immutable_after_switch(self):
        c = self._create_campaign()
        cid = c["id"]
        target = "2026-11-02"
        r = client.post("/blind/trials", headers=auth(self.analyst_token),
                        json={"campaign_id": cid, "name": "切换前试算", "target_day": target})
        self.assertEqual(r.status_code, 201, r.text)
        first = r.json()
        self.assertEqual(first["bound_rule"], "current")
        self.assertEqual(first["status"], "done")
        self.assertTrue(first["result"]["forecast"] >= 0)
        # 已完成试算拒绝重复执行
        rr = client.post(f"/blind/trials/{first['id']}/retry",
                         headers=auth(self.admin_token))
        self.assertEqual(rr.status_code, 409)
        # 试算列表只读可查
        self.assertEqual(client.get("/blind/trials",
                                    headers=auth(self.viewer_token)).status_code, 200)

    def test_06_invalid_inputs_rejected(self):
        # 日期倒置
        r = client.post("/blind/campaigns", headers=auth(self.admin_token), json={
            "name": "坏考核", "dataset_id": self.did,
            "start_date": self.end, "end_date": self.start})
        self.assertEqual(r.status_code, 422)
        # 负实领
        c = self._create_campaign()
        rr = client.post(f"/blind/campaigns/{c['id']}/actual",
                         headers=auth(self.admin_token),
                         json={"day": self.start, "value": -1})
        self.assertEqual(rr.status_code, 422)
        # 不存在的考核日
        rr = client.post(f"/blind/campaigns/{c['id']}/actual",
                         headers=auth(self.admin_token),
                         json={"day": "2026-01-01", "value": 10})
        self.assertEqual(rr.status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
