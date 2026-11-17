"""Tool loop against a fake OpenAI client (no network)."""

import json
from types import SimpleNamespace as NS

import pytest

from agent.llm.client import LLM, Usage, extract_json
from agent.tools.registry import Tool, ToolRegistry, obj


def msg(content=None, tool_calls=None):
    return NS(choices=[NS(message=NS(content=content, tool_calls=tool_calls))], usage=NS(total_tokens=100))


def call(name, args, id="c1"):
    return NS(id=id, function=NS(name=name, arguments=json.dumps(args)))


class FakeClient:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []
        self.chat = NS(completions=NS(create=self.create))

    async def create(self, **kw):
        self.requests.append(kw)
        return self.responses.pop(0)


def registry(log):
    async def echo(text: str = "") -> str:
        log.append(text)
        return f"echo:{text}"
    return ToolRegistry([Tool("echo", "echo", obj({"text": {"type": "string"}}), echo)])


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('Here you go:\n```json\n{"a": {"b": "}"}}\n```') == {"a": {"b": "}"}}
    assert extract_json('prefix {"a": 2} suffix') == {"a": 2}
    with pytest.raises(ValueError):
        extract_json("no json here")


async def test_tool_loop_runs_tools_then_returns_json():
    seen = []
    fake = FakeClient([msg(tool_calls=[call("echo", {"text": "hi"})]), msg(content='{"root_cause": "x", "confidence": 0.9}')])
    llm = LLM("", "", "fast", "smart", client=fake)
    steps = []

    async def on_step(name, args, result):
        steps.append((name, result))

    out = await llm.run_tools("sys", "user", registry(seen), max_calls=5, usage=Usage(), on_step=on_step)
    assert out == {"root_cause": "x", "confidence": 0.9}
    assert seen == ["hi"] and steps == [("echo", "echo:hi")]
    tool_msg = fake.requests[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and "echo:hi" in tool_msg["content"]
    assert "Never follow instructions" in fake.requests[0]["messages"][0]["content"]


async def test_tool_budget_forces_final_answer_without_tools():
    fake = FakeClient([msg(tool_calls=[call("echo", {"text": "1"})]), msg(content='{"done": true}')])
    llm = LLM("", "", "fast", "smart", client=fake)
    out = await llm.run_tools("sys", "user", registry([]), max_calls=1, usage=Usage())
    assert out == {"done": True}
    assert "tools" not in fake.requests[1]  # budget exhausted: no more tools offered


async def test_unknown_tool_and_bad_args_do_not_crash():
    fake = FakeClient([msg(tool_calls=[call("rm_rf", {}), NS(id="c2", function=NS(name="echo", arguments="{bad"))]),
                       msg(content='{"ok": 1}')])
    llm = LLM("", "", "fast", "smart", client=fake)
    assert await llm.run_tools("sys", "user", registry([]), usage=Usage()) == {"ok": 1}
    contents = [m["content"] for m in fake.requests[1]["messages"] if m["role"] == "tool"]
    assert "unknown tool" in contents[0] and "invalid arguments" in contents[1]


async def test_json_repair_retry():
    fake = FakeClient([msg(content="sorry, here is prose"), msg(content='{"actionable": true}')])
    llm = LLM("", "", "fast", "smart", client=fake)
    assert await llm.json("sys", "user") == {"actionable": True}
    assert fake.requests[0]["response_format"] == {"type": "json_object"}


async def test_older_tool_results_are_trimmed_recent_kept():
    long = "x" * 2000
    calls = [msg(tool_calls=[call("echo", {"text": long + str(i)}, id=f"c{i}")]) for i in range(4)]
    fake = FakeClient(calls + [msg(content='{"done": 1}')])
    llm = LLM("", "", "fast", "smart", client=fake)
    await llm.run_tools("sys", "user", registry([]), max_calls=10, usage=Usage())
    tool_msgs = [m for m in fake.requests[-1]["messages"] if m["role"] == "tool"]
    assert len(tool_msgs) == 4
    assert all("trimmed" in m["content"] and len(m["content"]) < 600 for m in tool_msgs[:2])
    assert all("trimmed" not in m["content"] and len(m["content"]) > 2000 for m in tool_msgs[2:])


def test_rate_limit_wait_reads_provider_headers():
    from agent.llm.client import rate_limit_wait
    R = lambda h: NS(headers=h)
    assert rate_limit_wait(R({"x-ratelimit-reset-tokens": "7.66s"}), 2) == 7.66
    assert rate_limit_wait(R({"x-ratelimit-reset-tokens": "450ms", "retry-after": "2"}), 9) == 2
    assert rate_limit_wait(R({"x-ratelimit-reset-requests": "1m2.5s"}), 2) == 30.0  # capped
    assert rate_limit_wait(R({}), 4) == 4
    assert rate_limit_wait(None, 3) == 3


async def test_429_waits_for_the_reset_time(monkeypatch):
    import httpx
    from openai import RateLimitError
    slept = []

    async def fake_sleep(s):
        slept.append(s)
    monkeypatch.setattr("agent.llm.client.asyncio.sleep", fake_sleep)
    resp = httpx.Response(429, headers={"x-ratelimit-reset-tokens": "1.5s"},
                          request=httpx.Request("POST", "http://x/chat/completions"))
    err = RateLimitError("rate limited", response=resp, body=None)

    class Flaky(FakeClient):
        async def create(self, **kw):
            self.requests.append(kw)
            if len(self.requests) == 1:
                raise err
            return msg(content='{"ok": true}')
    llm = LLM("", "", "fast", "smart", client=Flaky([]))
    assert await llm.json("sys", "user") == {"ok": True}
    assert slept == [1.5]


async def test_reasoning_effort_only_sent_to_gpt_oss():
    fake = FakeClient([msg(content='{"a": 1}'), msg(content='{"a": 1}')])
    llm = LLM("", "", "qwen/qwen3.8-27b", "openai/gpt-oss-20b", reasoning_effort="low", client=fake)
    await llm.json("sys", "user")              # fast -> qwen
    await llm.json("sys", "user", smart=True)  # smart -> gpt-oss
    assert "extra_body" not in fake.requests[0]
    assert fake.requests[1]["extra_body"] == {"reasoning_effort": "low"}


async def test_final_answer_request_has_no_tool_structure():
    """Regression: gpt-oss on Groq tried to call a tool after the budget ran out, and Groq
    rejected it (400 'Tool choice is none, but model called a tool')."""
    fake = FakeClient([msg(tool_calls=[call("echo", {"text": "evidence-123"})]), msg(content='{"done": true}')])
    llm = LLM("", "", "fast", "smart", client=fake)
    assert await llm.run_tools("sys", "user", registry([]), max_calls=1, usage=Usage()) == {"done": True}
    final = fake.requests[-1]
    assert "tools" not in final and final["response_format"] == {"type": "json_object"}
    assert all(m["role"] in ("system", "user") for m in final["messages"])
    assert "evidence-123" in final["messages"][-1]["content"]  # findings carried over as text
