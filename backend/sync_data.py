from __future__ import annotations

import argparse

try:
    from .services.stock_data import get_stock_fundamentals, init_storage, list_stocks, sync_f10_reports
except ImportError:
    from services.stock_data import get_stock_fundamentals, init_storage, list_stocks, sync_f10_reports


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize and sync local Chanlun SQLite data.")
    parser.add_argument("--stocks", action="store_true", help="Refresh the fast searchable security list.")
    parser.add_argument("--f10", metavar="SYMBOL", help="Synchronize all F10 reports for one stock symbol.")
    parser.add_argument("--fundamentals", metavar="SYMBOL", help="Synchronize core fundamentals for one stock symbol.")
    args = parser.parse_args()

    init_storage()
    if args.stocks:
        items = list_stocks()
        print(f"stocks={len(items)}")
    if args.f10:
        print(sync_f10_reports(args.f10))
    if args.fundamentals:
        print(get_stock_fundamentals(args.fundamentals, refresh_core=True, start_background=False))
    if not args.stocks and not args.f10 and not args.fundamentals:
        print("database initialized")


if __name__ == "__main__":
    main()
