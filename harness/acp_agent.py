"""Agent Client Protocol (ACP) Server: exposes Basalt Agent over stdio for IDEs (Zed, JetBrains)."""
from __future__ import annotations

import io
import json
import sys
import uuid
from typing import Any, Dict, Optional, TextIO

from harness.agent_loop import AgentLoop
from harness.client import BasaltClient
from harness.tools import create_standard_tool_registry


class ACPAgentServer:
    def __init__(
        self,
        workspace_dir: str = ".",
        client: Optional[BasaltClient] = None,
        in_stream: Optional[TextIO] = None,
        out_stream: Optional[TextIO] = None,
    ) -> None:
        self.workspace_dir = workspace_dir
        self.client = client or BasaltClient()
        self.in_stream = in_stream or sys.stdin
        self.out_stream = out_stream or sys.stdout
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self.running = False

    def handle_request(self, req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params", {})

        if not method:
            return None

        # Notifications (no id)
        if req_id is None:
            return None

        try:
            if method == "initialize":
                result = {
                    "protocolVersion": "1.0",
                    "agentInfo": {"name": "BasaltAgent", "version": "1.0"},
                    "capabilities": {
                        "prompts": True,
                        "diffEdits": True,
                    },
                }
            elif method == "session/new":
                session_id = f"acp_{uuid.uuid4().hex[:8]}"
                self.sessions[session_id] = {
                    "workspace": params.get("cwd", self.workspace_dir),
                    "created": True,
                }
                result = {"sessionId": session_id}
            elif method == "session/prompt":
                session_id = params.get("sessionId")
                user_prompt = params.get("prompt", "")
                ws = self.sessions.get(session_id, {}).get("workspace", self.workspace_dir)

                registry, sandbox = create_standard_tool_registry(ws)
                agent = AgentLoop(client=self.client, tool_registry=registry, max_turns=5)
                turns = agent.run(user_prompt)

                final_text = turns[-1].assistant_content if turns else ""
                diffs = [c["diff"] for c in sandbox.changes if "diff" in c]

                result = {
                    "sessionId": session_id,
                    "content": final_text,
                    "diffs": diffs,
                }
            elif method == "session/cancel":
                result = {"status": "cancelled"}
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }

            return {"jsonrpc": "2.0", "id": req_id, "result": result}
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32000, "message": str(exc)},
            }

    def run_one(self, line: str) -> Optional[str]:
        line = line.strip()
        if not line:
            return None
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            return json.dumps({
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            })

        resp = self.handle_request(req)
        return json.dumps(resp, ensure_ascii=False) if resp else None

    def serve_forever(self) -> None:
        self.running = True
        for line in self.in_stream:
            if not self.running:
                break
            out = self.run_one(line)
            if out:
                self.out_stream.write(out + "\n")
                self.out_stream.flush()
