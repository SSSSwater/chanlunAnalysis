from __future__ import annotations

import unittest

from backend.services.stock_data import (
    _extract_dividend,
    _extract_finance,
    _extract_holder,
    _extract_main_operation,
    _extract_management,
    _extract_profile,
)


class FundamentalFieldMappingTests(unittest.TestCase):
    def test_eastmoney_finance_aliases_are_mapped(self):
        result = _extract_finance(
            {
                "REPORT_DATE": "2026-03-31 00:00:00",
                "EPSJB": -0.06,
                "BPS": 0.68,
                "ROEJQ": -8.14,
                "ROEJQTZ": -39.14,
                "PARENTNETPROFIT": -16795013.87,
                "PARENTNETPROFITTZ": 39.86,
                "TOTALOPERATEREVE": 14266912.62,
                "TOTALOPERATEREVETZ": -47.66,
                "XSMLL": 9.08,
                "XSJLL": -117.72,
                "ZCFZL": 68.59,
                "NETCASH_OPERATE": -14198694.34,
            }
        )

        self.assertEqual(result["reportDate"], "2026-03-31")
        self.assertEqual(result["roe"], -8.14)
        self.assertEqual(result["netProfit"], -16795013.87)
        self.assertEqual(result["revenue"], 14266912.62)
        self.assertEqual(result["debtRatio"], 68.59)
        self.assertEqual(result["operatingCashFlow"], -14198694.34)

    def test_extended_f10_rows_are_mapped(self):
        operation = _extract_main_operation(
            {
                "REPORT_DATE": "2025-12-31 00:00:00",
                "ITEM_NAME": "主营项目",
                "MAIN_BUSINESS_INCOME": 10000,
                "MBI_RATIO": 0.4,
                "GROSS_RPOFIT_RATIO": 0.2,
            }
        )
        management = _extract_management(
            {
                "PERSON_NAME": "测试高管",
                "POSITION": "董事",
                "INCUMBENT_DATE": "2026-04-17 00:00:00",
                "AGE": "45",
            }
        )
        dividend = _extract_dividend(
            {
                "NOTICE_DATE": "2026-04-03 00:00:00",
                "REPORT_DATE": "2025年报",
                "IMPL_PLAN_PROFILE": "不分配不转增",
                "TOTAL_DIVIDEND": 0,
            }
        )

        self.assertEqual(operation["name"], "主营项目")
        self.assertEqual(operation["income"], 10000)
        self.assertEqual(operation["incomeRatio"], 0.4)
        self.assertEqual(management["name"], "测试高管")
        self.assertEqual(management["incumbentDate"], "2026-04-17")
        self.assertEqual(dividend["plan"], "不分配不转增")
        self.assertEqual(dividend["totalDividend"], 0)

    def test_holder_and_profile_aliases_are_mapped(self):
        holder = _extract_holder(
            {
                "END_DATE": "2026-07-20 00:00:00",
                "HOLDER_RANK": 1,
                "HOLDER_NAME": "测试股东",
                "HOLD_NUM": 24707628,
                "HOLD_NUM_RATIO": 8.56,
                "HOLD_NUM_CHANGE": "新进",
            }
        )
        profile = _extract_profile(
            {
                "ORG_NAME": "测试公司",
                "EM2016": "机械设备",
                "PROVINCE": "辽宁",
                "LISTING_DATE": "2010-07-28 00:00:00",
                "CHAIRMAN": "测试董事长",
                "SECRETARY": "测试秘书",
                "REG_CAPITAL": 29618.66,
                "EMP_NUM": 379,
            }
        )

        self.assertEqual(holder["name"], "测试股东")
        self.assertEqual(holder["shares"], 24707628)
        self.assertEqual(holder["ratio"], 8.56)
        self.assertEqual(profile["companyName"], "测试公司")
        self.assertEqual(profile["secretary"], "测试秘书")
        self.assertEqual(profile["employeeCount"], 379)


if __name__ == "__main__":
    unittest.main()
