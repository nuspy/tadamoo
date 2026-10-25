"""Basalt's own ACP agent on stdio with a scripted model: one shell command, then an answer. Arguments: workspace, data folder."""
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
sys.path[:0] = [str(here), str(here.parent)]

from harness.acp_agent import ACPAgentServer  # noqa: E402
from test_agent_loop import ScriptedClient, call, say  # noqa: E402

workspace, data = sys.argv[1], sys.argv[2]
client = ScriptedClient(call("shell_command", {"command": "echo from-the-agent"}), say("Done: the command was run."))
ACPAgentServer(workspace_dir=workspace, client=client, data_dir=data).serve_forever()
