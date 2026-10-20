"""Stateless Responses transport helpers; reporting permissions stay in Odoo."""


def uses_responses(model):
    return model.startswith("gpt-6")


def responses_payload(model, system, messages, tools, max_tokens):
    inputs = []
    for message in messages:
        if "_responses_output" in message:
            # Includes encrypted reasoning and call IDs needed by the next round.
            inputs.extend(message["_responses_output"])
        elif message["role"] == "tool":
            inputs.append({"type": "function_call_output", "call_id": message["tool_call_id"],
                           "output": message["content"]})
        else:
            inputs.append({"role": message["role"], "content": message["content"]})
    payload = {"model": model, "instructions": system, "input": inputs, "store": False,
               "include": ["reasoning.encrypted_content"], "reasoning": {"effort": "low"},
               "max_output_tokens": min(16384, max(8192, max_tokens))}
    if tools:
        # Optional filters must remain optional; validation still runs server-side.
        payload["tools"] = [{"type": "function", "name": tool["name"],
                             "description": tool["description"], "parameters": tool["input_schema"],
                             "strict": False} for tool in tools]
    return payload


def normalize_response(response):
    status = response.get("status")
    if status not in {"completed", "incomplete"} or response.get("error"):
        raise ValueError("The provider response did not complete.")
    output = response.get("output") or []
    calls, text = [], []
    for item in output:
        if item.get("type") == "function_call":
            if status != "completed" or item.get("status") != "completed":
                raise ValueError("Incomplete tool calls cannot execute.")
            calls.append({"id": item["call_id"], "type": "function",
                          "function": {"name": item["name"], "arguments": item["arguments"]}})
        elif item.get("type") == "message":
            for part in item.get("content") or []:
                if part.get("type") == "output_text":
                    text.append(part["text"])
                elif part.get("type") == "refusal":
                    text.append(part["refusal"])
    if status == "incomplete" and (response.get("incomplete_details") or {}).get("reason") != "max_output_tokens":
        raise ValueError("The provider response was interrupted.")
    message = {"role": "assistant", "content": "\n".join(text) or None}
    if calls:
        message["tool_calls"] = calls
    return {"choices": [{"message": message,
                         "finish_reason": "length" if status == "incomplete" else ("tool_calls" if calls else "stop")}],
            "_responses_output": output}
