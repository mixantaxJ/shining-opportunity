import asyncio
import json
import os
import sys
from typing import List, Dict, Any, Optional

from openai import AsyncOpenAI
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.session import ClientSession
import mcp.types as types

# Setup OpenAI client for DeepSeek V4.1-Flash (or compatible API)
API_KEY = os.environ.get("DEEPSEEK_API_KEY", "dummy_key")
BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")

client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL)

MODEL_NAME = "deepseek-chat" # Fallback if specific flash model isn't available

class AutonomousAgent:
    def __init__(self, objective: str):
        self.objective = objective
        self.messages: List[Dict[str, Any]] = []

        # We will connect to our local MCP server
        self.server_params = StdioServerParameters(
            command=sys.executable,
            args=["mcp_server.py"],
            env=os.environ.copy()
        )

        self._setup_system_prompt()
        self.mcp_session: Optional[ClientSession] = None
        self.available_tools = []

    def _setup_system_prompt(self):
        prompt = f"""You are an Autonomous AI Agent designed to execute complex tasks in a web browser.
Your current objective is: {self.objective}

Methodology & Rules:
1. Perception: Rely ONLY on the Accessibility Tree (AXTree) snapshots provided. Do not use CSS selectors or XPath.
2. Interaction: Interact using the temporary 'ref' IDs (e.g., ref=e15) provided in the snapshot.
3. Resilience: If modal dialogs, alerts, or banners block the interface, close them first.
4. Autonomy: Make decisions step-by-step. Ask the user via 'browser_ask_user' ONLY if completely stuck or if the task explicitly requires handing over control (e.g. final payment step).
5. State changes: ALWAYS call 'browser_snapshot' after every action (navigate, click, type) to observe the new state.

Never try to guess refs. Always take a snapshot first to see available elements.
"""
        self.messages.append({"role": "system", "content": prompt})

    async def run(self):
        print(f"Starting agent with objective: {self.objective}")
        async with stdio_client(self.server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                self.mcp_session = session

                # Fetch available tools
                tools_response = await session.list_tools()
                self.available_tools = []
                for tool in tools_response.tools:
                    self.available_tools.append({
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": tool.inputSchema if hasattr(tool, "inputSchema") else tool.input_schema
                        }
                    })

                print(f"Connected to MCP Server. Available tools: {[t['function']['name'] for t in self.available_tools]}")

                # Start loop
                await self._loop()

    async def _loop(self):
        # Initial instruction
        self.messages.append({
            "role": "user",
            "content": "Begin execution. Start by navigating to the relevant website or taking a snapshot if already there."
        })

        step_count = 0
        max_steps = 30

        while step_count < max_steps:
            step_count += 1
            print(f"\n--- Step {step_count} ---")

            # Compress memory before sending to LLM
            self._compress_memory()

            try:
                response = await client.chat.completions.create(
                    model=MODEL_NAME,
                    messages=self.messages,
                    tools=self.available_tools,
                    tool_choice="auto"
                )
            except Exception as e:
                print(f"LLM API Error: {e}")
                # Mock response for testing if API fails
                if "dummy_key" in API_KEY:
                    print("Running with dummy key, simulating tool call...")
                    break
                return

            message = response.choices[0].message
            self.messages.append(message.model_dump())

            if message.content:
                print(f"Agent reasoning: {message.content}")

            if not message.tool_calls:
                print("No tool calls. Agent finished or stuck.")
                if message.content:
                    print(message.content)
                break


            for tool_call in message.tool_calls:
                func_name = tool_call.function.name
                args = json.loads(tool_call.function.arguments)
                print(f"Executing tool: {func_name}({args})")

                try:
                    if func_name == 'browser_ask_user':
                        question = args.get('question', 'User input requested:')
                        print(f"\n[AGENT ASKS]: {question}")
                        loop = asyncio.get_event_loop()
                        print('Your answer: ', end='', flush=True)
                        user_response = await loop.run_in_executor(None, sys.stdin.readline)
                        result_text = user_response.strip()
                    else:
                        tool_result = await self.mcp_session.call_tool(func_name, args)
                        result_text = tool_result.content[0].text if tool_result.content else 'Success'

                    print(f"Result: {result_text[:200]}...")

                    self.messages.append({
                        'role': 'tool',
                        'tool_call_id': tool_call.id,
                        'content': result_text
                    })
                except Exception as e:
                    print(f"Tool execution error: {e}")
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": f"Error: {e}"
                    })


    def _compress_memory(self):
        """
        Implementation of Section 6 (Context Optimization):
        - Keep system prompt intact (Prefix caching).
        - Keep only the last 2-3 full AXTree snapshots.
        - Compress older snapshots into short summaries.
        """
        if len(self.messages) <= 10:
            return

        # Keep system prompt at index 0
        system_prompt = self.messages[0]

        # We'll build a new list of messages
        new_messages = [system_prompt]

        # We need to keep tool call and tool response pairs intact if possible,
        # but we specifically want to compress large 'browser_snapshot' responses
        # that are old.

        # Find all tool responses for 'browser_snapshot'
        snapshot_indices = []
        for i, msg in enumerate(self.messages):
            if msg.get("role") == "tool" and "content" in msg:
                # Check if it looks like a large JSON snapshot
                if isinstance(msg["content"], str) and msg["content"].startswith("{") and '"ref":' in msg["content"]:
                    snapshot_indices.append(i)

        # If we have more than 2 snapshots, compress the older ones
        if len(snapshot_indices) > 2:
            indices_to_compress = set(snapshot_indices[:-2])

            for i in range(1, len(self.messages)):
                msg = self.messages[i]
                if i in indices_to_compress:
                    # Summarize
                    # A basic summary instead of full JSON
                    new_messages.append({
                        "role": "tool",
                        "tool_call_id": msg.get("tool_call_id"),
                        "content": "[Compressed AXTree Snapshot. Use a new browser_snapshot if needed.]"
                    })
                else:
                    new_messages.append(msg)

            self.messages = new_messages
            print("Compressed old AXTree snapshots to save context window.")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        objective = " ".join(sys.argv[1:])
    else:
        objective = "Go to google.com, search for 'DeepSeek', and find the main link."

    agent = AutonomousAgent(objective)
    asyncio.run(agent.run())
