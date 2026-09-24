# Autonomous Web Agent (Specification-Driven Development)

This repository contains an autonomous AI agent capable of executing complex multi-step tasks in a web browser, fully complying with the provided SDD 1.0.0 specification.

## Components

1. **`mcp_server.py`**: A Model Context Protocol (MCP) server that exposes Playwright browser capabilities. It handles:
   - Persistent browser sessions (`playwright_data/` directory).
   - Session Cookie persistence via `state.json` (workaround for Playwright bug #36139).
   - Generating Semantic Accessibility Tree (AXTree) snapshots with temporary `ref` IDs (no CSS/XPath needed).

2. **`agent.py`**: A standalone custom orchestrator script that connects to the MCP server and communicates with the DeepSeek API.
   - It maintains memory and compresses old AXTree snapshots to optimize the context window.
   - It iterates through a step-by-step reasoning loop to accomplish user objectives.

## Setup

1. Create a virtual environment and install dependencies:
   ```bash
   pip install playwright mcp openai python-dotenv
   playwright install chromium
   ```

2. Set your DeepSeek API key:
   ```bash
   export DEEPSEEK_API_KEY="your-api-key-here"
   # Optionally set base URL if different from default
   # export DEEPSEEK_BASE_URL="https://api.deepseek.com/v1"
   ```

## Running the Standalone Agent

To run the agent with a specific objective, execute `agent.py`:

```bash
python agent.py "Go to github.com and find the trending repositories."
```

The browser will launch in visible mode (headless=False) so you can monitor its actions, while reasoning logs will appear in the terminal.

## Using with Claude Code or Cursor

You can use `mcp_server.py` as a standard MCP server in IDEs like Cursor or CLI tools like Claude Code.

**Claude Code:**
```bash
claude mcp add playwright-mcp-server python mcp_server.py
```

**Cursor:**
Add the following to your MCP configuration:
- Type: `stdio`
- Command: `python`
- Args: `mcp_server.py` (ensure you use the absolute path or run from this directory).

## Pre-authenticating or Manual Browsing
Since the agent uses a persistent browser profile (`playwright_data` directory), you can manually open the browser to log in to websites beforehand.
Just run this helper command in your terminal before starting the agent:

```bash
python -c "from playwright.sync_api import sync_playwright; p = sync_playwright().start(); browser = p.chromium.launch_persistent_context('playwright_data', headless=False); page = browser.new_page(); page.pause()"
```
This will open a visible Playwright browser. You can navigate, log in to GitHub/HH.ru/etc. When you are done, close the browser window. The session cookies will be saved in `playwright_data` and the agent will use them automatically!


### ⚠️ Important Note for Claude Code / IDE Users
Do **not** run `mcp_server.py` manually in your terminal or IDE (like PyCharm). MCP servers communicating over `stdio` are meant to be spawned automatically by the client (Claude Code). If you run it manually, it will wait for JSON-RPC inputs on `stdin` indefinitely and Claude Code will fail to connect with a `-32000` error.
