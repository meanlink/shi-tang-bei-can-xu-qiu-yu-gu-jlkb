"""盲评引擎纯函数测试（仅标准库，可在无第三方依赖环境运行）。

运行：python3 -m unittest test_blind_engine -v
覆盖：盲预测不看未来、双方同口径评分、迟报/缺报排除、更正重算、
晋级门槛、回退判定、参数校验、WAPE 无定义保护。
"""
import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from src.services import blind_engine as eng  # noqa: E402


def make_history(n=30, base=400, slope=2.0, seed_day=None):
    seed_day = seed_day or date(2026, 10, 1)
    rows = []
    for i in range(n):
        weekday = (seed_day + timedelta(days=i)).weekday()
        weekly = [40, 60, 70, 55, 20, -180, -200][weekday]
        rows.append({"date": (seed_day + timedelta(days=i)).isoformat(),
                     "demand": max(0.0, base + slope * i + weekly)})
    return rows


class ForecastTests(unittest.TestCase):
    def test_naive_uses_last_seven_days(self):
        history = make_history(21)
        target = date.fromisoformat(history[-1]["date"]) + timedelta(days=1)
        rows = eng.forecast(eng.RULE_CURRENT, {}, history, target)
        # 现行规则：目标日星期与 7 天前对齐
        expect = history[-7]["demand"]
        self.assertAlmostEqual(rows[0]["forecast"], float(expect), places=4)

    def test_blind_forecast_ignores_future_rows(self):
        history = make_history(30)
        target = date.fromisoformat(history[19]["date"])  # 目标日在历史中间
        rows = eng.forecast(eng.RULE_CANDIDATE, {"alpha": 0.4}, history, target)
        # 与只截取到目标日前的历史结果必须一致
        cut = [r for r in history if date.fromisoformat(r["date"]) < target]
        rows_cut = eng.forecast(eng.RULE_CANDIDATE, {"alpha": 0.4}, cut, target)
        self.assertEqual(rows, rows_cut)

    def test_forecast_non_negative_and_horizon(self):
        history = [{"date": (date(2026, 10, 1) + timedelta(days=i)).isoformat(),
                    "demand": 0.0} for i in range(20)]
        rows = eng.forecast(eng.RULE_CANDIDATE, {"alpha": 0.3}, history, date(2026, 10, 21), 5)
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(r["forecast"] >= 0 for r in rows))

    def test_gap_in_history_rejected(self):
        history = make_history(20)
        del history[10]
        with self.assertRaises(eng.EngineError):
            eng.forecast(eng.RULE_CURRENT, {}, history, date(2026, 10, 21))

    def test_candidate_needs_two_periods(self):
        history = make_history(10)
        with self.assertRaises(eng.EngineError):
            eng.forecast(eng.RULE_CANDIDATE, {"alpha": 0.3}, history, date(2026, 10, 11))

    def test_alpha_range_validated(self):
        with self.assertRaises(eng.EngineError):
            eng.validate_rule(eng.RULE_CANDIDATE, {"alpha": 5.0})
        p = eng.validate_rule(eng.RULE_CANDIDATE, {"alpha": 0.5})
        self.assertEqual(p["beta"], eng.DEFAULT_TREND)
        self.assertEqual(p["gamma"], eng.DEFAULT_SEASON)


class ScoreTests(unittest.TestCase):
    def _pairs(self, actuals, currents, candidates):
        return [{"actual": a, "current": c, "candidate": k}
                for a, c, k in zip(actuals, currents, candidates)]

    def test_wape_formula_and_better_rule(self):
        pairs = self._pairs([100, 200], [95, 195], [90, 180])  # 现行更准
        score = eng.score_pair(pairs)
        # 现行 abs err = 5+5=10，候选 = 10+20=30；实际合计 300
        self.assertAlmostEqual(score["current"]["wape"], 10 / 300 * 100, places=4)
        self.assertAlmostEqual(score["candidate"]["wape"], 30 / 300 * 100, places=4)
        self.assertEqual(score["better"], eng.RULE_CURRENT)

    def test_candidate_better(self):
        pairs = self._pairs([100, 200, 150], [90, 180, 140], [99, 199, 149])
        score = eng.score_pair(pairs)
        self.assertEqual(score["better"], eng.RULE_CANDIDATE)
        self.assertLess(score["wape_delta_pct"], 0)

    def test_missing_values_excluded_from_scoring(self):
        pairs = self._pairs([100, 200], [90, 180], [95, 195])
        pairs.append({"actual": None, "current": 100, "candidate": 100})  # 实领未齐
        pairs.append({"actual": 80, "current": None, "candidate": 70})   # 预测未齐
        score = eng.score_pair(pairs)
        self.assertEqual(score["scored_days"], 2)

    def test_zero_total_actual_wape_undefined(self):
        pairs = self._pairs([0, 0], [10, 20], [5, 8])
        score = eng.score_pair(pairs)
        self.assertIsNone(score["current"]["wape"])
        self.assertIsNone(score["better"])
        ok, reason = eng.decide_promotion(score, {"metric": "wape"})
        self.assertFalse(ok)
        self.assertIn("无定义", reason)

    def test_tie_not_promoted(self):
        pairs = self._pairs([100], [90], [90])
        score = eng.score_pair(pairs)
        self.assertEqual(score["better"], "tie")
        ok, _ = eng.decide_promotion(score, {"metric": "wape", "min_improve_pct": 0})
        self.assertFalse(ok)


class PromotionTests(unittest.TestCase):
    def test_threshold_min_improve_blocks(self):
        # 现行 WAPE 10%（误差100），候选 WAPE 9.8%（误差98），相对改善 2%，门槛 5%
        pairs = [{"actual": 1000, "current": 900, "candidate": 902}]
        score = eng.score_pair(pairs)
        ok, reason = eng.decide_promotion(score, {"metric": "wape", "min_improve_pct": 5})
        self.assertFalse(ok)
        self.assertIn("未达门槛", reason)
        ok2, reason2 = eng.decide_promotion(score, {"metric": "wape", "min_improve_pct": 1})
        self.assertTrue(ok2)
        self.assertIn("达到门槛", reason2)

    def test_min_scored_days_gate(self):
        pairs = [{"actual": 1000, "current": 500, "candidate": 990}]
        score = eng.score_pair(pairs)
        ok, reason = eng.decide_promotion(
            score, {"metric": "wape", "min_improve_pct": 0, "min_scored_days": 7})
        self.assertFalse(ok)
        self.assertIn("最少 7 天", reason)

    def test_mae_metric(self):
        pairs = [{"actual": 100, "current": 80, "candidate": 95},
                 {"actual": 200, "current": 170, "candidate": 198}]
        score = eng.score_pair(pairs)
        ok, _ = eng.decide_promotion(score, {"metric": "mae", "min_improve_pct": 50})
        # 现行 MAE=25，候选 MAE=3.5，改善 86%
        self.assertTrue(ok)

    def test_rollback_when_candidate_becomes_worse(self):
        # 第一次：候选优 → 晋级；更正实领后候选变差 → 不达标
        good = eng.score_pair([{"actual": 1000, "current": 800, "candidate": 990}])
        promoted, _ = eng.decide_promotion(good, {"metric": "wape", "min_improve_pct": 1})
        self.assertTrue(promoted)
        bad = eng.score_pair([{"actual": 1000, "current": 990, "candidate": 800}])
        still, reason = eng.decide_promotion(bad, {"metric": "wape", "min_improve_pct": 1})
        self.assertFalse(still)
        self.assertIn("未优于现行", reason)

    def test_threshold_validation(self):
        with self.assertRaises(eng.EngineError):
            eng.validate_threshold({"metric": "rmse"})
        with self.assertRaises(eng.EngineError):
            eng.validate_threshold({"min_improve_pct": 200})
        t = eng.validate_threshold({})
        self.assertEqual(t["metric"], "wape")


if __name__ == "__main__":
    unittest.main(verbosity=2)
