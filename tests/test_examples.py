"""Exercise the documented agent through the real episode runner, without providers."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from mac_bench.evaluation.episodes.identity import call_identity
from mac_bench.evaluation.episodes.runner import run_episode
from mac_bench.evaluation.tasks.protocol import TaskView, ToolDefinition
from mac_bench.runtime.agents.workflow import AgentBudget
from mac_bench.runtime.llm.client import ChatResponse


def example(name):
    path = Path(__file__).resolve().parents[1] / "examples" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CounterTask:
    def __init__(self):
        self.record = SimpleNamespace(category="developer_release", split="dev")
        self.view = TaskView("counter", "test", "Set the counter to 3.", (
            ToolDefinition("set_counter", "Set a counter.", {
                "type": "object", "properties": {"value": {"type": "integer"}},
                "required": ["value"],
            }),
        ), "Use the provided tool, then report completion.")
        self.is_open = False
        self.accepting = False
        self.value = 0
        self.calls = []
        self.environment = SimpleNamespace(call=self.call)

    def open(self):
        self.is_open = True

    def assert_initial(self):
        return {"matches": self.value == 0, "observation": self.observe()}

    def begin_episode(self, instance_id, *, admit_call, event):
        self.admit_call = admit_call
        self.accepting = True

    def call(self, name, arguments):
        assert self.accepting
        self.admit_call()
        self.calls.append((name, arguments, call_identity()))
        self.value = arguments["value"]
        return {"value": self.value}

    def end_episode(self):
        self.accepting = False

    def observe(self):
        return {"value": self.value}

    def evaluate(self):
        assert not self.accepting
        return {"status": "passed" if self.value == 3 else "failed",
                "gc": float(self.value == 3)}

    def export_receipts(self, destination):
        pass

    def close(self):
        self.is_open = False


class ScriptedClient:
    def __init__(self):
        self.calls = 0

    def complete(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            assert messages[1].content == "Set the counter to 3."
            assert kwargs["tools"][0]["function"]["name"] == "set_counter"
            return ChatResponse("", "fixture", {"prompt_tokens": 10, "completion_tokens": 5},
                tool_calls=({"id": "call_1", "type": "function", "function": {
                    "name": "set_counter", "arguments": '{"value": 3}'}},))
        assert messages[-1].tool_call_id == "call_1"
        assert json.loads(messages[-1].content) == {"value": 3}
        return ChatResponse("Done.", "fixture", {"prompt_tokens": 20, "completion_tokens": 2})


def test_minimal_agent_records_calls_and_scores_final_state(tmp_path):
    task = CounterTask()
    result = run_episode(task, example("minimal_agent").MinimalAgent(), ScriptedClient(),
                         output=tmp_path / "episode")
    assert result["status"] == "completed"
    assert result["ps"] == result["rr"] == 1
    assert result["model_calls"] == 2 and result["tool_calls"] == 1
    assert result["input_tokens"] == 30 and result["output_tokens"] == 7
    assert result["closed"] and not task.is_open
    assert result["agents_used"] == ["agent-1"]
    assert result["agents_active_at_return"] == 0
    assert task.calls == [("set_counter", {"value": 3}, {"agent_id": "agent-1"})]
    assert json.loads((tmp_path / "episode/trajectory.json").read_text())["final_response"] == "Done."
    receipt = json.loads((tmp_path / "episode/model_calls/0001.json").read_text())
    assert receipt["identity"]["agent_id"] == "agent-1"


def test_minimal_agent_obeys_tool_budget_and_finalizes(tmp_path):
    task = CounterTask()
    budget = AgentBudget(max_model_calls=2, max_tool_calls=0, max_env_actions=0,
                         max_tokens=100, max_wall_seconds=60)
    result = run_episode(task, example("minimal_agent").MinimalAgent(), ScriptedClient(),
                         output=tmp_path / "episode", budget=budget)
    assert result["stop_reason"] == "budget:tool_calls"
    assert result["status"] == "completed" and result["closed"]
    assert result["ps"] == result["rr"] == 0
    assert not task.calls
    assert result["agents_active_at_return"] == 0


def test_result_reader_distinguishes_unscored_from_zero():
    render = example("read_results").render_summary
    report = {"task_id": "counter", "baseline": "minimal_agent", "status": "error",
              "episodes": [{"status": "error", "ps": None, "rr": None}],
              "metrics": {"scored_episodes": 0, "ps": None, "rr": None,
                          "excluded": {"episode_not_completed": 1}}}
    text = render(report)
    assert "PS: n/a | RR: n/a" in text
    assert "Scored episodes: 0/1" in text
    assert "Excluded from means:" in text
    report["metrics"].update(scored_episodes=1, ps=0, rr=0, excluded={})
    assert "PS: 0.00% | RR: 0.00%" in render(report)
