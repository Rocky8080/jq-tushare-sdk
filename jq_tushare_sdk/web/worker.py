from __future__ import annotations

import argparse
import ctypes
import json
import signal
import sys
import threading
import traceback
from pathlib import Path

from jq_tushare_sdk.config import BacktestConfig
from jq_tushare_sdk.web.app import BacktestCancelled, _run_local_backtest, _write_json_atomic


_ACTIVITY_TOKEN = None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one Web backtest in an isolated process.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--progress", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args(argv)

    _promote_compute_qos()
    _begin_user_initiated_activity()

    config_path = Path(args.config)
    progress_path = Path(args.progress)
    result_path = Path(args.result)
    config = BacktestConfig(**json.loads(config_path.read_text(encoding="utf-8")))
    cancelled = threading.Event()

    def request_cancel(_signum, _frame) -> None:
        cancelled.set()

    signal.signal(signal.SIGTERM, request_cancel)
    signal.signal(signal.SIGINT, request_cancel)

    def report(percent: float, stage: str, detail: str | None = None) -> None:
        if cancelled.is_set():
            raise BacktestCancelled("backtest cancelled by user")
        _write_json_atomic(
            progress_path,
            {"percent": percent, "stage": stage, "detail": detail},
        )

    try:
        manifest = _run_local_backtest(config, progress_callback=report)
        _write_json_atomic(
            result_path,
            {
                "status": "completed",
                "run_id": manifest.run_id,
                "run_dir": str(manifest.run_dir),
            },
        )
        return 0
    except BacktestCancelled:
        _write_json_atomic(result_path, {"status": "cancelled"})
        return 2
    except Exception as exc:
        _write_json_atomic(
            result_path,
            {
                "status": "failed",
                "error": str(exc),
                "traceback": traceback.format_exc(limit=20),
            },
        )
        return 1


def _promote_compute_qos() -> None:
    if sys.platform != "darwin":
        return
    try:
        function = ctypes.CDLL(None).pthread_set_qos_class_self_np
        function.argtypes = [ctypes.c_uint32, ctypes.c_int]
        function.restype = ctypes.c_int
        function(0x19, 0)  # QOS_CLASS_USER_INITIATED
    except (AttributeError, OSError):
        return


def _begin_user_initiated_activity() -> None:
    global _ACTIVITY_TOKEN
    if sys.platform != "darwin":
        return
    try:
        ctypes.CDLL("/System/Library/Frameworks/Foundation.framework/Foundation")
        objc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        message_address = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value
        message_no_args = ctypes.CFUNCTYPE(
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )(message_address)
        message_c_string = ctypes.CFUNCTYPE(
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_char_p,
        )(message_address)
        message_activity = ctypes.CFUNCTYPE(
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint64,
            ctypes.c_void_p,
        )(message_address)

        process_info = message_no_args(
            objc.objc_getClass(b"NSProcessInfo"),
            objc.sel_registerName(b"processInfo"),
        )
        reason = message_c_string(
            objc.objc_getClass(b"NSString"),
            objc.sel_registerName(b"stringWithUTF8String:"),
            b"JQ Tushare SDK Web backtest",
        )
        token = message_activity(
            process_info,
            objc.sel_registerName(b"beginActivityWithOptions:reason:"),
            0x00FFFFFF,  # NSActivityUserInitiated
            reason,
        )
        if token:
            _ACTIVITY_TOKEN = message_no_args(token, objc.sel_registerName(b"retain"))
    except (AttributeError, OSError, TypeError, ValueError):
        return


if __name__ == "__main__":
    raise SystemExit(main())
