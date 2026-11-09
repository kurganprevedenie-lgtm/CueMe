"""Безопасный журнал API-затрат. Здесь НЕТ промптов, ответов, URL и user_id.

JSONL отдельно от bot.db; ошибки учёта не ломают генерацию. Для инвойса
необходима сверка кабинета провайдера. reasoning — часть output, не добавка.
"""
import contextvars
import functools
import json
import logging
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from llm_pricing import price_usage

_action = contextvars.ContextVar("usage_action", default=None)
_call = contextvars.ContextVar("usage_call", default=None)
_last_request = contextvars.ContextVar("usage_last_request", default=None)
_attempt = contextvars.ContextVar("usage_attempt", default=None)
_lock = threading.RLock()
_handlers = {}
_warned = False


def usage_dir():
    return Path(os.getenv("LLM_USAGE_DIR", str(Path.home() / ".local/state/cueme/llm_usage")))


def emit(kind, **fields):
    global _warned
    if os.getenv("LLM_USAGE_ENABLED", "true").lower() in ("0", "false", "no"):
        return
    try:
        # Все поля ниже формируются программой из чисел и контролируемых имён.
        row = dict(schema=1, type=kind, ts=datetime.now(timezone.utc).isoformat(), **fields)
        with _lock:
            path = usage_dir() / "usage.jsonl"
            if str(path) not in _handlers:
                path.parent.mkdir(parents=True, exist_ok=True)
                handler = TimedRotatingFileHandler(path, when="midnight", utc=True, backupCount=60, encoding="utf-8")
                handler.setFormatter(logging.Formatter("%(message)s"))
                _handlers[str(path)] = handler
            message = json.dumps(row, ensure_ascii=True, allow_nan=False)
            _handlers[str(path)].emit(logging.LogRecord("llm_usage", logging.INFO, "", 0, message, (), None))
    except Exception as exc:
        if not _warned:
            logging.warning("LLM usage: запись недоступна (%s), расход не измерен", type(exc).__name__)
            _warned = True


@dataclass
class Action:
    origin: str
    purpose: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    start: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    clock: float = field(default_factory=time.monotonic)
    requests: int = 0
    tags: set = field(default_factory=set)
    outcome: str | None = None


@contextmanager
def action_scope(origin, purpose):
    action = Action(origin, purpose)
    token = _action.set(action)
    try:
        yield action
    except BaseException:
        action.outcome = "exception_or_cancelled"
        raise
    finally:
        _action.reset(token)
        if action.requests or action.outcome:
            emit("action", action_id=action.id, origin=origin, purpose=purpose,
                 started_at=action.start, duration_ms=round((time.monotonic() - action.clock) * 1000, 2),
                 api_calls=action.requests, functions=sorted(action.tags),
                 outcome=action.outcome or "completed_unverified")


def note_outcome(status):
    current = _action.get()
    if current:
        current.outcome = status


class UsageActionMiddleware:
    async def __call__(self, handler, event, data):
        with action_scope("user", "telegram_update"):
            return await handler(event, data)


def background_usage(function):
    @functools.wraps(function)
    async def wrapped(*args, **kwargs):
        # create_task копирует contextvars. Новое фоновое задание не должно
        # приписываться пользовательскому действию, породившему эту задачу.
        with action_scope("background", function.__name__):
            return await function(*args, **kwargs)
    return wrapped


def logical_call(function):
    @functools.wraps(function)
    async def wrapped(*args, **kwargs):
        call_id = uuid.uuid4().hex
        token = _call.set(call_id)
        attempt_token = _attempt.set([0])
        last = _last_request.set(None)
        async def run():
            try:
                result = await function(*args, **kwargs)
                emit("logical_result", call_id=call_id, outcome="valid_output" if result else "empty_output")
                return result
            except BaseException:
                emit("logical_result", call_id=call_id, outcome="failed")
                raise
        try:
            if _action.get() is None:
                with action_scope("unattributed", "direct_llm_call"):
                    return await run()
            return await run()
        finally:
            _call.reset(token)
            _attempt.reset(attempt_token)
            _last_request.reset(last)
    return wrapped


def integer(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def extract_usage(provider, data):
    """None означает неизвестно, в том числе для неизвестного cache split."""
    data = data if isinstance(data, dict) else {}
    u = data.get("usage") or data.get("usageMetadata") or {}
    if not isinstance(u, dict):
        u = {}
    if provider == "Gemini":
        i = integer(u.get("promptTokenCount"))
        c = integer(u.get("cachedContentTokenCount"))
        # Gemini отдельно возвращает кандидатов и мысли. total включает оба.
        total, visible = integer(u.get("totalTokenCount")), integer(u.get("candidatesTokenCount"))
        reasoning = integer(u.get("thoughtsTokenCount"))
        o = total - i if total is not None and i is not None and total >= i else (
            visible + reasoning if visible is not None and reasoning is not None else None)
    else:
        i = integer(u.get("prompt_tokens", u.get("input_tokens")))
        o = integer(u.get("completion_tokens", u.get("output_tokens")))
        details = u.get("prompt_tokens_details") or u.get("input_tokens_details") or {}
        output_details = u.get("completion_tokens_details") or u.get("output_tokens_details") or {}
        details = details if isinstance(details, dict) else {}
        output_details = output_details if isinstance(output_details, dict) else {}
        c = integer(u.get("prompt_cache_hit_tokens", details.get("cached_tokens")))
        reasoning = integer(output_details.get("reasoning_tokens", u.get("reasoning_tokens")))
    return dict(input_tokens=i, cached_input_tokens=c, output_tokens=o, reasoning_tokens=reasoning,
                cache_write_tokens=integer(u.get("cache_creation_input_tokens", u.get("cache_write_tokens"))))


def safe_name(value):
    value = str(value or "unknown")
    return value if re.fullmatch(r"[A-Za-z0-9_.:/-]{1,160}", value) else "unknown"


def request_start(provider, model, function, credential_ref=None):
    current = _action.get()
    attempt = 1
    if current:
        current.requests += 1
        current.tags.add(safe_name(function))
    counter = _attempt.get()
    if counter is not None:
        counter[0] += 1
        attempt = counter[0]
    request = dict(request_id=uuid.uuid4().hex, action_id=current.id if current else None,
                   credential_ref=credential_ref,
                   origin=current.origin if current else "unattributed",
                   call_id=_call.get(), provider=safe_name(provider), requested_model=safe_name(model),
                   function=safe_name(function), attempt=attempt, retry=attempt > 1,
                   action_api_index=current.requests if current else None,
                   started_at=datetime.now(timezone.utc).isoformat())
    _last_request.set(request["request_id"])
    return request


def request_end(request, elapsed_ms, response=None, error_type=None):
    try:
        _request_end(request, elapsed_ms, response, error_type)
    except Exception as exc:
        # Наблюдение никогда не должно отменять уже полученный ответ модели.
        emit("measurement_error", request_id=request.get("request_id"), error_type=type(exc).__name__)


def _request_end(request, elapsed_ms, response=None, error_type=None):
    data = {}
    if response is not None:
        try:
            data = response.json()
        except Exception:
            pass
    if not isinstance(data, dict):
        data = {}
    usage = extract_usage(request["provider"], data)
    model = safe_name(data.get("model") or data.get("modelVersion") or request["requested_model"])
    # Для неизвестного resolved model нет молчаливого подбора тарифа по alias.
    cost = price_usage(request["provider"], model, usage,
                       datetime.fromisoformat(request["started_at"]), data.get("service_tier"))
    emit("api", **request, model=model, http_status=response.status_code if response is not None else None,
         outcome=("http_success_unvalidated" if response is not None and response.is_success else "http_or_network_error"),
         error_type=safe_name(error_type) if error_type else None, duration_ms=round(elapsed_ms, 2),
         **usage, tariff_cost_usd=cost["usd"], cost_reason=cost["reason"], tariff=cost.get("tariff"))


def validation(outcome):
    request_id = _last_request.get()
    if request_id:
        emit("validation", request_id=request_id, outcome=outcome)
