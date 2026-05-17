"""v1.6.8 SessionTraceRecorder + Agent.replay_trace tests."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from web_agent import AppConfig, DiagnosticsConfig, SessionTraceRecorder


def _make_recorder(tmp_path: Path, trace_enabled: bool) -> SessionTraceRecorder:
    diag = DiagnosticsConfig(trace_enabled=trace_enabled, trace_dir=str(tmp_path / "traces"))
    return SessionTraceRecorder(diag, base_dir=str(tmp_path))


# ---------------------------------------------------------------------------
# off-by-default
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trace_recorder_disabled_does_not_create_file(tmp_path: Path) -> None:
    rec = _make_recorder(tmp_path, trace_enabled=False)
    await rec.record(
        session_id="sid1", method="action.click", args={}, status="success", elapsed_ms=10
    )
    assert not (tmp_path / "traces").exists()
    assert rec.enabled is False


# ---------------------------------------------------------------------------
# Append-only JSONL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trace_recorder_appends_one_jsonl_line_per_action(tmp_path: Path) -> None:
    rec = _make_recorder(tmp_path, trace_enabled=True)
    for i in range(3):
        await rec.record(
            session_id="sid1",
            method="action.click",
            args={"selector": f"#a{i}"},
            status="success",
            elapsed_ms=10.5,
        )
    p = rec.path_for("sid1")
    assert p.exists()
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    parsed = [json.loads(line) for line in lines]
    assert [e["args"]["selector"] for e in parsed] == ["#a0", "#a1", "#a2"]


@pytest.mark.asyncio
async def test_trace_recorder_ordinal_increments_in_order(tmp_path: Path) -> None:
    rec = _make_recorder(tmp_path, trace_enabled=True)
    for _ in range(4):
        await rec.record(
            session_id="sid1", method="action.click", args={}, status="success", elapsed_ms=1
        )
    entries = [
        json.loads(line)
        for line in rec.path_for("sid1").read_text(encoding="utf-8").strip().splitlines()
    ]
    assert [e["ordinal"] for e in entries] == [0, 1, 2, 3]


@pytest.mark.asyncio
async def test_trace_recorder_serializes_correlation_id_into_entry(tmp_path: Path) -> None:
    from web_agent.correlation import correlation_scope

    rec = _make_recorder(tmp_path, trace_enabled=True)
    with correlation_scope() as cid:
        await rec.record(
            session_id="sid1", method="action.click", args={}, status="success", elapsed_ms=1
        )
    entry = json.loads(rec.path_for("sid1").read_text(encoding="utf-8").strip())
    assert entry["correlation_id"] == cid


@pytest.mark.asyncio
async def test_trace_recorder_path_per_session_id(tmp_path: Path) -> None:
    rec = _make_recorder(tmp_path, trace_enabled=True)
    await rec.record(
        session_id="sidA", method="action.click", args={}, status="success", elapsed_ms=1
    )
    await rec.record(
        session_id="sidB", method="action.click", args={}, status="success", elapsed_ms=1
    )
    assert rec.path_for("sidA").exists()
    assert rec.path_for("sidB").exists()
    assert rec.path_for("sidA") != rec.path_for("sidB")


# ---------------------------------------------------------------------------
# Path traversal defense
# ---------------------------------------------------------------------------


def test_trace_recorder_rejects_unsafe_session_id(tmp_path: Path) -> None:
    rec = _make_recorder(tmp_path, trace_enabled=True)
    with pytest.raises(ValueError, match="Unsafe session_id"):
        rec.path_for("../escape")
    with pytest.raises(ValueError, match="Unsafe session_id"):
        rec.path_for("foo/bar")


# ---------------------------------------------------------------------------
# list_traces / load_entries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trace_recorder_list_traces_returns_session_ids(tmp_path: Path) -> None:
    rec = _make_recorder(tmp_path, trace_enabled=True)
    assert rec.list_traces() == []  # nothing yet, dir may not exist
    await rec.record(
        session_id="alpha", method="action.click", args={}, status="success", elapsed_ms=1
    )
    await rec.record(
        session_id="beta", method="action.click", args={}, status="success", elapsed_ms=1
    )
    assert rec.list_traces() == ["alpha", "beta"]


@pytest.mark.asyncio
async def test_trace_recorder_load_entries_round_trip(tmp_path: Path) -> None:
    rec = _make_recorder(tmp_path, trace_enabled=True)
    await rec.record(
        session_id="sid1",
        method="action.click",
        args={"selector": "#submit"},
        status="success",
        elapsed_ms=42.5,
        url="https://example.com/form",
    )
    entries = rec.load_entries(rec.path_for("sid1"))
    assert len(entries) == 1
    assert entries[0]["method"] == "action.click"
    assert entries[0]["args"] == {"selector": "#submit"}
    assert entries[0]["url"] == "https://example.com/form"


def test_trace_recorder_load_entries_missing_file_raises(tmp_path: Path) -> None:
    rec = _make_recorder(tmp_path, trace_enabled=True)
    with pytest.raises(FileNotFoundError):
        rec.load_entries(tmp_path / "does-not-exist.jsonl")


# ---------------------------------------------------------------------------
# Agent.replay_trace (action reconstruction)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_trace_reconstructs_action_list(tmp_path: Path) -> None:
    """Write a tiny trace and verify replay_trace reconstructs the action list."""
    from web_agent import Agent

    cfg = AppConfig(
        base_dir=str(tmp_path),
        diagnostics=DiagnosticsConfig(
            trace_enabled=True, trace_dir=str(tmp_path / "traces")
        ),
    )
    agent = Agent(cfg)
    # Stub execute_sequence so we don't actually launch a browser.
    expected = MagicMock(name="ActionSequenceResult")
    agent._actions.execute_sequence = AsyncMock(return_value=expected)

    # Write a 2-action trace by hand.
    trace_dir = Path(cfg.diagnostics.trace_dir)
    trace_dir.mkdir(parents=True, exist_ok=True)
    sid = "test-sid"
    f = trace_dir / f"{sid}.jsonl"
    f.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "method": "action.click",
                        "args": {"selector": "#a", "action": "click"},
                        "url": "https://example.com/start",
                        "status": "success",
                        "elapsed_ms": 1,
                    }
                ),
                json.dumps(
                    {
                        "method": "action.wait",
                        # WaitInput accepts ``timeout`` (ms) -- not duration_ms.
                        # Pydantic ignores unknown fields silently by default,
                        # but using a real WaitInput field makes the test
                        # actually exercise the deserialisation it claims.
                        "args": {"timeout": 100, "action": "wait"},
                        "status": "success",
                        "elapsed_ms": 100,
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    result = await agent.replay_trace(f)
    assert result is expected
    # execute_sequence was called with the start URL + reconstructed action list
    call = agent._actions.execute_sequence.await_args
    assert call.args[0] == "https://example.com/start"
    actions = call.args[1]
    assert len(actions) == 2
    # First reconstructed action should be ClickInput (action='click')
    assert actions[0].action == "click"
    assert actions[1].action == "wait"


@pytest.mark.asyncio
async def test_replay_trace_raises_when_no_replayable_actions(tmp_path: Path) -> None:
    from web_agent import Agent

    cfg = AppConfig(
        base_dir=str(tmp_path),
        diagnostics=DiagnosticsConfig(
            trace_enabled=True, trace_dir=str(tmp_path / "traces")
        ),
    )
    agent = Agent(cfg)
    trace_dir = Path(cfg.diagnostics.trace_dir)
    trace_dir.mkdir(parents=True, exist_ok=True)
    f = trace_dir / "empty.jsonl"
    f.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="no replayable"):
        await agent.replay_trace(f)


@pytest.mark.asyncio
async def test_agent_list_traces_returns_session_ids(tmp_path: Path) -> None:
    from web_agent import Agent

    cfg = AppConfig(
        base_dir=str(tmp_path),
        diagnostics=DiagnosticsConfig(
            trace_enabled=True, trace_dir=str(tmp_path / "traces")
        ),
    )
    agent = Agent(cfg)
    # Initially empty
    assert agent.list_traces() == []
    # Now create a trace file directly
    trace_dir = Path(cfg.diagnostics.trace_dir)
    trace_dir.mkdir(parents=True, exist_ok=True)
    (trace_dir / "abc.jsonl").write_text("{}\n", encoding="utf-8")
    assert agent.list_traces() == ["abc"]
