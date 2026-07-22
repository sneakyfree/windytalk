"""engine/tools.py — the hands-contract → OpenAI tool-spec bridge."""
import json

from engine.tools import _CONTRACT, hands_tools_enabled, load_hands_tools


def _contract_tools():
    with open(_CONTRACT, encoding="utf-8") as f:
        return json.load(f)["tools"]


def test_loads_every_contract_tool_in_order():
    specs = load_hands_tools()
    assert [s["function"]["name"] for s in specs] == [t["name"] for t in _contract_tools()]
    assert len(specs) == 12


def test_openai_function_shape():
    for spec in load_hands_tools():
        assert spec["type"] == "function"
        fn = spec["function"]
        assert fn["name"] and fn["description"]
        assert fn["parameters"]["type"] == "object"


def test_input_schema_passes_through_verbatim():
    by_name = {t["name"]: t for t in _contract_tools()}
    for spec in load_hands_tools():
        assert spec["function"]["parameters"] == by_name[spec["function"]["name"]]["inputSchema"]


def test_kill_switch(monkeypatch):
    monkeypatch.delenv("WINDYTALK_NO_HANDS_TOOLS", raising=False)
    assert hands_tools_enabled()
    monkeypatch.setenv("WINDYTALK_NO_HANDS_TOOLS", "1")
    assert not hands_tools_enabled()
