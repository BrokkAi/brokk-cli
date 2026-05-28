import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from acp.agent.connection import AgentSideConnection
from acp.helpers import update_agent_message_text, update_plan
from acp.schema import (
    InitializeResponse,
    NewSessionResponse,
    PlanEntry,
    PromptResponse,
    SetSessionConfigOptionResponse,
    SetSessionModelResponse,
)

import brokk_code.headless_anvil as headless_anvil_module
from brokk_code.headless_anvil import (
    ANVIL_READY_MESSAGE,
    HeadlessAcpClient,
    HeadlessAnvilError,
    _session_update_to_event,
    build_commit_prompt,
    build_headless_prompt,
    build_pr_create_prompt,
    build_pr_review_prompt,
)


def test_build_issue_writer_prompt_requires_repo_tags() -> None:
    with pytest.raises(HeadlessAnvilError, match="repo_owner"):
        build_headless_prompt(task_input="Create issue", mode="ISSUE_WRITER", tags={})


def test_build_issue_writer_prompt_includes_create_contract() -> None:
    prompt = build_headless_prompt(
        task_input="Investigate auth failure",
        mode="ISSUE_WRITER",
        tags={
            "repo_owner": "acme",
            "repo_name": "service",
        },
    )

    assert "Draft a GitHub issue for acme/service" in prompt
    assert '"title": "Issue title"' in prompt
    assert "Do not create the issue" in prompt
    assert "Investigate auth failure" in prompt


def test_build_issue_diagnose_prompt_requests_inner_body_only() -> None:
    prompt = build_headless_prompt(
        task_input="ignored",
        mode="ISSUE_DIAGNOSE",
        tags={
            "repo_owner": "acme",
            "repo_name": "service",
            "issue_number": "123",
        },
    )

    assert "Diagnose GitHub Issue #123" in prompt
    assert "Use this issue context" in prompt
    assert "Output only the inner Markdown diagnosis content" in prompt
    assert "The CLI will add the Brokk" in prompt
    assert "Do not include:" in prompt
    assert "fenced code blocks with triple backticks" in prompt
    assert "<!-- brokk:diagnosis:v1" not in prompt


def test_build_issue_solve_prompt_uses_prefetched_context_without_github_side_effects() -> None:
    prompt = build_headless_prompt(
        task_input="ignored",
        mode="ISSUE",
        tags={
            "repo_owner": "acme",
            "repo_name": "service",
            "issue_number": "123",
            "issue_context": "Issue title and comments",
        },
        skip_verification=False,
        max_issue_fix_attempts=2,
    )

    assert "Resolve GitHub Issue #123 in acme/service" in prompt
    assert "Issue title and comments" in prompt
    assert "Run focused verification after making changes" in prompt
    assert "Use at most 2 verification fix attempt(s)" in prompt
    assert (
        "Do not create branches, commit, push, open pull requests, or run GitHub commands" in prompt
    )
    assert "Fetch the issue title" not in prompt
    assert "Use the repository's GitHub tooling/authentication" not in prompt


def test_build_pr_review_prompt_includes_threshold() -> None:
    prompt = build_pr_review_prompt(
        owner="acme",
        repo="service",
        pr_number=42,
        severity_threshold="MEDIUM",
    )

    assert "pull request #42" in prompt.lower()
    assert "acme/service" in prompt
    assert "severity >= MEDIUM" in prompt


def test_build_commit_prompt_includes_contract_and_message() -> None:
    prompt = build_commit_prompt(message="Fix parser bug")

    assert "Return this exact git commit message" in prompt
    assert "Fix parser bug" in prompt
    assert "commit" in prompt.lower()


def test_headless_anvil_env_strips_sensitive_auth_tokens(monkeypatch) -> None:
    monkeypatch.setenv("BROKK_TEST_SECRET", "secret")

    env = headless_anvil_module._anvil_subprocess_env()

    assert "BROKK_TEST_SECRET" not in env


def test_build_pr_create_prompt_includes_contract_and_branches() -> None:
    prompt = build_pr_create_prompt(
        title="Ship ACP",
        body="Port headless commands.",
        base_branch="main",
        head_branch="feature/acp",
    )

    assert "Use `main` as the base branch." in prompt
    assert "Use `feature/acp` as the head branch." in prompt
    assert "Ship ACP" in prompt
    assert "Port headless commands." in prompt
    assert '"title": "Pull request title"' in prompt
    assert "Do not create the pull request" in prompt


def test_session_update_to_event_maps_agent_text_to_llm_token() -> None:
    event = _session_update_to_event(update_agent_message_text("hello"))

    assert event == {"type": "LLM_TOKEN", "data": {"token": "hello"}}


def test_session_update_to_event_maps_anvil_ready_message_to_notification() -> None:
    event = _session_update_to_event(update_agent_message_text(ANVIL_READY_MESSAGE))

    assert event == {
        "type": "NOTIFICATION",
        "data": {"level": "INFO", "message": ANVIL_READY_MESSAGE},
    }


def test_session_update_to_event_maps_plan_to_tool_output() -> None:
    event = _session_update_to_event(
        update_plan(
            [
                PlanEntry(content="Inspect", priority="high", status="completed"),
            ]
        )
    )

    assert event is not None
    assert event["type"] == "TOOL_OUTPUT"
    assert event["data"]["plan"][0]["content"] == "Inspect"


class _SdkTestAgent:
    def __init__(self) -> None:
        self.client: Any = None
        self.cwd: str | None = None
        self.config_options: dict[str, str] = {}
        self.prompt_text: str | None = None

    def on_connect(self, client: Any) -> None:
        self.client = client

    async def initialize(self, protocol_version: int, **_kwargs: Any) -> InitializeResponse:
        return InitializeResponse(protocol_version=protocol_version)

    async def new_session(self, cwd: str, **_kwargs: Any) -> NewSessionResponse:
        self.cwd = cwd
        return NewSessionResponse(session_id="sdk-test-session")

    async def set_session_model(
        self,
        model_id: str,
        session_id: str,
        **_kwargs: Any,
    ) -> SetSessionModelResponse:
        assert session_id == "sdk-test-session"
        return SetSessionModelResponse()

    async def set_config_option(
        self,
        config_id: str,
        session_id: str,
        value: str,
        **_kwargs: Any,
    ) -> SetSessionConfigOptionResponse:
        assert session_id == "sdk-test-session"
        self.config_options[config_id] = value
        return SetSessionConfigOptionResponse(configOptions=[])

    async def prompt(
        self,
        prompt: list[Any],
        session_id: str,
        message_id: str | None = None,
        **_kwargs: Any,
    ) -> PromptResponse:
        assert session_id == "sdk-test-session"
        assert message_id
        self.prompt_text = prompt[0].text

        await self.client.session_update(session_id, update_agent_message_text("starting "))
        await self.client.session_update(
            session_id,
            update_agent_message_text("done"),
        )
        return PromptResponse(stop_reason="end_turn")


@contextlib.asynccontextmanager
async def _sdk_agent_transport(
    agent: _SdkTestAgent,
) -> AsyncIterator[tuple[asyncio.StreamReader, asyncio.StreamWriter, object]]:
    async def open_socket_stream(
        sock: socket.socket,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        transport, _ = await loop.connect_accepted_socket(lambda: protocol, sock)
        writer = asyncio.StreamWriter(transport, protocol, reader, loop)
        return reader, writer

    async def handle_connection(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        connection = AgentSideConnection(
            agent,
            writer,
            reader,
            listening=False,
            use_unstable_protocol=True,
        )
        try:
            await connection.listen()
        finally:
            with contextlib.suppress(Exception):
                await connection.close()

    client_sock, agent_sock = socket.socketpair()
    client_sock.setblocking(False)
    agent_sock.setblocking(False)
    reader, writer = await open_socket_stream(client_sock)
    agent_reader, agent_writer = await open_socket_stream(agent_sock)
    connection_task = asyncio.create_task(handle_connection(agent_reader, agent_writer))
    try:
        yield reader, writer, object()
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        with contextlib.suppress(Exception):
            await connection_task


@pytest.mark.asyncio
async def test_headless_acp_client_round_trips_against_sdk_agent(
    monkeypatch,
    tmp_path: Path,
) -> None:
    agent = _SdkTestAgent()

    monkeypatch.setattr(
        headless_anvil_module,
        "resolve_anvil_binary",
        lambda **_kwargs: tmp_path / "fake-anvil",
    )

    @contextlib.asynccontextmanager
    async def fake_spawn_stdio_transport(
        *_args: Any,
        **_kwargs: Any,
    ) -> AsyncIterator[tuple[asyncio.StreamReader, asyncio.StreamWriter, object]]:
        async with _sdk_agent_transport(agent) as streams:
            yield streams

    monkeypatch.setattr(
        headless_anvil_module,
        "spawn_stdio_transport",
        fake_spawn_stdio_transport,
    )

    client = HeadlessAcpClient(
        workspace_dir=tmp_path,
        default_model="default-model",
    )

    await client.start()
    try:
        events = [
            event
            async for event in client.run_prompt(
                "hello ACP",
                model="chosen-model",
                reasoning_effort="high",
            )
        ]
    finally:
        await client.stop()

    assert agent.cwd == str(tmp_path)
    assert agent.config_options["model_selection"] == "chosen-model"
    assert agent.config_options["reasoning_effort"] == "high"
    assert agent.prompt_text == "hello ACP"
    assert {"type": "LLM_TOKEN", "data": {"token": "starting "}} in events
    assert {"type": "LLM_TOKEN", "data": {"token": "done"}} in events
    assert events[-1] == {"type": "STATE_CHANGE", "data": {"state": "COMPLETED"}}
