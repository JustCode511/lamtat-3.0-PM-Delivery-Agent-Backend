"""
CloudWatch observability for LAMTAT chat.

Emits two things for every chat turn:
  1. A structured JSON log event to CloudWatch Logs  → /lamtat/chat  (queryable via Insights)
  2. Three custom CloudWatch Metrics → LAMTAT/Chat namespace
       - MessageCount   (Count)         by Module
       - ResponseLatency (Milliseconds) by Module
       - ErrorCount     (Count)         by Module

And for every HTTP request (via the FastAPI middleware):
  - RequestCount  (Count) by Path
  - RequestLatency (Milliseconds) by Path
  - ErrorRate  (Count) by Path  (only on 4xx/5xx)

Activated when AWS_REGION is set in the environment.
Silently falls back to local logger if AWS is not configured or credentials
are missing — so the app always starts cleanly in local dev.

Required env vars (all optional — disables CW if absent):
  AWS_REGION          e.g. ap-southeast-1
  CW_LOG_GROUP        default: /lamtat/chat
  CW_METRICS_NS       default: LAMTAT/Chat
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

log = logging.getLogger(__name__)

_LOG_GROUP   = os.getenv("CW_LOG_GROUP",   "/lamtat/chat")
_LOG_STREAM  = os.getenv("CW_LOG_STREAM",  "events")
_METRICS_NS  = os.getenv("CW_METRICS_NS",  "LAMTAT/Chat")
_REGION      = os.getenv("AWS_REGION",     "ap-southeast-1")

_logs_client    = None
_metrics_client = None
_seq_token: str | None = None
_enabled        = False


def _init() -> None:
    global _logs_client, _metrics_client, _enabled, _seq_token
    if not os.getenv("AWS_REGION"):
        log.info("CloudWatch observability disabled — AWS_REGION not set")
        return
    try:
        import boto3
        session = boto3.Session(region_name=_REGION)
        _logs_client    = session.client("logs")
        _metrics_client = session.client("cloudwatch")

        # Ensure log group exists
        try:
            _logs_client.create_log_group(logGroupName=_LOG_GROUP)
            log.info("CloudWatch: created log group %s", _LOG_GROUP)
        except _logs_client.exceptions.ResourceAlreadyExistsException:
            pass

        # Ensure log stream exists and grab any existing sequence token
        try:
            _logs_client.create_log_stream(
                logGroupName=_LOG_GROUP, logStreamName=_LOG_STREAM
            )
        except _logs_client.exceptions.ResourceAlreadyExistsException:
            resp = _logs_client.describe_log_streams(
                logGroupName=_LOG_GROUP,
                logStreamNamePrefix=_LOG_STREAM,
                limit=1,
            )
            streams = resp.get("logStreams", [])
            if streams:
                _seq_token = streams[0].get("uploadSequenceToken")

        _enabled = True
        log.info(
            "CloudWatch observability enabled — group=%s ns=%s region=%s",
            _LOG_GROUP, _METRICS_NS, _REGION,
        )
    except Exception as exc:
        log.warning("CloudWatch observability init failed (%s) — running without CW", exc)


_init()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def log_chat_event(
    *,
    module: str,
    user: str,
    query: str,
    response_ms: float,
    session_id: str | None = None,
    intent: str | None = None,
    status: str = "ok",
    error: str | None = None,
) -> None:
    """Log one chat turn to CloudWatch Logs + Metrics.
    Called from each chat endpoint after the agent responds.
    Never raises — failures are swallowed so they can't break the response.
    """
    event: dict[str, Any] = {
        "event":        "chat_turn",
        "ts":           int(time.time() * 1000),
        "module":       module,
        "user":         user,
        "session_id":   session_id,
        "query_len":    len(query),
        "query_preview": query[:120],
        "response_ms":  round(response_ms, 1),
        "status":       status,
        "intent":       intent,
    }
    if error:
        event["error"] = error[:200]

    # Always emit to the local structured logger (visible in Lambda logs too)
    log.info("chat_event %s", json.dumps({k: v for k, v in event.items() if v is not None}))

    if not _enabled:
        return

    # Fire-and-forget — don't await, don't block the response
    _put_log_sync(event)
    _put_metrics_sync(module=module, response_ms=response_ms, is_error=(status == "error"))


def log_request(
    *,
    path: str,
    method: str,
    status_code: int,
    latency_ms: float,
) -> None:
    """Log an HTTP request to CloudWatch Metrics. Called from the middleware."""
    if not _enabled:
        return
    _put_request_metrics(path=path, status_code=status_code, latency_ms=latency_ms)


# ---------------------------------------------------------------------------
# Internal helpers — all synchronous; called outside the async event loop
# ---------------------------------------------------------------------------

def _put_log_sync(event: dict[str, Any]) -> None:
    global _seq_token
    try:
        kwargs: dict[str, Any] = {
            "logGroupName":  _LOG_GROUP,
            "logStreamName": _LOG_STREAM,
            "logEvents": [
                {"timestamp": event["ts"], "message": json.dumps(event)}
            ],
        }
        if _seq_token:
            kwargs["sequenceToken"] = _seq_token
        resp = _logs_client.put_log_events(**kwargs)
        _seq_token = resp.get("nextSequenceToken")
    except Exception as exc:
        log.debug("CloudWatch put_log_events failed: %s", exc)


def _put_metrics_sync(*, module: str, response_ms: float, is_error: bool) -> None:
    try:
        _metrics_client.put_metric_data(
            Namespace=_METRICS_NS,
            MetricData=[
                {
                    "MetricName": "MessageCount",
                    "Dimensions": [{"Name": "Module", "Value": module}],
                    "Value": 1,
                    "Unit": "Count",
                },
                {
                    "MetricName": "ResponseLatency",
                    "Dimensions": [{"Name": "Module", "Value": module}],
                    "Value": response_ms,
                    "Unit": "Milliseconds",
                },
                {
                    "MetricName": "ErrorCount",
                    "Dimensions": [{"Name": "Module", "Value": module}],
                    "Value": 1 if is_error else 0,
                    "Unit": "Count",
                },
            ],
        )
    except Exception as exc:
        log.debug("CloudWatch put_metric_data failed: %s", exc)


def _put_request_metrics(*, path: str, status_code: int, latency_ms: float) -> None:
    try:
        _metrics_client.put_metric_data(
            Namespace=_METRICS_NS,
            MetricData=[
                {
                    "MetricName": "RequestCount",
                    "Dimensions": [{"Name": "Path", "Value": path}],
                    "Value": 1,
                    "Unit": "Count",
                },
                {
                    "MetricName": "RequestLatency",
                    "Dimensions": [{"Name": "Path", "Value": path}],
                    "Value": latency_ms,
                    "Unit": "Milliseconds",
                },
                {
                    "MetricName": "ErrorRate",
                    "Dimensions": [{"Name": "Path", "Value": path}],
                    "Value": 1 if status_code >= 400 else 0,
                    "Unit": "Count",
                },
            ],
        )
    except Exception as exc:
        log.debug("CloudWatch request metrics failed: %s", exc)
