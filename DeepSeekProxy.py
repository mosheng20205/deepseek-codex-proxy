import json
import os
import re
import sys
import time
import uuid

from collections.abc import AsyncGenerator

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

app = FastAPI()

# Shared httpx client for connection reuse (avoids TCP/TLS handshake per request)
_http_client: httpx.AsyncClient | None = None


async def _get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(180.0),
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
        )
    return _http_client


def _get_env(key: str, default: str = "") -> str:
    """Read env var, falling back to Windows registry (HKCU, then HKLM)."""
    value = os.getenv(key, "")
    if value:
        return value
    if sys.platform == "win32":
        try:
            import winreg
            for root, subkey in (
                (winreg.HKEY_CURRENT_USER, r"Environment"),
                (winreg.HKEY_LOCAL_MACHINE,
                 r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
            ):
                try:
                    with winreg.OpenKey(root, subkey) as reg_key:
                        value, _ = winreg.QueryValueEx(reg_key, key)
                        if value:
                            return value
                except OSError:
                    continue
        except Exception:
            pass
    return default


# --- Config ---
DEEPSEEK_API_KEY = _get_env("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = _get_env("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
TARGET_MODEL = _get_env("DEEPSEEK_MODEL", "deepseek-v4-pro")
DEEPSEEK_THINKING = _get_env("DEEPSEEK_THINKING", "disabled").strip().lower()

HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade",
}

STOP_REASON_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "stop_sequence",
}


# ============================================================
# Helpers
# ============================================================

def _filter_request_headers(headers: dict) -> dict:
    filtered = {}
    for k, v in headers.items():
        key = k.lower()
        if key in HOP_BY_HOP_HEADERS or key in {"host", "content-length"}:
            continue
        filtered[key] = v
    return filtered


def _filter_response_headers(headers: dict) -> dict:
    filtered = {}
    for k, v in headers.items():
        key = k.lower()
        if key in HOP_BY_HOP_HEADERS or key == "content-length":
            continue
        filtered[k] = v
    return filtered


def normalize_model(name: str) -> str:
    """Map Codex/OpenAI model names to the configured DeepSeek model."""
    clean = re.sub(r"\[\d+[km]\]$", "", (name or "").strip())
    if clean.startswith("deepseek-"):
        return clean
    return re.sub(r"\[\d+[km]\]$", "", TARGET_MODEL.strip()) or "deepseek-v4-pro"


def _repair_tool_message_chain(messages: list[dict]) -> list[dict]:
    """Make Chat Completions tool-call history valid before forwarding."""
    consumed_tool_indexes = set()
    repaired = []

    for index, msg in enumerate(messages):
        if index in consumed_tool_indexes:
            continue
        if msg.get("role") == "tool":
            continue

        tool_calls = msg.get("tool_calls") or []
        if msg.get("role") != "assistant" or not tool_calls:
            repaired.append(msg)
            continue

        required_ids = [
            tc.get("id")
            for tc in tool_calls
            if isinstance(tc, dict) and tc.get("id")
        ]
        matching_tools = []
        for call_id in required_ids:
            for tool_index, candidate in enumerate(messages[index + 1:], start=index + 1):
                if tool_index in consumed_tool_indexes:
                    continue
                if (
                    candidate.get("role") == "tool"
                    and candidate.get("tool_call_id") == call_id
                ):
                    matching_tools.append(candidate)
                    consumed_tool_indexes.add(tool_index)
                    break

        if len(matching_tools) == len(required_ids):
            repaired.append(msg)
            repaired.extend(matching_tools)
            continue

        content = msg.get("content")
        if content:
            repaired.append({"role": "assistant", "content": content})

    return repaired


# ============================================================
# Request-type detection
# ============================================================

def detect_request_type(path: str, headers: dict) -> str:
    """Return 'anthropic' | 'responses' | 'openai'.

    - Anthropic:   ``/v1/messages``  OR  ``anthropic-version`` header present
    - Responses:   ``/v1/responses``  WITHOUT  ``anthropic-version`` header
    - OpenAI:      everything else (Chat Completions, embeddings, etc.)
    """
    clean = path.strip("/")
    if clean == "v1/messages":
        return "anthropic"
    if clean == "v1/responses":
        for h in headers:
            if h.lower() == "anthropic-version":
                return "anthropic"
        return "responses"
    return "openai"


# ============================================================
# Anthropic helpers (reused from earlier)
# ============================================================

def _extract_text(block) -> str:
    if isinstance(block, str):
        return block
    if isinstance(block, list):
        return "\n".join(_extract_text(b) for b in block)
    if isinstance(block, dict):
        if block.get("type") == "text":
            return block.get("text", "")
        if block.get("type") in ("tool_use", "tool_result"):
            return json.dumps(block, ensure_ascii=False)
        if "text" in block:
            return block["text"]
    return str(block)


def _content_to_openai(content):
    if isinstance(content, str):
        return {"content": content}
    text_parts = []
    tool_calls = []
    blocks = content if isinstance(content, list) else [content]
    for block in blocks:
        if not isinstance(block, dict):
            text_parts.append(str(block))
            continue
        t = block.get("type", "")
        if t == "text":
            text_parts.append(block.get("text", ""))
        elif t == "tool_use":
            tool_calls.append({
                "id": block.get("id", ""),
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
                    "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                },
            })
        elif t == "tool_result":
            text_parts.append(json.dumps(block, ensure_ascii=False))
    result: dict = {}
    if text_parts:
        result["content"] = "\n".join(text_parts)
    if tool_calls:
        result["tool_calls"] = tool_calls
    return result


# ============================================================
#  MODE 1:  Anthropic Messages  <->  Chat Completions
# ============================================================

def anthropic_to_chat(body: dict) -> dict:
    """Anthropic Messages/Responses request -> Chat Completions."""
    model = normalize_model(body.get("model", "") or TARGET_MODEL)

    # system / instructions
    system_text = ""
    raw_system = body.get("system", "")
    if isinstance(raw_system, list):
        system_text = "\n".join(
            b.get("text", "") for b in raw_system if isinstance(b, dict) and b.get("type") == "text"
        )
    elif isinstance(raw_system, str):
        system_text = raw_system
    if not system_text:
        instr = body.get("instructions", "")
        system_text = instr if isinstance(instr, str) else ""

    anthropic_msgs = body.get("messages") or body.get("input") or []
    oai_msgs = []
    if system_text:
        oai_msgs.append({"role": "system", "content": system_text})

    for msg in anthropic_msgs:
        role = msg.get("role", "user")
        converted = _content_to_openai(msg.get("content", ""))
        oai_msg = {"role": role}
        if "content" in converted:
            oai_msg["content"] = converted["content"]
        if "tool_calls" in converted:
            oai_msg["tool_calls"] = converted["tool_calls"]
        oai_msgs.append(oai_msg)

    oai = {"model": model, "messages": oai_msgs}

    max_tokens = body.get("max_tokens") or body.get("max_output_tokens")
    if max_tokens:
        oai["max_tokens"] = max_tokens
    if body.get("stream"):
        oai["stream"] = True
    if body.get("temperature") is not None:
        oai["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        oai["top_p"] = body["top_p"]
    if body.get("stop_sequences"):
        oai["stop"] = body["stop_sequences"]

    anthropic_tools = body.get("tools") or []
    if anthropic_tools:
        oai_tools = []
        for tool in anthropic_tools:
            if isinstance(tool, dict):
                oai_tools.append({
                    "type": "function",
                    "function": {
                        "name": tool.get("name", ""),
                        "description": tool.get("description", ""),
                        "parameters": tool.get("input_schema", {}),
                    },
                })
        if oai_tools:
            oai["tools"] = oai_tools

    oai["messages"] = _repair_tool_message_chain(oai["messages"])
    return oai


def chat_to_anthropic(chat_resp: dict, model: str) -> dict:
    """Chat Completions response -> Anthropic Messages response."""
    choice = chat_resp.get("choices", [{}])[0]
    message = choice.get("message", {})
    finish_reason = choice.get("finish_reason", "stop")
    usage = chat_resp.get("usage", {})

    content = []
    if message.get("content"):
        content.append({"type": "text", "text": message["content"]})
    for tc in message.get("tool_calls") or []:
        func = tc.get("function", {})
        try:
            inp = json.loads(func.get("arguments", "{}"))
        except (json.JSONDecodeError, TypeError):
            inp = {}
        content.append({
            "type": "tool_use",
            "id": tc.get("id", ""),
            "name": func.get("name", ""),
            "input": inp,
        })

    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": STOP_REASON_MAP.get(finish_reason, finish_reason),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


# --- Anthropic streaming translator ---

class AnthropicStreamTranslator:
    """Chat Completions SSE -> Anthropic SSE (with thinking blocks)."""

    def __init__(self, model: str):
        self.model = model
        self.msg_id = f"msg_{uuid.uuid4().hex[:24]}"
        self._started = False
        self._finished = False
        self._content_idx = -1
        self._block_type: str | None = None  # 'thinking' | 'text'

    @property
    def finished(self) -> bool:
        return self._finished

    def feed(self, chunk_data: dict) -> list[str]:
        if self._finished:
            return []
        events: list[str] = []
        choices = chunk_data.get("choices") or []
        choice = choices[0] if choices else {}
        delta = choice.get("delta") or {}
        finish = choice.get("finish_reason")
        usage = chunk_data.get("usage") or {}

        if not self._started:
            self._started = True
            events.append(_sse("message_start", {
                "type": "message_start",
                "message": {
                    "id": self.msg_id, "type": "message", "role": "assistant",
                    "model": self.model, "content": [],
                    "stop_reason": None, "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            }))

        rc = delta.get("reasoning_content")
        if rc:
            if self._block_type != "thinking":
                self._content_idx += 1
                self._block_type = "thinking"
                events.append(_sse("content_block_start", {
                    "type": "content_block_start",
                    "index": self._content_idx,
                    "content_block": {"type": "thinking", "thinking": ""},
                }))
            events.append(_sse("content_block_delta", {
                "type": "content_block_delta",
                "index": self._content_idx,
                "delta": {"type": "thinking_delta", "thinking": rc},
            }))

        c = delta.get("content")
        if c:
            if self._block_type != "text":
                self._content_idx += 1
                self._block_type = "text"
                events.append(_sse("content_block_start", {
                    "type": "content_block_start",
                    "index": self._content_idx,
                    "content_block": {"type": "text", "text": ""},
                }))
            events.append(_sse("content_block_delta", {
                "type": "content_block_delta",
                "index": self._content_idx,
                "delta": {"type": "text_delta", "text": c},
            }))

        if finish:
            sr = STOP_REASON_MAP.get(finish, finish)
            events.append(_sse("message_delta", {
                "type": "message_delta",
                "delta": {"stop_reason": sr, "stop_sequence": None},
                "usage": {
                    "input_tokens": usage.get("prompt_tokens", 0),
                    "output_tokens": usage.get("completion_tokens", 0),
                },
            }))
            events.append(_sse("message_stop", {"type": "message_stop"}))
            self._finished = True

        return events


# ============================================================
#  MODE 2:  OpenAI Responses API  <->  Chat Completions
# ============================================================

def responses_to_chat(body: dict) -> dict:
    """OpenAI Responses API request -> Chat Completions request."""
    model = normalize_model(body.get("model", "") or TARGET_MODEL)

    oai_msgs = []

    # instructions -> system message
    instructions = body.get("instructions", "")
    if instructions:
        oai_msgs.append({"role": "system", "content": instructions})

    raw_input = body.get("input") or []
    if isinstance(raw_input, str):
        raw_input = [{"role": "user", "content": raw_input}]

    # input -> messages
    for item in raw_input:
        if isinstance(item, str):
            oai_msgs.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            continue

        item_type = item.get("type", "")
        if item_type == "function_call":
            call_id = item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:24]}"
            oai_msgs.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", "{}"),
                    },
                }],
            })
            continue

        if item_type == "function_call_output":
            call_id = item.get("call_id") or item.get("id") or item.get("tool_call_id")
            output = item.get("output", "")
            if not isinstance(output, str):
                output = json.dumps(output, ensure_ascii=False)
            oai_msgs.append({
                "role": "tool",
                "tool_call_id": call_id or f"call_{uuid.uuid4().hex[:24]}",
                "content": output,
            })
            continue

        role = item.get("role", "")
        # Skip items with null/empty role or null content (Codex placeholders)
        if not role or item.get("content") is None:
            continue
        # DeepSeek doesn't support "developer" role; map to "system"
        if role == "developer":
            role = "system"
        content = item.get("content", "")
        if isinstance(content, list):
            # Responses API content format: [{type: "input_text", text: "..."}]
            text_parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") in ("input_text", "output_text", "text"):
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "input_image":
                        text_parts.append("[image]")
                    else:
                        text_parts.append(json.dumps(block, ensure_ascii=False))
                else:
                    text_parts.append(str(block))
            content = "\n".join(text_parts)
        oai_msgs.append({"role": role, "content": content})

    # Merge consecutive system messages into one (DeepSeek compat)
    merged_msgs = []
    for msg in oai_msgs:
        if msg["role"] == "system" and merged_msgs and merged_msgs[-1]["role"] == "system":
            merged_msgs[-1]["content"] += "\n\n" + msg["content"]
        else:
            merged_msgs.append(msg)
    oai_msgs = merged_msgs

    oai = {"model": model, "messages": oai_msgs}

    max_tokens = body.get("max_output_tokens")
    if max_tokens:
        oai["max_tokens"] = max_tokens
    else:
        oai["max_tokens"] = 65536  # high default: reasoning model needs room for thinking + output

    if body.get("stream"):
        oai["stream"] = True

    if body.get("temperature") is not None:
        oai["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        oai["top_p"] = body["top_p"]

    if DEEPSEEK_THINKING in {"enabled", "disabled"}:
        oai["thinking"] = {"type": DEEPSEEK_THINKING}

    tools = []
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function":
            function_def = {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "parameters": tool.get("parameters", {}),
            }
            if tool.get("strict") is not None:
                function_def["strict"] = tool["strict"]
            tools.append({"type": "function", "function": function_def})
        elif tool.get("type") == "namespace":
            namespace = tool.get("name", "").rstrip("_")
            for nested in tool.get("tools") or []:
                if not isinstance(nested, dict) or nested.get("type") != "function":
                    continue
                nested_name = nested.get("name", "")
                if not namespace or not nested_name:
                    continue
                function_def = {
                    "name": f"{namespace}__{nested_name}",
                    "description": nested.get("description", ""),
                    "parameters": nested.get("parameters", {}),
                }
                if nested.get("strict") is not None:
                    function_def["strict"] = nested["strict"]
                tools.append({"type": "function", "function": function_def})
    if tools:
        oai["tools"] = tools
        if body.get("tool_choice"):
            oai["tool_choice"] = body["tool_choice"]
        if body.get("parallel_tool_calls") is not None:
            oai["parallel_tool_calls"] = body["parallel_tool_calls"]

    oai["messages"] = _repair_tool_message_chain(oai["messages"])
    return oai


def chat_to_responses(chat_resp: dict, model: str) -> dict:
    """Chat Completions response -> OpenAI Responses API response."""
    choice = chat_resp.get("choices", [{}])[0]
    message = choice.get("message", {})
    usage = chat_resp.get("usage", {})

    resp_id = f"resp_{uuid.uuid4().hex[:24]}"
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"

    output = []
    if message.get("content"):
        output.append({
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": message["content"]}],
        })

    for tc in message.get("tool_calls") or []:
        function_def = tc.get("function", {})
        output.append({
            "id": f"fc_{uuid.uuid4().hex[:24]}",
            "type": "function_call",
            "call_id": tc.get("id", f"call_{uuid.uuid4().hex[:24]}"),
            "name": function_def.get("name", ""),
            "arguments": function_def.get("arguments", "{}"),
            "status": "completed",
        })

    return {
        "id": resp_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": model,
        "output": output,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
    }


# --- Responses API streaming translator ---

class ResponsesStreamTranslator:
    """Chat Completions SSE -> OpenAI Responses API SSE."""

    def __init__(self, model: str):
        self.model = model
        self.resp_id = f"resp_{uuid.uuid4().hex[:24]}"
        self.msg_id = f"msg_{uuid.uuid4().hex[:24]}"
        self._started = False
        self._finished = False
        self._reasoning_item_added = False
        self._reasoning_id = f"rs_{uuid.uuid4().hex[:16]}"
        self._text_item_added = False
        self._text_parts: list[str] = []
        self._tool_calls: dict[int, dict] = {}
        self._output_items: list[dict] = []

    @property
    def finished(self) -> bool:
        return self._finished

    def feed(self, chunk_data: dict) -> list[str]:
        if self._finished:
            return []

        events: list[str] = []
        choices = chunk_data.get("choices") or []
        choice = choices[0] if choices else {}
        delta = choice.get("delta") or {}
        finish = choice.get("finish_reason")
        usage = chunk_data.get("usage") or {}

        # --- first chunk: response.created ---
        if not self._started:
            self._started = True
            events.append(_sse("response.created", {
                "type": "response.created",
                "response": {
                    "id": self.resp_id,
                    "object": "response",
                    "created_at": int(time.time()),
                    "status": "in_progress",
                    "model": self.model,
                    "output": [],
                },
            }))
            events.append(_sse("response.in_progress", {
                "type": "response.in_progress",
                "response": {
                    "id": self.resp_id,
                    "object": "response",
                    "created_at": int(time.time()),
                    "status": "in_progress",
                    "model": self.model,
                    "output": [],
                },
            }))

        # --- reasoning_content -> reasoning item ---
        rc = delta.get("reasoning_content")
        if rc:
            if not self._reasoning_item_added:
                self._reasoning_item_added = True
                self._output_items.append({
                    "id": self._reasoning_id,
                    "type": "reasoning",
                    "status": "in_progress",
                    "summary": [],
                })
                events.append(_sse("response.output_item.added", {
                    "type": "response.output_item.added",
                    "output_index": len(self._output_items) - 1,
                    "item": self._output_items[-1],
                }))

        # --- content (text) -> output_text events ---
        c = delta.get("content")
        if c:
            if not self._text_item_added:
                self._text_item_added = True
                self._output_items.append({
                    "id": self.msg_id,
                    "type": "message",
                    "role": "assistant",
                    "status": "in_progress",
                    "content": [],
                })
                msg_output_index = len(self._output_items) - 1
                events.append(_sse("response.output_item.added", {
                    "type": "response.output_item.added",
                    "output_index": msg_output_index,
                    "item": self._output_items[msg_output_index],
                }))
                events.append(_sse("response.content_part.added", {
                    "type": "response.content_part.added",
                    "item_id": self.msg_id,
                    "output_index": msg_output_index,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": ""},
                }))

            self._text_parts.append(c)
            msg_output_index = self._find_output_index(self.msg_id)
            events.append(_sse("response.output_text.delta", {
                "type": "response.output_text.delta",
                "item_id": self.msg_id,
                "output_index": msg_output_index,
                "content_index": 0,
                "delta": c,
            }))

        for tool_call in delta.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            index = tool_call.get("index", 0)
            state = self._tool_calls.setdefault(index, {
                "item_id": f"fc_{uuid.uuid4().hex[:24]}",
                "call_id": tool_call.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                "name": "",
                "arguments": "",
                "added": False,
            })
            if tool_call.get("id"):
                state["call_id"] = tool_call["id"]
            function_delta = tool_call.get("function") or {}
            if function_delta.get("name"):
                state["name"] += function_delta["name"]
            arguments_delta = function_delta.get("arguments") or ""

            if not state["added"]:
                state["added"] = True
                self._output_items.append({
                    "id": state["item_id"],
                    "type": "function_call",
                    "call_id": state["call_id"],
                    "name": state["name"],
                    "arguments": "",
                    "status": "in_progress",
                })
                events.append(_sse("response.output_item.added", {
                    "type": "response.output_item.added",
                    "output_index": len(self._output_items) - 1,
                    "item": self._output_items[-1],
                }))

            if arguments_delta:
                state["arguments"] += arguments_delta
                events.append(_sse("response.function_call_arguments.delta", {
                    "type": "response.function_call_arguments.delta",
                    "item_id": state["item_id"],
                    "output_index": self._find_output_index(state["item_id"]),
                    "delta": arguments_delta,
                }))

        # --- finish ---
        if finish:
            full_text = "".join(self._text_parts)

            if self._text_item_added:
                msg_output_index = self._find_output_index(self.msg_id)
                events.append(_sse("response.output_text.done", {
                    "type": "response.output_text.done",
                    "item_id": self.msg_id,
                    "output_index": msg_output_index,
                    "content_index": 0,
                    "text": full_text,
                }))
                events.append(_sse("response.content_part.done", {
                    "type": "response.content_part.done",
                    "item_id": self.msg_id,
                    "output_index": msg_output_index,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": full_text},
                }))
                msg_output = {
                    "id": self.msg_id,
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": full_text}],
                }
                events.append(_sse("response.output_item.done", {
                    "type": "response.output_item.done",
                    "output_index": msg_output_index,
                    "item": msg_output,
                }))
                self._output_items[msg_output_index] = msg_output

            # Reasoning item done
            if self._reasoning_item_added:
                reasoning_output_index = self._find_output_index(self._reasoning_id)
                reasoning_output = {
                    "id": self._reasoning_id,
                    "type": "reasoning",
                    "status": "completed",
                    "summary": [{"type": "summary_text", "text": ""}],
                }
                events.append(_sse("response.output_item.done", {
                    "type": "response.output_item.done",
                    "output_index": reasoning_output_index,
                    "item": reasoning_output,
                }))
                self._output_items[reasoning_output_index] = reasoning_output

            for state in self._tool_calls.values():
                output_index = self._find_output_index(state["item_id"])
                tool_output = {
                    "id": state["item_id"],
                    "type": "function_call",
                    "call_id": state["call_id"],
                    "name": state["name"],
                    "arguments": state["arguments"],
                    "status": "completed",
                }
                events.append(_sse("response.function_call_arguments.done", {
                    "type": "response.function_call_arguments.done",
                    "item_id": state["item_id"],
                    "output_index": output_index,
                    "arguments": state["arguments"],
                }))
                events.append(_sse("response.output_item.done", {
                    "type": "response.output_item.done",
                    "output_index": output_index,
                    "item": tool_output,
                }))
                self._output_items[output_index] = tool_output

            events.append(_sse("response.completed", {
                "type": "response.completed",
                "response": {
                    "id": self.resp_id,
                    "object": "response",
                    "created_at": int(time.time()),
                    "status": "completed",
                    "model": self.model,
                    "output": self._output_items,
                    "usage": {
                        "input_tokens": usage.get("prompt_tokens", 0),
                        "output_tokens": usage.get("completion_tokens", 0),
                        "total_tokens": usage.get("total_tokens", 0),
                    },
                },
            }))
            self._finished = True

        return events

    def _find_output_index(self, item_id: str) -> int:
        for idx, item in enumerate(self._output_items):
            if item.get("id") == item_id:
                return idx
        return max(0, len(self._output_items) - 1)


# ============================================================
# SSE helpers
# ============================================================

def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _parse_sse_stream(upstream) -> "AsyncGenerator[dict, None]":
    """Parse an OpenAI Chat Completions SSE stream, yielding JSON chunks."""
    leftover = ""
    async for raw_chunk in upstream.aiter_raw():
        text = raw_chunk.decode("utf-8", errors="replace")
        leftover += text
        while "\n" in leftover:
            line, leftover = leftover.split("\n", 1)
            line = line.strip()
            if not line or not line.startswith("data: "):
                continue
            payload = line[6:]
            if payload == "[DONE]":
                return
            try:
                yield json.loads(payload)
            except json.JSONDecodeError:
                continue


# ============================================================
# Main proxy route
# ============================================================

# ============================================================
# Model metadata endpoints (for Codex model lookup)
# ============================================================

@app.api_route("/v1/models", methods=["GET", "OPTIONS"])
async def list_models():
    """Return supported models list with metadata Codex needs."""
    return {
        "object": "list",
        "data": [
            {
                "id": "deepseek-v4-pro",
                "object": "model",
                "created": 1735689600,
                "owned_by": "deepseek",
            },
            {
                "id": "deepseek-v4-flash",
                "object": "model",
                "created": 1735689600,
                "owned_by": "deepseek",
            },
        ],
    }


@app.api_route("/v1/models/{model_id:path}", methods=["GET", "OPTIONS"])
async def get_model(model_id: str):
    """Return per-model metadata."""
    model = normalize_model(model_id)
    return {
        "id": model,
        "object": "model",
        "created": 1735689600,
        "owned_by": "deepseek",
    }


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def proxy(request: Request, path: str):
    if not DEEPSEEK_API_KEY:
        raise HTTPException(status_code=500, detail="DEEPSEEK_API_KEY is not set")

    req_headers = _filter_request_headers(dict(request.headers))
    content = await request.body()
    req_type = detect_request_type(path, dict(request.headers))

    # Quick log for debugging Codex requests
    ct = dict(request.headers).get("content-type", "")
    # print(f"[REQ] {request.method} /{path}  type={req_type}  len={len(content)}", flush=True)

    # ================================================================
    #  MODE 1: Anthropic -> Chat Completions -> Anthropic
    # ================================================================
    if req_type == "anthropic":
        try:
            anthropic_body = json.loads(content.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(status_code=400, detail="Invalid JSON in Anthropic request")

        chat_body = anthropic_to_chat(anthropic_body)
        is_stream = chat_body.get("stream", False)
        model = chat_body["model"]

        upstream_url = f"{DEEPSEEK_BASE_URL}/v1/chat/completions"
        fwd_headers = {
            "authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "content-type": "application/json",
        }

        client = await _get_client()
        try:
            upstream = await client.send(
                client.build_request(
                    method="POST", url=upstream_url, headers=fwd_headers,
                    content=json.dumps(chat_body, ensure_ascii=False).encode("utf-8"),
                ),
                stream=True,
            )
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Upstream Error: {str(e)}")

        async def _cleanup():
            await upstream.aclose()

        if is_stream:
            async def _anthropic_stream():
                translator = AnthropicStreamTranslator(model)
                async for chunk in _parse_sse_stream(upstream):
                    for evt in translator.feed(chunk):
                        yield evt.encode("utf-8")
                if not translator.finished:
                    yield _sse("message_stop", {"type": "message_stop"}).encode("utf-8")
                await _cleanup()

            return StreamingResponse(
                _anthropic_stream(),
                status_code=upstream.status_code,
                headers={
                    "content-type": "text/event-stream",
                    "cache-control": "no-cache",
                    "connection": "keep-alive",
                    "x-request-id": str(uuid.uuid4()),
                },
            )
        else:
            raw = b""
            async for chunk in upstream.aiter_raw():
                raw += chunk
            await _cleanup()

            try:
                chat_resp = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return Response(content=raw, status_code=upstream.status_code)
            anthropic_resp = chat_to_anthropic(chat_resp, model)
            return Response(
                content=json.dumps(anthropic_resp, ensure_ascii=False).encode("utf-8"),
                status_code=200, media_type="application/json",
            )

    # ================================================================
    #  MODE 2: OpenAI Responses API -> Chat Completions -> Responses
    # ================================================================
    if req_type == "responses":
        try:
            raw_text = content.decode("utf-8")
            responses_body = json.loads(raw_text)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Log the failing content so we can debug
            print(f"[RESPONSES 400] raw content: {content[:2000]!r}", flush=True)
            raise HTTPException(status_code=400, detail="Invalid JSON in Responses request")
        chat_body = responses_to_chat(responses_body)
        is_stream = chat_body.get("stream", False)
        model = chat_body["model"]

        upstream_url = f"{DEEPSEEK_BASE_URL}/v1/chat/completions"
        fwd_headers = {
            "authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "content-type": "application/json",
        }

        client = await _get_client()
        try:
            upstream = await client.send(
                client.build_request(
                    method="POST", url=upstream_url, headers=fwd_headers,
                    content=json.dumps(chat_body, ensure_ascii=False).encode("utf-8"),
                ),
                stream=True,
            )
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Upstream Error: {str(e)}")

        async def _cleanup2():
            await upstream.aclose()

        if is_stream:
            if upstream.status_code >= 400:
                # Read and log the error body from DeepSeek
                err_body = b""
                async for chunk in upstream.aiter_raw():
                    err_body += chunk
                print(f"[UPSTREAM ERROR] status={upstream.status_code} body={err_body[:2000]!r}", flush=True)
                await _cleanup2()
                return Response(content=err_body, status_code=upstream.status_code,
                                media_type="application/json")

            async def _responses_stream():
                translator = ResponsesStreamTranslator(model)
                async for chunk in _parse_sse_stream(upstream):
                    for evt in translator.feed(chunk):
                        yield evt.encode("utf-8")
                if not translator.finished:
                    # force finish
                    pass
                await _cleanup2()

            return StreamingResponse(
                _responses_stream(),
                status_code=upstream.status_code,
                headers={
                    "content-type": "text/event-stream",
                    "cache-control": "no-cache",
                    "connection": "keep-alive",
                    "x-request-id": str(uuid.uuid4()),
                },
            )
        else:
            raw = b""
            async for chunk in upstream.aiter_raw():
                raw += chunk
            await _cleanup2()

            if upstream.status_code >= 400:
                print(f"[UPSTREAM ERROR] status={upstream.status_code} body={raw[:2000]!r}", flush=True)
                return Response(content=raw, status_code=upstream.status_code,
                                media_type="application/json")

            try:
                chat_resp = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return Response(content=raw, status_code=upstream.status_code)
            responses_resp = chat_to_responses(chat_resp, model)
            return Response(
                content=json.dumps(responses_resp, ensure_ascii=False).encode("utf-8"),
                status_code=200, media_type="application/json",
            )

    # ================================================================
    #  MODE 3: OpenAI passthrough  (Chat Completions, embeddings, …)
    # ================================================================
    url = f"{DEEPSEEK_BASE_URL}/{path.lstrip('/')}"
    req_headers["authorization"] = f"Bearer {DEEPSEEK_API_KEY}"

    body = None
    if content and path.endswith("chat/completions"):
        try:
            data = json.loads(content.decode("utf-8"))
            if "messages" in data:
                for msg in data["messages"]:
                    if msg.get("role") == "developer":
                        msg["role"] = "system"
                data["messages"] = _repair_tool_message_chain(data["messages"])
            data["model"] = normalize_model(data.get("model", "") or TARGET_MODEL)
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = content
    elif content:
        body = content

    client = await _get_client()
    req = client.build_request(
        method=request.method, url=url, headers=req_headers,
        content=body, params=request.query_params,
    )
    try:
        upstream = await client.send(req, stream=True)
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Upstream Error: {str(e)}")

    async def _cleanup3():
        await upstream.aclose()

    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        headers=_filter_response_headers(dict(upstream.headers)),
        background=BackgroundTask(_cleanup3),
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=3000)
