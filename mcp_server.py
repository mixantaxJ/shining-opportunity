import asyncio
import json
import os
import sys
from typing import Any, Dict, Optional

# Correct import for standard MCP server 1.0+ / 2.0+
from mcp.server import Server, NotificationOptions
from mcp.server.stdio import stdio_server
from mcp.server.models import InitializationOptions
import mcp.types as types

from playwright.async_api import async_playwright, Playwright, BrowserContext, Page, FrameLocator

USER_DATA_DIR = os.path.join(os.getcwd(), "playwright_data")
STATE_FILE = os.path.join(USER_DATA_DIR, "state.json")

server = Server("playwright-mcp-server")

class BrowserManager:
    def __init__(self):
        self.playwright: Optional[Playwright] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.current_frame = None  # Track active frame
        self.refs: Dict[str, Any] = {}
        self.ref_counter = 0

    async def start(self):
        self.playwright = await async_playwright().start()
        os.makedirs(USER_DATA_DIR, exist_ok=True)


        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=True
        )
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f:
                    state = json.load(f)
                    if "cookies" in state:
                        await self.context.add_cookies(state["cookies"])
            except Exception as e:
                print(f"Failed to load cookies: {e}", file=sys.stderr)

        pages = self.context.pages
        if pages:
            self.page = pages[0]
        else:
            self.page = await self.context.new_page()

        self.current_frame = self.page

    async def save_state(self):
        if self.context:
            try:
                state = await self.context.storage_state()
                with open(STATE_FILE, "w") as f:
                    json.dump(state, f)
            except Exception:
                pass

    async def stop(self):
        await self.save_state()
        if self.context:
            await self.context.close()
        if self.playwright:
            await self.playwright.stop()

browser_manager = BrowserManager()

async def handle_list_tools(ctx, request: types.ListToolsRequest) -> types.ListToolsResult:
    tools = [
        types.Tool(
            name="browser_navigate",
            description="Navigates to a URL. Includes built-in networkidle wait.",
            inputSchema={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"]
            }
        ),
        types.Tool(
            name="browser_snapshot",
            description="Returns Accessibility Tree snapshot with refs.",
            inputSchema={
                "type": "object",
                "properties": {"boxes": {"type": "boolean", "default": False}}
            }
        ),
        types.Tool(
            name="browser_click",
            description="Clicks on an element specified by its ref ID.",
            inputSchema={
                "type": "object",
                "properties": {"ref": {"type": "string"}},
                "required": ["ref"]
            }
        ),
        types.Tool(
            name="browser_type",
            description="Types value into an element specified by its ref ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "ref": {"type": "string"},
                    "value": {"type": "string"},
                    "press_enter": {"type": "boolean", "default": False}
                },
                "required": ["ref", "value"]
            }
        ),
        types.Tool(
            name="browser_switch_to_frame",
            description="Switches context to an iframe.",
            inputSchema={
                "type": "object",
                "properties": {"ref": {"type": "string"}},
                "required": ["ref"]
            }
        ),
        types.Tool(
            name="browser_ask_user",
            description="Asks the user a question in the terminal and waits for a response.",
            inputSchema={
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"]
            }
        )
    ]
    return types.ListToolsResult(tools=tools)

async def handle_call_tool(ctx, request: types.CallToolRequest) -> types.CallToolResult:
    name = request.params.name
    arguments = request.params.arguments
    if not arguments:
        arguments = {}

    target = browser_manager.current_frame or browser_manager.page

    if name == "browser_navigate":
        url = arguments.get("url")
        if not url:
            raise ValueError("url is required")
        await browser_manager.page.goto(url, wait_until="networkidle")
        browser_manager.current_frame = browser_manager.page # reset frame
        await browser_manager.save_state()
        return types.CallToolResult(content=[types.TextContent(type="text", text=f"Navigated to {url}")])

    elif name == "browser_snapshot":
        # We must use accessibility.snapshot as required by the specification.
        # It doesn't give us DOM bounding boxes natively or standard DOM node traversal directly,
        # but it gives us the sematic tree.
        try:
            snapshot = await target.accessibility.snapshot(interesting_only=True)

            browser_manager.refs = {}
            browser_manager.ref_counter = 0

            def process_node(node):
                if not node:
                    return None

                browser_manager.ref_counter += 1
                ref_id = f"e{browser_manager.ref_counter}"
                node["ref"] = ref_id

                browser_manager.refs[ref_id] = {
                    "role": node.get("role"),
                    "name": node.get("name"),
                    "value": node.get("value"),
                }

                children = node.get("children", [])
                for child in children:
                    process_node(child)

                return node

            if snapshot:
                snapshot = process_node(snapshot)

            return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(snapshot, ensure_ascii=False, indent=2))])
        except Exception as e:
            return types.CallToolResult(content=[types.TextContent(type="text", text=f"Snapshot error: {e}")])
    elif name == "browser_click":
        ref = arguments.get("ref")
        if not ref or ref not in browser_manager.refs:
            raise ValueError(f"Invalid or missing ref: {ref}")

        try:
            node = browser_manager.refs[ref]
            role = node.get('role', 'generic')
            name_val = node.get('name')
            loc = target.get_by_role(role, name=name_val) if name_val else target.get_by_role(role)
            loc = loc.first
            await loc.click()
            await browser_manager.page.wait_for_load_state("networkidle")
            await browser_manager.save_state()
            return types.CallToolResult(content=[types.TextContent(type="text", text=f"Clicked on {ref}")])
        except Exception as e:
            return types.CallToolResult(content=[types.TextContent(type="text", text=f"Failed to click {ref}: {e}")])

    elif name == "browser_type":
        ref = arguments.get("ref")
        value = arguments.get("value")
        press_enter = arguments.get("press_enter", False)

        if not ref or ref not in browser_manager.refs:
            raise ValueError(f"Invalid or missing ref: {ref}")

        try:
            node = browser_manager.refs[ref]
            role = node.get('role', 'generic')
            name_val = node.get('name')
            loc = target.get_by_role(role, name=name_val) if name_val else target.get_by_role(role)
            loc = loc.first
            await loc.fill(value)
            if press_enter:
                await loc.press("Enter")
                await browser_manager.page.wait_for_load_state("networkidle")
            await browser_manager.save_state()
            return types.CallToolResult(content=[types.TextContent(type="text", text=f"Typed '{value}' into {ref}")])
        except Exception as e:
            return types.CallToolResult(content=[types.TextContent(type="text", text=f"Failed to type in {ref}: {e}")])

    elif name == "browser_switch_to_frame":
        # Frame switching by accessibility tree ref is a best-effort using the name (title of iframe)
        ref = arguments.get("ref")
        if not ref or ref not in browser_manager.refs:
            raise ValueError(f"Invalid or missing ref: {ref}")

        node = browser_manager.refs[ref]
        name_val = node.get("name")
        if not name_val:
            return types.CallToolResult(content=[types.TextContent(type="text", text=f"Frame {ref} has no name/title to identify it.")])

        try:
            # Search for frame by name attribute
            frame = None
            for f in browser_manager.page.frames:
                if f.name == name_val or name_val in (await f.title()):
                    frame = f
                    break

            if frame:
                browser_manager.current_frame = frame
                return types.CallToolResult(content=[types.TextContent(type="text", text=f"Switched to frame {ref}")])
            else:
                return types.CallToolResult(content=[types.TextContent(type="text", text=f"Could not find frame matching name: {name_val}")])
        except Exception as e:
            return types.CallToolResult(content=[types.TextContent(type="text", text=f"Failed to switch to frame {ref}: {e}")])
    elif name == "browser_ask_user":
        question = arguments.get("question")
        # In stdio transport, we cannot read sys.stdin without breaking JSON-RPC.
        # So we return a directive to the agent/client to prompt the user themselves.
        return types.CallToolResult(content=[types.TextContent(type="text", text=f"SYSTEM: To ask this question, you must pause execution and ask the user directly in your chat interface. Question to ask: {question}")])

        raise ValueError(f"Unknown tool: {name}")

# Register Handlers
server.add_request_handler(types.ListToolsRequest.model_fields['method'].default, types.ListToolsRequest, handle_list_tools)
server.add_request_handler(types.CallToolRequest.model_fields['method'].default, types.CallToolRequest, handle_call_tool)

async def main():

    await browser_manager.start()
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="playwright-mcp-server",
                    server_version="1.0.0",
                    capabilities=server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={},
                    )
                )
            )
    finally:
        await browser_manager.stop()

if __name__ == "__main__":
    asyncio.run(main())
