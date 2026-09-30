# Production launcher for the Chanlun Flask backend (waitress, no debug reloader).
import logging
import os
import sys
import faulthandler
from pathlib import Path

# Import the backend as a package so service modules keep their parent package
# context when this launcher is started from the backend directory.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from waitress import serve
from backend.app import app
from backend.services.binance_snapshot_worker import start_binance_snapshot_worker


LOGGER = logging.getLogger("chanlun.production")
_FATAL_LOG_HANDLE = None


def _enable_fatal_diagnostics() -> None:
    """Write native-crash diagnostics beside the existing backend log."""

    global _FATAL_LOG_HANDLE
    try:
        log_path = Path(__file__).resolve().parents[1] / "data" / "backend-fatal.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _FATAL_LOG_HANDLE = log_path.open("a", encoding="utf-8")
        faulthandler.enable(_FATAL_LOG_HANDLE, all_threads=True)
    except Exception:
        # Diagnostics must never prevent the API from starting.
        LOGGER.debug("Unable to enable fatal diagnostics", exc_info=True)


def _start_background_workers() -> None:
    """Start cache workers without making optional WebSocket startup fatal."""

    start_binance_snapshot_worker()
    # The helper starts the manager only for accounts that opted into
    # WebSocket. If that optional transport cannot initialize, REST remains
    # available for account and protection monitoring.
    try:
        from backend.services.binance_websocket_worker import start_configured_binance_websocket_worker

        start_configured_binance_websocket_worker()
    except Exception:
        LOGGER.warning("Binance WebSocket worker was not started; REST remains available", exc_info=True)

if __name__ == "__main__":
    _enable_fatal_diagnostics()
    _start_background_workers()
    port = int(os.environ.get("PORT", "5000"))
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    serve(app, host=host, port=port, threads=32)
