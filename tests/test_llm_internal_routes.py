"""The server's own LLM jobs (plans, targets, memories, personality learning, conversation cards, skills, diagram repair) go through the LLM switch, each as
one of its functions, and with no choice made they follow the Primary model."""
import types
from pathlib import Path

from serve.agent_sessions import SessionManager
from tadamoo.llm import functions
from tadamoo.llm.client import RoutedClient
from tadamoo.llm.store import PRIMARY, SecureRegistry

ROOT = Path(__file__).resolve().parent.parent


def test_new_functions_are_known_and_personal(tmp_path):
    for name in ("memory", "skills"):
        assert functions.known(name) and functions.spec(name)["kind"] == "chat" and functions.spec(name)["data"] == "personal"


def test_a_function_with_no_choice_follows_the_primary_model(tmp_path):
    reg = SecureRegistry(tmp_path / "llm_providers.json")
    reg.set_primary("", True)
    for name in ("memory", "skills", "planner", "technical"):
        assert reg.resolve_route(name)[1] in ("local", "primary")


def test_session_manager_jobs_use_the_function_asked_for():
    me = types.SimpleNamespace(endpoint="http://127.0.0.1:1", api_key="")
    skills = SessionManager._skill_client(me)
    quick = SessionManager._skill_client(me, "technical")
    assert isinstance(skills, RoutedClient) and skills.function == "skills"
    assert isinstance(quick, RoutedClient) and quick.function == "technical"


def test_the_sites_name_their_function():
    handler = (ROOT / "serve" / "handler.py").read_text(encoding="utf-8")
    assert handler.count('client_for("memory"') == 3 and 'client_for("planner"' in handler and 'model_client("technical")' in handler
    assert 'client_for("planner"' in (ROOT / "serve" / "target_api.py").read_text(encoding="utf-8")
