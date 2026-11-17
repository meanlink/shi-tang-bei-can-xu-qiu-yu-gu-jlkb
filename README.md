# 食堂备餐需求预估

季节需求预测的真实业务分析小型项目。完整模拟客户、痛点、上下游、算法公式与一天工作剧本见 [BUSINESS.md](BUSINESS.md)。

## 模块与业务流程
数据接入（JSON/CSV、字段验证、批次指纹）→ 参数化分析（异步 Python worker）→ 结果复核（指标、图形、明细、解释、批准）→ 定时分发（本地收件箱、CSV/JSON、SHA256）；另有业务总览和审计轨迹。
三个贯穿全栈的实体：Dataset 数据批次、Job 分析任务、Report 分发计划；辅助 User、Audit、Delivery。

### 候选规则盲评晋级（/blind，导航「盲评晋级」）
计划组让候选预测规则先盲评、达标再接替现行规则，独立于普通分析任务的闭环：
- **登记冻结**：一场考核（BlindCampaign）登记双方规则参数快照（现行＝上周同日季节朴素；候选＝加法 Holt-Winters，α 可调、β/γ 固定）、晋级门槛（WAPE/MAE、最小相对改善、最少评分天数）与考核日期、开餐锁报时刻；一经登记不可变。
- **开餐前锁报**：`prelock-window` 在首个考核日前用考核期前历史一次性预生成整窗双方盲预测（任何考核日实领都不可见）；也可逐日/逐规则锁定。迟于开餐时刻的预测标 `late`，**永不参评**；锁定后不可改写，单规则重复锁报 409，整窗重试幂等。
- **实领补齐才评分**：未补实领不出分；同日双方共用同一份实领、同一 WAPE/MAE 口径。补报即更正时，旧评分版本整体置 `superseded` 并全量重算（版本号递增、留痕）。
- **晋级与回退**：候选按门槛达标才晋级为现行规则；已晋级后若更正复算不再达标，自动退回原规则。晋级/回退均写 BlindPromotion 流水，绑定评分版本与采用理由，可逐条追查。
- **并发安全**：按考核单加锁（Redis 可用时分布式锁，否则进程锁兜底），晋级与实领更正串行化，不凭过期评分切换；手动晋级只认当前 `current` 版本。
- **试算绑定**：TrialRun 创建即冻结当时现行规则快照，规则切换不改变已排队/已建试算；失败可重试，已完成拒绝重复执行。
- **中断恢复**：`rescore` 幂等重算，重试不重复计分或晋级；审计记录每个动作。
新表：`blind_campaigns / blind_days / blind_score_versions / blind_promotions / blind_trial_runs`；核心纯函数在 `backend/src/services/blind_engine.py`（仅标准库），状态机在 `backend/src/services/blind.py`。测试在 `tests/`（引擎纯函数、SQLite 服务层、FastAPI 端到端，共 35 例）。

## 快速启动
```bash
cp .env.example .env
docker compose up -d --build --wait
docker compose exec -T backend python -m scripts.seed_demo
```
访问 http://localhost:18213；后端文档 http://localhost:19213/docs；数据库宿主端口 17713。
`SEED_DEMO=true` 自动幂等 seed；不覆盖用户修改。手动 seed 命令可重复执行。

## 演示账号与权限
|账号|密码|真实权限|
|---|---|---|
|admin@demo.local|Demo123!|接入、执行、批准、分发、暂停、读取与下载|
|analyst@demo.local|Demo123!|接入、执行、读取与下载；不能批准或安排分发|
|viewer@demo.local|Demo123!|只读与下载|

## 技术与本地开发
Python 3.12 / FastAPI / SQLAlchemy / NumPy / SciPy / scikit-learn；MySQL 8.0；Redis 7；Nginx；原生 HTML CSS JavaScript ES modules（用户指定）。后端所有校验、持久化和算法在服务端。无前端打包步骤。
后端：安装 backend/requirements.txt，设置 DATABASE_URL、REDIS_URL、JWT_SECRET、ARTIFACT_DIR 后在 backend/ 执行 `python -m migrations.initial`、`python -m scripts.seed_demo`、`uvicorn src.main:app`，另开 `python -m src.worker`。前端使用带 /api 反向代理的 Nginx，直接打开 index.html 不支持 API。

## 目录与工程约定
backend/src/{models,schemas,services,routes,controllers,middlewares,constants,config,utils}；backend/scripts/seed_demo.py；backend/migrations/initial.py。
frontend/src/{api,stores,types,components/common,hooks,pages,router,utils,constants}。
Role 枚举：admin/analyst/viewer；JobStatus：queued/running/completed/failed。后端 backend/src/constants/enums.py，前端 frontend/src/constants/enums.js；路由权限在 middlewares/auth.py；页面按相同角色控制操作入口。

## 环境变量与 Docker
COMPOSE_PROJECT_NAME=meal-seasonal；FRONTEND_PORT=18213；BACKEND_PORT=19213；DB_PORT=17713；DB_PASSWORD、JWT_SECRET 见 .env.example；SEED_DEMO 默认 true。共享命名产物卷供后端下载与 worker 写入，数据库/Redis 各持久卷。
恰好五服务均有健康检查。Redis 队列加 SQL 排队状态兜底；worker 按 next_run 生成本地收件箱文件。周期 60 秒至 30 天。分发结果为批准分析的固定快照；若需新数据重新计算并建立新计划。当前不连接邮件或企业聊天。
错误响应、请求编号、Redis 每 IP 每分钟 240 次限流、JWT 和数据库操作审计。演示密钥与密码仅供本机模拟；生产部署前配置独立密钥与账号。

## 数据与验收
所有数据为固定随机种子生成的模拟业务数据，不能作为实际生产决策证据。项目独立算法、输入边界及业务局限详见 BUSINESS.md。测试与截图统一存放 ../../test-artifacts/gb-1014/，源码不含日志/截图/测试报告。
清理本项目演示数据：`docker compose down -v --remove-orphans`（会删除本项目命名卷）。

## License
MIT，见 LICENSE。
