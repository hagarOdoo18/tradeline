"""Bounded SSE decoding. No Odoo state, credentials, or executable tool code."""
import json
import time

from .openai_responses import normalize_response


def provider_events(response, deadline):
    response.encoding = "utf-8"
    data = []
    size = 0
    for line in response.iter_lines(chunk_size=1, decode_unicode=True):
        if time.monotonic() > deadline:
            raise TimeoutError("The response time budget was reached.")
        if isinstance(line, bytes):
            line = line.decode("utf-8")
        size += len(line)
        if size > 2_000_000:
            raise ValueError("Provider stream exceeded the response limit.")
        if not line:
            if data:
                yield "\n".join(data)
                data = []
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        yield "\n".join(data)


def collect_provider_stream(response, provider, deadline):
    """Yield text deltas; return the reconstructed native provider message."""
    content, partial_inputs, tool_calls = {}, {}, {}
    text, finish, complete = "", None, False
    for raw in provider_events(response, deadline):
        if raw == "[DONE]":
            complete = True
            break
        event = json.loads(raw)
        if event.get("error") or event.get("type") == "error":
            raise ValueError("The provider reported a streaming error.")
        if provider == "openai_responses":
            kind = event.get("type")
            if kind in {"response.output_text.delta", "response.refusal.delta"}:
                yield {"type": "delta", "text": event.get("delta", "")}
            elif kind in {"response.completed", "response.incomplete"}:
                return normalize_response(event["response"])
            elif kind in {"response.failed", "response.cancelled"}:
                raise ValueError("The provider response did not complete.")
            continue
        if provider == "claude":
            kind = event.get("type")
            index = event.get("index", 0)
            if kind == "content_block_start":
                block = dict(event["content_block"])
                content[index] = block
                if block.get("type") == "text" and block.get("text"):
                    yield {"type": "delta", "text": block["text"]}
            elif kind == "content_block_delta":
                delta = event["delta"]
                block = content[index]
                if delta["type"] == "text_delta":
                    chunk = delta.get("text", "")
                    block["text"] = block.get("text", "") + chunk
                    yield {"type": "delta", "text": chunk}
                elif delta["type"] == "input_json_delta":
                    partial_inputs[index] = partial_inputs.get(index, "") + delta.get("partial_json", "")
                elif delta["type"] == "thinking_delta":
                    block["thinking"] = block.get("thinking", "") + delta.get("thinking", "")
                elif delta["type"] == "signature_delta":
                    block["signature"] = block.get("signature", "") + delta.get("signature", "")
            elif kind == "content_block_stop" and index in partial_inputs:
                content[index]["input"] = json.loads(partial_inputs[index])
            elif kind == "message_delta":
                finish = event.get("delta", {}).get("stop_reason", finish)
            elif kind == "message_stop":
                complete = True
                break
        else:
            choices = event.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            delta = choice.get("delta") or {}
            if delta.get("content"):
                text += delta["content"]
                yield {"type": "delta", "text": delta["content"]}
            for part in delta.get("tool_calls") or []:
                call = tool_calls.setdefault(part["index"], {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if part.get("id"):
                    call["id"] = part["id"]
                for key in ("name", "arguments"):
                    call["function"][key] += part.get("function", {}).get(key) or ""
            finish = choice.get("finish_reason") or finish
    if not complete or not finish:
        raise ValueError("The response stream ended before completion.")
    if provider == "claude":
        return {"content": [content[index] for index in sorted(content)], "stop_reason": finish}
    message = {"role": "assistant", "content": text or None}
    if tool_calls:
        message["tool_calls"] = [tool_calls[index] for index in sorted(tool_calls)]
    return {"choices": [{"message": message, "finish_reason": finish}]}
