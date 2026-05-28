"""Headless Anvil ACP client used by non-interactive CLI commands."""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, AsyncIterator

from acp import PROTOCOL_VERSION, text_block
from acp.client import ClientSideConnection
from acp.schema import (
    AllowedOutcome,
    ClientCapabilities,
    Implementation,
    RequestPermissionResponse,
)
from acp.transports import spawn_stdio_transport

from brokk_code import __version__
from brokk_code.anvil_launcher import BUNDLED_ANVIL_VERSION, resolve_anvil_binary
from brokk_code.workspace import resolve_workspace_dir

ANVIL_MODEL_CONFIG_ID = "model_selection"
ANVIL_REASONING_EFFORT_CONFIG_ID = "reasoning_effort"
ANVIL_READY_MESSAGE = "Anvil is ready. Run `/setup` anytime to change or repair model setup."


class HeadlessAnvilError(Exception):
    """Raised when headless Anvil ACP execution fails before prompt completion."""


class HeadlessAcpClient:
    """Small ACP client facade for running Anvil prompts headlessly."""

    def __init__(
        self,
        *,
        workspace_dir: Path,
        anvil_binary: Path | None = None,
        anvil_version: str = BUNDLED_ANVIL_VERSION,
        default_model: str | None = None,
    ) -> None:
        self.workspace_dir = resolve_workspace_dir(workspace_dir)
        self.anvil_binary = anvil_binary
        self.anvil_version = anvil_version
        self.default_model = default_model
        self.session_id: str | None = None
        self.config_options: list[Any] = []

        self._stack: AsyncExitStack | None = None
        self._connection: ClientSideConnection | None = None
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def start(self) -> None:
        """Start Anvil and open an ACP session."""
        binary = await asyncio.to_thread(
            resolve_anvil_binary,
            version=self.anvil_version,
            override=self.anvil_binary,
        )
        args: list[str] = []
        if self.default_model:
            args.extend(["--default-model", self.default_model])

        stack = AsyncExitStack()
        try:
            reader, writer, _process = await stack.enter_async_context(
                spawn_stdio_transport(
                    str(binary),
                    *args,
                    cwd=self.workspace_dir,
                    env=_anvil_subprocess_env(),
                    stderr=asyncio.subprocess.DEVNULL,
                    limit=50 * 1024 * 1024,
                )
            )
            connection = ClientSideConnection(
                self,
                writer,
                reader,
                use_unstable_protocol=True,
            )
            await connection.initialize(
                protocol_version=PROTOCOL_VERSION,
                client_capabilities=ClientCapabilities(
                    terminal=False,
                ),
                client_info=Implementation(
                    name="brokk-code-headless",
                    title="Brokk Code Headless",
                    version=__version__,
                ),
            )
            response = await connection.new_session(cwd=str(self.workspace_dir))
            self.session_id = response.session_id
            self.config_options = list(response.config_options or [])
            self._stack = stack
            self._connection = connection
        except Exception:
            await stack.aclose()
            raise

    async def stop(self) -> None:
        """Close the ACP connection and terminate Anvil."""
        if self._connection is not None:
            with contextlib.suppress(Exception):
                await self._connection.close()
            self._connection = None
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None

    async def run_prompt(
        self,
        prompt: str,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Submit a prompt and yield normalized events until completion."""
        if self._connection is None or self.session_id is None:
            raise HeadlessAnvilError("Anvil ACP client not started")

        if model:
            await self.set_config_option(ANVIL_MODEL_CONFIG_ID, model)
        if reasoning_effort:
            await self.set_config_option(ANVIL_REASONING_EFFORT_CONFIG_ID, reasoning_effort)

        while not self._events.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._events.get_nowait()

        prompt_task = asyncio.create_task(
            self._connection.prompt(
                prompt=[text_block(prompt)],
                session_id=self.session_id,
                message_id=str(uuid.uuid4()),
            )
        )

        while True:
            if prompt_task.done() and self._events.empty():
                break
            try:
                yield await asyncio.wait_for(self._events.get(), timeout=0.1)
            except asyncio.TimeoutError:
                continue

        try:
            response = await prompt_task
        except Exception as exc:
            yield {"type": "ERROR", "data": {"message": str(exc)}}
            yield {"type": "STATE_CHANGE", "data": {"state": "FAILED"}}
            return

        state = "COMPLETED"
        if response.stop_reason == "cancelled":
            state = "CANCELLED"
        elif response.stop_reason == "refusal":
            state = "FAILED"
        yield {"type": "STATE_CHANGE", "data": {"state": state}}

    async def set_config_option(self, config_id: str, value: str) -> list[Any]:
        if self._connection is None or self.session_id is None:
            raise HeadlessAnvilError("Anvil ACP client not started")
        try:
            response = await self._connection.set_config_option(
                config_id=config_id,
                session_id=self.session_id,
                value=value,
            )
            self.config_options = list(response.config_options)
            return self.config_options
        except Exception as exc:
            raise HeadlessAnvilError(
                f"Anvil rejected session option {config_id}={value!r}: {exc}"
            ) from exc

    async def session_update(self, session_id: str, update: Any) -> None:
        """Receive ACP session/update notifications from Anvil."""
        if self.session_id and session_id != self.session_id:
            return
        event = _session_update_to_event(update)
        if event is not None:
            await self._events.put(event)

    async def request_permission(
        self,
        options: list[Any],
        **_kwargs: Any,
    ) -> RequestPermissionResponse:
        """Headless mode auto-approves the strongest allow option Anvil offers."""
        preferred = None
        for option in options:
            if getattr(option, "kind", "") == "allow_always":
                preferred = option
                break
            if getattr(option, "kind", "") == "allow_once":
                preferred = option
        if preferred is None and options:
            preferred = options[0]
        option_id = getattr(preferred, "option_id", "approve")
        return RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id=option_id)
        )


def build_headless_prompt(
    *,
    task_input: str,
    mode: str,
    tags: dict[str, str],
    skip_verification: bool | None = None,
    max_issue_fix_attempts: int | None = None,
) -> str:
    """Build an Anvil prompt for a non-interactive CLI mode."""
    mode = mode.upper()
    if mode == "ISSUE_WRITER":
        return _issue_writer_prompt(task_input=task_input, tags=tags)
    if mode == "ISSUE_DIAGNOSE":
        return _issue_diagnose_prompt(tags=tags)
    if mode == "ISSUE":
        return _issue_solve_prompt(
            tags=tags,
            skip_verification=skip_verification,
            max_issue_fix_attempts=max_issue_fix_attempts,
        )
    return (
        f"{task_input}\n\n"
        "Run as a headless coding task. Inspect the repository, make the necessary changes, "
        "and report the final result. Prefer focused verification, but do not run long or "
        "destructive commands unless they are directly needed."
    )


def build_pr_review_prompt(
    *,
    owner: str,
    repo: str,
    pr_number: int,
    diff: str | None = None,
    pr_title: str | None = None,
    pr_description: str | None = None,
    severity_threshold: str | None = None,
) -> str:
    severity = severity_threshold or "HIGH"
    diff_block = diff or "<diff unavailable>"
    title = pr_title or ""
    description = pr_description or ""
    return f"""
Review GitHub pull request #{pr_number} in {owner}/{repo}.

PR title:
{title}

PR description:
{description}

Diff:
```diff
{diff_block}
```

Review the changes for correctness, security, behavioral regressions, and missing tests.

Output only a JSON object with this exact shape:

{{
  "summaryMarkdown": "## Brokk PR Review\\n\\n1-3 sentences describing the most important risks.",
  "comments": [
    {{
      "path": "relative/path.py",
      "line": 42,
      "severity": "HIGH",
      "bodyMarkdown": "Concise actionable review finding."
    }}
  ]
}}

Only include comments with severity >= {severity}. Use an empty comments array if nothing
meets that threshold. Do not post anything to GitHub; the CLI will post the review.
""".strip()


def build_commit_prompt(*, message: str | None = None) -> str:
    """Build an Anvil prompt for deriving a commit message."""
    if message and message.strip():
        return f"Return this exact git commit message and nothing else:\n\n{message.strip()}"
    return """
Inspect the staged and unstaged repository changes and derive one concise git commit
message.

Output only the commit message. Do not stage files, do not commit, and do not run git.
""".strip()


def build_pr_create_prompt(
    *,
    title: str | None = None,
    body: str | None = None,
    base_branch: str | None = None,
    head_branch: str | None = None,
) -> str:
    """Build an Anvil prompt for deriving pull request text."""
    title_guidance = (
        f"Use this exact pull request title:\n\n{title.strip()}"
        if title and title.strip()
        else "Derive a clear pull request title from the branch diff and commit history."
    )
    body_guidance = (
        f"Use this exact pull request body:\n\n{body.strip()}"
        if body and body.strip()
        else "Derive a useful Markdown pull request body from the branch diff and commit history."
    )
    base_guidance = (
        f"Use `{base_branch}` as the base branch."
        if base_branch
        else "Use the repository default branch as the base branch."
    )
    head_guidance = (
        f"Use `{head_branch}` as the head branch."
        if head_branch
        else "Use the current branch as the head branch."
    )
    return f"""
Draft GitHub pull request metadata for this repository.

{base_guidance}
{head_guidance}
{title_guidance}
{body_guidance}

Output only a JSON object with this exact shape:

{{
  "title": "Pull request title",
  "body": "Markdown pull request body"
}}

Do not create the pull request and do not run GitHub commands; the CLI will create it.
""".strip()


def _issue_writer_prompt(*, task_input: str, tags: dict[str, str]) -> str:
    owner = _required_tag(tags, "repo_owner")
    repo = _required_tag(tags, "repo_name")
    return f"""
Draft a GitHub issue for {owner}/{repo}.

Inspect the repository for evidence relevant to this request:

{task_input}

Output only a JSON object with this exact shape:

{{
  "title": "Issue title",
  "body": "Markdown issue body"
}}

Do not create the issue and do not run GitHub commands; the CLI will create it.
""".strip()


def _issue_diagnose_prompt(*, tags: dict[str, str]) -> str:
    owner = _required_tag(tags, "repo_owner")
    repo = _required_tag(tags, "repo_name")
    issue_number = _required_tag(tags, "issue_number")
    issue_context = tags.get("issue_context", "").strip() or "(No issue context provided)"
    return f"""
Diagnose GitHub Issue #{issue_number} in {owner}/{repo}.

Use this issue context plus the repository files available in this workspace:

{issue_context}

Output only the inner Markdown diagnosis content. The CLI will add the Brokk
diagnosis marker, the "## Issue Analysis" heading, and next-step instructions.

Do not include:

- HTML comments
- A "## Issue Analysis" heading
- "---" separators
- "Next steps" text
- brokk issue solve commands

Do not post the comment and do not run GitHub commands; the CLI will post it.
""".strip()


def _issue_solve_prompt(
    *,
    tags: dict[str, str],
    skip_verification: bool | None,
    max_issue_fix_attempts: int | None,
) -> str:
    owner = _required_tag(tags, "repo_owner")
    repo = _required_tag(tags, "repo_name")
    issue_number = _required_tag(tags, "issue_number")
    build_settings = tags.get("build_settings")
    build_guidance = (
        f"Use these repository build settings when relevant:\n\n{build_settings.strip()}"
        if build_settings and build_settings.strip()
        else "Use the repository's normal build and test conventions."
    )
    verification = (
        "Skip per-task and final verification because skipVerification=true."
        if skip_verification
        else "Run focused verification after making changes."
    )
    attempts = (
        f"Use at most {max_issue_fix_attempts} verification fix attempt(s)."
        if max_issue_fix_attempts is not None
        else "Use a reasonable number of verification fix attempts."
    )
    return f"""
Resolve GitHub Issue #{issue_number} in {owner}/{repo}.

Use the repository's GitHub tooling/authentication, such as `gh`. Fetch the issue title,
body, recent comments, and any prior Brokk diagnosis comments. Create a new branch for the
fix, inspect the repository, implement the smallest correct change, and commit the result.

{build_guidance}
{verification}
{attempts}

Push the branch and open a pull request against the repository default branch. The pull
request body must reference and close issue #{issue_number}. Finish by reporting the PR URL.
""".strip()


def _required_tag(tags: dict[str, str], key: str) -> str:
    value = tags.get(key)
    if value is None or not value.strip():
        raise HeadlessAnvilError(f"Missing required tag for headless Anvil prompt: {key}")
    return value.strip()


def _anvil_subprocess_env() -> dict[str, str]:
    """Build Anvil's environment without forwarding sensitive auth variables."""
    sensitive_markers = ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
    return {
        key: value
        for key, value in os.environ.items()
        if not any(marker in key.upper() for marker in sensitive_markers)
    }


def _session_update_to_event(update: Any) -> dict[str, Any] | None:
    update_kind = getattr(update, "session_update", "")
    if update_kind in {"agent_message_chunk", "agent_thought_chunk"}:
        text = _content_text(getattr(update, "content", None))
        if text.strip().startswith(ANVIL_READY_MESSAGE):
            return {"type": "NOTIFICATION", "data": {"level": "INFO", "message": text.strip()}}
        if text:
            return {"type": "LLM_TOKEN", "data": {"token": text}}
    if update_kind == "tool_call":
        title = getattr(update, "title", "")
        if title:
            return {"type": "NOTIFICATION", "data": {"level": "INFO", "message": title}}
    if update_kind == "tool_call_update":
        title = getattr(update, "title", "")
        status = getattr(update, "status", None)
        if title or status:
            message = title if title else f"Tool status: {status}"
            return {"type": "TOOL_OUTPUT", "data": {"text": message, "status": status}}
    if update_kind == "plan":
        entries = [entry.model_dump(mode="json") for entry in getattr(update, "entries", [])]
        return {"type": "TOOL_OUTPUT", "data": {"plan": entries}}
    if update_kind == "usage_update":
        return {
            "type": "TOKEN_USAGE",
            "data": {
                "used": getattr(update, "used", None),
                "size": getattr(update, "size", None),
                "cost": getattr(update, "cost", None),
            },
        }
    return None


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if getattr(content, "type", None) == "text":
        return str(getattr(content, "text", ""))
    return ""
