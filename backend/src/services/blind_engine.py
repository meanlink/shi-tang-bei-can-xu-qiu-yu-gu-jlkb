"""盲评规则引擎：候选预测规则与现行规则的纯函数实现、评分与晋级判定。

设计约束（见业务需求）：
- 只依赖 Python 标准库，便于在无第三方包环境下独立单测。
- 预测只允许使用「目标日之前」的历史，绝不读取未来值（盲评）。
- 评分口径对双方完全一致：同日同份实领、同一指标公式。
- 所有数值口径集中在此，服务层只负责状态机与持久化。
"""
from __future__ import annotations

import math
from datetime import date, timedelta

PERIOD = 7
RULE_CURRENT = "current"      # 现行规则：上周同日（季节朴素）
RULE_CANDIDATE = "candidate"  # 候选规则：加法 Holt-Winters
RULES = (RULE_CURRENT, RULE_CANDIDATE)
RULE_LABELS = {RULE_CURRENT: "现行规则 · 上周同日", RULE_CANDIDATE: "候选规则 · 周期平滑"}

# 候选规则固定参数（登记时写入快照，引擎不再读取外部可变配置）
DEFAULT_TREND = 0.15
DEFAULT_SEASON = 0.20
MIN_HISTORY = 2 * PERIOD  # 两个完整周期才能初始化候选规则


class EngineError(ValueError):
    """规则参数或历史数据不满足计算条件。"""


def _coerce_history(history):
    """把 [{date, demand}] 规整为按日期升序的 (date, float) 序列并校验。"""
    if not isinstance(history, (list, tuple)):
        raise EngineError("历史观测必须是数组")
    parsed = []
    for row in history:
        try:
            day = date.fromisoformat(str(row["date"]))
            value = float(row["demand"])
        except (KeyError, TypeError, ValueError) as exc:
            raise EngineError("历史日期须 YYYY-MM-DD，领餐份数须为数值") from exc
        if not math.isfinite(value) or value < 0:
            raise EngineError("领餐份数须为非负有限值")
        parsed.append((day, value))
    parsed.sort(key=lambda pair: pair[0])
    if len({day for day, _ in parsed}) != len(parsed):
        raise EngineError("历史日期不可重复")
    return parsed


def _forecast_naive(history_values, target_index, horizon):
    """现行规则：取目标日之前最后 7 天循环外推（季节朴素法）。"""
    if target_index < PERIOD:
        raise EngineError("现行规则至少需要 7 天历史")
    base = history_values[target_index - PERIOD:target_index]
    return [max(0.0, base[(target_index + step) % PERIOD]) for step in range(horizon)]


def _forecast_hw(history_values, target_index, horizon, alpha, beta, gamma):
    """候选规则：加法 Holt-Winters，初始化只用前两个周期，逐点递推到目标日前。"""
    if target_index < MIN_HISTORY:
        raise EngineError("候选规则至少需要连续 14 天历史才能初始化季节项")
    values = history_values[:target_index]
    level = sum(values[:PERIOD]) / PERIOD
    trend = (sum(values[PERIOD:2 * PERIOD]) / PERIOD - level) / PERIOD
    seasons = [v - level for v in values[:PERIOD]]
    for index in range(PERIOD, len(values)):
        slot = index % PERIOD
        prev_level, prev_season = level, seasons[slot]
        observed = values[index]
        level = alpha * (observed - prev_season) + (1 - alpha) * (level + trend)
        trend = beta * (level - prev_level) + (1 - beta) * trend
        seasons[slot] = gamma * (observed - level) + (1 - gamma) * prev_season
    out = []
    for step in range(1, horizon + 1):
        raw = level + step * trend + seasons[(len(values) + step - 1) % PERIOD]
        out.append(max(0.0, raw))
    return out


def validate_rule(rule, params):
    if rule not in RULES:
        raise EngineError("未知预测规则")
    params = params or {}
    if rule == RULE_CANDIDATE:
        try:
            alpha = float(params.get("alpha", 0.35))
            beta = float(params.get("beta", DEFAULT_TREND))
            gamma = float(params.get("gamma", DEFAULT_SEASON))
        except (TypeError, ValueError) as exc:
            raise EngineError("候选规则平滑系数须为数值") from exc
        if not (0.1 <= alpha <= 0.9 and 0.0 < beta < 1.0 and 0.0 < gamma < 1.0):
            raise EngineError("α 须 0.1~0.9，β、γ 须位于 (0,1)")
        return {"alpha": round(alpha, 6), "beta": round(beta, 6), "gamma": round(gamma, 6)}
    return {"method": "seasonal_naive"}  # 现行规则无可调参数


def forecast(rule, params, history, target_day, horizon=1):
    """生成 target_day 起 horizon 天的盲预测；target_day 当天及之后的数据一律不可见。"""
    if horizon < 1:
        raise EngineError("预测跨度至少 1 天")
    target_day = date.fromisoformat(str(target_day))
    clean = _coerce_history(history)
    cutoff = target_day - timedelta(days=1)
    visible = [(day, value) for day, value in clean if day <= cutoff]
    if not visible:
        raise EngineError("目标日之前没有可用历史，无法盲预测")
    days = [day for day, _ in visible]
    values = [value for _, value in visible]
    # 历史须连续到目标日前一天，保证「上周同日」对齐不错位
    expected = [target_day - timedelta(days=k) for k in range(len(days), 0, -1)]
    if days != expected:
        raise EngineError("历史须连续覆盖到目标日前一天，停餐日请显式记 0")
    if rule == RULE_CURRENT:
        preds = _forecast_naive(values, len(values), horizon)
    else:
        p = validate_rule(RULE_CANDIDATE, params)
        preds = _forecast_hw(values, len(values), horizon, p["alpha"], p["beta"], p["gamma"])
    rows = []
    for step, value in enumerate(preds):
        day = target_day + timedelta(days=step)
        rows.append({"date": day.isoformat(), "forecast": round(float(value), 4)})
    return rows


def score_pair(pairs):
    """双方同一口径评分。pairs: [{actual, current, candidate}, ...]，只统计三方齐备的天。"""
    usable = []
    for row in pairs:
        if row.get("actual") is None or row.get(RULE_CURRENT) is None or row.get(RULE_CANDIDATE) is None:
            continue
        actual = float(row["actual"])
        if not math.isfinite(actual) or actual < 0:
            raise EngineError("实领份数须为非负有限值")
        usable.append((actual, float(row[RULE_CURRENT]), float(row[RULE_CANDIDATE])))
    if not usable:
        return None
    total_actual = sum(a for a, _, _ in usable)
    result = {"scored_days": len(usable)}
    for rule, idx in ((RULE_CURRENT, 1), (RULE_CANDIDATE, 2)):
        abs_err = sum(abs(a - p) for a, p in ((x[0], x[idx]) for x in usable))
        mae = abs_err / len(usable)
        wape = (abs_err / total_actual * 100.0) if total_actual > 0 else None
        result[rule] = {
            "mae": round(mae, 6),
            "wape": round(wape, 6) if wape is not None else None,
            "abs_error": round(abs_err, 6),
        }
    cur = result[RULE_CURRENT]["wape"]
    cand = result[RULE_CANDIDATE]["wape"]
    if cur is None or cand is None:
        result["better"] = None  # 实领合计为 0，WAPE 无定义，不能据此晋级
    else:
        result["better"] = RULE_CANDIDATE if cand < cur else (RULE_CURRENT if cur < cand else "tie")
        result["wape_delta_pct"] = round(cand - cur, 6)  # 负值=候选更优
    return result


def decide_promotion(score, threshold):
    """按登记门槛判定候选是否晋级。返回 (是否晋级, 理由)。

    门槛 metric=wape：候选 WAPE 须低于现行，且相对改善幅度 >= min_improve_pct（相对现行 WAPE）。
    门槛 metric=mae：候选 MAE 须低于现行，且相对改善幅度 >= min_improve_pct。
    平局或 WAPE 无定义一律不晋级。
    """
    if score is None:
        return False, "尚无齐备评分，不能判定"
    metric = (threshold or {}).get("metric", "wape")
    min_improve = float((threshold or {}).get("min_improve_pct", 0.0))
    min_days = int((threshold or {}).get("min_scored_days", 1))
    if score["scored_days"] < min_days:
        return False, f"已评分 {score['scored_days']} 天，未达最少 {min_days} 天门槛"
    if metric == "wape":
        cur, cand = score[RULE_CURRENT]["wape"], score[RULE_CANDIDATE]["wape"]
        if cur is None or cand is None:
            return False, "实领合计为 0，WAPE 无定义，不能据此晋级"
    elif metric == "mae":
        cur, cand = score[RULE_CURRENT]["mae"], score[RULE_CANDIDATE]["mae"]
    else:
        raise EngineError("门槛指标仅支持 wape 或 mae")
    if cur <= 0:
        return False, "现行规则误差为 0，候选无改善空间"
    improve_pct = (cur - cand) / cur * 100.0
    if cand >= cur:
        return False, f"候选未优于现行（改善 {improve_pct:.2f}%）"
    if improve_pct + 1e-9 < min_improve:
        return False, f"相对改善 {improve_pct:.2f}% 未达门槛 {min_improve:g}%"
    return True, f"相对改善 {improve_pct:.2f}% 达到门槛 {min_improve:g}%（{metric.upper()} {cur:.2f}→{cand:.2f}）"


def validate_threshold(threshold):
    metric = (threshold or {}).get("metric", "wape")
    if metric not in ("wape", "mae"):
        raise EngineError("门槛指标仅支持 wape 或 mae")
    try:
        min_improve = float((threshold or {}).get("min_improve_pct", 0.0))
        min_days = int((threshold or {}).get("min_scored_days", 1))
    except (TypeError, ValueError) as exc:
        raise EngineError("门槛参数须为数值") from exc
    if not (0.0 <= min_improve <= 100.0):
        raise EngineError("最小改善幅度须在 0~100% 之间")
    if min_days < 1:
        raise EngineError("最少评分天数须 >= 1")
    return {"metric": metric, "min_improve_pct": min_improve, "min_scored_days": min_days}
