import time, json
from datetime import date, datetime, timezone
from types import SimpleNamespace
from sqlalchemy import select, func
from src.config.database import Session
from migrations.initial import migrate
from src.models import User, Dataset, Job, Report, Delivery, Audit, BlindCampaign
from src.services.business import metadata, seed_records, analyze
from src.services import blind
from src.services.reports import deliver
from src.utils.security import hash_password
from src.utils.serialization import checksum


def _seed_blind(db, dataset, admin, m):
    """落一场已跑完的盲评样例：仅用窗口前历史锁预测，再逐日补实领，触发完整评分/晋级留痕。"""
    name = '盲评样例 · ' + m['name']
    if db.scalar(select(BlindCampaign).where(BlindCampaign.name == name)):
        return
    window = dataset.records[-7:]
    start, end = window[0]['date'], window[-1]['date']
    data = SimpleNamespace(
        name=name, dataset_id=dataset.id, start_date=start, end_date=end, lock_hour=10,
        current_params={}, candidate_params={'alpha': 0.35},
        threshold={'metric': 'wape', 'min_improve_pct': 1.0, 'min_scored_days': 7})
    campaign = blind.create_campaign(db, data, admin)
    db.flush()
    for row in window:
        day = row['date']
        lock_ts = datetime.fromisoformat(f'{day}T09:00:00').replace(tzinfo=timezone.utc).timestamp()
        blind.lock_both(db, campaign.id, day, admin, now=lock_ts)
        actual_ts = datetime.fromisoformat(f'{day}T12:00:00').replace(tzinfo=timezone.utc).timestamp()
        blind.report_actual(db, campaign.id, day, float(row['demand']), admin, now=actual_ts)
    db.add(Audit(actor='seed', action='demo.blind', detail={'campaign': campaign.id}, at=time.time()))


def seed():
    migrate()
    with Session() as db:
        for role,name in [('admin','林知 · 业务负责人'),('analyst','陈舟 · 分析员'),('viewer','周禾 · 观察员')]:
            email=role+'@demo.local'
            if not db.scalar(select(User).where(User.email==email)):
                db.add(User(email=email,name=name,role=role,password=hash_password('Demo123!')))
        db.flush()
        m=metadata()
        d=db.scalar(select(Dataset).where(Dataset.name==m['dataset_name']))
        if not d:
            rows=seed_records()
            d=Dataset(name=m['dataset_name'],source=m['short']+'-202611.csv',records=rows,checksum=checksum(rows),created_at=time.time())
            db.add(d); db.flush()
        if not db.scalar(select(Job).where(Job.name=='基线复核 · '+m['name'])):
            params={p['key']:p['default'] for p in m.get('parameters',[])}
            result=analyze(d.records,params)
            json.dumps(result,ensure_ascii=False,allow_nan=False)
            admin=db.scalar(select(User).where(User.role=='admin'))
            j=Job(name='基线复核 · '+m['name'],dataset_id=d.id,params=params,status='completed',result=result,created_by=admin.id,created_at=time.time()-3600,finished_at=time.time()-3595,approved=True)
            db.add(j);db.flush()
            r=Report(name='每日业务复核简报',job_id=j.id,destination='业务复核组 · 本地收件箱',interval_seconds=86400,next_run=time.time()+86400,enabled=True)
            db.add(r);db.flush();deliver(db,r)
            db.add(Audit(actor='seed',action='demo.baseline',detail={'dataset':d.id},at=time.time()))
        admin=db.scalar(select(User).where(User.role=='admin'))
        _seed_blind(db, d, admin, m)
        db.commit()
        all_models=[User,Dataset,Job,Report,Delivery,Audit,BlindCampaign]
        counts={model.__tablename__:db.scalar(select(func.count()).select_from(model)) for model in all_models}
        print(json.dumps(counts,ensure_ascii=False))
        return counts
if __name__=='__main__':seed()
