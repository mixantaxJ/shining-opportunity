import asyncio
import json
import os
import sys
from typing import Any, Dict, Optional

from mcp.server.mcpserver import MCPServer
import mcp.types as types

from playwright.async_api import async_playwright, Playwright, BrowserContext, Page, FrameLocator

USER_DATA_DIR = os.path.join(os.getcwd(), "playwright_data")
STATE_FILE = os.path.join(USER_DATA_DIR, "state.json")

mcp_server = MCPServer("playwright-mcp-server")

class BrowserManager:
    def __init__(self):
        self.playwright: Optional[Playwright] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.current_frame = None
        self.refs: Dict[str, Any] = {}
        self.ref_counter = 0

    async def start(self):
        self.playwright = await async_playwright().start()
        os.makedirs(USER_DATA_DIR, exist_ok=True)

        headless = True
        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=headless
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

@mcp_server.tool()
async def browser_navigate(url: str) -> str:
    """Navigates to a URL. Includes built-in networkidle wait."""
    await browser_manager.page.goto(url, wait_until="networkidle")
    browser_manager.current_frame = browser_manager.page
    await browser_manager.save_state()
    return f"Navigated to {url}"

@mcp_server.tool()
async def browser_snapshot(boxes: bool = False) -> str:
    """Returns Accessibility Tree snapshot with refs."""
    target = browser_manager.current_frame or browser_manager.page
    try:
        snapshot = await target.accessibility.snapshot(interesting_only=True)

        browser_manager.refs = {}
        browser_manager.ref_counter = 0

        def process_node(node):
            if not node: return None
            browser_manager.ref_counter += 1
            ref_id = f"e{browser_manager.ref_counter}"
            node["ref"] = ref_id
            browser_manager.refs[ref_id] = {
                "role": node.get("role"),
                "name": node.get("name"),
                "value": node.get("value"),
            }
            for child in node.get("children", []):
                process_node(child)
            return node

        if snapshot:
            snapshot = process_node(snapshot)

        return json.dumps(snapshot, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Snapshot error: {e}"

@mcp_server.tool()
async def browser_click(ref: str) -> str:
    """Clicks on an element specified by its ref ID."""
    target = browser_manager.current_frame or browser_manager.page
    if not ref or ref not in browser_manager.refs:
        return f"Invalid or missing ref: {ref}"

    try:
        node = browser_manager.refs[ref]
        role = node.get('role', 'generic')
        name_val = node.get('name')
        loc = target.get_by_role(role, name=name_val) if name_val else target.get_by_role(role)
        loc = loc.first
        await loc.click()
        await browser_manager.page.wait_for_load_state("networkidle")
        await browser_manager.save_state()
        return f"Clicked on {ref}"
    except Exception as e:
        return f"Failed to click {ref}: {e}"

@mcp_server.tool()
async def browser_type(ref: str, value: str, press_enter: bool = False) -> str:
    """Types value into an element specified by its ref ID."""
    target = browser_manager.current_frame or browser_manager.page
    if not ref or ref not in browser_manager.refs:
        return f"Invalid or missing ref: {ref}"

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
        return f"Typed '{value}' into {ref}"
    except Exception as e:
        return f"Failed to type in {ref}: {e}"

@mcp_server.tool()
async def browser_switch_to_frame(ref: str) -> str:
    """Switches context to an iframe."""
    if not ref or ref not in browser_manager.refs:
        return f"Invalid or missing ref: {ref}"

    node = browser_manager.refs[ref]
    name_val = node.get("name")
    if not name_val:
        return f"Frame {ref} has no name/title to identify it."

    try:
        frame = None
        for f in browser_manager.page.frames:
            if f.name == name_val or name_val in (await f.title()):
                frame = f
                break

        if frame:
            browser_manager.current_frame = frame
            return f"Switched to frame {ref}"
        else:
            return f"Could not find frame matching name: {name_val}"
    except Exception as e:
        return f"Failed to switch to frame {ref}: {e}"

@mcp_server.tool()
async def browser_ask_user(question: str) -> str:
    """Asks the user a question in the terminal and waits for a response."""
    return f"SYSTEM: To ask this question, you must pause execution and ask the user directly in your chat interface. Question to ask: {question}"

if __name__ == "__main__":
    import anyio

    # We must run the fastmcp server, but start browser first.
    # MCPServer uses its own event loop logic with run()
    # It also has run_stdio_async() we can use.

    async def run_server():
        await browser_manager.start()
        try:
            await mcp_server.run_stdio_async()
        finally:
            await browser_manager.stop()

    # anyio is recommended for run_stdio_async
    anyio.run(run_server)
