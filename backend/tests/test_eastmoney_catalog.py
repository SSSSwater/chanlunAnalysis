from __future__ import annotations

import unittest
from unittest.mock import patch

from backend.services import eastmoney_client


def _page(page_number: int, total: int = 205) -> dict:
    start = (page_number - 1) * 100
    end = min(start + 100, total)
    return {
        "data": {
            "total": total,
            "diff": [
                {"f12": f"{index:06d}", "f13": 0, "f14": f"测试股{index}", "f2": 1000}
                for index in range(start + 1, end + 1)
            ],
        }
    }


class EastmoneyCatalogTests(unittest.TestCase):
    def test_list_a_stocks_pages_past_the_provider_100_item_cap(self):
        requested_pages: list[int] = []

        def fetch(_urls, *, params, **_kwargs):
            page_number = int(params["pn"])
            requested_pages.append(page_number)
            return _page(page_number)

        with patch.object(eastmoney_client, "_get_json", side_effect=fetch):
            items = eastmoney_client.list_a_stocks(page_size=205)

        self.assertEqual(len(items), 205)
        self.assertEqual({item["symbol"] for item in items}, {f"{index:06d}" for index in range(1, 206)})
        self.assertEqual(set(requested_pages), {1, 2, 3})


if __name__ == "__main__":
    unittest.main()
