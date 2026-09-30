from __future__ import annotations

import math
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, g, jsonify, request
from flask_cors import CORS

try:
    from .services.ai_analysis import analyze_with_ai
    from .services.email import EmailConfigurationError, EmailDeliveryError, send_resend_test_email
    from .services.price_action import MIN_ANALYSIS_BARS, analyze_price_action
    from .services.a_share_strategy import attach_a_share_strategy_plan
    from .services.price_action_contract import get_price_action_profile
    from .services.discipline_strategy import evaluate_discipline_strategy
    from .services.golden_pillar import detect_golden_pillar_watch
    from .services.main_rise import (
        MAIN_RISE_MAX_ATTEMPTS,
        evaluate_main_rise_candidate,
    )
    from .services.intraday import PERIODS, analyze_intraday
    from .services.binance_client import (
        BinanceApiError,
        get_klines as get_binance_klines,
        get_spot_markets,
        get_ticker as get_binance_ticker,
    )
    from .services.binance_account_worker import (
        cache_binance_account_snapshot,
        clear_cached_binance_account_snapshot,
        get_cached_binance_account_snapshot,
        get_binance_account_snapshot,
        request_binance_account_refresh,
    )
    from .services.binance_futures_client import MAX_FUTURES_LEVERAGE, BinanceEntryOrderError, apply_futures_plan_protection, cancel_futures_order, close_futures_position_market, get_futures_klines, get_futures_markets, place_futures_limit_plan_order, place_futures_market_entry_order, update_futures_partial_protection, update_futures_position_protection, websocket_api_requests
    from .services.binance_websocket_test import test_binance_futures_account_websocket, test_binance_futures_websocket
    from .services.binance_websocket_worker import (
        get_binance_connection_status,
        set_user_connection_mode,
        start_configured_binance_websocket_worker,
    )
    from .services.binance_futures_strategy import _plan_is_actionable, _plan_is_trial_eligible, get_futures_market_analysis, get_market_analysis, start_futures_market_analysis, start_market_analysis
    from .services.binance_strategy_backtest import (
        get_current_strategy_backtest,
        get_fixed_history_fill,
        get_fixed_history_completeness,
        start_fixed_history_fill,
        start_current_strategy_backtest,
    )
    from .services.binance_ml import (
        get_binance_ml_status,
        list_binance_ml_models,
        get_binance_ml_training_job,
        start_binance_ml_training,
    )
    from .services.binance_simulated_portfolio import (
        create_pending_binance_entry_monitor,
        discard_pending_binance_entry_monitor,
        get_binance_simulated_portfolio,
        preview_live_futures_position_monitor,
        remove_binance_simulated_position,
        restore_binance_simulated_position,
        save_binance_simulated_position,
    )
    from .services.binance_copy_trading import get_cached_binance_copy_trading_history, project_binance_copy_trading_follows
    from .services.binance_browser_auth import BinanceBrowserAutomationError, capture_smart_money_auth
    from .services.binance_snapshot_worker import (
        get_snapshot as get_binance_snapshot,
        get_snapshot_without_target as get_binance_snapshot_without_target,
        notify_snapshot_update,
        start_binance_snapshot_worker,
        upsert_poll_monitor_target,
    )
    from .services.update_log import read_update_log
    from .services.stock_data import (
        get_cached_index_history,
        get_cached_stock_history,
        get_index_intraday_history,
        get_index_history,
        get_f10_status,
        get_market_today,
        synchronize_market_data,
        get_portfolio,
        get_watchlist,
        get_stock_quotes,
        get_stock_fundamentals,
        get_stock_fundamentals_quick,
        get_stock_intraday_history,
        get_stock_history,
        resolve_stock_name,
        init_storage,
        list_price_limited_non_st_stocks,
        list_portfolio_trades,
        list_stocks,
        execute_portfolio_trade,
        remove_portfolio_position,
        remove_watchlist_item,
        search_stocks,
        save_portfolio_position,
        save_watchlist_item,
        set_portfolio_cash,
        sync_f10_reports,
        ensure_f10_background_sync,
    )
    from .services import database as db
except ImportError:
    from services.ai_analysis import analyze_with_ai
    from services.email import EmailConfigurationError, EmailDeliveryError, send_resend_test_email
    from services.price_action import MIN_ANALYSIS_BARS, analyze_price_action
    from services.a_share_strategy import attach_a_share_strategy_plan
    from services.price_action_contract import get_price_action_profile
    from services.discipline_strategy import evaluate_discipline_strategy
    from services.golden_pillar import detect_golden_pillar_watch
    from services.main_rise import (
        MAIN_RISE_MAX_ATTEMPTS,
        evaluate_main_rise_candidate,
    )
    from services.intraday import PERIODS, analyze_intraday
    from services.binance_client import (
        BinanceApiError,
        get_klines as get_binance_klines,
        get_spot_markets,
        get_ticker as get_binance_ticker,
    )
    from services.binance_account_worker import (
        cache_binance_account_snapshot,
        clear_cached_binance_account_snapshot,
        get_cached_binance_account_snapshot,
        get_binance_account_snapshot,
        request_binance_account_refresh,
    )
    from services.binance_futures_client import MAX_FUTURES_LEVERAGE, BinanceEntryOrderError, apply_futures_plan_protection, cancel_futures_order, close_futures_position_market, get_futures_klines, get_futures_markets, place_futures_limit_plan_order, place_futures_market_entry_order, update_futures_partial_protection, update_futures_position_protection, websocket_api_requests
    from services.binance_websocket_test import test_binance_futures_account_websocket, test_binance_futures_websocket
    from services.binance_websocket_worker import (
        get_binance_connection_status,
        set_user_connection_mode,
        start_configured_binance_websocket_worker,
    )
    from services.binance_futures_strategy import _plan_is_actionable, _plan_is_trial_eligible, get_futures_market_analysis, get_market_analysis, start_futures_market_analysis, start_market_analysis
    from services.binance_strategy_backtest import (
        get_current_strategy_backtest,
        get_fixed_history_fill,
        get_fixed_history_completeness,
        start_fixed_history_fill,
        start_current_strategy_backtest,
    )
    from services.binance_ml import (
        get_binance_ml_status,
        list_binance_ml_models,
        get_binance_ml_training_job,
        start_binance_ml_training,
    )
    from services.binance_simulated_portfolio import (
        create_pending_binance_entry_monitor,
        discard_pending_binance_entry_monitor,
        get_binance_simulated_portfolio,
        preview_live_futures_position_monitor,
        remove_binance_simulated_position,
        restore_binance_simulated_position,
        save_binance_simulated_position,
    )
    from services.binance_copy_trading import get_cached_binance_copy_trading_history, project_binance_copy_trading_follows
    from services.binance_browser_auth import BinanceBrowserAutomationError, capture_smart_money_auth
    from services.binance_snapshot_worker import (
        get_snapshot as get_binance_snapshot,
        get_snapshot_without_target as get_binance_snapshot_without_target,
        notify_snapshot_update,
        start_binance_snapshot_worker,
        upsert_poll_monitor_target,
    )
    from services.update_log import read_update_log
    from services.stock_data import (
        get_cached_index_history,
        get_cached_stock_history,
        get_index_intraday_history,
        get_index_history,
        get_f10_status,
        get_market_today,
        synchronize_market_data,
        get_portfolio,
        get_watchlist,
        get_stock_quotes,
        get_stock_fundamentals,
        get_stock_fundamentals_quick,
        get_stock_intraday_history,
        get_stock_history,
        resolve_stock_name,
        init_storage,
        list_price_limited_non_st_stocks,
        list_portfolio_trades,
        list_stocks,
        execute_portfolio_trade,
        remove_portfolio_position,
        remove_watchlist_item,
        search_stocks,
        save_portfolio_position,
        save_watchlist_item,
        set_portfolio_cash,
        sync_f10_reports,
        ensure_f10_background_sync,
    )
    from services import database as db


app = Flask(__name__)
# Keep the session cookie usable when the local frontend (5173) calls the
# local API (5000), as well as when the public tunnel proxies both services.
CORS(app, supports_credentials=True)
init_storage()


AUTH_COOKIE_NAME = "jiaren_session"
AUTH_COOKIE_MAX_AGE = db.AUTH_SESSION_DAYS * 24 * 60 * 60
TEST_EMAIL_MIN_INTERVAL_SECONDS = 60
_test_email_last_sent_at: dict[int, float] = {}


def _auth_token_candidates() -> list[str]:
    candidates = []
    authorization = str(request.headers.get("Authorization") or "").strip()
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        candidates.append(token.strip())
    cookie_token = str(request.cookies.get(AUTH_COOKIE_NAME) or "").strip()
    if cookie_token and cookie_token not in candidates:
        candidates.append(cookie_token)
    return candidates


def _bearer_token() -> str | None:
    return next(iter(_auth_token_candidates()), None)


def _current_user_from_request() -> tuple[dict | None, str | None]:
    for token in _auth_token_candidates():
        user = db.get_user_by_session_token(token)
        if user:
            return user, token
    return None, None


def _set_session_cookie(response, token: str):
    forwarded_proto = str(request.headers.get("X-Forwarded-Proto") or "").split(",", 1)[0].strip().lower()
    response.set_cookie(
        AUTH_COOKIE_NAME,
        token,
        max_age=AUTH_COOKIE_MAX_AGE,
        httponly=True,
        secure=request.is_secure or forwarded_proto == "https",
        samesite="Lax",
        path="/",
    )
    return response


def require_current_user(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user, token = _current_user_from_request()
        if not user:
            return jsonify({"message": "请先登录后再访问个人交易数据", "authenticationRequired": True}), 401
        g.current_user = user
        g.auth_token = token
        return view(*args, **kwargs)

    return wrapped


def _current_user_id() -> int:
    return int(g.current_user["id"])


@app.get("/")
def root():
    return jsonify(
        {
            "service": "price-action-analysis-api",
            "status": "ok",
            "health": "/api/health",
            "routes": [
                "/api/auth/register",
                "/api/auth/login",
                "/api/auth/logout",
                "/api/auth/me",
                "/api/account/ai-settings",
                "/api/account/notification-settings",
                "/api/notifications/test-email",
                "/api/update-log",
                "/api/stocks",
                "/api/stocks/search",
                "/api/stocks/history",
                "/api/stocks/quotes",
                "/api/stocks/find-good",
                "/api/stocks/find-main-rise",
                "/api/stocks/find-golden-pillar",
                "/api/stocks/<symbol>/fundamentals",
                "/api/stocks/<symbol>/f10/sync",
                "/api/stocks/<symbol>/f10/status",
                "/api/market/today",
                "/api/market/refresh",
                "/api/portfolio",
                "/api/portfolio/account",
                "/api/portfolio/trades",
                "/api/watchlist",
                "/api/discipline/plan",
                "/api/discipline/portfolio",
                "/api/discipline/watchlist",
                "/api/discipline/journal",
                "/api/analyze",
                "/api/index/analyze",
                "/api/intraday/analyze",
                "/api/index/intraday/analyze",
                "/api/price-action/profile",
                "/api/ai/analyze",
                "/api/binance/ticker",
                "/api/binance/klines",
                "/api/binance/spot/markets",
                "/api/binance/futures/markets",
                "/api/binance/snapshot",
                "/api/binance/spot/analyze",
                "/api/binance/futures/analyze",
                "/api/binance/connect",
                "/api/binance/account",
                "/api/binance/websocket-test",
                "/api/binance/futures/positions/protection",
                "/api/binance/futures/positions/partial-protection",
                "/api/binance/futures/positions/position-protection",
                "/api/binance/futures/positions/apply-plan",
                "/api/binance/futures/orders/limit-plan",
                "/api/binance/futures/copy-trading/order",
                "/api/binance/credentials",
                "/api/binance/connection-settings",
                "/api/binance/smart-money/auth/capture",
                "/api/binance/ui-settings",
                "/api/binance/strategy-settings",
                "/api/binance/simulated-positions",
            ],
        }
    )


@app.get("/api/health")
def health():
    return jsonify({"status": "ok"})


def _binance_error_response(exc: BinanceApiError, *, prefix: str = "Binance 请求失败"):
    payload = {"message": f"{prefix}：{exc}"}
    if exc.exchange_code is not None:
        payload["code"] = exc.exchange_code
    return jsonify(payload), exc.status_code


def _request_current_user_account_reconciliation() -> None:
    try:
        request_binance_account_refresh(_current_user_id())
    except Exception:
        # A follow-up REST refresh is best-effort; the successful order response
        # remains useful even when the cache refresh cannot be queued.
        pass


@contextmanager
def _user_futures_transport(user_id: int):
    settings = db.get_user_binance_connection_settings(int(user_id))
    with websocket_api_requests(settings.get("connectionMode") == "WEBSOCKET"):
        yield


@contextmanager
def _user_futures_rest_transport():
    """Force authenticated futures mutations through the REST adapter.

    The account connection mode is still used by the snapshot/account workers
    and public live market stream.  Binance's WebSocket API is not reliable
    for the order/conditional-order endpoints used by the execution routes,
    so all real entry and protection mutations deliberately opt out here.
    """

    with websocket_api_requests(False):
        yield


@app.get("/api/binance/ticker")
def binance_ticker():
    try:
        return jsonify(get_binance_ticker(request.args.get("network", "mainnet"), request.args.get("symbol", "BTCUSDT")))
    except BinanceApiError as exc:
        return _binance_error_response(exc)
    except (TypeError, ValueError) as exc:
        return jsonify({"message": str(exc)}), 400


@app.get("/api/binance/klines")
def binance_klines():
    try:
        network = request.args.get("network", "mainnet")
        symbol = request.args.get("symbol", "BTCUSDT")
        interval = request.args.get("interval", "1h")
        limit = int(request.args.get("limit", 72))
        market = str(request.args.get("market", "spot") or "spot").strip().lower()
        if market in {"futures", "contract", "contracts"}:
            return jsonify(get_futures_klines(network, symbol, interval, limit, with_meta=True))
        if market not in {"spot", "现货"}:
            return jsonify({"message": "行情类型必须选择现货或合约"}), 400
        return jsonify(get_binance_klines(network, symbol, interval, limit, with_meta=True))
    except BinanceApiError as exc:
        return _binance_error_response(exc)
    except (TypeError, ValueError) as exc:
        return jsonify({"message": str(exc)}), 400


@app.get("/api/binance/spot/markets")
def binance_spot_markets():
    try:
        return jsonify(get_spot_markets(request.args.get("network", "mainnet"), limit=100))
    except BinanceApiError as exc:
        return _binance_error_response(exc)
    except (TypeError, ValueError) as exc:
        return jsonify({"message": str(exc)}), 400


@app.get("/api/binance/futures/markets")
def binance_futures_markets():
    try:
        return jsonify(get_futures_markets(request.args.get("network", "mainnet"), limit=100))
    except BinanceApiError as exc:
        return _binance_error_response(exc)
    except (TypeError, ValueError) as exc:
        return jsonify({"message": str(exc)}), 400


@app.get("/api/binance/snapshot")
def binance_snapshot():
    """Return the latest process-local snapshot without an exchange request."""

    # Keep the refresh workers available when the app is started through
    # ``flask run`` instead of the production launcher.
    start_binance_snapshot_worker()
    user, _ = _current_user_from_request()
    user_id = int(user["id"]) if user else None
    client_id = str(request.args.get("clientId") or "default").strip()
    owner_key = f"user-{user_id}" if user_id is not None else "anonymous"
    try:
        network = request.args.get("network", "mainnet")
        market_type = request.args.get("market", request.args.get("marketType", "SPOT"))
        symbol = str(request.args.get("symbol") or "").strip()
        interval = request.args.get("interval", "1h")
        if symbol:
            subscription_id = upsert_poll_monitor_target(
                f"{owner_key}-{client_id}",
                network,
                market_type,
                symbol,
                interval,
                owner_user_id=user_id,
            )
            payload = get_binance_snapshot(subscription_id, user_id)
        else:
            # Keep the public market/account cache available, but do not
            # register or fetch any chart target until the user selects one.
            upsert_poll_monitor_target(
                f"{owner_key}-{client_id}",
                network,
                market_type,
                "",
                interval,
                owner_user_id=user_id,
            )
            payload = get_binance_snapshot_without_target(network, market_type, interval, user_id)
        response = jsonify(payload)
        # The polling URL is intentionally stable. Do not let the browser
        # cache a previous JSON response in place of process-local data.
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response
    except (TypeError, ValueError) as exc:
        return jsonify({"message": str(exc)}), 400


@app.post("/api/binance/futures/analyze")
def binance_futures_analyze():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"message": "请求格式无效"}), 400
    try:
        # Public market scanning remains available without login. When the
        # caller is logged in, freeze that user's route in this analysis job
        # so the displayed plan and any later execution snapshot agree.
        # Target analysis always loads historical klines over REST, regardless
        # of the user's account/market connectionMode.
        current_user, _ = _current_user_from_request()
        # The UI sends the immutable settings snapshot with each analysis
        # request. This keeps a just-switched MODEL/BEST/STABLE route from
        # being replaced by a stale account snapshot (or classic defaults for
        # public target analysis). Persisted account settings remain the
        # fallback when no request snapshot is supplied.
        request_settings = payload.get("strategySettings")
        if isinstance(request_settings, dict) and request_settings:
            strategy_kwargs = {"strategy_settings": request_settings}
        elif current_user:
            strategy_kwargs = {
                "strategy_settings": db.get_user_binance_strategy_settings(int(current_user["id"]))
            }
        else:
            strategy_kwargs = {}
        if payload.get("symbol"):
            job = start_market_analysis(
                "FUTURES",
                payload.get("network", "mainnet"),
                payload.get("limit", 24),
                symbol=payload.get("symbol"),
                **strategy_kwargs,
            )
        else:
            job = start_futures_market_analysis(
                payload.get("network", "mainnet"),
                payload.get("limit", 24),
                **strategy_kwargs,
            )
        return jsonify({"job": job}), 202
    except (TypeError, ValueError) as exc:
        return jsonify({"message": str(exc)}), 400


@app.post("/api/binance/spot/analyze")
def binance_spot_analyze():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"message": "请求格式无效"}), 400
    try:
        if payload.get("symbol"):
            job = start_market_analysis("SPOT", payload.get("network", "mainnet"), payload.get("limit", 24), symbol=payload.get("symbol"))
        else:
            job = start_market_analysis("SPOT", payload.get("network", "mainnet"), payload.get("limit", 24))
        return jsonify({"job": job}), 202
    except (TypeError, ValueError) as exc:
        return jsonify({"message": str(exc)}), 400


@app.get("/api/binance/futures/analyze/<job_id>")
def binance_futures_analyze_status(job_id: str):
    job = get_futures_market_analysis(job_id)
    if not job:
        return jsonify({"message": "合约扫描任务不存在或已过期"}), 404
    return jsonify({"job": job})


@app.get("/api/binance/spot/analyze/<job_id>")
def binance_spot_analyze_status(job_id: str):
    job = get_market_analysis(job_id)
    if not job:
        return jsonify({"message": "现货扫描任务不存在或已过期"}), 404
    return jsonify({"job": job})


@app.post("/api/binance/futures/backtest")
def binance_futures_strategy_backtest():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"message": "请求格式无效"}), 400
    try:
        current_user, _ = _current_user_from_request()
        settings_kwargs = {"user_id": int(current_user["id"])} if current_user else {}
        if "backtestOptions" in payload:
            settings_kwargs["backtest_options"] = payload.get("backtestOptions")
        if "strategySettings" in payload:
            settings_kwargs["strategy_settings"] = payload.get("strategySettings")
        return jsonify({"job": start_current_strategy_backtest(payload.get("network", "mainnet"), **settings_kwargs)}), 202
    except (TypeError, ValueError) as exc:
        return jsonify({"message": str(exc)}), 400


@app.get("/api/binance/futures/backtest/history-status")
def binance_futures_strategy_backtest_history_status():
    try:
        return jsonify({"status": get_fixed_history_completeness(request.args.get("network", "mainnet"))})
    except BinanceApiError as exc:
        return _binance_error_response(exc)
    except (TypeError, ValueError) as exc:
        return jsonify({"message": str(exc)}), 400


@app.post("/api/binance/futures/backtest/history-fill")
def binance_futures_strategy_backtest_history_fill():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"message": "请求格式无效"}), 400
    try:
        return jsonify({"job": start_fixed_history_fill(payload.get("network", "mainnet"))}), 202
    except (TypeError, ValueError) as exc:
        return jsonify({"message": str(exc)}), 400


@app.get("/api/binance/futures/backtest/history-fill/<job_id>")
def binance_futures_strategy_backtest_history_fill_status(job_id: str):
    job = get_fixed_history_fill(job_id)
    if not job:
        return jsonify({"message": "合约历史补全任务不存在或已过期"}), 404
    return jsonify({"job": job})


@app.get("/api/binance/futures/backtest/<job_id>")
def binance_futures_strategy_backtest_status(job_id: str):
    job = get_current_strategy_backtest(job_id)
    if not job:
        return jsonify({"message": "合约策略回测任务不存在或已过期"}), 404
    return jsonify({"job": job})


@app.get("/api/binance/futures/model/status")
def binance_futures_model_status():
    try:
        return jsonify({"status": get_binance_ml_status(request.args.get("network", "mainnet"))})
    except (TypeError, ValueError, RuntimeError) as exc:
        return jsonify({"message": str(exc)}), 400


@app.get("/api/binance/futures/model/runs")
def binance_futures_model_runs():
    try:
        limit = request.args.get("limit", 24)
        return jsonify(list_binance_ml_models(request.args.get("network", "mainnet"), limit))
    except (TypeError, ValueError, RuntimeError) as exc:
        return jsonify({"message": str(exc)}), 400


@app.post("/api/binance/futures/model/train")
def binance_futures_model_train():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"message": "请求格式无效"}), 400
    try:
        current_user, _ = _current_user_from_request()
        if current_user:
            strategy_settings = db.get_user_binance_strategy_settings(int(current_user["id"]))
            job = start_binance_ml_training(
                payload.get("network", "mainnet"),
                payload.get("config"),
                strategy_settings=strategy_settings,
            )
        else:
            # Preserve the public, unauthenticated training API contract; the
            # model service applies its own classic defaults in this case.
            job = start_binance_ml_training(payload.get("network", "mainnet"), payload.get("config"))
        return jsonify({"job": job}), 202
    except (TypeError, ValueError, RuntimeError) as exc:
        return jsonify({"message": str(exc)}), 400


@app.get("/api/binance/futures/model/train/<job_id>")
def binance_futures_model_train_status(job_id: str):
    job = get_binance_ml_training_job(job_id)
    if not job:
        return jsonify({"message": "时序模型训练任务不存在或已过期"}), 404
    return jsonify({"job": job})


@app.post("/api/binance/connect")
@require_current_user
def binance_connect():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"message": "请求格式无效"}), 400
    try:
        db.ensure_binance_credential_storage()
        account = get_binance_account_snapshot(
            "mainnet",
            payload.get("apiKey", ""),
            payload.get("apiSecret", ""),
        )
        user_id = _current_user_id()
        credentials = db.save_user_binance_credentials(
            user_id,
            network="mainnet",
            api_key=payload.get("apiKey", ""),
            api_secret=payload.get("apiSecret", ""),
        )
        futures = account.get("futures") if isinstance(account, dict) else None
        futures_unavailable = not isinstance(futures, dict) or futures.get("available") is False
        cache_binance_account_snapshot(
            user_id,
            account,
            credentials,
            stale=futures_unavailable,
            error=(str(futures.get("error") or "合约账户暂不可用") if futures_unavailable and isinstance(futures, dict) else None),
        )
        # The credential validation above is deliberately REST based.  Once
        # the encrypted credentials are stored, hand the selected transport to
        # the long-lived connection manager so it can start/reconcile the
        # account and market lines without another browser request.
        set_user_connection_mode(
            user_id,
            db.get_user_binance_connection_settings(user_id).get("connectionMode", "REST"),
        )
        notify_snapshot_update()
        return jsonify({"account": account, "credentials": credentials})
    except BinanceApiError as exc:
        return _binance_error_response(exc)
    except (TypeError, ValueError) as exc:
        return jsonify({"message": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"message": str(exc)}), 503


@app.get("/api/binance/account")
@require_current_user
def binance_account():
    try:
        user_id = _current_user_id()
        credentials = db.get_user_binance_credentials(user_id)
        if not credentials["configured"]:
            return jsonify({
                "configured": False,
                "credentials": credentials,
                "connection": get_binance_connection_status(user_id),
            })
        snapshot = get_cached_binance_account_snapshot(user_id)
        if snapshot is None:
            request_binance_account_refresh(user_id)
            return jsonify(
                {
                    "configured": True,
                    "account": None,
                    "credentials": credentials,
                    "stale": True,
                    "error": "账户实时数据正在初始化",
                    "connection": get_binance_connection_status(user_id),
                }
            )
        return jsonify(
            {
                "configured": bool(snapshot.get("configured")),
                "account": snapshot.get("account"),
                "credentials": snapshot.get("credentials") or credentials,
                "stale": bool(snapshot.get("stale")),
                "updatedAt": snapshot.get("updatedAt"),
                "checkedAt": snapshot.get("checkedAt"),
                "error": snapshot.get("error"),
                "positionPnlHistory": snapshot.get("positionPnlHistory") or [],
                "connection": get_binance_connection_status(user_id),
            }
        )
    except RuntimeError as exc:
        return jsonify({"message": str(exc)}), 503


@app.post("/api/binance/websocket-test")
@require_current_user
def binance_websocket_test():
    """Run one short-lived public or authenticated WebSocket probe for Settings."""

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"message": "请求格式无效"}), 400
    try:
        scope = str(payload.get("scope") or "market").strip().lower()
        if scope == "market":
            result = test_binance_futures_websocket(payload.get("network", "mainnet"))
        elif scope == "account":
            credentials = db.get_user_binance_credentials(_current_user_id(), include_secrets=True)
            if not credentials.get("configured"):
                return jsonify({"message": "请先在账户 API 页保存 Binance API Key 和 Secret Key"}), 400
            result = test_binance_futures_account_websocket(
                credentials.get("network", "mainnet"),
                credentials.get("apiKey", ""),
                credentials.get("apiSecret", ""),
            )
        else:
            return jsonify({"message": "WebSocket 测试类型无效"}), 400
        return jsonify({"result": result})
    except BinanceApiError as exc:
        return _binance_error_response(exc, prefix="WebSocket 测试失败")
    except ValueError as exc:
        return jsonify({"message": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"message": str(exc)}), 503


@app.post("/api/binance/futures/positions/market-close")
@require_current_user
def binance_futures_market_close():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"message": "请求格式无效"}), 400
    try:
        ratio = float(payload.get("quantityRatio", 100))
        if not math.isfinite(ratio) or ratio <= 0 or ratio > 100:
            raise ValueError("平仓比例必须在 1% 到 100% 之间")
        credentials = db.get_user_binance_credentials(_current_user_id(), include_secrets=True)
        if not credentials["configured"]:
            return jsonify({"message": "请先连接 Binance API"}), 400
        symbol = str(payload.get("symbol") or "").strip().upper()
        if not symbol:
            raise ValueError("交易对不能为空")
        with _user_futures_rest_transport():
            result = close_futures_position_market(
                credentials["network"], credentials["apiKey"], credentials["apiSecret"],
                symbol=symbol,
                position_side=payload.get("positionSide", "BOTH"),
                quantity_ratio=ratio,
            )
        _request_current_user_account_reconciliation()
        notify_snapshot_update()
        return jsonify({"position": result})
    except BinanceApiError as exc:
        return _binance_error_response(exc)
    except (TypeError, ValueError) as exc:
        return jsonify({"message": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"message": str(exc)}), 503


@app.put("/api/binance/futures/positions/position-protection")
@app.put("/api/binance/futures/positions/protection")
@require_current_user
def binance_futures_position_protection():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"message": "请求格式无效"}), 400
    try:
        credentials = db.get_user_binance_credentials(_current_user_id(), include_secrets=True)
        if not credentials["configured"]:
            return jsonify({"message": "请先连接 Binance API"}), 400

        def optional_price(name: str) -> float | None:
            value = payload.get(name)
            if value is None or (isinstance(value, str) and not value.strip()):
                return None
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise ValueError(f"{name}必须是正数") from None
            if not math.isfinite(number) or number <= 0:
                raise ValueError(f"{name}必须是正数")
            return number

        with _user_futures_rest_transport():
            protection = update_futures_position_protection(
                credentials["network"], credentials["apiKey"], credentials["apiSecret"],
                symbol=payload.get("symbol", ""), position_side=payload.get("positionSide", "BOTH"),
                stop_loss=optional_price("stopLoss"), take_profit=optional_price("takeProfit"),
            )
        _request_current_user_account_reconciliation()
        notify_snapshot_update()
        return jsonify({"protection": protection})
    except BinanceApiError as exc:
        return _binance_error_response(exc)
    except (TypeError, ValueError) as exc:
        return jsonify({"message": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"message": str(exc)}), 503


@app.put("/api/binance/futures/positions/partial-protection")
@require_current_user
def binance_futures_partial_position_protection():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"message": "请求格式无效"}), 400
    try:
        credentials = db.get_user_binance_credentials(_current_user_id(), include_secrets=True)
        if not credentials["configured"]:
            return jsonify({"message": "请先连接 Binance API"}), 400

        def optional_price(name: str) -> float | None:
            value = payload.get(name)
            if value is None or (isinstance(value, str) and not value.strip()):
                return None
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise ValueError(f"{name}必须是正数") from None
            if not math.isfinite(number) or number <= 0:
                raise ValueError(f"{name}必须是正数")
            return number

        try:
            quantity_ratio = float(payload.get("quantityRatio", 50))
        except (TypeError, ValueError):
            raise ValueError("止盈止损比例必须在 1% 到 100% 之间") from None
        if not math.isfinite(quantity_ratio) or quantity_ratio <= 0 or quantity_ratio > 100:
            raise ValueError("止盈止损比例必须在 1% 到 100% 之间")
        with _user_futures_rest_transport():
            protection = update_futures_partial_protection(
                credentials["network"], credentials["apiKey"], credentials["apiSecret"],
                symbol=payload.get("symbol", ""), position_side=payload.get("positionSide", "BOTH"),
                stop_loss=optional_price("stopLoss"), take_profit=optional_price("takeProfit"), quantity_ratio=quantity_ratio,
            )
        _request_current_user_account_reconciliation()
        notify_snapshot_update()
        return jsonify({"protection": protection})
    except BinanceApiError as exc:
        return _binance_error_response(exc)
    except (TypeError, ValueError) as exc:
        return jsonify({"message": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"message": str(exc)}), 503


@app.post("/api/binance/futures/positions/apply-plan")
@require_current_user
def binance_futures_apply_plan():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"message": "请求格式无效"}), 400
    try:
        credentials = db.get_user_binance_credentials(_current_user_id(), include_secrets=True)
        if not credentials["configured"]:
            return jsonify({"message": "请先连接 Binance API"}), 400

        def required_price(name: str) -> float:
            value = payload.get(name)
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise ValueError(f"{name}必须是正数") from None
            if not math.isfinite(number) or number <= 0:
                raise ValueError(f"{name}必须是正数")
            return number

        def optional_price(name: str) -> float | None:
            value = payload.get(name)
            if value is None or (isinstance(value, str) and not value.strip()):
                return None
            return required_price(name)

        protection_kwargs = {
            "symbol": payload.get("symbol", ""),
            "position_side": payload.get("positionSide", "BOTH"),
            "stop_loss": required_price("stopLoss"),
            "first_take_profit": required_price("firstTakeProfit"),
            "extension_take_profit": optional_price("extensionTakeProfit"),
        }
        if "protectiveTakeProfit" in payload:
            protection_kwargs["protective_take_profit"] = optional_price("protectiveTakeProfit")
        # A missing protective target is a valid two-target plan.  Ignore any
        # stale ratio field from older clients unless a real protective price
        # was supplied; otherwise a zero ratio would be validated as a
        # phantom third ladder leg.
        protective_target = protection_kwargs.get("protective_take_profit")
        if protective_target is not None and "protectiveTakeProfitRatio" in payload:
            protection_kwargs["protective_take_profit_ratio"] = float(payload.get("protectiveTakeProfitRatio"))
        if "firstTakeProfitRatio" in payload:
            protection_kwargs["first_take_profit_ratio"] = float(payload.get("firstTakeProfitRatio"))
        if "secondTakeProfitRatio" in payload:
            protection_kwargs["second_take_profit_ratio"] = float(payload.get("secondTakeProfitRatio"))
        with _user_futures_rest_transport():
            protection = apply_futures_plan_protection(credentials["network"], credentials["apiKey"], credentials["apiSecret"], **protection_kwargs)
        _request_current_user_account_reconciliation()
        notify_snapshot_update()
        return jsonify({"protection": protection})
    except BinanceApiError as exc:
        return _binance_error_response(exc)
    except (TypeError, ValueError) as exc:
        return jsonify({"message": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"message": str(exc)}), 503


@app.post("/api/binance/futures/orders/limit-plan")
@require_current_user
def binance_futures_limit_plan_order():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"message": "请求格式无效"}), 400
    pending_monitor_id = None

    def cleanup_pending_monitor() -> bool:
        if pending_monitor_id is None:
            return True
        try:
            return bool(discard_pending_binance_entry_monitor(_current_user_id(), int(pending_monitor_id)))
        except Exception:
            app.logger.warning(
                "Could not remove rejected Binance pending monitor %s",
                pending_monitor_id,
                exc_info=True,
            )
            return False

    try:
        user_id = _current_user_id()
        credentials = db.get_user_binance_credentials(user_id, include_secrets=True)
        if not credentials["configured"]:
            return jsonify({"message": "请先连接 Binance API"}), 400
        if str(payload.get("marketMode") or "FUTURES").strip().upper() != "FUTURES":
            return jsonify({"message": "真实限价计划只支持合约市场"}), 400

        source_plan = payload.get("plan")
        if not isinstance(source_plan, dict):
            return jsonify({"message": "真实限价单必须来自分析计划"}), 400
        if source_plan.get("automatedOrder") is True:
            return jsonify({"message": "分析计划不能启用自动下单"}), 400
        if not _plan_is_actionable(source_plan):
            return jsonify({"message": "只能执行符合条件的分析计划"}), 400
        if _plan_is_trial_eligible(source_plan) and payload.get("allowTrial") is not True:
            return jsonify({"message": "试错计划必须明确确认，且只能使用 9/10 计划"}), 400

        def required_number(name: str, fallback: object = None, *, use_payload: bool = True) -> float:
            value = payload.get(name, fallback) if use_payload else fallback
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise ValueError(f"{name}必须是正数") from None
            if not math.isfinite(number) or number <= 0:
                raise ValueError(f"{name}必须是正数")
            return number

        def optional_number(name: str, fallback: object = None) -> float | None:
            value = payload.get(name, fallback)
            if value is None or (isinstance(value, str) and not value.strip()):
                return None
            return required_number(name, fallback)

        targets = source_plan.get("takeProfits") if isinstance(source_plan.get("takeProfits"), list) else []
        target_roles = {
            str(item.get("role") or "").strip().upper(): item
            for item in targets
            if isinstance(item, dict) and str(item.get("role") or "").strip()
        }
        if target_roles:
            protective_record = target_roles.get("PROTECTIVE_TARGET") or {}
            first_record = target_roles.get("FIRST_TARGET") or {}
            extension_record = target_roles.get("EXTENSION_TARGET") or {}
            protective_target = protective_record.get("price")
            first_target = first_record.get("price")
            extension_target = extension_record.get("price") if extension_record.get("available", True) is not False else None
        elif len(targets) >= 3:
            protective_target = targets[0].get("price") if isinstance(targets[0], dict) else None
            first_target = targets[1].get("price") if isinstance(targets[1], dict) else None
            extension_target = targets[2].get("price") if isinstance(targets[2], dict) else None
        else:
            protective_target = None
            first_target = targets[0].get("price") if targets and isinstance(targets[0], dict) else None
            extension_target = targets[1].get("price") if len(targets) > 1 and isinstance(targets[1], dict) else None
        strategy_snapshot = source_plan.get("strategySettings") if isinstance(source_plan.get("strategySettings"), dict) else {}
        first_record = target_roles.get("FIRST_TARGET") if target_roles else None
        extension_record = target_roles.get("EXTENSION_TARGET") if target_roles else None
        effective_first_ratio = (first_record or {}).get("cumulativeRatio") if isinstance(first_record, dict) else None
        effective_second_ratio = (extension_record or {}).get("cumulativeRatio") if isinstance(extension_record, dict) else None
        # Without a confirmed extension target there is only one fixed LIMIT
        # target.  Its cumulative boundary is always the configured second
        # target ratio (default 75%), even when a legacy FIRST_TARGET record
        # still carries an old 50% value.
        if extension_target is None:
            effective_first_ratio = strategy_snapshot.get("secondTakeProfitRatio", 75)
        order_kwargs = {
            "symbol": payload.get("symbol") or source_plan.get("symbol", ""),
            "direction": payload.get("direction") or source_plan.get("direction", ""),
            "quantity": required_number("quantity"),
            "cost_price": required_number("costPrice"),
            "leverage": required_number("leverage", 1),
            "stop_loss": required_number("stopLoss", source_plan.get("stopLoss")),
            "first_take_profit": required_number("firstTakeProfit", first_target),
            "extension_take_profit": optional_number("extensionTakeProfit", extension_target),
            "first_take_profit_ratio": required_number(
                "firstTakeProfitRatio",
                effective_first_ratio if extension_target is None else (effective_first_ratio or strategy_snapshot.get("firstTakeProfitRatio", 50)),
                use_payload=extension_target is not None,
            ),
            "second_take_profit_ratio": required_number("secondTakeProfitRatio", effective_second_ratio or strategy_snapshot.get("secondTakeProfitRatio", 75)),
            "position_side": payload.get("positionSide"),
        }
        if not float(order_kwargs["leverage"]).is_integer() or not 1 <= order_kwargs["leverage"] <= MAX_FUTURES_LEVERAGE:
            raise ValueError(f"真实合约杠杆必须是 1 到 {MAX_FUTURES_LEVERAGE} 倍的整数")
        if protective_target is not None:
            order_kwargs["protective_take_profit"] = optional_number("protectiveTakeProfit", protective_target)
        # Ratios are tied to actual ladder legs, not to the presence of a
        # compatibility field.  A stale protectiveTakeProfitRatio=0 must be
        # ignored whenever the source plan has no protective target, for both
        # CLASSIC and MODEL plans.
        if protective_target is not None:
            order_kwargs["protective_take_profit_ratio"] = required_number(
                "protectiveTakeProfitRatio",
                strategy_snapshot.get("protectiveTakeProfitRatio", 25)
                if strategy_snapshot
                else 25,
            )

        requested_network = str(payload.get("network") or credentials.get("network") or "mainnet").strip().lower()
        credential_network = str(credentials.get("network") or "mainnet").strip().lower()
        if requested_network != credential_network:
            raise ValueError("真实下单网络必须与当前账户 API 网络一致")

        # Establish the durable monitor before the exchange call. This closes
        # the failure window where an entry order could exist without a local
        # execution-plan monitor.
        pending_monitor = create_pending_binance_entry_monitor(
            user_id,
            {
                "network": requested_network,
                "marketMode": "FUTURES",
                "symbol": order_kwargs["symbol"],
                "side": order_kwargs["direction"],
                "quantity": order_kwargs["quantity"],
                "costPrice": order_kwargs["cost_price"],
                "leverage": order_kwargs["leverage"],
                "plan": source_plan,
                "hasExistingPosition": False,
                "submitRealLimitOrder": True,
                "allowTrial": payload.get("allowTrial") is True,
                "note": payload.get("note", ""),
            },
        )
        pending_monitor_id = pending_monitor.get("id") if isinstance(pending_monitor, dict) else None
        if pending_monitor_id is None:
            raise RuntimeError("计划监控创建失败，已阻止提交真实入场单")
        with _user_futures_rest_transport():
            order = place_futures_limit_plan_order(credentials["network"], credentials["apiKey"], credentials["apiSecret"], **order_kwargs)
        _request_current_user_account_reconciliation()
        notify_snapshot_update()
        return jsonify({"order": order, "monitor": pending_monitor}), 201
    except BinanceEntryOrderError as exc:
        uncertain = bool(
            getattr(exc, "entry_order_submitted", True)
            and (
                exc.is_transport_error
                or exc.is_transient
                or (int(exc.status_code or 0) >= 500)
            )
        )
        cleanup_ok = cleanup_pending_monitor() if not uncertain else True
        if uncertain:
            prefix = "入场单状态未确认，计划监控已保留，请先核对 Binance 委托"
        elif cleanup_ok:
            prefix = "入场单提交失败"
        else:
            prefix = "入场单提交失败，计划监控清理未确认"
        return _binance_error_response(exc, prefix=prefix)
    except BinanceApiError as exc:
        cleanup_pending_monitor()
        return _binance_error_response(exc)
    except (TypeError, ValueError) as exc:
        cleanup_pending_monitor()
        return jsonify({"message": str(exc)}), 400
    except RuntimeError as exc:
        cleanup_pending_monitor()
        return jsonify({"message": str(exc)}), 503


@app.post("/api/binance/futures/copy-trading/order")
@require_current_user
def binance_futures_copy_trading_order():
    """Submit a user-configured copy-trade entry without creating a local plan."""

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"message": "请求格式无效"}), 400
    order_type = str(payload.get("orderType") or "LIMIT").strip().upper()
    if order_type not in {"LIMIT", "MARKET"}:
        return jsonify({"message": "下单类型必须是市价或限价"}), 400

    def required_number(name: str, fallback: object = None) -> float:
        value = payload.get(name, fallback)
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name}必须是正数") from None
        if not math.isfinite(number) or number <= 0:
            raise ValueError(f"{name}必须是正数")
        return number

    def optional_number(name: str) -> float | None:
        value = payload.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return required_number(name)

    try:
        user_id = _current_user_id()
        credentials = db.get_user_binance_credentials(user_id, include_secrets=True)
        if not credentials["configured"]:
            return jsonify({"message": "请先连接 Binance API"}), 400
        if str(credentials.get("network") or "mainnet").strip().lower() != "mainnet":
            return jsonify({"message": "公开带单只支持使用主网账户跟单"}), 400
        symbol = str(payload.get("symbol") or "").strip().upper()
        direction = str(payload.get("direction") or "").strip().upper()
        if not symbol or direction not in {"LONG", "SHORT"}:
            raise ValueError("交易对和方向无效")
        quantity = required_number("quantity")
        leverage = required_number("leverage", 1)
        if not leverage.is_integer() or not 1 <= leverage <= MAX_FUTURES_LEVERAGE:
            raise ValueError(f"真实合约杠杆必须是 1 到 {MAX_FUTURES_LEVERAGE} 倍的整数")
        cost_price = required_number("costPrice")
        with _user_futures_rest_transport():
            if order_type == "MARKET":
                order_result = place_futures_market_entry_order(
                    credentials["network"], credentials["apiKey"], credentials["apiSecret"],
                    symbol=symbol, direction=direction, quantity=quantity, leverage=leverage,
                    position_side=payload.get("positionSide"),
                )
            else:
                order_result = place_futures_limit_plan_order(
                    credentials["network"], credentials["apiKey"], credentials["apiSecret"],
                    symbol=symbol, direction=direction, quantity=quantity, cost_price=cost_price,
                    leverage=leverage, position_side=payload.get("positionSide"), with_protection=False,
                )
        if not isinstance(order_result, dict) or order_result.get("entryAccepted") is not True:
            raise RuntimeError("交易所未确认入场单成功")
        _request_current_user_account_reconciliation()
        notify_snapshot_update()
        return jsonify({"order": order_result, "monitor": None}), 201
    except BinanceEntryOrderError as exc:
        return _binance_error_response(exc, prefix="跟单入场单提交失败")
    except BinanceApiError as exc:
        return _binance_error_response(exc, prefix="跟单请求失败")
    except (TypeError, ValueError) as exc:
        return jsonify({"message": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"message": str(exc)}), 503


@app.delete("/api/binance/credentials")
@require_current_user
def binance_credentials_delete():
    try:
        user_id = _current_user_id()
        deleted = db.delete_user_binance_credentials(user_id)
        # A deleted account cannot sustain an authenticated WebSocket line.
        # Persist the fallback as well as stopping the in-process line so a
        # later login/page load cannot resurrect the old transport preference.
        db.save_user_binance_connection_settings(user_id, {"connectionMode": "REST"})
        clear_cached_binance_account_snapshot(user_id)
        set_user_connection_mode(user_id, "REST")
        notify_snapshot_update()
        return jsonify({"deleted": deleted})
    except Exception as exc:
        return jsonify({"message": f"Binance 凭据删除失败：{exc}"}), 500


@app.get("/api/binance/strategy-settings")
@require_current_user
def binance_strategy_settings():
    try:
        return jsonify({"settings": db.get_user_binance_strategy_settings(_current_user_id())})
    except Exception as exc:
        return jsonify({"message": f"策略设置加载失败：{exc}"}), 500


@app.put("/api/binance/strategy-settings")
@require_current_user
def binance_strategy_settings_save():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"message": "请求格式无效"}), 400
    try:
        return jsonify({"settings": db.save_user_binance_strategy_settings(_current_user_id(), payload)})
    except ValueError as exc:
        return jsonify({"message": str(exc)}), 400
    except Exception as exc:
        return jsonify({"message": f"策略设置保存失败：{exc}"}), 500


@app.get("/api/binance/copy-trading-settings")
@require_current_user
def binance_copy_trading_settings():
    try:
        user_id = _current_user_id()
        cached = get_cached_binance_copy_trading_history(user_id)
        db.ensure_user_binance_smart_money_trader_settings(
            user_id,
            [item.get("topTraderId") for item in cached.get("subscriptions", []) if isinstance(item, dict)],
        )
        settings = db.get_user_binance_copy_trading_settings(user_id)
        follows = db.list_user_binance_smart_money_position_follows(user_id)
        return jsonify({"settings": settings, "subscriptions": cached.get("subscriptions", []), "follows": follows})
    except Exception as exc:
        return jsonify({"message": f"跟单参数加载失败：{exc}"}), 500


@app.put("/api/binance/copy-trading-settings")
@require_current_user
def binance_copy_trading_settings_save():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"message": "请求格式无效"}), 400
    try:
        user_id = _current_user_id()
        db.save_user_binance_copy_trading_settings(user_id, payload)
        # Settings writes never fetch Binance. Remote Smart Money data is
        # owned exclusively by the 15-second background refresh.
        copy_trading = get_cached_binance_copy_trading_history(user_id)
        db.ensure_user_binance_smart_money_trader_settings(
            user_id,
            [item.get("topTraderId") for item in copy_trading.get("subscriptions", []) if isinstance(item, dict)],
        )
        settings = db.get_user_binance_copy_trading_settings(user_id)
        follows = db.list_user_binance_smart_money_position_follows(user_id)
        copy_trading = project_binance_copy_trading_follows(copy_trading, follows)
        notify_snapshot_update()
        return jsonify({"settings": settings, "subscriptions": copy_trading.get("subscriptions", []), "follows": follows, "copyTrading": copy_trading})
    except ValueError as exc:
        return jsonify({"message": str(exc)}), 400
    except Exception as exc:
        return jsonify({"message": f"跟单参数保存失败：{exc}"}), 500


@app.post("/api/binance/smart-money/auth/capture")
@require_current_user
def binance_smart_money_auth_capture():
    try:
        return jsonify({"auth": capture_smart_money_auth()})
    except BinanceBrowserAutomationError as exc:
        return jsonify({"message": str(exc)}), 503
    except Exception as exc:
        return jsonify({"message": f"聪明钱登录态获取失败：{exc}"}), 500


@app.put("/api/binance/smart-money/follows")
@require_current_user
def binance_smart_money_position_follow_save():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"message": "请求格式无效"}), 400
    try:
        user_id = _current_user_id()
        settings = db.get_user_binance_copy_trading_settings(user_id)
        top_trader_id = payload.get("topTraderId") or settings.get("topTraderId")
        db.set_user_binance_smart_money_position_follow(
            user_id,
            top_trader_id=top_trader_id,
            symbol=payload.get("symbol"),
            position_side=payload.get("positionSide") or payload.get("side"),
            enabled=bool(payload.get("enabled", True)),
        )
        follows = db.list_user_binance_smart_money_position_follows(user_id)
        copy_trading = project_binance_copy_trading_follows(
            get_cached_binance_copy_trading_history(user_id),
            follows,
        )
        notify_snapshot_update()
        return jsonify({"follows": follows, "copyTrading": copy_trading})
    except ValueError as exc:
        return jsonify({"message": str(exc)}), 400
    except Exception as exc:
        return jsonify({"message": f"聪明钱持仓跟随保存失败：{exc}"}), 500


@app.get("/api/binance/connection-settings")
@require_current_user
def binance_connection_settings():
    try:
        user_id = _current_user_id()
        return jsonify({
            "settings": db.get_user_binance_connection_settings(user_id),
            "connection": get_binance_connection_status(user_id),
        })
    except Exception as exc:
        return jsonify({"message": f"连接方式加载失败：{exc}"}), 500


@app.put("/api/binance/connection-settings")
@require_current_user
def binance_connection_settings_save():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"message": "请求格式无效"}), 400
    try:
        user_id = _current_user_id()
        settings = db.save_user_binance_connection_settings(user_id, payload)
        set_user_connection_mode(user_id, settings.get("connectionMode", "REST"))
        notify_snapshot_update()
        return jsonify({
            "settings": settings,
            "connection": get_binance_connection_status(user_id),
        })
    except ValueError as exc:
        return jsonify({"message": str(exc)}), 400
    except Exception as exc:
        return jsonify({"message": f"连接方式保存失败：{exc}"}), 500


@app.get("/api/binance/ui-settings")
@require_current_user
def binance_ui_settings():
    try:
        return jsonify({"settings": db.get_user_binance_ui_settings(_current_user_id())})
    except Exception as exc:
        return jsonify({"message": f"Binance 界面设置加载失败：{exc}"}), 500


@app.put("/api/binance/ui-settings")
@require_current_user
def binance_ui_settings_save():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"message": "请求格式无效"}), 400
    try:
        return jsonify({"settings": db.save_user_binance_ui_settings(_current_user_id(), payload)})
    except ValueError as exc:
        return jsonify({"message": str(exc)}), 400
    except Exception as exc:
        return jsonify({"message": f"Binance 界面设置保存失败：{exc}"}), 500


@app.get("/api/binance/simulated-positions")
@require_current_user
def binance_simulated_positions_list():
    try:
        return jsonify(get_binance_simulated_portfolio(_current_user_id()))
    except Exception as exc:
        return jsonify({"message": f"持仓计划监控加载失败：{exc}"}), 500


@app.post("/api/binance/futures/positions/monitor-preview")
@require_current_user
def binance_futures_position_monitor_preview():
    try:
        preview = preview_live_futures_position_monitor(_current_user_id(), request.get_json(silent=True) or {})
        return jsonify(preview)
    except ValueError as exc:
        return jsonify({"message": str(exc)}), 400
    except BinanceApiError as exc:
        return _binance_error_response(exc, prefix="读取持仓监控行情失败")
    except Exception as exc:
        return jsonify({"message": f"生成真实持仓监控计划失败：{exc}"}), 500


@app.post("/api/binance/simulated-positions")
@require_current_user
def binance_simulated_positions_create():
    try:
        item = save_binance_simulated_position(_current_user_id(), request.get_json(silent=True) or {})
        notify_snapshot_update()
        return jsonify({"item": item}), 201
    except ValueError as exc:
        return jsonify({"message": str(exc)}), 400
    except Exception as exc:
        return jsonify({"message": f"持仓计划监控保存失败：{exc}"}), 500


@app.put("/api/binance/simulated-positions/<int:position_id>")
@require_current_user
def binance_simulated_positions_update(position_id: int):
    try:
        item = save_binance_simulated_position(_current_user_id(), request.get_json(silent=True) or {}, position_id)
        notify_snapshot_update()
        return jsonify({"item": item})
    except ValueError as exc:
        return jsonify({"message": str(exc)}), 400
    except Exception as exc:
        return jsonify({"message": f"持仓计划监控保存失败：{exc}"}), 500


@app.post("/api/binance/simulated-positions/<int:position_id>/restore")
@require_current_user
def binance_simulated_positions_restore(position_id: int):
    try:
        item = restore_binance_simulated_position(_current_user_id(), position_id)
        notify_snapshot_update()
        return jsonify({"item": item})
    except ValueError as exc:
        return jsonify({"message": str(exc)}), 400
    except Exception as exc:
        return jsonify({"message": f"持仓计划监控恢复失败：{exc}"}), 500


@app.delete("/api/binance/simulated-positions/<int:position_id>")
@require_current_user
def binance_simulated_positions_delete(position_id: int):
    try:
        result = remove_binance_simulated_position(_current_user_id(), position_id)
        notify_snapshot_update()
        return jsonify(result)
    except BinanceApiError as exc:
        return _binance_error_response(exc, prefix="取消交易所同向委托失败")
    except ValueError as exc:
        return jsonify({"message": str(exc)}), 400
    except Exception as exc:
        return jsonify({"message": f"持仓计划监控删除失败：{exc}"}), 500


@app.get("/api/update-log")
def update_log():
    try:
        return jsonify(read_update_log())
    except (FileNotFoundError, OSError, UnicodeError, ValueError) as exc:
        return jsonify({"message": str(exc)}), 500


@app.get("/api/price-action/profile")
def price_action_profile():
    timeframe = request.args.get("timeframe", "1d").strip() or "1d"
    return jsonify(get_price_action_profile(timeframe=timeframe))


@app.post("/api/auth/register")
def auth_register():
    payload = request.get_json(silent=True) or {}
    try:
        user = db.register_user(payload.get("username"), payload.get("password"))
        session = db.create_auth_session(user["id"])
        response = jsonify({"user": user, **session})
        _set_session_cookie(response, session["token"])
        return response, 201
    except ValueError as exc:
        return jsonify({"message": str(exc)}), 400
    except Exception as exc:
        return jsonify({"message": f"注册失败：{exc}"}), 500


@app.post("/api/auth/login")
def auth_login():
    payload = request.get_json(silent=True) or {}
    user = db.authenticate_user(payload.get("username"), payload.get("password"))
    if not user:
        return jsonify({"message": "用户名或密码不正确"}), 401
    try:
        session = db.create_auth_session(user["id"])
        response = jsonify({"user": user, **session})
        _set_session_cookie(response, session["token"])
        return response
    except Exception as exc:
        return jsonify({"message": f"登录失败：{exc}"}), 500


@app.get("/api/auth/me")
@require_current_user
def auth_me():
    return jsonify({"user": g.current_user})


@app.post("/api/auth/logout")
@require_current_user
def auth_logout():
    db.revoke_auth_session(getattr(g, "auth_token", None))
    response = jsonify({"ok": True})
    response.delete_cookie(AUTH_COOKIE_NAME, path="/")
    return response


@app.get("/api/account/ai-settings")
@require_current_user
def account_ai_settings():
    try:
        return jsonify({"settings": db.get_user_ai_settings(_current_user_id())})
    except Exception as exc:
        return jsonify({"message": f"AI设置加载失败：{exc}"}), 500


@app.put("/api/account/ai-settings")
@require_current_user
def account_ai_settings_save():
    payload = request.get_json(silent=True) or {}
    try:
        settings = db.save_user_ai_settings(
            _current_user_id(),
            base_url=payload.get("baseUrl"),
            model=payload.get("model"),
            api_key=payload.get("apiKey") if "apiKey" in payload else None,
        )
        return jsonify({"settings": settings})
    except ValueError as exc:
        return jsonify({"message": str(exc)}), 400
    except Exception as exc:
        return jsonify({"message": f"AI设置保存失败：{exc}"}), 500


@app.get("/api/account/notification-settings")
@require_current_user
def account_notification_settings():
    try:
        return jsonify({"settings": db.get_user_notification_settings(_current_user_id())})
    except ValueError as exc:
        return jsonify({"message": str(exc)}), 400
    except Exception as exc:
        return jsonify({"message": f"邮件通知设置加载失败：{exc}"}), 500


@app.put("/api/account/notification-settings")
@require_current_user
def account_notification_settings_save():
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify({"settings": db.save_user_notification_settings(_current_user_id(), payload)})
    except ValueError as exc:
        return jsonify({"message": str(exc)}), 400
    except Exception as exc:
        return jsonify({"message": f"邮件通知设置保存失败：{exc}"}), 500


@app.post("/api/notifications/test-email")
@require_current_user
def notifications_test_email():
    payload = request.get_json(silent=True) or {}
    user_id = _current_user_id()
    now = time.monotonic()
    last_sent_at = _test_email_last_sent_at.get(user_id, 0)
    remaining_seconds = TEST_EMAIL_MIN_INTERVAL_SECONDS - (now - last_sent_at)
    if remaining_seconds > 0:
        return jsonify({"message": f"测试邮件已发送，请在 {int(remaining_seconds) + 1} 秒后再试。"}), 429

    try:
        recipient = payload.get("recipient") or db.get_user_notification_settings(user_id).get("email")
        message_id = send_resend_test_email(recipient)
    except (ValueError, EmailConfigurationError) as exc:
        return jsonify({"message": str(exc)}), 400
    except EmailDeliveryError as exc:
        return jsonify({"message": str(exc)}), 502

    _test_email_last_sent_at[user_id] = now
    return jsonify({"sent": True, "messageId": message_id})


@app.get("/api/stocks/search")
def stock_search():
    keyword = request.args.get("keyword", "").strip()
    if not keyword:
        return jsonify({"items": []})

    try:
        return jsonify({"items": search_stocks(keyword)})
    except Exception as exc:
        return jsonify({"message": f"股票搜索失败：{exc}"}), 500


@app.get("/api/stocks")
def stock_list():
    try:
        return jsonify({"items": list_stocks()})
    except Exception as exc:
        return jsonify({"message": f"股票列表加载失败：{exc}"}), 500


@app.get("/api/stocks/history")
@require_current_user
def stock_analysis_history_list():
    try:
        return jsonify({"items": db.list_stock_analysis_history(_current_user_id(), 10)})
    except Exception as exc:
        return jsonify({"message": f"个股分析历史加载失败：{exc}"}), 500


@app.post("/api/stocks/history")
@require_current_user
def stock_analysis_history_save():
    payload = request.get_json(silent=True) or {}
    try:
        item = db.upsert_stock_analysis_history(
            _current_user_id(),
            {"symbol": payload.get("symbol"), "name": payload.get("name")},
        )
        return jsonify({"item": item})
    except ValueError as exc:
        return jsonify({"message": str(exc)}), 400
    except Exception as exc:
        return jsonify({"message": f"个股分析历史保存失败：{exc}"}), 500


@app.delete("/api/stocks/history/<symbol>")
@require_current_user
def stock_analysis_history_delete(symbol):
    try:
        deleted = db.delete_stock_analysis_history(_current_user_id(), symbol)
        return jsonify({"deleted": deleted})
    except Exception as exc:
        return jsonify({"message": f"个股分析历史删除失败：{exc}"}), 500


@app.get("/api/market/today")
def market_today():
    try:
        include_growth_boards = str(request.args.get("includeGrowthBoards", "false")).strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        refresh_indices = str(request.args.get("refreshIndices", "false")).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        return jsonify(
            get_market_today(
                include_growth_boards=include_growth_boards,
                refresh_indices=refresh_indices,
            )
        )
    except Exception as exc:
        return jsonify({"message": f"今日行情加载失败：{exc}"}), 500


@app.post("/api/market/refresh")
def market_refresh():
    try:
        force = str(request.args.get("force", "false")).strip().lower() in {"1", "true", "yes"}
        return jsonify(synchronize_market_data(force=force))
    except Exception as exc:
        return jsonify({"message": f"全市场行情同步失败：{exc}"}), 503


@app.get("/api/stocks/quotes")
def stock_quotes():
    symbols = request.args.get("symbols", "")
    try:
        items = [symbol.strip() for symbol in symbols.split(",") if symbol.strip()]
        return jsonify(get_stock_quotes(items))
    except Exception as exc:
        return jsonify({"message": f"股票行情加载失败：{exc}"}), 500


@app.get("/api/portfolio")
@require_current_user
def portfolio_list():
    try:
        return jsonify(get_portfolio(_current_user_id()))
    except Exception as exc:
        return jsonify({"message": f"持仓加载失败：{exc}"}), 500


@app.post("/api/portfolio")
@require_current_user
def portfolio_save():
    payload = request.get_json(silent=True) or {}
    try:
        item = save_portfolio_position(
            _current_user_id(),
            symbol=str(payload.get("symbol") or ""),
            cost_price=float(payload.get("costPrice") or 0),
            shares=int(payload.get("shares") or 0),
            note=str(payload.get("note") or ""),
            name=str(payload.get("name") or ""),
        )
        return jsonify({"item": item})
    except ValueError as exc:
        return jsonify({"message": str(exc)}), 400
    except Exception as exc:
        return jsonify({"message": f"持仓保存失败：{exc}"}), 500


@app.put("/api/portfolio/account")
@require_current_user
def portfolio_account_save():
    payload = request.get_json(silent=True) or {}
    try:
        account = set_portfolio_cash(_current_user_id(), float(payload.get("cashBalance")))
        return jsonify({"account": account})
    except (TypeError, ValueError) as exc:
        return jsonify({"message": str(exc) or "现金余额必须为有效数字"}), 400
    except Exception as exc:
        return jsonify({"message": f"现金余额保存失败：{exc}"}), 500


@app.get("/api/portfolio/trades")
@require_current_user
def portfolio_trade_list():
    try:
        return jsonify({"items": list_portfolio_trades(_current_user_id(), _int_arg("limit", 30))})
    except Exception as exc:
        return jsonify({"message": f"成交记录加载失败：{exc}"}), 500


@app.post("/api/portfolio/trades")
@require_current_user
def portfolio_trade_save():
    payload = request.get_json(silent=True) or {}
    try:
        item = execute_portfolio_trade(
            _current_user_id(),
            action=str(payload.get("action") or ""),
            symbol=str(payload.get("symbol") or ""),
            price=float(payload.get("price") or 0),
            shares=int(payload.get("shares") or 0),
            fee=None if payload.get("fee") is None else float(payload.get("fee")),
            note=str(payload.get("note") or ""),
            name=str(payload.get("name") or ""),
            discipline_override=payload.get("disciplineOverride") is True,
        )
        return jsonify({"item": item})
    except db.DisciplineConfirmationRequired as exc:
        return jsonify(
            {
                "message": str(exc),
                "requiresDisciplineConfirmation": True,
                "disciplineViolations": exc.violations,
            }
        ), 409
    except ValueError as exc:
        return jsonify({"message": str(exc)}), 400
    except Exception as exc:
        return jsonify({"message": f"成交执行失败：{exc}"}), 500


@app.delete("/api/portfolio/<symbol>")
@require_current_user
def portfolio_delete(symbol: str):
    try:
        removed = remove_portfolio_position(_current_user_id(), symbol)
        return jsonify({"removed": removed})
    except Exception as exc:
        return jsonify({"message": f"持仓删除失败：{exc}"}), 500


@app.get("/api/watchlist")
@require_current_user
def watchlist_list():
    try:
        return jsonify(get_watchlist(_current_user_id()))
    except Exception as exc:
        return jsonify({"message": f"自选观察加载失败：{exc}"}), 500


@app.post("/api/watchlist")
@require_current_user
def watchlist_save():
    payload = request.get_json(silent=True) or {}
    try:
        item = save_watchlist_item(
            _current_user_id(),
            symbol=str(payload.get("symbol") or ""),
            name=str(payload.get("name") or ""),
            note=str(payload.get("note") or ""),
        )
        return jsonify({"item": item})
    except ValueError as exc:
        return jsonify({"message": str(exc)}), 400
    except Exception as exc:
        return jsonify({"message": f"自选观察保存失败：{exc}"}), 500


@app.delete("/api/watchlist/<symbol>")
@require_current_user
def watchlist_delete(symbol: str):
    try:
        removed = remove_watchlist_item(_current_user_id(), symbol)
        return jsonify({"removed": removed})
    except Exception as exc:
        return jsonify({"message": f"自选观察删除失败：{exc}"}), 500


@app.get("/api/discipline/plan")
@require_current_user
def discipline_plan():
    symbol = request.args.get("symbol", "").strip()
    if not re.fullmatch(r"\d{1,6}", symbol):
        return jsonify({"message": "请输入有效股票代码"}), 400
    symbol = symbol.zfill(6)

    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=420)
    try:
        user_id = _current_user_id()
        portfolio = get_portfolio(user_id)
        account = portfolio.get("account") or {}
        account_value = float(account.get("totalAccountValue") or 0)
        cash_balance = float(account.get("cashBalance") or 0)
        if account_value <= 0 or account_value > 100_000_000:
            return jsonify({"message": "请先在纪律交易中设置可用现金或录入持仓"}), 400
        index_history = get_cached_index_history("000001", start_date.strftime("%Y%m%d"))
        plan = _build_discipline_plan(
            symbol,
            account_value,
            cash_balance=cash_balance,
            index_history=index_history,
            cache_only=True,
            user_id=user_id,
        )
        plan["account"] = account
        return jsonify(plan)
    except Exception as exc:
        return jsonify({"message": f"纪律计划生成失败：{exc}"}), 500


@app.get("/api/discipline/portfolio")
@require_current_user
def discipline_portfolio():
    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=420)
    try:
        user_id = _current_user_id()
        portfolio = get_portfolio(user_id)
        account = portfolio.get("account") or {}
        account_value = float(account.get("totalAccountValue") or 0)
        cash_balance = float(account.get("cashBalance") or 0)
        if account_value < 0 or account_value > 100_000_000:
            return jsonify({"message": "账户总金额必须在 0 到 1 亿元之间"}), 400
        positions = portfolio.get("items") or []
        if not positions:
            return jsonify({
                "account": account,
                "portfolioSummary": portfolio.get("summary") or {},
                "quoteMinute": portfolio.get("quoteMinute"),
                "updatedAt": portfolio.get("updatedAt"),
                "stale": portfolio.get("stale"),
                "marketData": portfolio.get("marketData") or {},
                "items": [],
                "summary": {"positionCount": 0, "actionCounts": {}, "errorCount": 0, "plannedRisk": 0},
            })

        index_history = get_cached_index_history("000001", start_date.strftime("%Y%m%d"))
        args = (start_date.strftime("%Y%m%d"), end_date.strftime("%Y%m%d"), account_value, cash_balance, index_history, user_id)
        results = []
        with ThreadPoolExecutor(max_workers=min(4, len(positions))) as executor:
            futures = [executor.submit(_build_portfolio_discipline_item, position, args) for position in positions]
            for future in futures:
                results.append(future.result())

        action_counts: dict[str, int] = {}
        error_count = 0
        planned_risk = 0.0
        for item in results:
            if item.get("error"):
                error_count += 1
                continue
            plan = item.get("plan", {})
            action = plan.get("action", "WAIT")
            action_counts[action] = action_counts.get(action, 0) + 1
            planned_risk += float((plan.get("order") or {}).get("riskAmount") or 0)
        return jsonify({
            "account": account,
            "portfolioSummary": portfolio.get("summary") or {},
            "quoteMinute": portfolio.get("quoteMinute"),
            "updatedAt": portfolio.get("updatedAt"),
            "stale": portfolio.get("stale"),
            "marketData": portfolio.get("marketData") or {},
            "items": results,
            "summary": {
                "positionCount": len(positions),
                "actionCounts": action_counts,
                "errorCount": error_count,
                "plannedRisk": round(planned_risk, 2),
            },
        })
    except Exception as exc:
        return jsonify({"message": f"持仓纪律分析失败：{exc}"}), 500


@app.get("/api/discipline/watchlist")
@require_current_user
def discipline_watchlist():
    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=420)
    try:
        user_id = _current_user_id()
        watchlist = get_watchlist(user_id)
        portfolio = get_portfolio(user_id)
        account = portfolio.get("account") or {}
        account_value = float(account.get("totalAccountValue") or 0)
        cash_balance = float(account.get("cashBalance") or 0)
        if account_value < 0 or account_value > 100_000_000:
            return jsonify({"message": "账户总金额必须在 0 到 1 亿元之间"}), 400
        items = watchlist.get("items") or []
        if not items:
            return jsonify({
                **watchlist,
                "account": account,
                "summary": {"watchCount": 0, "actionCounts": {}, "errorCount": 0},
            })

        index_history = get_cached_index_history("000001", start_date.strftime("%Y%m%d"))
        args = (start_date.strftime("%Y%m%d"), end_date.strftime("%Y%m%d"), account_value, cash_balance, index_history, user_id)
        results = []
        with ThreadPoolExecutor(max_workers=min(4, len(items))) as executor:
            futures = [executor.submit(_build_watchlist_discipline_item, item, args) for item in items]
            for future in futures:
                results.append(future.result())

        action_counts: dict[str, int] = {}
        error_count = 0
        for item in results:
            if item.get("error"):
                error_count += 1
                continue
            action = (item.get("plan") or {}).get("action", "WAIT")
            action_counts[action] = action_counts.get(action, 0) + 1
        return jsonify({
            **watchlist,
            "account": account,
            "items": results,
            "summary": {
                "watchCount": len(items),
                "actionCounts": action_counts,
                "errorCount": error_count,
            },
        })
    except Exception as exc:
        return jsonify({"message": f"自选观察分析失败：{exc}"}), 500


@app.get("/api/discipline/journal")
@require_current_user
def discipline_journal_list():
    try:
        return jsonify({"items": db.list_discipline_journal(_current_user_id(), _int_arg("limit", 12))})
    except Exception as exc:
        return jsonify({"message": f"纪律日志加载失败：{exc}"}), 500


@app.post("/api/discipline/journal")
@require_current_user
def discipline_journal_save():
    payload = request.get_json(silent=True) or {}
    acknowledged = payload.get("acknowledged") or []
    note = str(payload.get("note") or "").strip()
    if len(set(acknowledged)) < 4:
        return jsonify({"message": "请完整核对四项纪律确认后再记录计划"}), 400
    if not note:
        return jsonify({"message": "请填写执行或复盘备注"}), 400
    try:
        item = db.create_discipline_journal(_current_user_id(), payload)
        return jsonify({"item": item})
    except ValueError as exc:
        return jsonify({"message": str(exc)}), 400
    except Exception as exc:
        return jsonify({"message": f"纪律日志保存失败：{exc}"}), 500


@app.get("/api/stocks/<symbol>/fundamentals")
def stock_fundamentals(symbol: str):
    try:
        return jsonify(get_stock_fundamentals(symbol, refresh_core=False, start_background=True))
    except Exception as exc:
        return jsonify({"message": f"基本面加载失败：{exc}"}), 500


@app.post("/api/stocks/<symbol>/f10/sync")
def stock_f10_sync(symbol: str):
    mode = request.args.get("mode", "background").strip()
    try:
        if mode == "wait":
            return jsonify(sync_f10_reports(symbol))
        return jsonify(ensure_f10_background_sync(symbol))
    except Exception as exc:
        return jsonify({"message": f"F10同步失败：{exc}"}), 500


@app.get("/api/stocks/<symbol>/f10/status")
def stock_f10_status(symbol: str):
    try:
        return jsonify(get_f10_status(symbol))
    except Exception as exc:
        return jsonify({"message": f"F10状态读取失败：{exc}"}), 500


def _find_main_rise_response():
    min_price = _float_arg("minPrice", 0.0)
    max_price = _float_arg("maxPrice", 30.0)

    if min_price < 0 or max_price <= 0 or min_price > max_price:
        return jsonify({"message": "请输入有效的股价区间"}), 400

    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=185)
    candidates = list_price_limited_non_st_stocks(min_price, max_price)
    if not candidates:
        return jsonify({"message": "未找到符合股价区间的非 ST 股票"}), 404

    random.shuffle(candidates)
    selected_candidates = candidates[:MAIN_RISE_MAX_ATTEMPTS]
    errors: list[str] = []
    rejection_reasons: list[str] = []
    start_text = start_date.strftime("%Y%m%d")
    end_text = end_date.strftime("%Y%m%d")

    for attempts, candidate in enumerate(selected_candidates, start=1):
        candidate, result, error = _analyze_main_rise_candidate(candidate, start_text, end_text)
        if error:
            errors.append(error)
            continue
        evaluation = evaluate_main_rise_candidate(result, candidate.get("latestPrice"))
        if not evaluation["matched"]:
            rejection_reasons.extend(
                f"{candidate.get('symbol')}: {reason}"
                for reason in evaluation.get("reasons") or []
            )
            continue

        match = evaluation["match"]
        result["symbol"] = candidate["symbol"]
        result["name"] = candidate["name"]
        result["dateRange"] = {
            "start": result["rawKlines"][0]["date"],
            "end": result["rawKlines"][-1]["date"],
        }
        result["mainRiseMeta"] = {
            "policyVersion": evaluation.get("policyVersion"),
            "attempts": attempts,
            "candidateCount": len(candidates),
            "maxAttempts": MAIN_RISE_MAX_ATTEMPTS,
            "minPrice": min_price,
            "maxPrice": max_price,
            "latestPrice": candidate.get("latestPrice"),
            "matchedSetup": match,
            "notice": "主升筛选只表示当前日线结构符合条件，不代表收益保证。",
        }
        # Keep the existing result shape usable for older hosted frontends.
        result["findMeta"] = {
            "attempts": attempts,
            "candidateCount": len(candidates),
            "maxAttempts": MAIN_RISE_MAX_ATTEMPTS,
            "minPrice": min_price,
            "maxPrice": max_price,
            "latestPrice": candidate.get("latestPrice"),
            "matchedSignal": match,
        }
        return jsonify(result)

    return jsonify(
        {
            "message": f"已逐只检查 {len(selected_candidates)} 只股票，未找到符合主升启动条件的标的",
            "attempts": len(selected_candidates),
            "candidateCount": len(candidates),
            "maxAttempts": MAIN_RISE_MAX_ATTEMPTS,
            "minPrice": min_price,
            "maxPrice": max_price,
            "errors": errors[-5:],
            "rejections": rejection_reasons[-10:],
        }
    ), 404


@app.get("/api/stocks/find-main-rise")
def find_main_rise_stock():
    return _find_main_rise_response()


@app.get("/api/stocks/find-good")
def find_good_stock():
    """Compatibility route; the old generic-good semantics are retired."""

    return _find_main_rise_response()


@app.get("/api/stocks/find-golden-pillar")
def find_golden_pillar_stock():
    min_price = _float_arg("minPrice", 0.0)
    max_price = _float_arg("maxPrice", 30.0)
    max_attempts = max(1, min(_int_arg("maxAttempts", 80), 160))
    support_tolerance = max(0.0, min(_float_arg("supportTolerance", 0.01), 0.03))

    if min_price < 0 or max_price <= 0 or min_price > max_price:
        return jsonify({"message": "请输入有效的股价区间"}), 400

    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=120)
    candidates = list_price_limited_non_st_stocks(min_price, max_price)
    if not candidates:
        return jsonify({"message": "未找到符合股价区间的非 ST 股票"}), 404

    random.shuffle(candidates)
    selected_candidates = candidates[:max_attempts]
    errors: list[str] = []

    executor = ThreadPoolExecutor(max_workers=min(8, len(selected_candidates)))
    futures = [
        executor.submit(
            _analyze_golden_pillar_candidate,
            candidate,
            start_date.strftime("%Y%m%d"),
            end_date.strftime("%Y%m%d"),
            support_tolerance,
        )
        for candidate in selected_candidates
    ]
    try:
        for attempts, future in enumerate(as_completed(futures), start=1):
            candidate, history, meta, error = future.result()
            if error:
                errors.append(error)
                continue
            if history and meta:
                result = analyze_price_action(history)
                result["symbol"] = candidate["symbol"]
                result["name"] = candidate["name"]
                result["dateRange"] = {
                    "start": result["rawKlines"][0]["date"],
                    "end": result["rawKlines"][-1]["date"],
                }
                result["goldenPillarMeta"] = {
                    **meta,
                    "supplemental": True,
                    "priceActionEligible": bool(
                        _match_price_action_buy(result, threshold=0.2, current_price=candidate.get("latestPrice"))
                    ),
                    "notice": "黄金柱仅为量价观察证据；是否可买须以已确认的价格行为计划为准。",
                    "attempts": attempts,
                    "candidateCount": len(candidates),
                    "minPrice": min_price,
                    "maxPrice": max_price,
                    "latestPrice": candidate.get("latestPrice"),
                }
                executor.shutdown(wait=False, cancel_futures=True)
                return jsonify(result)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    return jsonify(
        {
            "message": f"已随机分析 {len(selected_candidates)} 只股票，未找到第3天或第4天黄金柱待观察标的",
            "attempts": len(selected_candidates),
            "candidateCount": len(candidates),
            "errors": errors[-5:],
        }
    ), 404


def _history_source(history: list[dict]) -> str:
    for item in reversed(history):
        source = str(item.get("source") or item.get("provider") or "").strip().lower()
        if source and source != "unknown":
            return source
    return "unknown"


def _analysis_quote(symbol: str, history: list[dict], cached_quote: dict | None = None) -> dict:
    """Keep the analysis quote aligned with the last analyzed daily bar."""

    latest = history[-1] if history else {}
    previous = history[-2] if len(history) > 1 else {}
    quote = dict(cached_quote or {})
    quote.update(
        {
            "symbol": symbol,
            "latestPrice": latest.get("close"),
            "priceChange": latest.get("priceChange"),
            "pctChange": latest.get("pctChange"),
            "open": latest.get("open"),
            "high": latest.get("high"),
            "low": latest.get("low"),
            "previousClose": previous.get("close"),
            "volume": latest.get("volume"),
            "amount": latest.get("amount"),
            "turnoverRate": latest.get("turnoverRate"),
            "quoteDate": latest.get("date"),
            "source": latest.get("source") or latest.get("provider") or "unknown",
            "provider": latest.get("provider") or latest.get("source") or "unknown",
        }
    )
    return quote


@app.get("/api/analyze")
def analyze():
    symbol = request.args.get("symbol", "").strip()
    if not symbol:
        return jsonify({"message": "缺少股票代码"}), 400

    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=370)

    try:
        history = get_stock_history(
            symbol=symbol,
            start_date=start_date.strftime("%Y%m%d"),
            end_date=end_date.strftime("%Y%m%d"),
        )
        if not history:
            return jsonify({"message": "未获取到近一年日 K 数据"}), 404

        result = attach_a_share_strategy_plan(analyze_price_action(history), history, scope="STOCK")
        result["symbol"] = symbol
        result["dateRange"] = {
            "start": history[0]["date"],
            "end": history[-1]["date"],
        }
        result["source"] = _history_source(history)
        result["provider"] = result["source"]
        result["quote"] = _analysis_quote(symbol, history, (db.list_daily_quotes([symbol]) or [{}])[0])
        result["fundamentals"] = get_stock_fundamentals_quick(symbol, start_background=True)
        result["intraday"] = {"periods": {}, "summary": _empty_intraday_summary(), "errors": {}}
        return jsonify(result)
    except Exception as exc:
        return jsonify({"message": f"分析失败：{exc}"}), 500


@app.get("/api/index/analyze")
def analyze_index():
    symbol = request.args.get("symbol", "000001").strip() or "000001"
    name = request.args.get("name", "上证指数").strip() or "上证指数"
    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=370)

    try:
        history = get_index_history(
            symbol=symbol,
            start_date=start_date.strftime("%Y%m%d"),
            end_date=end_date.strftime("%Y%m%d"),
        )
        if not history:
            return jsonify({"message": "未获取到近一年指数日 K 数据"}), 404

        result = attach_a_share_strategy_plan(analyze_price_action(history), history, scope="INDEX")
        result["symbol"] = symbol
        result["name"] = name
        result["dateRange"] = {
            "start": history[0]["date"],
            "end": history[-1]["date"],
        }
        result["source"] = _history_source(history)
        result["provider"] = result["source"]
        result["priceAction"]["assessment"]["scope"] = "INDEX_EXPOSURE"
        result["priceAction"]["assessment"]["label"] = f"市场暴露：{result['priceAction']['assessment']['label']}"
        result["intraday"] = {"periods": {}, "summary": _empty_intraday_summary(), "errors": {}}
        return jsonify(result)
    except Exception as exc:
        return jsonify({"message": f"指数分析失败：{exc}"}), 500


@app.get("/api/intraday/analyze")
def analyze_intraday_stock():
    symbol = request.args.get("symbol", "").strip()
    period = request.args.get("period", "").strip()
    if not symbol:
        return jsonify({"message": "缺少股票代码"}), 400
    try:
        return jsonify(_build_intraday_analysis(symbol, is_index=False, periods=[period] if period else None))
    except Exception as exc:
        return jsonify({"message": f"分时分析失败：{exc}"}), 500


@app.get("/api/index/intraday/analyze")
def analyze_intraday_index():
    symbol = request.args.get("symbol", "000001").strip() or "000001"
    period = request.args.get("period", "").strip()
    try:
        return jsonify(_build_intraday_analysis(symbol, is_index=True, periods=[period] if period else None))
    except Exception as exc:
        return jsonify({"message": f"指数分时分析失败：{exc}"}), 500


@app.post("/api/ai/analyze")
@require_current_user
def ai_analyze():
    payload = request.get_json(silent=True) or {}
    result = payload.get("result") or {}
    if not result:
        return jsonify({"message": "缺少行情分析数据"}), 400

    try:
        settings = db.get_user_ai_settings(_current_user_id(), include_secret=True)
        if not settings.get("apiKey"):
            return jsonify({"message": "请先在当前账户的AI设置中填写 API Key"}), 400
        analysis = analyze_with_ai(
            result=result,
            intraday=payload.get("intraday"),
            target_type=payload.get("targetType") or "stock",
            base_url=settings.get("baseUrl") or "",
            api_key=settings.get("apiKey") or "",
            model=settings.get("model") or None,
        )
        return jsonify(analysis)
    except Exception as exc:
        return jsonify({"message": f"AI分析失败：{exc}"}), 500


def _match_price_action_buy(result: dict, threshold: float, current_price: float | None = None) -> dict | None:
    try:
        latest_price = float(current_price or 0)
    except (TypeError, ValueError):
        latest_price = 0
    if latest_price <= 0:
        latest_price = float(result.get("summary", {}).get("latestClose") or 0)
    if latest_price <= 0:
        return None

    price_action = result.get("priceAction") or {}
    buy_signals = [
        signal
        for signal in price_action.get("signals", [])
        if signal.get("direction") == "BUY"
        and signal.get("status") == "CONFIRMED"
        and not signal.get("reviewRequired")
        and float(signal.get("entryLimit") or 0) > 0
        and float(signal.get("invalidationPrice") or 0) > 0
        and float(signal.get("firstTarget") or 0) > 0
    ]
    if not buy_signals:
        return None

    matches = []
    for signal in buy_signals:
        anchor_price = float(signal["entryLimit"])
        distance = abs(latest_price - anchor_price) / anchor_price
        if distance <= threshold:
            item = dict(signal)
            item["currentPrice"] = round(latest_price, 3)
            item["distancePct"] = round(distance * 100, 2)
            item["riskReward"] = round(
                (float(signal["firstTarget"]) - anchor_price) / max(anchor_price - float(signal["invalidationPrice"]), 0.001),
                2,
            )
            if item["riskReward"] < 1.5:
                continue
            matches.append(item)

    if not matches:
        return None
    return sorted(matches, key=lambda item: item["distancePct"])[0]


def _build_intraday_analysis(symbol: str, is_index: bool, periods: list[str] | None = None) -> dict:
    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=12)
    period_bars: dict[str, list[dict]] = {}
    errors: dict[str, str] = {}

    target_periods = [period for period in (periods or list(PERIODS)) if period in PERIODS]

    for period in target_periods:
        try:
            fetcher = get_index_intraday_history if is_index else get_stock_intraday_history
            bars = fetcher(
                symbol=symbol,
                period=period,
                start_date=start_date.strftime("%Y%m%d"),
                end_date=end_date.strftime("%Y%m%d"),
            )
            if bars:
                period_bars[period] = bars[-180:]
        except Exception as exc:
            errors[period] = str(exc)

    result = analyze_intraday(period_bars)
    result["errors"] = errors
    return result


def _empty_intraday_summary() -> dict:
    return {
        "bias": "等待",
        "periodCount": 0,
        "latestSignal": None,
        "actionableSetupCount": 0,
        "reviewRequiredSetupCount": 0,
        "signalCount": 0,
        "crossPeriodContext": "尚未请求分时K线。",
    }


def _analyze_find_candidate(
    candidate: dict,
    start_date: str,
    end_date: str,
    threshold: float,
) -> tuple[dict, dict | None, dict | None, str | None]:
    symbol = candidate["symbol"]
    try:
        history = get_stock_history(symbol=symbol, start_date=start_date, end_date=end_date)
        if not history:
            return candidate, None, None, None
        result = analyze_price_action(history)
        matched_signal = _match_price_action_buy(result, threshold, candidate.get("latestPrice"))
        return candidate, result, matched_signal, None
    except Exception as exc:
        return candidate, None, None, f"{symbol}: {exc}"


def _analyze_main_rise_candidate(
    candidate: dict,
    start_date: str,
    end_date: str,
) -> tuple[dict, dict | None, str | None]:
    symbol = candidate["symbol"]
    try:
        history = get_stock_history(
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            minimum_bars=MIN_ANALYSIS_BARS,
        )
        if not history:
            return candidate, None, f"{symbol}: 未获取到完整日K数据"
        return candidate, analyze_price_action(history), None
    except Exception as exc:
        return candidate, None, f"{symbol}: {exc}"


def _analyze_golden_pillar_candidate(
    candidate: dict,
    start_date: str,
    end_date: str,
    support_tolerance: float,
) -> tuple[dict, list[dict] | None, dict | None, str | None]:
    symbol = candidate["symbol"]
    try:
        history = get_stock_history(symbol=symbol, start_date=start_date, end_date=end_date)
        if not history:
            return candidate, None, None, None
        meta = detect_golden_pillar_watch(history, support_tolerance=support_tolerance)
        return candidate, history, meta, None
    except Exception as exc:
        return candidate, None, None, f"{symbol}: {exc}"


def _build_discipline_plan(
    symbol: str,
    account_value: float,
    *,
    user_id: int | None = None,
    cash_balance: float | None = None,
    index_history: list[dict] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    cache_only: bool = False,
) -> dict:
    end = end_date or datetime.now().date().strftime("%Y%m%d")
    start = start_date or (datetime.now().date() - timedelta(days=420)).strftime("%Y%m%d")
    if cache_only:
        history = get_cached_stock_history(symbol, start)
        # Batch requests stay local when possible, but a short local segment
        # cannot support a price-action plan. Refresh only that symbol instead.
        if len(history) < MIN_ANALYSIS_BARS:
            history = get_stock_history(symbol, start, end, minimum_bars=MIN_ANALYSIS_BARS)
    else:
        history = get_stock_history(symbol, start, end)
    if not history:
        raise RuntimeError("未获取到日K数据，无法生成纪律计划")
    position = db.get_portfolio_position(user_id, symbol) if user_id is not None else None
    security = db.search_securities(symbol, limit=1)
    plan = evaluate_discipline_strategy(
        history,
        account_value=account_value,
        available_cash=cash_balance,
        position=position,
        index_history=index_history,
    )
    trailing_stop = (plan.get("strategyPlan") or {}).get("trailingStop") or {}
    if user_id is not None and position and trailing_stop.get("applicable"):
        persisted_position = db.update_portfolio_position_trailing_state(user_id, symbol, trailing_stop)
        if persisted_position:
            plan["position"] = persisted_position
    plan["symbol"] = symbol
    plan["name"] = resolve_stock_name(symbol, position or {}, security[0] if security else {}) or symbol
    return plan


def _build_portfolio_discipline_item(position: dict, args: tuple) -> dict:
    start_date, end_date, account_value, cash_balance, index_history, user_id = args
    symbol = str(position.get("symbol") or "")
    try:
        plan = _build_discipline_plan(
            symbol,
            account_value,
            user_id=user_id,
            cash_balance=cash_balance,
            index_history=index_history,
            start_date=start_date,
            end_date=end_date,
            cache_only=True,
        )
        return {**position, "plan": plan, "error": None}
    except Exception as exc:
        return {**position, "plan": None, "error": str(exc)}


def _build_watchlist_discipline_item(item: dict, args: tuple) -> dict:
    start_date, end_date, account_value, cash_balance, index_history, user_id = args
    symbol = str(item.get("symbol") or "")
    try:
        plan = _build_discipline_plan(
            symbol,
            account_value,
            user_id=user_id,
            cash_balance=cash_balance,
            index_history=index_history,
            start_date=start_date,
            end_date=end_date,
            cache_only=True,
        )
        # A watchlist item without an actual position is an entry workspace.
        # Price-action analysis may retain bearish candidates for audit, but
        # exposing those candidates as tradePlans makes the UI look like it is
        # asking the user to sell a stock they do not own. Keep only entry-side
        # candidates in this endpoint; holding management remains on the
        # portfolio endpoint where the position context is explicit.
        position_context = dict(plan.get("positionContext") or {})
        has_position = bool(position_context.get("hasPosition")) and int(position_context.get("shares") or 0) > 0
        if not has_position:
            all_trade_plans = list(plan.get("tradePlans") or [])
            entry_trade_plans = [
                candidate for candidate in all_trade_plans
                if str(candidate.get("direction") or "").upper() != "SELL"
            ]
            plan["candidateTradePlans"] = all_trade_plans
            plan["tradePlans"] = entry_trade_plans
            position_context.update({
                "hasPosition": False,
                "shares": 0,
                "mode": "ENTRY_WATCH",
                "entryAllowed": True,
                "managementAllowed": False,
                "allowedActions": ["BUY", "WAIT"],
                "excludedManagementPlans": len(all_trade_plans) - len(entry_trade_plans),
            })
            plan["positionContext"] = position_context
        plan["name"] = item.get("name") or plan.get("name") or symbol
        return {**item, "plan": plan, "error": None}
    except Exception as exc:
        return {**item, "plan": None, "error": str(exc)}


def _float_arg(name: str, default: float) -> float:
    try:
        return float(request.args.get(name, default))
    except (TypeError, ValueError):
        return default


def _int_arg(name: str, default: int) -> int:
    try:
        return int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default


if __name__ == "__main__":
    start_binance_snapshot_worker()
    start_configured_binance_websocket_worker()
    app.run(host="127.0.0.1", port=5000, debug=True)
