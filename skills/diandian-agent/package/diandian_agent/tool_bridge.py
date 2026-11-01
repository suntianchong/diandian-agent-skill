"""Portable function-call bridge for self-hosted Grok and other tool-capable bots."""
import argparse
import asyncio
import json
import sys

from .mcp_server import create_mcp

READ_TOOLS = {"diandian_status", "diandian_list_tasks", "diandian_get_task", "diandian_notifications"}
WRITE_TOOLS = {"diandian_ack", "diandian_update_task"}


async def invoke(server, name, arguments):
    blocks = await server.call_tool(name, arguments)
    if isinstance(blocks, dict):
        return blocks
    texts = [item.text for item in blocks if getattr(item, "type", "") == "text"]
    return json.loads(texts[0]) if len(texts) == 1 else {"messages": texts}


async def run(args):
    server = create_mcp(args.config, args.server, "tool-bot")
    allowed = READ_TOOLS | (WRITE_TOOLS if args.execute else set())
    if args.list:
        tools = await server.list_tools()
        print(json.dumps([{"type": "function", "function": {"name": tool.name, "description": tool.description,
                                                          "parameters": tool.inputSchema}}
                          for tool in tools if tool.name in allowed], ensure_ascii=False, indent=2))
    else:
        if args.call not in allowed:
            raise ValueError("Tool is not allowed. Pair locally with CLI/MCP; write tools require --execute.")
        arguments = json.load(sys.stdin)
        print(json.dumps(await invoke(server, args.call, arguments), ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Export tool schemas or run one function call locally; no model API key needed.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--server", default="http://127.0.0.1:4195")
    parser.add_argument("--execute", action="store_true", help="enable ack and doing/review; server permissions still apply")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--list", action="store_true")
    action.add_argument("--call")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except Exception:
        parser.exit(1, "Tool bridge failed. Check pairing, service, scope and JSON arguments; no credential printed.\n")


if __name__ == "__main__":
    main()
