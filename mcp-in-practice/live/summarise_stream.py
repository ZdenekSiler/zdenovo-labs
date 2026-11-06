"""Turn Claude Code's stream-json into a readable transcript: tools, calls, answer, cost."""
import json
import sys

for line in sys.stdin:
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        continue
    if ev.get("type") == "system" and ev.get("subtype") == "init":
        print(f"model: {ev.get('model')}")
        print(f"tools offered: {', '.join(ev.get('tools', []))}")
        print(f"mcp servers: {[(s['name'], s['status']) for s in ev.get('mcp_servers', [])]}\n")
    elif ev.get("type") == "assistant":
        for b in ev["message"]["content"]:
            if b.get("type") == "tool_use":
                print(f"→ {b['name']}({json.dumps(b['input'])})")
    elif ev.get("type") == "user":
        for b in ev["message"]["content"]:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                c = b.get("content")
                text = c if isinstance(c, str) else "".join(x.get("text", "") for x in c or [])
                print(f"  ← {'ERROR ' if b.get('is_error') else ''}{len(text)} chars")
    elif ev.get("type") == "result":
        print(f"\nA: {ev.get('result')}\n")
        print(f"[{ev.get('num_turns')} turns, {ev.get('duration_ms', 0) / 1000:.1f}s, "
              f"total_cost_usd={ev.get('total_cost_usd')}]")
