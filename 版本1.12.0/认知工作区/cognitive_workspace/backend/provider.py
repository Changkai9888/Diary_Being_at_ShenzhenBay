"""DeepSeek streaming transport, raw audit logs and a conservative UTC-month cap.

Only this module reads DEEPSEEK_API_KEY. No credentials are written to request
logs. Prices are the documented peak CNY rates, not an account billing claim.
Unknown usage retains the pre-call reservation; failed calls are never retried.
Budget locking covers threads/provider instances in one server process. Run only
one server process for a log directory (the application server owns that rule).
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import socket
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import uuid4


PRICING = {
    "deepseek-flash": {"cache_hit": 0.04, "cache_miss": 2.0, "output": 8.0},
    "deepseek-v4-pro": {"cache_hit": 0.30, "cache_miss": 9.0, "output": 27.0},
}
PRICING_SOURCE = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
PRICING_VERIFIED = "2026-10-09"
MAX_PAYLOAD_CHARS = 120_000
MAX_RESPONSE_BYTES = 1_048_576
SOCKET_TIMEOUT_SECONDS = 60
CALL_TIMEOUT_SECONDS = 120
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_LOCK = threading.Lock()


class ProviderError(Exception):
    """Safe for display. The partial result may contain an interrupted answer."""

    def __init__(self, message: str, *, result: dict | None = None):
        super().__init__(message)
        self.result = result


def _utc_now():
    return datetime.now(timezone.utc)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _tokens(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def usage_fields(usage):
    """Missing/cache-unknown values stay null instead of being invented as zero."""
    usage = usage if isinstance(usage, dict) else {}
    details = usage.get("prompt_tokens_details")
    details = details if isinstance(details, dict) else {}
    hit = _tokens(usage.get("prompt_cache_hit_tokens"))
    if hit is None:
        hit = _tokens(details.get("cached_tokens"))
    return {
        "input_tokens": _tokens(usage.get("prompt_tokens")),
        "cache_hit_tokens": hit,
        "cache_miss_tokens": _tokens(usage.get("prompt_cache_miss_tokens")),
        "output_tokens": _tokens(usage.get("completion_tokens")),
        "total_tokens": _tokens(usage.get("total_tokens")),
    }


def estimate_cost_upper(model, usage):
    fields = usage_fields(usage)
    prompt, output = fields["input_tokens"], fields["output_tokens"]
    hit, miss = fields["cache_hit_tokens"], fields["cache_miss_tokens"]
    if prompt is None and hit is not None and miss is not None:
        prompt = hit + miss
    if model not in PRICING or prompt is None or output is None:
        return None
    rates = PRICING[model]
    # Incomplete or inconsistent cache accounting uses the full miss price.
    if hit is None or miss is None or hit + miss != prompt:
        hit, miss = 0, prompt
    return round((hit * rates["cache_hit"] + miss * rates["cache_miss"] + output * rates["output"]) / 1_000_000, 10)


def _write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class DeepSeekProvider:
    def __init__(self, log_dir, api_key=None, endpoint="https://api.deepseek.com/chat/completions", *, max_calls=None, max_cost_cny=50.0):
        self.log_dir = Path(log_dir).resolve()
        self.api_key = (os.environ.get("DEEPSEEK_API_KEY", "") if api_key is None else api_key).strip()
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            raise ValueError("The provider endpoint must be HTTPS and contain no credentials.")
        if max_calls is not None and (type(max_calls) is not int or max_calls < 1):
            raise ValueError("max_calls must be a positive integer or None")
        if not _number(max_cost_cny) or max_cost_cny <= 0:
            raise ValueError("max_cost_cny must be positive and finite")
        self.endpoint = endpoint
        self.max_calls = max_calls
        self.max_cost_cny = float(max_cost_cny)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        with _LOCKS_LOCK:
            self._lock = _LOCKS.setdefault(os.path.normcase(str(self.log_dir)), threading.RLock())

    @property
    def configured(self):
        return bool(self.api_key)

    def _budget_unlocked(self, month):
        count, cost = 0, 0.0
        for directory in self.log_dir.iterdir():
            if not directory.is_dir() or not (directory / "request.json").exists():
                continue
            request_path = directory / "request.json"
            meta_path = directory / "metadata.json"
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
                recorded_month = meta.get("budget_month_utc")
                if recorded_month is None:
                    recorded_month = datetime.fromtimestamp(request_path.stat().st_mtime, timezone.utc).strftime("%Y-%m")
                if recorded_month != month:
                    continue
                accounted = meta.get("budget_accounted_cny")
                if not _number(accounted):
                    body = request_path.read_bytes()
                    request = json.loads(body)
                    rates = PRICING[request["model"]]
                    output = _tokens(request["max_tokens"])
                    if output is None:
                        raise ValueError("invalid output reservation")
                    accounted = (len(body) * rates["cache_miss"] + output * rates["output"]) / 1_000_000
                count += 1
                cost += accounted
            except (OSError, ValueError, KeyError, TypeError):
                # A damaged ledger must not silently release an unknown charge.
                raise ProviderError("调用费用记录暂时无法核验，已暂停新请求。请检查本地调用日志。") from None
        return {"month_utc": month, "calls": count, "accounted_cny_upper": round(cost, 10), "max_calls": self.max_calls, "max_cost_cny": self.max_cost_cny}

    def budget_status(self):
        with self._lock:
            return self._budget_unlocked(_utc_now().strftime("%Y-%m"))

    def complete(self, messages, model, *, tools=None, on_delta=None, role="conversation", request_id="", max_tokens=1400, json_mode=False, thinking=False):
        if not self.configured:
            raise ProviderError("未配置 DEEPSEEK_API_KEY，暂时无法生成回复。")
        if model not in PRICING:
            raise ProviderError("请选择受支持的 Flash 或 Pro 模型。")
        if not isinstance(messages, list) or not messages:
            raise ProviderError("对话内容为空，未发送模型请求。")
        if type(max_tokens) is not int or not 1 <= max_tokens <= 393216:
            raise ProviderError("本次回复长度配置无效，未发送模型请求。")
        payload = {"model": model, "messages": messages, "thinking": {"type": "enabled" if thinking else "disabled"}, "max_tokens": max_tokens, "stream": True, "stream_options": {"include_usage": True}}
        if thinking:
            payload["reasoning_effort"] = "low"
        if tools:
            payload["tools"] = tools
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (ValueError, TypeError):
            raise ProviderError("请求内容无法编码，未发送模型请求。") from None
        if len(serialized) > MAX_PAYLOAD_CHARS:
            raise ProviderError("本次上下文超过请求上限，请缩小需要读取的历史范围。")
        body = serialized.encode("utf-8")
        now = _utc_now()
        call_id = str(uuid4())
        directory = self.log_dir / call_id
        rates = PRICING[model]
        reserved = (len(body) * rates["cache_miss"] + max_tokens * rates["output"]) / 1_000_000
        result = {"message": {"role": "assistant", "content": ""}, "usage": None, "model": model, "requested_model": model, "elapsed_ms": 0.0, "first_token_ms": None, "call_id": call_id, "finish_reason": None, "estimated_cost_cny_upper": None}
        metadata = {"call_id": call_id, "request_id": str(request_id), "role": str(role), "timestamp_utc": now.isoformat(), "budget_month_utc": now.strftime("%Y-%m"), "endpoint": self.endpoint, "requested_model": model, "status": "reserved", "reserved_cost_cny_upper": reserved, "budget_accounted_cny": reserved, "price_source": PRICING_SOURCE, "price_verified_date": PRICING_VERIFIED, "price_basis": "peak_CNY_per_million_upper_estimate_not_bill", "rates": rates, "reservation_basis": "request_UTF8_bytes_as_input_token_upper_bound_and_max_tokens_output_no_cache_assumed", "raw_response_truncated": False}
        with self._lock:
            budget = self._budget_unlocked(metadata["budget_month_utc"])
            if self.max_calls is not None and budget["calls"] >= self.max_calls:
                raise ProviderError("本轮调用次数预算已用完，已停止新的模型请求。")
            if budget["accounted_cny_upper"] + reserved > self.max_cost_cny:
                raise ProviderError("本月费用预算不足以覆盖本次请求上界，已停止新的模型请求。")
            directory.mkdir()
            (directory / "request.json").write_bytes(body)
            _write_json(directory / "metadata.json", metadata)

        start = time.monotonic()
        deadline = start + CALL_TIMEOUT_SECONDS
        raw_file = None
        response = None
        timer = None
        tool_calls = {}
        done = False
        pending = b""
        event_data = []
        raw_count = 0
        safe_error = None
        status = "error"

        def check_deadline():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderError("模型响应超时，已保留收到的内容；此次请求不会自动重试。")
            return remaining

        def set_read_timeout():
            remaining = check_deadline()
            try:
                response.fp.raw._sock.settimeout(min(SOCKET_TIMEOUT_SECONDS, remaining))
            except AttributeError:
                pass

        def interrupt_socket():
            # Shutdown wakes a blocked read without waiting for buffered-I/O locks.
            try:
                response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
            except (AttributeError, OSError):
                pass

        def record(chunk):
            nonlocal raw_count
            allowed = MAX_RESPONSE_BYTES - raw_count
            raw_file.write(chunk[:allowed])
            raw_file.flush()
            raw_count += min(len(chunk), allowed)
            if len(chunk) > allowed:
                metadata["raw_response_truncated"] = True
                raise ProviderError("模型响应超过单次接收上限，已停止接收并保留已有内容。")

        def process_event(data):
            nonlocal done
            if data == b"[DONE]":
                done = True
                return
            if done:
                raise ProviderError("模型流在结束后仍返回数据，已保留已有内容供检查。")
            try:
                chunk = json.loads(data.decode("utf-8"))
                if not isinstance(chunk, dict) or "error" in chunk:
                    raise ValueError("invalid stream event")
                if isinstance(chunk.get("model"), str):
                    result["model"] = chunk["model"]
                if isinstance(chunk.get("usage"), dict):
                    result["usage"] = chunk["usage"]
                choices = chunk.get("choices", [])
                if not isinstance(choices, list) or len(choices) > 1:
                    raise ValueError("invalid choice list")
                for choice in choices:
                    if choice.get("index", 0) != 0:
                        raise ValueError("unsupported choice")
                    finish = choice.get("finish_reason")
                    if finish is not None:
                        result["finish_reason"] = finish
                    delta = choice.get("delta") or {}
                    reasoning = delta.get("reasoning_content")
                    if reasoning is not None and not isinstance(reasoning, str):
                        raise ValueError("invalid reasoning content")
                    if thinking and reasoning:
                        result["message"]["reasoning_content"] = result["message"].get("reasoning_content", "") + reasoning
                    content = delta.get("content")
                    if content is not None and not isinstance(content, str):
                        raise ValueError("invalid content")
                    if content:
                        result["message"]["content"] += content
                        if result["first_token_ms"] is None and content.strip():
                            result["first_token_ms"] = round((time.monotonic() - start) * 1000, 3)
                        if on_delta is not None:
                            on_delta(content)
                    for fragment in delta.get("tool_calls") or []:
                        index = fragment["index"]
                        if type(index) is not int or not 0 <= index < 128:
                            raise ValueError("invalid tool index")
                        call = tool_calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                        if fragment.get("type") not in (None, "function"):
                            raise ValueError("invalid tool type")
                        for key in ("id",):
                            if fragment.get(key) is not None:
                                call[key] += fragment[key]
                        function = fragment.get("function") or {}
                        for key in ("name", "arguments"):
                            if function.get(key) is not None:
                                call["function"][key] += function[key]
                    if tool_calls:
                        result["message"]["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
            except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
                raise ProviderError("模型返回了无法解析的数据，已保留已有内容；此次请求不会自动重试。") from None

        try:
            raw_file = (directory / "response.sse").open("wb")
            request = Request(self.endpoint, data=body, headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json", "Accept": "text/event-stream"}, method="POST")
            timer = threading.Timer(CALL_TIMEOUT_SECONDS, interrupt_socket)
            timer.daemon = True
            timer.start()
            try:
                response = urlopen(request, timeout=min(SOCKET_TIMEOUT_SECONDS, check_deadline()))
            except HTTPError as error:
                response = error
                metadata["http_status"] = error.code
                while True:
                    set_read_timeout()
                    chunk = response.read1(min(8192, MAX_RESPONSE_BYTES - raw_count + 1))
                    if not chunk:
                        break
                    record(chunk)
                raise ProviderError(f"模型服务返回 HTTP {error.code}，此次请求未自动重试。") from None
            metadata["http_status"] = response.status
            while not done:
                set_read_timeout()
                chunk = response.read1(min(8192, MAX_RESPONSE_BYTES - raw_count + 1))
                check_deadline()
                if not chunk:
                    break
                record(chunk)
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    line = line.removesuffix(b"\r")
                    if not line:
                        if event_data:
                            process_event(b"\n".join(event_data))
                            event_data.clear()
                    elif line.startswith(b"data:"):
                        event_data.append(line[5:].removeprefix(b" "))
            if not done:
                raise ProviderError("模型响应中途结束，未收到完整结束标记；已保留已有内容，请勿视为完整回答。")
            finish = result["finish_reason"]
            if finish == "length":
                raise ProviderError("回复达到长度上限，已有内容已保留，但本次回答尚未完成。")
            if finish == "content_filter":
                raise ProviderError("模型服务未完整返回此次内容，已有内容已保留。")
            if finish not in ("stop", "tool_calls"):
                raise ProviderError("模型未正常完成回复，已有内容已保留；此次请求不会自动重试。")
            if finish == "tool_calls" and (not tool_calls or any(not call["id"] or not call["function"]["name"] for call in tool_calls.values())):
                raise ProviderError("模型返回的工具请求不完整，未执行工具操作。")
            if finish == "stop" and not result["message"]["content"].strip():
                raise ProviderError("模型未返回可见正文，请稍后重新发送。")
            status = "success"
        except ProviderError as error:
            safe_error = str(error)
        except (TimeoutError, socket.timeout):
            safe_error = "模型响应超时，已保留收到的内容；此次请求不会自动重试。"
        except (URLError, OSError):
            safe_error = "模型连接中断，已保留收到的内容；此次请求不会自动重试。"
        except Exception:
            # Callback failures and provider internals never expose credentials,
            # exception reprs or upstream response bodies to the user.
            safe_error = "本次模型请求未能完成，已保留调用日志；此次请求不会自动重试。"
        finally:
            if timer:
                timer.cancel()
            if response is not None:
                try:
                    response.close()
                except OSError:
                    pass
            if raw_file:
                raw_file.close()
            result["elapsed_ms"] = round((time.monotonic() - start) * 1000, 3)
            result["estimated_cost_cny_upper"] = estimate_cost_upper(model, result["usage"])
            metadata.update(result)
            metadata.update(usage_fields(result["usage"]))
            metadata.update({"finished_at_utc": _utc_now().isoformat(), "status": status, "error": safe_error, "raw_response_bytes": raw_count, "received_done": done, "budget_accounted_cny": result["estimated_cost_cny_upper"] if result["estimated_cost_cny_upper"] is not None else reserved})
            with self._lock:
                _write_json(directory / "metadata.json", metadata)
        if safe_error:
            raise ProviderError(safe_error, result=result) from None
        return result
