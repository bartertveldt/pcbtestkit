#!/usr/bin/env python3
"""Reference agent: let Claude make the judgement calls for a board, via PCB TestKit's validated tools.

    pip install anthropic
    export ANTHROPIC_API_KEY=...            (or `ant auth login`)
    python3 examples/agent_claude.py path/to/project [--read-only] [--effort medium]

Any other model can drive the same tools: run `python3 -m pcbtestkit mcp --project ...`
for MCP clients, or export schemas with `python3 -m pcbtestkit tools --format openai|anthropic`
and route tool calls to `pcbtestkit.agent.dispatch(workspace, name, arguments)`.
"""
import argparse
import json
import os
import sys

import anthropic

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pcbtestkit.agent import GUIDE, Workspace, dispatch, tool_definitions  # noqa: E402

MODEL = "claude-opus-5-5"

SYSTEM = ("You are a senior hardware test engineer preparing a KiCad board for volume production. "
          "Decide expected rail voltages and tolerances from the parts on each net (use your datasheet knowledge), "
          "choose the supply and current limits, and add functional test steps only where they catch real assembly "
          "faults. Never invent net or reference names. Prefer fixing weak points over waiving them; only waive "
          "with a concrete reason.\n\n" + GUIDE)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("--goal", default="Prepare this board for production testing and generate all outputs. "
                                      "Finish with a short summary of your decisions and what a human must still check.")
    ap.add_argument("--read-only", action="store_true", help="only inspection tools (review mode)")
    ap.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--max-turns", type=int, default=40)
    a = ap.parse_args()

    ws = Workspace(a.project, persist=not a.read_only, actor=MODEL)
    tools = tool_definitions("anthropic", include_writes=not a.read_only)
    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": a.goal}]

    for _ in range(a.max_turns):
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",                       # re-run on a fallback model if a request is declined
            thinking={"type": "adaptive"},
            output_config={"effort": a.effort},
            cache_control={"type": "ephemeral"},       # system + tools are stable: cache the prefix
            system=SYSTEM,
            tools=tools,
            messages=messages,
        )
        if resp.stop_reason == "refusal":
            print("Request declined:", getattr(resp, "stop_details", None))
            return 1
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason == "tool_use":
            results = []
            for block in resp.content:
                if block.type == "tool_use":
                    out = dispatch(ws, block.name, block.input)
                    ok = json.loads(out).get("ok", False)
                    print("  tool %-22s %s" % (block.name, "ok" if ok else "ERROR " + out[:160]))
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": out, "is_error": not ok})
            messages.append({"role": "user", "content": results})   # all results in one message
            continue
        if resp.stop_reason == "max_tokens":
            messages.append({"role": "user", "content": "Continue where you left off."})
            continue
        print("\n".join(b.text for b in resp.content if b.type == "text"))
        return 0
    print("Stopped after %d turns." % a.max_turns)
    return 1


if __name__ == "__main__":
    sys.exit(main())
