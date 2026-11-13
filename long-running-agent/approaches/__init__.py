"""Two more ways to build the same agent, plugged into the same queue, state, runner and grading:

  agent_sdk.py    Claude Agent SDK: the SDK runs the loop; our tools ride in as an in-process MCP server
  claude_code.py  Claude Code headless (`claude -p`): built-in tools, a file convention for talking to us
"""
