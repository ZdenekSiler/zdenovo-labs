# MCP in Python: the labs

Companion code for the 12-part series **"Building MCP Servers in Python: A Field Manual for the
Stateless Era"** on [zdenovo.com](https://zdenovo.com/series/mcp-python-deep-dive). Written
against MCP spec revision **2026-07-28** and the Python SDK **`mcp` 2.2.0**.

| Folder | What |
|---|---|
| [`lab/`](lab/) | the uv project used throughout parts 1–11 |
| [`game/`](game/) | the capstone (parts 11–12): the arena asset builder, the workshop MCP server, and the Three.js game |

```bash
cd lab
uv sync
uv run mcp version            # MCP version 2.2.0
uv run mcp dev hello.py       # part 1's server under the Inspector
```

Files whose name ends in a number belong to that part (`tools4.py` is part 4, `auth10.py` is
part 10). The rest:

| File | Part | What |
|---|---|---|
| `hello.py` | 1–2 | the first server |
| `probe.py`, `probe3.py` | 1, 3 | in-process clients that call a server and print what came back |
| `tools3.py`, `toolerr.py`, `noschema.py` | 3 | tool-interface experiments (schemas, errors, a tool without a schema) |
| `slow.py`, `live_tools.py` | 6 | long-running work and a changing tool list |
| `prod9.py` | 9 | MCP mounted inside an existing FastAPI/Starlette app |
| `probe_blender.py`, `probe_blender2.py` | 11 | probing a third-party Blender MCP server (needs Blender + that server) |

The capstone, from `game/`:

```bash
python3 make_glb.py                                   # writes assets/arena.glb
uv run --project ../lab python workshop_server.py     # the workshop MCP server
python3 -m http.server 8000                           # then open http://localhost:8000
```

`vendor/three.module.js` is Three.js r170 (MIT, licence header kept). Driving Blender itself
(part 11) needs a local Blender and a Blender MCP server; everything else runs on its own.
The protocol is moving fast: check the spec revision before reusing code.
