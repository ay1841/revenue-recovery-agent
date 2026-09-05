"""
Agentic reconciliation engine.

Replaces the earlier dice-roll prototype for unknown/pending
transactions with a real Claude-driven agent that investigates using
tools and reasons about when it has enough evidence to decide.

Bounded by design:
- Max 3 diagnostic tool calls (check_live_status / check_with_network)
  before the agent is forced to finalize with whatever evidence it has.
- Max 6 total loop turns as a hard ceiling regardless of tool choice.
- The agent can ONLY end a transaction by calling one of the three
  finalize_* tools -- it cannot just stop talking and leave a transaction
  unresolved.
- If ANTHROPIC_API_KEY is not set, falls back to a deterministic heuristic
  agent (same tools, same bounds, no LLM) so the pipeline is testable
  offline. This fallback is clearly logged as non-LLM in the audit trail.

Two entry points:
- investigate_stream(txn): a generator that yields each audit step as it
  happens, ending with a {"step": "__final__", ...} marker. Used by the
  live single-transaction demo (streams to the dashboard over SSE).
- resolve_unknown(txn): consumes the stream and returns the final dict in
  one call. Used by the batch pipeline, where a live stream isn't needed.
"""

import json
import os
import time

import agent_tools

MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
MAX_DIAGNOSTIC_CALLS = 3
MAX_TOTAL_TURNS = 6

SYSTEM_PROMPT = """You are a payment reconciliation agent for an Indian payment gateway.

You are handed a transaction whose live payment confirmation never arrived --
it may have succeeded (money already debited) or failed (money never left
the customer's account). You do not know which.

Rules you must follow:
1. You may NEVER assume an outcome without tool evidence. Do not guess.
2. You may call check_live_status and check_with_network, in any order,
   up to a combined total of {max_calls} times.
3. Once you have enough evidence -- or you've used your diagnostic budget --
   you MUST call exactly one of: finalize_recovered, finalize_refunded,
   finalize_pending_reconciliation, finalize_escalated.
4. finalize_recovered requires a tool result that confirmed the debit
   succeeded. finalize_refunded requires a tool result that confirmed the
   debit did NOT happen. finalize_pending_reconciliation is the correct,
   HONEST outcome when both live checks came back inconclusive -- a real
   settlement file cannot be fetched on demand, so do not fabricate a
   result. finalize_escalated is only for genuinely anomalous situations,
   not the normal "still inconclusive" case.
5. Be efficient -- don't call a tool you don't need. If the first tool
   gives you a confirmed result, finalize immediately rather than calling
   the second tool "just to be sure".
""".format(max_calls=MAX_DIAGNOSTIC_CALLS)

STATUS_MAP = {
    "finalize_recovered": "recovered",
    "finalize_refunded": "refunded",
    "finalize_pending_reconciliation": "pending_recon",
    "finalize_escalated": "escalated",
}


def investigate_stream(txn: dict):
    """Yields audit step dicts as the investigation proceeds. Final item is
    {"step": "__final__", "final_status": ..., "resolved_via": ...}.

    Provider is chosen by whichever API key is present. GEMINI_API_KEY is
    checked first since Google AI Studio's free tier makes it the easiest
    path for most people to actually run this with a real LLM."""
    if os.environ.get("GEMINI_API_KEY"):
        yield from _stream_with_gemini(txn)
    elif os.environ.get("ANTHROPIC_API_KEY"):
        yield from _stream_with_claude(txn)
    else:
        yield from _stream_with_fallback(txn)


def resolve_unknown(txn: dict) -> dict:
    audit_steps = []
    final = None
    for item in investigate_stream(txn):
        if item.get("step") == "__final__":
            final = item
        else:
            audit_steps.append(item)
    return {
        "final_status": final["final_status"],
        "resolved_via": final["resolved_via"],
        "audit_steps": audit_steps,
    }


def _stream_with_claude(txn: dict):
    import anthropic

    client = anthropic.Anthropic()
    diagnostic_calls_used = 0

    messages = [{
        "role": "user",
        "content": (
            f"Investigate transaction {txn['txn_id']} (amount \u20b9{txn['amount']}, "
            f"method {txn['method']}, decline_code={txn['decline_code']}). "
            f"Its live confirmation never arrived. Determine the correct outcome."
        ),
    }]

    for turn in range(MAX_TOTAL_TURNS):
        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=agent_tools.TOOL_SCHEMAS,
            messages=messages,
        )

        messages.append({"role": "assistant", "content": response.content})

        for block in response.content:
            if block.type == "text" and block.text.strip():
                yield {"step": "agent_reasoning", "detail": block.text.strip()}

        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
        if not tool_use_blocks:
            messages.append({
                "role": "user",
                "content": "You must call a tool. If you have enough evidence, finalize now.",
            })
            continue

        tool_results = []
        for block in tool_use_blocks:
            if block.name in agent_tools.FINALIZE_TOOLS:
                reasoning = block.input.get("reasoning", "")
                yield {"step": block.name, "detail": reasoning}
                yield {
                    "step": "__final__",
                    "final_status": STATUS_MAP[block.name],
                    "resolved_via": "agent_" + block.name.replace("finalize_", ""),
                }
                return

            if diagnostic_calls_used >= MAX_DIAGNOSTIC_CALLS:
                result = {"result": "budget_exhausted",
                          "note": "Diagnostic tool-call budget used up, must finalize now"}
            else:
                diagnostic_calls_used += 1
                result = agent_tools.TOOL_FUNCTIONS[block.name](txn)

            yield {"step": f"tool_call_{block.name}", "detail": json.dumps(result)}
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result),
            })

        messages.append({"role": "user", "content": tool_results})

    yield {"step": "turn_limit_reached",
           "detail": f"Agent did not finalize within {MAX_TOTAL_TURNS} turns, force-escalating"}
    yield {"step": "__final__", "final_status": "escalated", "resolved_via": "forced_turn_limit"}


def _to_gemini_schema(schema: dict) -> dict:
    """Gemini's FunctionDeclaration expects schema 'type' values in
    UPPERCASE (e.g. 'OBJECT', 'STRING'), unlike the lowercase JSON-schema
    convention used in agent_tools.TOOL_SCHEMAS. Recursively convert."""
    if not isinstance(schema, dict):
        return schema
    result = {}
    for key, value in schema.items():
        if key == "type" and isinstance(value, str):
            result[key] = value.upper()
        elif isinstance(value, dict):
            result[key] = _to_gemini_schema(value)
        else:
            result[key] = value
    return result


def _stream_with_gemini(txn: dict):
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    diagnostic_calls_used = 0

    function_declarations = [
        types.FunctionDeclaration(
            name=s["name"], description=s["description"],
            parameters=_to_gemini_schema(s["input_schema"]),
        )
        for s in agent_tools.TOOL_SCHEMAS
    ]
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=[types.Tool(function_declarations=function_declarations)],
    )

    initial_text = (
        f"Investigate transaction {txn['txn_id']} (amount \u20b9{txn['amount']}, "
        f"method {txn['method']}, decline_code={txn['decline_code']}). "
        f"Its live confirmation never arrived. Determine the correct outcome."
    )
    contents = [types.Content(role="user", parts=[types.Part.from_text(text=initial_text)])]

    for turn in range(MAX_TOTAL_TURNS):
        response = client.models.generate_content(
            model=GEMINI_MODEL, contents=contents, config=config,
        )
        candidate = response.candidates[0]

        safe_model_parts = []
        function_calls = []
        for part in candidate.content.parts:
            if part.function_call:
                function_calls.append(part.function_call)
                safe_model_parts.append(part)
            elif part.text and part.text.strip():
                yield {"step": "agent_reasoning", "detail": part.text.strip()}
                safe_model_parts.append(part)

        contents.append(types.Content(role="model", parts=safe_model_parts))

        if not function_calls:
            contents.append(types.Content(role="user", parts=[types.Part.from_text(
                text="You must call a tool. If you have enough evidence, finalize now."
            )]))
            continue

        response_parts = []
        for fc in function_calls:
            name = fc.name
            args = dict(fc.args) if fc.args else {}

            if name in agent_tools.FINALIZE_TOOLS:
                reasoning = args.get("reasoning", "")
                yield {"step": name, "detail": reasoning}
                yield {
                    "step": "__final__",
                    "final_status": STATUS_MAP[name],
                    "resolved_via": "agent_" + name.replace("finalize_", ""),
                }
                return

            if diagnostic_calls_used >= MAX_DIAGNOSTIC_CALLS:
                result = {"result": "budget_exhausted",
                          "note": "Diagnostic tool-call budget used up, must finalize now"}
            else:
                diagnostic_calls_used += 1
                result = agent_tools.TOOL_FUNCTIONS[name](txn)

            yield {"step": f"tool_call_{name}", "detail": json.dumps(result)}
            response_parts.append(types.Part.from_function_response(
                name=name, response={"result": result},
            ))

        contents.append(types.Content(role="user", parts=response_parts))

    yield {"step": "turn_limit_reached",
           "detail": f"Agent did not finalize within {MAX_TOTAL_TURNS} turns, force-escalating"}
    yield {"step": "__final__", "final_status": "escalated", "resolved_via": "forced_turn_limit"}


def _stream_with_fallback(txn: dict):
    """Deterministic stand-in used when no API key is set, so the pipeline
    is fully testable offline. Same tools, same bounds, no LLM -- this is
    explicitly logged so it's never confused with the real agent."""
    PAUSE = 0.6

    yield {"step": "fallback_mode",
           "detail": "No GEMINI_API_KEY or ANTHROPIC_API_KEY set -- using deterministic heuristic agent, not a real LLM"}
    time.sleep(PAUSE)

    result = agent_tools.check_live_status(txn)
    yield {"step": "tool_call_check_live_status", "detail": json.dumps(result)}
    time.sleep(PAUSE)

    if result["result"] == "confirmed_success":
        yield {"step": "finalize_recovered", "detail": "Live status confirmed the debit succeeded"}
        yield {"step": "__final__", "final_status": "recovered", "resolved_via": "agent_recovered"}
        return
    if result["result"] == "confirmed_failed":
        yield {"step": "finalize_refunded", "detail": "Live status confirmed the debit never happened"}
        yield {"step": "__final__", "final_status": "refunded", "resolved_via": "agent_refunded"}
        return

    result2 = agent_tools.check_with_network(txn)
    yield {"step": "tool_call_check_with_network", "detail": json.dumps(result2)}
    time.sleep(PAUSE)

    if result2["result"] == "confirmed_success":
        yield {"step": "finalize_recovered", "detail": "Network cross-check confirmed the debit succeeded"}
        yield {"step": "__final__", "final_status": "recovered", "resolved_via": "agent_recovered"}
        return
    if result2["result"] == "confirmed_failed":
        yield {"step": "finalize_refunded", "detail": "Network cross-check confirmed the debit never happened"}
        yield {"step": "__final__", "final_status": "refunded", "resolved_via": "agent_refunded"}
        return

    yield {
        "step": "finalize_pending_reconciliation",
        "detail": (
            "NPCI, issuing bank, and acquiring bank were all checked live, but none "
            "could give a final confirmation. The real settlement file doesn't exist "
            "yet for this transaction -- customer will be notified once it lands."
        ),
    }
    yield {"step": "__final__", "final_status": "pending_recon", "resolved_via": "agent_pending_recon"}
