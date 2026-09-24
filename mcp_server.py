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
    """Navigates to a URL. Includes built-in wait."""
    try:
        await browser_manager.page.goto(url, wait_until="domcontentloaded", timeout=15000)
    except Exception:
        try:
            await browser_manager.page.goto(url, wait_until="commit", timeout=15000)
        except Exception:
            pass
    # Give the page a tiny bit of time to render JS
    await browser_manager.page.wait_for_timeout(1500)
    browser_manager.current_frame = browser_manager.page
    await browser_manager.save_state()
    return f"Navigated to {url}"

@mcp_server.tool()
async def browser_snapshot(boxes: bool = False) -> str:
    """Returns Accessibility Tree snapshot with refs."""
    target = browser_manager.current_frame or browser_manager.page
    try:
        # Playwright Python doesn't have page.accessibility, so we use CDP
        client = await browser_manager.page.context.new_cdp_session(browser_manager.page)
        snapshot = await client.send("Accessibility.getFullAXTree")

        browser_manager.refs = {}
        browser_manager.ref_counter = 0

        # We will parse the CDP AXTree which has a different format
        # nodes are in snapshot['nodes']

        nodes = snapshot.get("nodes", [])
        tree_map = {node["nodeId"]: node for node in nodes}

        def process_node(node_id):
            if node_id not in tree_map: return None
            node = tree_map[node_id]

            role = node.get("role", {}).get("value")
            name = node.get("name", {}).get("value")

            is_interesting = role in ["button", "link", "textbox", "searchbox", "combobox", "checkbox", "radio", "switch", "slider", "spinbutton", "menuitem", "tab", "treeitem"] or name

            result = {}
            if is_interesting:
                browser_manager.ref_counter += 1
                ref_id = f"e{browser_manager.ref_counter}"
                result["ref"] = ref_id
                result["role"] = role
                result["name"] = name

                browser_manager.refs[ref_id] = {
                    "role": role,
                    "name": name,
                    "backendNodeId": node.get("backendDOMNodeId")
                }

            children = []
            for child_id in node.get("childIds", []):
                child_result = process_node(child_id)
                if child_result:
                    if is_interesting:
                        children.append(child_result)
                    else:
                        if isinstance(child_result, list):
                            children.extend(child_result)
                        else:
                            children.append(child_result)

            if is_interesting:
                if children:
                    result["children"] = children
                return result
            else:
                return children if children else None

        root_id = None
        for node in nodes:
            if node.get("role", {}).get("value") == "RootWebArea":
                root_id = node.get("nodeId")
                break

        if not root_id and nodes:
            root_id = nodes[0].get("nodeId")

        simplified_tree = process_node(root_id) if root_id else []

        # Make sure it's a dict or list for JSON serialization
        if not isinstance(simplified_tree, list):
            simplified_tree = [simplified_tree] if simplified_tree else []

        return json.dumps(simplified_tree, ensure_ascii=False, indent=2)
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
        backend_id = node.get('backendNodeId')

        client = await browser_manager.page.context.new_cdp_session(browser_manager.page)

        # Get coordinates
        box = await client.send("DOM.getBoxModel", {"backendNodeId": backend_id})
        quad = box["model"]["border"]
        x = (quad[0] + quad[2] + quad[4] + quad[6]) / 4
        y = (quad[1] + quad[3] + quad[5] + quad[7]) / 4

        # Native click
        await browser_manager.page.mouse.click(x, y)

        # Small wait for React to process click, no strict networkidle required
        await browser_manager.page.wait_for_timeout(500)

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
        backend_id = node.get('backendNodeId')

        client = await browser_manager.page.context.new_cdp_session(browser_manager.page)

        # Click to focus
        box = await client.send("DOM.getBoxModel", {"backendNodeId": backend_id})
        quad = box["model"]["border"]
        x = (quad[0] + quad[2] + quad[4] + quad[6]) / 4
        y = (quad[1] + quad[3] + quad[5] + quad[7]) / 4

        await browser_manager.page.mouse.click(x, y)

        # Select all and delete (Cmd+A/Ctrl+A -> Backspace) to clear
        await browser_manager.page.keyboard.press("Meta+A")
        await browser_manager.page.keyboard.press("Control+A")
        await browser_manager.page.keyboard.press("Backspace")

        # Type value
        await browser_manager.page.keyboard.type(value, delay=10) # 10ms delay simulates human

        if press_enter:
            await browser_manager.page.keyboard.press("Enter")

        await browser_manager.page.wait_for_timeout(500)
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
