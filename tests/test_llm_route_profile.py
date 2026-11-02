"""A cloud route's capability profile: the cache marks on the stable start of an Anthropic request, the limits read from the provider's
record, and whether the route is stateless. A route to a local model is unchanged."""
import pytest

from tadamoo.llm.client import RoutedClient, from_anthropic, to_anthropic
from tadamoo.llm.profile import RouteProfile, route_profile
from tadamoo.llmswitch.providers import TYPES
from test_llm_client import MSG, Cloud, Local, world  # noqa: F401  (the fixture)

TOOLS = [{"type": "function", "function": {"name": "a", "description": "a", "parameters": {"type": "object", "properties": {}}}},
         {"type": "function", "function": {"name": "b", "description": "b", "parameters": {"type": "object", "properties": {}}}}]


def test_the_profile_comes_from_the_type_and_the_record():
    plain = RouteProfile()
    assert route_profile(None) == plain and plain.stateless and plain.local and not plain.cache_control and plain.context_window is None
    assert route_profile({"type": "basalt"}, {"this_server": True, "api": "openai", "local": True}) == plain
    claude = route_profile({"type": "claude", "context_window": 200000, "max_output": 8192}, TYPES["claude"])
    assert claude.cache_control and claude.context_window == 200000 and claude.max_output == 8192 and claude.stateless and not claude.local
    assert not route_profile({"type": "claude", "cache_control": False}, TYPES["claude"]).cache_control
    openai = route_profile({"type": "openai"}, TYPES["openai"])
    assert not openai.cache_control and openai.context_window is None and openai.max_output is None
    assert not route_profile({"type": "openai", "accepts_ids": True}, TYPES["openai"]).stateless
    for junk in ("big", -5, 0, True, 2.5):
        assert route_profile({"type": "claude", "context_window": junk}, TYPES["claude"]).context_window is None
    assert route_profile({"type": "lmstudio"}, TYPES["lmstudio"]).local is True


def test_the_cache_marks_go_on_the_end_of_the_tools_and_of_the_system_text_only():
    body = to_anthropic([{"role": "system", "content": "rules"}, {"role": "user", "content": "hi"}], TOOLS, cache=True)
    mark = {"type": "ephemeral"}
    assert body["system"] == [{"type": "text", "text": "rules", "cache_control": mark}]
    assert "cache_control" not in body["tools"][0] and body["tools"][1]["cache_control"] == mark
    assert "cache_control" not in str(body["messages"])
    plain = to_anthropic([{"role": "system", "content": "rules"}, {"role": "user", "content": "hi"}], TOOLS)
    assert plain["system"] == "rules" and "cache_control" not in str(plain)


def test_what_the_cache_served_is_counted_in_the_prompt_and_reported_as_cached():
    out = from_anthropic({"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
                          "usage": {"input_tokens": 20, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 80, "output_tokens": 5}})
    assert out["usage"]["prompt_tokens"] == 1000 and out["usage"]["prompt_tokens_details"] == {"cached_tokens": 900} and out["usage"]["total_tokens"] == 1005
    plain = from_anthropic({"content": [], "usage": {"input_tokens": 11, "output_tokens": 7}})
    assert plain["usage"] == {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}


def test_a_claude_route_sends_the_marks_and_keeps_a_reply_inside_the_provider_s_limit(world):  # noqa: F811
    cloud, local, reg, ledger = world
    pid = reg.add({"type": "claude", "base_url": cloud.url + "/v1", "api_key": "sk-ant-abcdef", "model": "claude-test"})["id"]
    reg.assign_checked("eval", pid)
    reg.set_limits(pid, context_window=200000, max_output=300)
    client = RoutedClient("eval", reg, local, ledger)
    client.chat_completion(MSG, tools=TOOLS, max_tokens=5000)
    body = cloud.requests[0]["body"]
    assert body["max_tokens"] == 300 and body["system"][0]["cache_control"] == {"type": "ephemeral"} and body["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    client.chat_completion(MSG)                                                                   # no ask: the default, inside the limit
    assert cloud.requests[1]["body"]["max_tokens"] == 300
    assert client.profile().context_window == 200000 and client.context_window() == 200000
    reg.set_limits(pid, 0, 0)
    client.chat_completion(MSG, max_tokens=5000)
    assert cloud.requests[2]["body"]["max_tokens"] == 5000 and client.profile().context_window is None


def test_the_cache_marks_can_be_turned_off_in_the_record(world):  # noqa: F811
    cloud, local, reg, ledger = world
    pid = reg.add({"type": "claude", "base_url": cloud.url + "/v1", "api_key": "sk-ant-abcdef", "model": "claude-test"})["id"]
    data = reg.load()
    reg._find(data, pid)["cache_control"] = False
    reg.save(data)
    reg.assign_checked("eval", pid)
    RoutedClient("eval", reg, local, ledger).chat_completion(MSG, tools=TOOLS, max_tokens=100)
    assert cloud.requests[0]["body"]["system"] == "Sei Basalt." and "cache_control" not in str(cloud.requests[0]["body"])


def test_an_openai_compatible_route_keeps_its_limit_and_a_route_to_the_loaded_model_changes_nothing(world):  # noqa: F811
    cloud, local, reg, ledger = world
    pid = reg.add({"type": "openai_compat", "base_url": cloud.url + "/v1", "api_key": "k" * 12, "model": "m"})["id"]
    reg.assign_checked("eval", pid)
    reg.set_limits(pid, max_output=40)
    RoutedClient("eval", reg, local, ledger).chat_completion(MSG, max_tokens=500)
    assert cloud.requests[0]["body"]["max_tokens"] == 40 and "cache_control" not in str(cloud.requests[0]["body"])
    here = RoutedClient("executor", reg, local, ledger)                                           # nothing assigned: the loaded model
    here.local.context_window = lambda *a, **k: 32768
    assert here.profile() == RouteProfile() and here.context_window() == 32768
    here.chat_completion(MSG, tools=TOOLS, max_tokens=500)
    assert local.calls[-1]["max_tokens"] == 500 and local.calls[-1]["messages"] == MSG and local.calls[-1]["tools"] == TOOLS


def test_the_limits_of_a_provider_are_kept_cleared_and_shown_by_the_providers_page_api(world):  # noqa: F811
    from serve.llm_api import LlmApi, LlmError
    cloud, local, reg, ledger = world
    api = LlmApi(reg, ledger)
    added = api.add_provider({"type": "claude", "api_key": "sk-ant-abcdef", "model": "m", "context_window": "200000", "max_output": 8192})
    assert added["context_window"] == 200000 and added["max_output"] == 8192
    changed = api.update_provider(added["id"], {"max_output": ""})
    assert changed["max_output"] == 0 and changed["context_window"] == 200000
    for bad in ("many", -1, True):
        with pytest.raises(LlmError):
            api.update_provider(added["id"], {"context_window": bad})
    assert api.update_provider(added["id"], {"name": "x"})["context_window"] == 200000
