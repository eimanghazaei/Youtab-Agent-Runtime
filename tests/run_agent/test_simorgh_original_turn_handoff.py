"""The agent, not the model's tool arguments, owns the Simorgh turn sideband."""

from unittest.mock import MagicMock, patch

from run_agent import AIAgent


def test_original_turn_reaches_only_the_active_simorgh_business_read():
    tool = {"type": "function", "function": {
        "name": "mcp__simorgh__simorgh_chat", "description": "Test tool",
        "parameters": {"type": "object", "properties": {}},
    }}
    with (patch("run_agent.get_tool_definitions", return_value=[tool]),
          patch("run_agent.check_toolset_requirements", return_value={}),
          patch("run_agent.OpenAI")):
        agent = AIAgent(api_key="synthetic-test", base_url="https://invalid.example/v1",
                        quiet_mode=True, skip_context_files=True, skip_memory=True)
    agent.client = MagicMock()
    agent._current_turn_id = "turn-1"
    agent._simorgh_original_turn = ("turn-1", "Read account A, not B")
    agent._simorgh_account_turn = ("turn-1", "synthetic-account")
    proposed = {"thread_id": "t", "message": {
        "content": "Read account B", "business_read": {"account_id": "B"},
    }}
    with patch("run_agent.handle_function_call", return_value="refused") as dispatch:
        assert agent._invoke_tool("mcp__simorgh__simorgh_chat", proposed, "task") == "refused"
        assert dispatch.call_args.kwargs["user_task"] == "Read account A, not B"
        assert dispatch.call_args.kwargs["_host_account_bearer"] == "synthetic-account"
        assert dispatch.call_args.args[1] == proposed

        agent._current_turn_id = "turn-2"
        agent._invoke_tool("mcp__simorgh__simorgh_chat", proposed, "task")
        assert "user_task" not in dispatch.call_args.kwargs
        assert "_host_account_bearer" not in dispatch.call_args.kwargs

        agent._current_turn_id = "turn-1"
        agent._simorgh_original_turn = None
        agent._invoke_tool("mcp__simorgh__simorgh_chat", proposed, "task")
        assert "user_task" not in dispatch.call_args.kwargs
        assert "_host_account_bearer" not in dispatch.call_args.kwargs
