"""Run one Binance ML training job in a foreground process.

The HTTP service intentionally starts short-lived jobs on a daemon thread so
the API remains responsive.  A daemon thread is unsuitable for a long CUDA
run when the service is restarted, so this launcher persists the same job
record and executes it synchronously in its own process.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from uuid import uuid4


HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from services import binance_ml as ml  # noqa: E402
from services import database as db  # noqa: E402


def _parse_config(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid --config-json: {exc}") from exc
    return value if isinstance(value, dict) else {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a persisted Binance ML training job")
    parser.add_argument("--network", default="mainnet")
    parser.add_argument("--config-json", default=None)
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args()

    network = ml._legacy._normalize_network(args.network)
    config = ml._normalize_training_config(_parse_config(args.config_json))
    config["strategySettings"] = {
        "strategyEngine": "MODEL",
        "modelBranch": ml.MODEL_BRANCH_BEST,
        "modelRunId": None,
    }
    run_id = args.run_id or f"ml-{int(time.time() * 1000)}-{uuid4().hex[:8]}"
    now = int(time.time() * 1000)
    job = {
        "id": run_id,
        "network": network,
        "status": "QUEUED",
        "createdAt": now,
        "updatedAt": now,
        "progress": {
            "phase": "QUEUED",
            "message": "等待构建动作—结果语料。",
            "completed": 0,
            "total": 0,
            "currentSymbol": None,
        },
        "config": config,
        "dataset": {},
        "metrics": {},
        "error": None,
        "artifactPath": None,
    }

    db.init_db()
    db.create_binance_ml_training_run(run_id, network, config)
    with ml._legacy._jobs_lock:
        ml._legacy._jobs[run_id] = job
    ml._run_training_job(run_id)
    result = db.get_binance_ml_training_run(run_id) or {}
    print(json.dumps({
        "runId": run_id,
        "status": result.get("status"),
        "error": result.get("error"),
        "artifactPath": result.get("artifactPath"),
    }, ensure_ascii=False))
    return 0 if result.get("status") == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
