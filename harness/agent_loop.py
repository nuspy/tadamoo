"""ReAct Agent Loop with validation, auto-repair, loop detection, and budget control."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from harness.client import BasaltClient, extract_reasoning_and_content
from harness.tools.registry import ToolRegistry


TOOL_CALL_REGEX = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


@dataclass
class ToolExecutionResult:
    tool_name: str
    arguments: Dict[str, Any]
    output: Any
    is_error: bool = False


@dataclass
class AgentTurn:
    turn_index: int
    user_input: str
    reasoning: str
    assistant_content: str
    tool_calls: List[ToolExecutionResult] = field(default_factory=list)


class LoopDetector:
    """Detects cycles and infinite loops in tool execution."""

    def __init__(self, max_consecutive_repeats: int = 2) -> None:
        self.max_repeats = max_consecutive_repeats
        self.history: List[str] = []

    def check_and_record(self, tool_name: str, args: Dict[str, Any]) -> bool:
        """Returns True if a forbidden loop is detected."""
        canonical = json.dumps({"tool": tool_name, "args": args}, sort_keys=True)
        call_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        self.history.append(call_hash)

        if len(self.history) > self.max_repeats:
            last_n = self.history[-self.max_repeats - 1:]
            if len(set(last_n)) == 1:
                return True
        return False


class AgentLoop:
    def __init__(
        self,
        client: BasaltClient,
        tool_registry: ToolRegistry,
        max_turns: int = 15,
        system_prompt: str = "You are a helpful coding assistant equipped with tools.",
        on_turn_end: Optional[Callable[[AgentTurn], None]] = None,
    ) -> None:
        self.client = client
        self.tools = tool_registry
        self.max_turns = max_turns
        self.system_prompt = system_prompt
        self.on_turn_end = on_turn_end
        self.loop_detector = LoopDetector()

    def parse_tool_calls(self, message: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
        """Extracts tool calls from both OpenAI structure and Qwen XML tags."""
        calls: List[Tuple[str, Dict[str, Any]]] = []

        # 1. Check standard OpenAI tool_calls structure
        if message.get("tool_calls"):
            for tc in message["tool_calls"]:
                fn = tc.get("function", {})
                name = fn.get("name")
                raw_args = fn.get("arguments", "{}")
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    if name:
                        calls.append((name, args))
                except json.JSONDecodeError:
                    pass

        # 2. Check inline XML tags: <tool_call>{...}</tool_call>
        content = message.get("content") or ""
        matches = TOOL_CALL_REGEX.findall(content)
        for raw_json in matches:
            try:
                parsed = json.loads(raw_json)
                name = parsed.get("name")
                args = parsed.get("arguments", {})
                if name:
                    calls.append((name, args))
            except json.JSONDecodeError:
                pass

        return calls

    def run(self, user_goal: str) -> List[AgentTurn]:
        """Executes the agent loop until the goal is finished or max_turns is reached."""
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_goal},
        ]
        turns: List[AgentTurn] = []

        for turn_idx in range(1, self.max_turns + 1):
            resp = self.client.chat_completion(
                messages=messages,
                tools=self.tools.schemas(),
                temperature=0.0,
            )
            msg = resp["choices"][0]["message"]
            reasoning, content = extract_reasoning_and_content(msg)

            turn = AgentTurn(
                turn_index=turn_idx,
                user_input=user_goal if turn_idx == 1 else "",
                reasoning=reasoning,
                assistant_content=content,
            )

            tool_calls = self.parse_tool_calls(msg)
            if not tool_calls:
                # No more tools called, final answer reached
                messages.append({"role": "assistant", "content": content})
                turns.append(turn)
                if self.on_turn_end:
                    self.on_turn_end(turn)
                break

            # Process tool calls
            messages.append(msg)
            for tool_name, args in tool_calls:
                if self.loop_detector.check_and_record(tool_name, args):
                    err_msg = f"Loop detected: tool '{tool_name}' was repeatedly called with identical parameters. Try another strategy."
                    turn.tool_calls.append(ToolExecutionResult(tool_name=tool_name, arguments=args, output=err_msg, is_error=True))
                    messages.append({"role": "tool", "name": tool_name, "content": err_msg})
                    continue

                try:
                    output = self.tools.execute(tool_name, args)
                    out_str = json.dumps(output, ensure_ascii=False) if not isinstance(output, str) else output
                    turn.tool_calls.append(ToolExecutionResult(tool_name=tool_name, arguments=args, output=output, is_error=False))
                    messages.append({"role": "tool", "name": tool_name, "content": out_str})
                except Exception as exc:
                    err_str = f"Tool execution error: {type(exc).__name__}: {str(exc)}"
                    turn.tool_calls.append(ToolExecutionResult(tool_name=tool_name, arguments=args, output=err_str, is_error=True))
                    messages.append({"role": "tool", "name": tool_name, "content": err_str})

            turns.append(turn)
            if self.on_turn_end:
                self.on_turn_end(turn)

        return turns
