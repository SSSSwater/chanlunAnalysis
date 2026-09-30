import unittest
from unittest.mock import patch

from backend import app as app_module
from backend.services.main_rise import evaluate_main_rise_candidate, match_main_rise_candidate


def main_rise_result(
    *,
    setup_type="BREAKOUT_RETEST_UP",
    environment_state="BULL_TREND",
    volume_available=True,
    incomplete_latest=False,
    current_price=10.5,
    entry_limit=11.0,
    invalidation=10.0,
    first_target=13.0,
    risk_reward=None,
):
    signal = {
        "type": setup_type,
        "setupId": "setup-main-rise",
        "planId": "plan-main-rise",
        "direction": "BUY",
        "marketMode": "TREND",
        "status": "CONFIRMED",
        "executable": True,
        "reviewRequired": False,
        "triggerPrice": 10.8,
        "entryLimit": entry_limit,
        "invalidationPrice": invalidation,
        "firstTarget": first_target,
        "cancellationConditions": ["触发前跌破结构失效价则取消"],
        "reasons": ["多头趋势", "突破回测守住"],
        "confirmedAt": "2026-08-25",
        "volume": {
            "available": volume_available,
            "state": "EXPANSION" if volume_available else "UNAVAILABLE",
            "latestRatio": 1.4 if volume_available else 0,
            "qualified": volume_available,
        },
    }
    if risk_reward is not None:
        signal["firstTarget"] = entry_limit + (entry_limit - invalidation) * risk_reward

    return {
        "rawKlines": [{"date": f"2026-01-{index + 1:02d}", "completed": True} for index in range(35)],
        "summary": {"latestClose": current_price},
        "priceAction": {
            "environment": {
                "state": environment_state,
                "label": "多头趋势" if environment_state == "BULL_TREND" else "交易区间",
                "reviewRequired": False,
            },
            "marketMode": {"mode": "TREND", "label": "趋势"},
            "volume": {
                "available": volume_available,
                "state": "EXPANSION" if volume_available else "UNAVAILABLE",
                "latestRatio": 1.4 if volume_available else 0,
            },
            "uncertainty": {"incompleteLatestBar": incomplete_latest},
            "setups": [
                {
                    "type": setup_type,
                    "setupId": "setup-main-rise",
                    "status": "CONFIRMED",
                    "direction": "BUY",
                    "marketMode": "TREND",
                    "reviewRequired": False,
                    "confirmedAt": "2026-08-25",
                    "volume": signal["volume"],
                    "evidence": ["突破回测守住"],
                }
            ],
            "signals": [signal],
            "tradePlans": [
                {
                    "setupId": "setup-main-rise",
                    "planId": "plan-main-rise",
                    "status": "CONFIRMED",
                    "direction": "BUY",
                    "executable": True,
                    "entryLimit": entry_limit,
                    "structuralInvalidation": invalidation,
                    "firstTarget": {"price": signal["firstTarget"]},
                    "cancellationConditions": signal["cancellationConditions"],
                }
            ],
        },
    }


class MainRisePolicyTests(unittest.TestCase):
    def test_accepts_confirmed_trend_launch_with_complete_plan(self):
        result = main_rise_result()

        evaluation = evaluate_main_rise_candidate(result)

        self.assertTrue(evaluation["matched"])
        self.assertEqual(evaluation["match"]["setupType"], "BREAKOUT_RETEST_UP")
        self.assertGreaterEqual(evaluation["match"]["riskReward"], 1.5)
        self.assertEqual(match_main_rise_candidate(result)["status"], "CONFIRMED")

    def test_rejects_generic_bullish_signal_outside_allowlist(self):
        evaluation = evaluate_main_rise_candidate(main_rise_result(setup_type="BREAKOUT_UP"))

        self.assertFalse(evaluation["matched"])
        self.assertTrue(any("主升启动结构" in reason for reason in evaluation["reasons"]))

    def test_rejects_range_and_reversal_context(self):
        evaluation = evaluate_main_rise_candidate(main_rise_result(environment_state="RANGE"))

        self.assertFalse(evaluation["matched"])
        self.assertTrue(any("不是多头趋势" in reason for reason in evaluation["reasons"]))

    def test_rejects_unavailable_participation_data(self):
        evaluation = evaluate_main_rise_candidate(main_rise_result(volume_available=False))

        self.assertFalse(evaluation["matched"])
        self.assertTrue(any("成交量" in reason for reason in evaluation["reasons"]))

    def test_rejects_incomplete_latest_bar(self):
        evaluation = evaluate_main_rise_candidate(main_rise_result(incomplete_latest=True))

        self.assertFalse(evaluation["matched"])
        self.assertTrue(any("尚未完成" in reason for reason in evaluation["reasons"]))

    def test_rejects_chasing_above_entry_cap(self):
        evaluation = evaluate_main_rise_candidate(main_rise_result(current_price=11.2))

        self.assertFalse(evaluation["matched"])
        self.assertTrue(any("禁止追价" in reason for reason in evaluation["reasons"]))

    def test_rejects_insufficient_room(self):
        evaluation = evaluate_main_rise_candidate(main_rise_result(risk_reward=1.2))

        self.assertFalse(evaluation["matched"])
        self.assertTrue(any("空间风险比不足" in reason for reason in evaluation["reasons"]))

    def test_rejects_missing_trend_mode(self):
        result = main_rise_result()
        result["priceAction"]["marketMode"] = {}
        result["priceAction"]["signals"][0]["marketMode"] = None
        result["priceAction"]["setups"][0]["marketMode"] = None

        evaluation = evaluate_main_rise_candidate(result)

        self.assertFalse(evaluation["matched"])
        self.assertTrue(any("当前市场模式" in reason for reason in evaluation["reasons"]))

    def test_rejects_missing_executable_plan(self):
        result = main_rise_result()
        result["priceAction"]["tradePlans"][0]["executable"] = False

        evaluation = evaluate_main_rise_candidate(result)

        self.assertFalse(evaluation["matched"])
        self.assertTrue(any("交易计划不可执行" in reason for reason in evaluation["reasons"]))


class MainRiseEndpointTests(unittest.TestCase):
    def setUp(self):
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()

    def test_endpoint_stops_on_second_sequential_match(self):
        candidates = [
            {"symbol": "000001", "name": "第一只", "latestPrice": 10},
            {"symbol": "000002", "name": "第二只", "latestPrice": 10},
            {"symbol": "000003", "name": "不应检查", "latestPrice": 10},
        ]
        rejected = {"matched": False, "match": None, "reasons": ["不是主升"], "policyVersion": "1.2.0"}
        matched = {
            "matched": True,
            "match": {"setupType": "BREAKOUT_RETEST_UP", "entryLimit": 11},
            "reasons": [],
            "policyVersion": "1.2.0",
        }
        analyzed = []

        def analyze(candidate, start_date, end_date):
            analyzed.append(candidate["symbol"])
            return candidate, main_rise_result(), None

        with patch.object(app_module, "list_price_limited_non_st_stocks", return_value=candidates), patch.object(
            app_module.random, "shuffle", side_effect=lambda items: None
        ), patch.object(app_module, "_analyze_main_rise_candidate", side_effect=analyze), patch.object(
            app_module, "evaluate_main_rise_candidate", side_effect=[rejected, matched]
        ):
            response = self.client.get("/api/stocks/find-main-rise?minPrice=1&maxPrice=20")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(analyzed, ["000001", "000002"])
        self.assertEqual(response.get_json()["symbol"], "000002")
        self.assertEqual(response.get_json()["mainRiseMeta"]["attempts"], 2)

    def test_endpoint_reports_thirty_attempts_without_match(self):
        candidates = [{"symbol": f"{index:06d}", "name": "候选", "latestPrice": 10} for index in range(35)]
        analyzed = []

        def analyze(candidate, start_date, end_date):
            analyzed.append(candidate["symbol"])
            return candidate, main_rise_result(), None

        rejected = {"matched": False, "match": None, "reasons": ["未通过"], "policyVersion": "1.2.0"}
        with patch.object(app_module, "list_price_limited_non_st_stocks", return_value=candidates), patch.object(
            app_module.random, "shuffle", side_effect=lambda items: None
        ), patch.object(app_module, "_analyze_main_rise_candidate", side_effect=analyze), patch.object(
            app_module, "evaluate_main_rise_candidate", return_value=rejected
        ):
            response = self.client.get("/api/stocks/find-main-rise?minPrice=1&maxPrice=20")

        payload = response.get_json()
        self.assertEqual(response.status_code, 404)
        self.assertEqual(len(analyzed), 30)
        self.assertEqual(payload["attempts"], 30)
        self.assertEqual(payload["maxAttempts"], 30)

    def test_endpoint_continues_after_provider_error(self):
        candidates = [
            {"symbol": "000001", "name": "失败", "latestPrice": 10},
            {"symbol": "000002", "name": "命中", "latestPrice": 10},
        ]
        matched = {
            "matched": True,
            "match": {"setupType": "TREND_PULLBACK_H1", "entryLimit": 11},
            "reasons": [],
            "policyVersion": "1.2.0",
        }
        with patch.object(app_module, "list_price_limited_non_st_stocks", return_value=candidates), patch.object(
            app_module.random, "shuffle", side_effect=lambda items: None
        ), patch.object(
            app_module,
            "_analyze_main_rise_candidate",
            side_effect=[
                (candidates[0], None, "000001: provider error"),
                (candidates[1], main_rise_result(setup_type="TREND_PULLBACK_H1"), None),
            ],
        ), patch.object(app_module, "evaluate_main_rise_candidate", return_value=matched):
            response = self.client.get("/api/stocks/find-main-rise?minPrice=1&maxPrice=20")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["mainRiseMeta"]["attempts"], 2)

    def test_endpoint_rejects_invalid_price_range(self):
        response = self.client.get("/api/stocks/find-main-rise?minPrice=20&maxPrice=1")

        self.assertEqual(response.status_code, 400)
        self.assertIn("有效的股价区间", response.get_json()["message"])


if __name__ == "__main__":
    unittest.main()
