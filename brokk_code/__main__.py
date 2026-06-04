import argparse
import asyncio
import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from brokk_code.anvil_config import (
    configure_anvil_scripting_interactive,
    delete_anvil_scripting_config,
    format_anvil_scripting_config,
    resolve_anvil_selection,
)
from brokk_code.anvil_launcher import run_anvil_acp_server
from brokk_code.avante_config import configure_nvim_avante_acp_settings
from brokk_code.event_utils import is_failure_state, safe_data
from brokk_code.git_utils import infer_github_repo_from_remote
from brokk_code.headless_anvil import (
    ANVIL_READY_MESSAGE,
    HeadlessAcpClient,
    HeadlessAnvilError,
    build_commit_prompt,
    build_headless_prompt,
    build_pr_create_prompt,
    build_pr_review_prompt,
)
from brokk_code.intellij_config import configure_intellij_acp_settings
from brokk_code.mcp_config import (
    configure_claude_code_mcp_settings,
    configure_codex_mcp_settings,
    install_claude_mcp_summaries_skill,
    install_claude_mcp_workspace_skill,
    install_codex_local_plugin,
    install_codex_mcp_summaries_skill,
    install_codex_mcp_workspace_skill,
)
from brokk_code.nvim_config import configure_nvim_codecompanion_acp_settings
from brokk_code.nvim_init_patch import wire_nvim_plugin_setup
from brokk_code.uv_utils import UvSetupError, ensure_uv_ready
from brokk_code.workspace import resolve_workspace_dir
from brokk_code.zed_config import ExistingBrokkCodeEntryError, configure_zed_acp_settings

REPO_COMPONENT_ALLOWLIST_REGEX = r"^[A-Za-z0-9_.-]+$"


def _extract_event_text(event: dict[str, Any]) -> str:
    raw = event.get("data")
    if isinstance(raw, str):
        return raw.strip()
    data = safe_data(event)
    for key in ("message", "text", "detail", "error", "token", "resultText", "output"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("message", "text", "detail", "error", "token", "resultText", "output"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _extract_pr_url(text: str) -> str:
    match = re.search(r"https://github\.com/[^\s)]+/pull/\d+", text)
    return match.group(0) if match else ""


def _extract_issue_url(text: str) -> str:
    match = re.search(r"https://github\.com/[^\s)]+/issues/\d+", text)
    return match.group(0) if match else ""


def _print_progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _die(message: str) -> None:
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(1)


def _extract_json_object(text: str) -> dict[str, Any]:
    raw = text.strip()
    welcome_lines = {
        ANVIL_READY_MESSAGE.strip(),
        f"INFO: {ANVIL_READY_MESSAGE}".strip(),
        "Anvil found a working model setup and is ready to use.",
        "INFO: Anvil found a working model setup and is ready to use.",
        "Run `/setup` anytime to change or repair model setup.",
        "INFO: Run `/setup` anytime to change or repair model setup.",
    }
    filtered_lines = [line for line in raw.splitlines() if line.strip() not in welcome_lines]
    raw = "\n".join(filtered_lines).strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw).strip()
    candidates = [raw]
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        candidates.append(raw[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("Anvil response did not contain a valid JSON object")


def _json_string_field(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Anvil JSON response is missing required string field: {key}")
    return value.strip()


def _git_output(workspace_dir: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace_dir), *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip()


def _git_head(workspace_dir: Path) -> str | None:
    return _git_output(workspace_dir, "rev-parse", "HEAD")


def _git_status_porcelain(workspace_dir: Path) -> str | None:
    return _git_output(workspace_dir, "status", "--porcelain")


def _clean_generated_commit_message(text: str) -> str:
    """Normalize common LLM wrappers around an intended commit message."""
    message = text.strip()
    if not message:
        return ""

    welcome_lines = {
        ANVIL_READY_MESSAGE.strip(),
        f"INFO: {ANVIL_READY_MESSAGE}".strip(),
    }
    filtered_lines = [line for line in message.splitlines() if line.strip() not in welcome_lines]
    message = "\n".join(filtered_lines).strip()
    if not message:
        return ""

    fence_match = re.fullmatch(r"```(?:[A-Za-z0-9_-]+)?\s*\n?(.*?)\n?```", message, re.DOTALL)
    if fence_match:
        message = fence_match.group(1).strip()

    try:
        command_parts = shlex.split(message)
    except ValueError:
        command_parts = []
    if len(command_parts) >= 4 and command_parts[:2] == ["git", "commit"]:
        commit_message_parts: list[str] = []
        index = 2
        while index < len(command_parts):
            part = command_parts[index]
            if part in {"-m", "--message"} and index + 1 < len(command_parts):
                commit_message_parts.append(command_parts[index + 1])
                index += 2
                continue
            index += 1
        if commit_message_parts:
            message = "\n\n".join(commit_message_parts).strip()

    label_match = re.match(
        r"(?is)^(?:commit\s+message|message|here(?:'s| is) the commit message)\s*:\s*(.+)$",
        message,
    )
    if label_match:
        message = label_match.group(1).strip()

    if len(message) >= 2 and message[0] == message[-1] and message[0] in {'"', "'"}:
        message = message[1:-1].strip()

    return message


def _run_git(workspace_dir: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace_dir), *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip() or (exc.stdout or "").strip() or str(exc)
        _die(f"git {' '.join(args)} failed: {detail}")
    except OSError as exc:
        _die(f"git {' '.join(args)} failed: {exc}")
    return result.stdout.strip()


def _run_gh(args: list[str], *, cwd: Path | None = None, action_label: str) -> str:
    try:
        result = subprocess.run(
            ["gh", *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            env={**os.environ, "GH_PROMPT_DISABLED": "1"},
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip() or (exc.stdout or "").strip() or str(exc)
        _die(f"{action_label} failed: {detail}")
    except OSError as exc:
        _die(f"{action_label} failed: {exc}")
    return result.stdout.strip()


async def _run_anvil_text_prompt(
    *,
    workspace_dir: Path,
    prompt: str,
    model: str | None,
    reasoning_effort: str | None,
    anvil_binary: Path | None,
    anvil_version: str | None,
    progress_label: str,
    verbose: bool = False,
) -> str:
    manager = HeadlessAcpClient(
        workspace_dir=workspace_dir,
        anvil_binary=anvil_binary,
        anvil_version=anvil_version,
        default_model=model,
    )
    try:
        _print_progress(f"Starting Anvil for {progress_label}...")
        await manager.start()
        transcript: list[str] = []
        last_error: str | None = None
        last_state: str | None = None
        token_output_open = False

        def _print_verbose_event(event: dict[str, Any], text: str) -> None:
            nonlocal token_output_open
            event_type = str(event.get("type") or "UNKNOWN")
            data = safe_data(event)
            if event_type in {"TOKEN", "LLM_TOKEN"}:
                if text:
                    sys.stderr.write(text)
                    sys.stderr.flush()
                    token_output_open = True
                return
            if token_output_open:
                sys.stderr.write("\n")
                token_output_open = False
            payload = data if data else {k: v for k, v in event.items() if k != "type"}
            if payload:
                rendered = json.dumps(payload, ensure_ascii=True, default=str)
                print(f"[{event_type}] {rendered}", file=sys.stderr)
            else:
                print(f"[{event_type}]", file=sys.stderr)

        _print_progress(f"Submitting {progress_label} prompt to Anvil...")
        async for event in manager.run_prompt(
            prompt,
            model=model,
            reasoning_effort=reasoning_effort,
        ):
            event_type = event.get("type")
            data = safe_data(event)
            if event_type in {"TOKEN", "LLM_TOKEN"}:
                text = str(data.get("token", event.get("text", "")))
            else:
                text = _extract_event_text(event)
            if verbose:
                _print_verbose_event(event, text)
            if event_type in {"TOKEN", "LLM_TOKEN"} and text:
                transcript.append(text)
            if event_type == "ERROR":
                last_error = text or "unknown error"
            elif event_type == "STATE_CHANGE":
                last_state = str(data.get("state", event.get("state", "UNKNOWN")))
        if token_output_open:
            sys.stderr.write("\n")
            sys.stderr.flush()

        if last_error or (last_state and is_failure_state(last_state)):
            detail = f": {last_error}" if last_error else ""
            raise HeadlessAnvilError(f"{progress_label} failed{detail}")
        return "".join(transcript).strip()
    finally:
        await manager.stop()


def _resolve_neovim_plugin(*, plugin: str | None) -> str:
    if plugin:
        return plugin
    if not sys.stdin.isatty():
        return "codecompanion"

    print("Choose a Neovim plugin integration:")
    print("1) CodeCompanion (ACP adapter)")
    print("2) Avante (ACP provider)")
    choice = input("Selection [1/2] (default: 1): ").strip().lower()
    if choice in {"", "1", "codecompanion"}:
        return "codecompanion"
    if choice in {"2", "avante"}:
        return "avante"
    raise ValueError(f"Invalid plugin selection: '{choice}'")


def _add_github_issue_args(parser: argparse.ArgumentParser) -> None:
    """Add common GitHub issue arguments to a parser."""
    parser.add_argument(
        "--repo-owner",
        type=str,
        help="GitHub repository owner",
    )
    parser.add_argument(
        "--repo-name",
        type=str,
        help="GitHub repository name",
    )
    _add_anvil_selection_args(parser)
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=False,
        help="Show full Anvil ACP output (events/tokens) for debugging",
    )


def _add_anvil_selection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Override the configured Anvil model id for this task",
    )
    parser.add_argument(
        "--reasoning-effort",
        type=str,
        default=None,
        help="Override the configured Anvil reasoning effort for this task",
    )


def _validate_github_params(
    repo_owner: str | None,
    repo_name: str | None,
    command_name: str,
) -> None:
    if not repo_owner:
        print(f"Error: --repo-owner is required for {command_name}", file=sys.stderr)
        sys.exit(1)
    if not repo_name:
        print(f"Error: --repo-name is required for {command_name}", file=sys.stderr)
        sys.exit(1)

    if not re.match(REPO_COMPONENT_ALLOWLIST_REGEX, repo_owner):
        print(
            f"Error: Invalid --repo-owner '{repo_owner}'. "
            f"Repo owner must match {REPO_COMPONENT_ALLOWLIST_REGEX}",
            file=sys.stderr,
        )
        sys.exit(1)
    if not re.match(REPO_COMPONENT_ALLOWLIST_REGEX, repo_name):
        print(
            f"Error: Invalid --repo-name '{repo_name}'. "
            f"Repo name must match {REPO_COMPONENT_ALLOWLIST_REGEX}",
            file=sys.stderr,
        )
        sys.exit(1)


def _add_common_runtime_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--worktree",
        action="store_true",
        default=False,
        help="Create an isolated git worktree for this session and clean up on exit if no changes",
    )
    parser.add_argument(
        "--anvil-binary",
        type=Path,
        default=None,
        help="Path to an Anvil binary for headless/ACP commands",
    )
    parser.add_argument(
        "--anvil-version",
        type=str,
        default=None,
        help="Anvil version to use when downloading for headless/ACP commands (default: latest)",
    )


def _run_issue_command(
    args: argparse.Namespace,
    command_name: str,
    mode: str,
    task_input: str,
    action_label: str,
    *,
    include_issue_number: bool = False,
    skip_verification: bool | None = None,
    max_issue_fix_attempts: int | None = None,
    build_settings: str | None = None,
    tool_key: str,
) -> None:
    """Run a GitHub issue command with shared validation, checkout, and job execution."""
    _validate_github_params(args.repo_owner, args.repo_name, command_name)
    tags: dict[str, str] = {
        "repo_owner": args.repo_owner,
        "repo_name": args.repo_name,
    }
    if include_issue_number:
        tags["issue_number"] = str(args.issue_number)
    if build_settings:
        tags["build_settings"] = build_settings
    selection = resolve_anvil_selection(
        tool_key=tool_key,
        model_override=args.model,
        reasoning_override=args.reasoning_effort,
        workspace_dir=Path.cwd().resolve(),
        anvil_binary=args.anvil_binary,
        anvil_version=args.anvil_version,
    )

    with _temporary_issue_repo_checkout(
        repo_owner=args.repo_owner,
        repo_name=args.repo_name,
        action_label=action_label,
    ) as issue_workspace_path:
        if include_issue_number and mode in {"ISSUE_DIAGNOSE", "ISSUE"}:
            tags["issue_context"] = _fetch_github_issue_context(
                repo_owner=args.repo_owner,
                repo_name=args.repo_name,
                issue_number=args.issue_number,
                cwd=issue_workspace_path,
            )
        asyncio.run(
            run_headless_job(
                workspace_dir=issue_workspace_path,
                task_input=task_input,
                model=selection.model,
                reasoning_effort=selection.reasoning_effort,
                skip_verification=skip_verification,
                max_issue_fix_attempts=max_issue_fix_attempts,
                verbose=args.verbose,
                mode=mode,
                tags=tags,
                anvil_binary=args.anvil_binary,
                anvil_version=args.anvil_version,
            )
        )


def _run_issue_solve_command(args: argparse.Namespace) -> None:
    """Run issue solve with Python-owned git/GitHub side effects."""
    command_name = "issue solve"
    _validate_github_params(args.repo_owner, args.repo_name, command_name)
    tags: dict[str, str] = {
        "repo_owner": args.repo_owner,
        "repo_name": args.repo_name,
        "issue_number": str(args.issue_number),
    }
    if args.build_settings:
        tags["build_settings"] = args.build_settings
    selection = resolve_anvil_selection(
        tool_key="issue_solve",
        model_override=args.model,
        reasoning_override=args.reasoning_effort,
        workspace_dir=Path.cwd().resolve(),
        anvil_binary=args.anvil_binary,
        anvil_version=args.anvil_version,
    )

    with _temporary_issue_repo_checkout(
        repo_owner=args.repo_owner,
        repo_name=args.repo_name,
        action_label="Issue solve",
    ) as issue_workspace_path:
        issue_context = _fetch_github_issue_context(
            repo_owner=args.repo_owner,
            repo_name=args.repo_name,
            issue_number=args.issue_number,
            cwd=issue_workspace_path,
        )
        tags["issue_context"] = issue_context
        asyncio.run(
            run_issue_solve(
                workspace_dir=issue_workspace_path,
                issue_number=args.issue_number,
                repo_owner=args.repo_owner,
                repo_name=args.repo_name,
                issue_context=issue_context,
                tags=tags,
                model=selection.model,
                reasoning_effort=selection.reasoning_effort,
                skip_verification=args.skip_verification,
                max_issue_fix_attempts=args.max_issue_fix_attempts,
                verbose=args.verbose,
                anvil_binary=args.anvil_binary,
                anvil_version=args.anvil_version,
            )
        )


@contextlib.contextmanager
def _temporary_issue_repo_checkout(
    *,
    repo_owner: str,
    repo_name: str,
    action_label: str,
) -> Iterator[Path]:
    temp_parent = Path(tempfile.mkdtemp(prefix="brokk-issue-repo-"))
    temp_workspace_dir = temp_parent / repo_name
    repo_slug = f"{repo_owner}/{repo_name}"
    try:
        _ensure_gh_available(action_label=action_label)
        print(
            f"{action_label}: shallow cloning {repo_slug} into {temp_workspace_dir}",
            flush=True,
        )
        subprocess.run(
            [
                "gh",
                "repo",
                "clone",
                repo_slug,
                str(temp_workspace_dir),
                "--",
                "--depth",
                "1",
                "--single-branch",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={
                **os.environ,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ASKPASS": "true",
                "SSH_ASKPASS": "true",
                "GCM_INTERACTIVE": "Never",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
            },
        )
        print(f"{action_label}: cloned repository at {temp_workspace_dir}", flush=True)
        yield temp_workspace_dir
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip() or (exc.stdout or "").strip() or str(exc)
        print(f"Error: {action_label.lower()} clone failed: {detail}", file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        print(f"Error: {action_label.lower()} clone failed: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        print(f"{action_label}: removing temporary checkout at {temp_workspace_dir}", flush=True)
        shutil.rmtree(temp_parent, ignore_errors=True)


def _ensure_gh_available(*, action_label: str) -> None:
    if shutil.which("gh") is None:
        print(
            f"Error: {action_label.lower()} requires the GitHub CLI (`gh`) "
            "to clone the repository.",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        subprocess.run(
            ["gh", "auth", "status"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "GH_PROMPT_DISABLED": "1"},
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip() or (exc.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        print(
            f"Error: {action_label.lower()} requires an authenticated GitHub CLI "
            f"(`gh auth login`){suffix}",
            file=sys.stderr,
        )
        sys.exit(1)


def _github_repo_slug(repo_owner: str, repo_name: str) -> str:
    return f"{repo_owner}/{repo_name}"


def _create_github_issue(
    *,
    repo_owner: str,
    repo_name: str,
    title: str,
    body: str,
    cwd: Path | None = None,
) -> str:
    _ensure_gh_available(action_label="Issue create")
    output = _run_gh(
        [
            "issue",
            "create",
            "--repo",
            _github_repo_slug(repo_owner, repo_name),
            "--title",
            title,
            "--body",
            body,
        ],
        cwd=cwd,
        action_label="issue create",
    )
    issue_url = _extract_issue_url(output)
    if not issue_url:
        _die("issue create did not return a GitHub issue URL")
    return issue_url


def _post_github_issue_comment(
    *,
    repo_owner: str,
    repo_name: str,
    issue_number: int,
    body: str,
    cwd: Path | None = None,
) -> str:
    _ensure_gh_available(action_label="Issue comment")
    _run_gh(
        [
            "issue",
            "comment",
            str(issue_number),
            "--repo",
            _github_repo_slug(repo_owner, repo_name),
            "--body",
            body,
        ],
        cwd=cwd,
        action_label="issue comment",
    )
    comment_url = _find_github_issue_comment_url(
        repo_owner=repo_owner,
        repo_name=repo_name,
        issue_number=issue_number,
        body=body,
        cwd=cwd,
    )
    if not comment_url:
        _die("issue comment did not appear on GitHub after gh returned success")
    return comment_url


def _find_github_issue_comment_url(
    *,
    repo_owner: str,
    repo_name: str,
    issue_number: int,
    body: str,
    cwd: Path | None = None,
) -> str:
    output = _run_gh(
        [
            "issue",
            "view",
            str(issue_number),
            "--repo",
            _github_repo_slug(repo_owner, repo_name),
            "--comments",
            "--json",
            "comments",
        ],
        cwd=cwd,
        action_label="issue comment verification",
    )
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return ""
    comments = data.get("comments")
    if not isinstance(comments, list):
        return ""
    for comment in reversed(comments):
        if not isinstance(comment, dict):
            continue
        if comment.get("body") != body:
            continue
        url = comment.get("url")
        return url.strip() if isinstance(url, str) else ""
    return ""


def _validate_issue_diagnosis_body(diagnosis: str) -> None:
    lower = diagnosis.lower()
    if "<!-- brokk:diagnosis" in lower:
        raise ValueError("diagnosis included the Brokk diagnosis wrapper")
    if "## issue analysis" in lower:
        raise ValueError("diagnosis included the Issue Analysis heading")
    if "**next steps:**" in lower or "brokk issue solve" in lower:
        raise ValueError("diagnosis included CLI next-step instructions")
    if "anvil is ready. run `/setup`" in lower:
        raise ValueError("diagnosis included the Anvil startup message")
    if "llm request failed" in lower:
        raise ValueError("diagnosis included an LLM failure message")


def _sanitize_issue_diagnosis_body(diagnosis: str) -> str:
    return diagnosis.replace("```", "&#96;&#96;&#96;")


def _fetch_github_issue_context(
    *,
    repo_owner: str,
    repo_name: str,
    issue_number: int,
    cwd: Path | None = None,
) -> str:
    _ensure_gh_available(action_label="Issue fetch")
    output = _run_gh(
        [
            "issue",
            "view",
            str(issue_number),
            "--repo",
            _github_repo_slug(repo_owner, repo_name),
            "--comments",
            "--json",
            "title,body,comments,url",
        ],
        cwd=cwd,
        action_label="issue fetch",
    )
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return output
    title = data.get("title") if isinstance(data.get("title"), str) else ""
    body = data.get("body") if isinstance(data.get("body"), str) else ""
    url = data.get("url") if isinstance(data.get("url"), str) else ""
    out = [
        f"# GitHub Issue #{issue_number}: {title}",
        "",
        f"URL: {url}",
        "",
        "## Description",
        "",
        body,
    ]
    comments = data.get("comments")
    if isinstance(comments, list) and comments:
        out.extend(["", "## Comments", ""])
        for comment in comments[-50:]:
            if not isinstance(comment, dict):
                continue
            author = comment.get("author")
            login = author.get("login") if isinstance(author, dict) else "unknown"
            created = comment.get("createdAt", "unknown time")
            comment_body = comment.get("body", "")
            out.append(f"@{login} ({created}):")
            out.append(str(comment_body))
            out.append("")
    return "\n".join(out).strip()


def _issue_solve_branch_name(issue_number: int) -> str:
    return f"brokk/issue-{issue_number}-{uuid.uuid4().hex[:8]}"


def _issue_title_from_context(issue_context: str, issue_number: int) -> str:
    marker = f"# GitHub Issue #{issue_number}:"
    for line in issue_context.splitlines():
        if not line.startswith(marker):
            continue
        return line.removeprefix(marker).strip()
    return ""


def _issue_solve_pr_title(issue_context: str, issue_number: int) -> str:
    issue_title = _issue_title_from_context(issue_context, issue_number)
    return f"Fix {issue_title}" if issue_title else f"Fix issue #{issue_number}"


def _issue_solve_pr_body(issue_number: int) -> str:
    return f"""Fixes #{issue_number}

Implemented by `brokk issue solve`.
""".strip()


def _create_github_pr(
    *,
    title: str,
    body: str,
    base_branch: str | None,
    head_branch: str | None,
    cwd: Path,
) -> str:
    _ensure_gh_available(action_label="PR create")
    args = ["pr", "create", "--title", title, "--body", body]
    if base_branch:
        args.extend(["--base", base_branch])
    if head_branch:
        args.extend(["--head", head_branch])
    output = _run_gh(args, cwd=cwd, action_label="PR create")
    pr_url = _extract_pr_url(output)
    if not pr_url:
        _die("PR create did not return a GitHub pull request URL")
    return pr_url


def _fetch_pr_review_context(
    *,
    repo_owner: str,
    repo_name: str,
    pr_number: int,
    cwd: Path,
) -> tuple[str, str, str]:
    _ensure_gh_available(action_label="PR review")
    repo_slug = _github_repo_slug(repo_owner, repo_name)
    metadata_output = _run_gh(
        [
            "pr",
            "view",
            str(pr_number),
            "--repo",
            repo_slug,
            "--json",
            "title,body",
        ],
        cwd=cwd,
        action_label="PR metadata fetch",
    )
    try:
        metadata = json.loads(metadata_output)
    except json.JSONDecodeError:
        metadata = {}
    title = metadata.get("title") if isinstance(metadata.get("title"), str) else ""
    body = metadata.get("body") if isinstance(metadata.get("body"), str) else ""
    diff = _run_gh(
        ["pr", "diff", str(pr_number), "--repo", repo_slug, "--patch"],
        cwd=cwd,
        action_label="PR diff fetch",
    )
    return title, body, diff


def _format_pr_review_body(review: dict[str, Any], *, severity_threshold: str | None) -> str:
    summary = _json_string_field(review, "summaryMarkdown")
    comments = review.get("comments", [])
    if not isinstance(comments, list):
        raise ValueError("Anvil PR review JSON field comments must be an array")
    findings: list[str] = []
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        path = str(comment.get("path") or "").strip()
        line = comment.get("line")
        severity = str(comment.get("severity") or "LOW").strip().upper()
        body = str(comment.get("bodyMarkdown") or "").strip()
        if not body:
            continue
        location = (
            f"`{path}:{line}`" if path and isinstance(line, int) else (f"`{path}`" if path else "")
        )
        prefix = f"- **{severity}**"
        if location:
            prefix += f" at {location}"
        findings.append(f"{prefix}: {body}")
    if findings:
        return summary.rstrip() + "\n\n### Findings\n\n" + "\n".join(findings)
    threshold = severity_threshold or "HIGH"
    return (
        summary.rstrip() + f"\n\nNo findings met the configured severity threshold ({threshold})."
    )


def _post_github_pr_review(
    *,
    repo_owner: str,
    repo_name: str,
    pr_number: int,
    body: str,
    cwd: Path,
) -> str:
    _ensure_gh_available(action_label="PR review")
    return _run_gh(
        [
            "pr",
            "review",
            str(pr_number),
            "--repo",
            _github_repo_slug(repo_owner, repo_name),
            "--comment",
            "--body",
            body,
        ],
        cwd=cwd,
        action_label="PR review post",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Brokk Code - Interactive Terminal Interface")
    _add_common_runtime_args(parser)

    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser(
        "acp",
        help="Run Anvil in ACP server mode",
        add_help=False,
    )
    anvil_config_parser = subparsers.add_parser(
        "anvil-config",
        help="Configure model settings for Anvil-backed scripting commands",
    )
    anvil_config_parser.add_argument(
        "--anvil-binary",
        type=Path,
        default=None,
        help="Path to an Anvil binary",
    )
    anvil_config_parser.add_argument(
        "--anvil-version",
        type=str,
        default=None,
        help="Anvil version to use when downloading (default: latest release)",
    )
    anvil_config_parser.add_argument(
        "--show",
        action="store_true",
        default=False,
        help="Print the current Anvil scripting configuration",
    )
    anvil_config_parser.add_argument(
        "--reset",
        action="store_true",
        default=False,
        help="Delete the Anvil scripting configuration",
    )

    subparsers.add_parser(
        "mcp",
        help="Run the bifrost MCP server (downloads the latest release on first use)",
        add_help=False,
    )

    install_parser = subparsers.add_parser("install", help="Install integration settings")
    install_parser.add_argument(
        "target",
        choices=["zed", "intellij", "jetbrains", "nvim", "neovim", "mcp", "codex-plugin"],
        help="Install target for integration settings (jetbrains is an alias for intellij)",
    )
    install_parser.add_argument(
        "--plugin",
        choices=["codecompanion", "avante"],
        default=None,
        help=(
            "Neovim plugin integration to install (codecompanion or avante). "
            "Only used for install targets nvim/neovim; when omitted, an interactive "
            "selection menu is shown in TTY sessions."
        ),
    )
    install_parser.add_argument(
        "--native",
        action="store_true",
        default=False,
        help=(
            "[Deprecated] Native is now the default; this flag is a no-op alias "
            "kept for compatibility."
        ),
    )
    install_parser.add_argument(
        "--rust",
        action="store_true",
        default=False,
        help=(
            "Wire the editor at the Rust ACP server (brokk-acp) instead of the Python or "
            "Java implementations. Writes the literal commands `brokk-acp` and `bifrost` "
            "into the editor's agent_servers config; both must be on the PATH the editor "
            "inherits at agent-launch time (use --brokk-acp-binary to write an explicit "
            "path instead). Requires --model. Mutually exclusive with --native. "
            "zed/intellij only."
        ),
    )
    install_parser.add_argument(
        "--brokk-acp-binary",
        type=Path,
        default=None,
        help=(
            "Write this brokk-acp path verbatim into the editor's agent_servers args "
            "instead of the literal `brokk-acp`. Path must exist. Dev use only."
        ),
    )
    install_parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Overwrite existing install configuration when supported",
    )
    install_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=False,
        help="Accepted for compatibility; install no longer warms runtime dependencies",
    )
    install_parser.add_argument(
        "--model",
        default=None,
        help="Model name to write for --rust editor integration",
    )
    install_parser.add_argument(
        "--endpoint-url",
        default=None,
        help="Endpoint URL to write for --rust editor integration",
    )
    install_parser.add_argument(
        "--api-key",
        default="",
        help="API key to write for --rust editor integration",
    )

    commit_parser = subparsers.add_parser("commit", help="Commit current changes")
    _add_common_runtime_args(commit_parser)
    commit_parser.add_argument(
        "message",
        type=str,
        nargs="?",
        default=None,
        help="Commit message (optional; if omitted, a message will be generated)",
    )
    _add_anvil_selection_args(commit_parser)
    commit_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=False,
        help="Show full Anvil ACP output (events/tokens) for debugging",
    )

    exec_parser = subparsers.add_parser(
        "exec", help="Run a prompt with LITE_AGENT (scan + architect, no build)"
    )
    _add_common_runtime_args(exec_parser)
    exec_parser.add_argument(
        "prompt",
        type=str,
        help="The task to execute",
    )
    _add_anvil_selection_args(exec_parser)
    exec_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=False,
        help="Show full output (events/tokens) for debugging",
    )

    issue_parser = subparsers.add_parser("issue", help="Manage GitHub issues")
    issue_subparsers = issue_parser.add_subparsers(dest="issue_command", required=True)

    # Note: 'issue create' maps to ISSUE_WRITER mode in the Java HeadlessExecCli.
    # It follows the same required parameters: repo owner/name, token, and prompt.
    issue_create_parser = issue_subparsers.add_parser("create", help="Create a new GitHub issue")
    _add_common_runtime_args(issue_create_parser)
    issue_create_parser.add_argument(
        "prompt",
        type=str,
        help="Description of the issue to create",
    )
    _add_github_issue_args(issue_create_parser)

    issue_diagnose_parser = issue_subparsers.add_parser(
        "diagnose", help="Analyze a GitHub issue and post a diagnosis comment"
    )
    _add_common_runtime_args(issue_diagnose_parser)
    issue_diagnose_parser.add_argument(
        "--issue-number",
        type=int,
        required=True,
        help="The GitHub issue number to diagnose",
    )
    _add_github_issue_args(issue_diagnose_parser)

    issue_solve_parser = issue_subparsers.add_parser("solve", help="Fix an existing GitHub issue")
    _add_common_runtime_args(issue_solve_parser)
    issue_solve_parser.add_argument(
        "--issue-number",
        type=int,
        required=True,
        help="The GitHub issue number to solve",
    )
    _add_github_issue_args(issue_solve_parser)
    issue_solve_parser.add_argument(
        "--skip-verification",
        action="store_true",
        default=False,
        help="Skip per-task and final verification steps",
    )
    issue_solve_parser.add_argument(
        "--max-issue-fix-attempts",
        type=int,
        default=None,
        help="Maximum iterations for final verification fix loop",
    )
    issue_solve_parser.add_argument(
        "--build-settings",
        type=str,
        default=None,
        help="JSON string of build settings overrides",
    )

    # PR commands
    pr_parser = subparsers.add_parser("pr", help="Manage pull requests")
    pr_subparsers = pr_parser.add_subparsers(dest="pr_command", required=True)

    pr_create_parser = pr_subparsers.add_parser("create", help="Create a pull request")
    _add_common_runtime_args(pr_create_parser)
    pr_create_parser.add_argument(
        "--title",
        type=str,
        default=None,
        help="PR title (if omitted, will be suggested by LLM)",
    )
    pr_create_parser.add_argument(
        "--body",
        type=str,
        default=None,
        help="PR body/description (if omitted, will be suggested by LLM)",
    )
    pr_create_parser.add_argument(
        "--base",
        type=str,
        default=None,
        help="Target/base branch (defaults to repository default branch)",
    )
    pr_create_parser.add_argument(
        "--head",
        type=str,
        default=None,
        help="Source/head branch (defaults to current branch)",
    )
    _add_anvil_selection_args(pr_create_parser)
    pr_create_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=False,
        help="Show full Anvil ACP output (events/tokens) for debugging",
    )
    pr_review_parser = pr_subparsers.add_parser("review", help="Review a pull request")
    _add_common_runtime_args(pr_review_parser)
    pr_review_parser.add_argument(
        "--pr-number",
        type=int,
        required=True,
        help="The pull request number to review",
    )
    pr_review_parser.add_argument(
        "--repo-owner",
        type=str,
        default=None,
        help="GitHub repository owner (inferred from git remote if omitted)",
    )
    pr_review_parser.add_argument(
        "--repo-name",
        type=str,
        default=None,
        help="GitHub repository name (inferred from git remote if omitted)",
    )
    _add_anvil_selection_args(pr_review_parser)
    pr_review_parser.add_argument(
        "--severity",
        type=str,
        default=None,
        choices=["CRITICAL", "HIGH", "MEDIUM", "LOW"],
        help="Minimum severity threshold for inline comments (default: HIGH)",
    )
    pr_review_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=False,
        help="Show full Anvil ACP output (events/tokens) for debugging",
    )

    version_parser = subparsers.add_parser("version", help="Print version information")
    _add_common_runtime_args(version_parser)

    return parser


async def run_commit(
    workspace_dir: Path,
    message: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    verbose: bool = False,
    anvil_binary: Path | None = None,
    anvil_version: str | None = None,
) -> None:
    """Commits current changes with a message generated by Anvil ACP."""
    try:
        initial_head = _git_head(workspace_dir)
        status = _git_status_porcelain(workspace_dir)
        if status is None:
            _die("unable to inspect git status for commit")
        if not status:
            print("No uncommitted changes.")
            return

        commit_message = (message or "").strip()
        if not commit_message:
            generated_message = await _run_anvil_text_prompt(
                workspace_dir=workspace_dir,
                prompt=build_commit_prompt(message=None),
                model=model,
                reasoning_effort=reasoning_effort,
                anvil_binary=anvil_binary,
                anvil_version=anvil_version,
                progress_label="commit message",
                verbose=verbose,
            )
            commit_message = _json_string_field(
                _extract_json_object(generated_message),
                "message",
            )
        if not commit_message:
            _die("Anvil did not return a commit message")

        _run_git(workspace_dir, "add", "-A")
        staged = _git_output(workspace_dir, "diff", "--cached", "--name-only")
        if not staged:
            _die("no staged changes remain after git add")
        _run_git(workspace_dir, "commit", "-m", commit_message)

        current_head = _git_head(workspace_dir)
        if not current_head or current_head == initial_head:
            _die("git commit did not create a new commit")
        print(f"Committed {current_head[:7]}: {commit_message.splitlines()[0]}")

    except HeadlessAnvilError as e:
        print(f"Anvil ACP error during commit: {e}", file=sys.stderr)
        sys.exit(1)
    except SystemExit:
        raise
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        sys.exit(1)


async def run_issue_solve(
    *,
    workspace_dir: Path,
    issue_number: int,
    repo_owner: str,
    repo_name: str,
    issue_context: str,
    tags: dict[str, str],
    model: str | None = None,
    reasoning_effort: str | None = None,
    skip_verification: bool | None = None,
    max_issue_fix_attempts: int | None = None,
    verbose: bool = False,
    anvil_binary: Path | None = None,
    anvil_version: str | None = None,
) -> None:
    """Solve an issue with Anvil changes and Python-owned git/GitHub operations."""
    initial_head = _git_head(workspace_dir)
    if not initial_head:
        _die("unable to inspect git HEAD for issue solve")

    branch_name = _issue_solve_branch_name(issue_number)
    _run_git(workspace_dir, "checkout", "-b", branch_name)

    await run_headless_job(
        workspace_dir=workspace_dir,
        task_input=f"Resolve GitHub Issue #{issue_number}",
        model=model,
        reasoning_effort=reasoning_effort,
        skip_verification=skip_verification,
        max_issue_fix_attempts=max_issue_fix_attempts,
        verbose=verbose,
        mode="ISSUE",
        tags=tags,
        anvil_binary=anvil_binary,
        anvil_version=anvil_version,
    )

    status = _git_status_porcelain(workspace_dir)
    if status is None:
        _die("unable to inspect git status for issue solve")
    if not status:
        _die("issue solve did not produce any file changes")

    _run_git(workspace_dir, "add", "-A")
    staged = _git_output(workspace_dir, "diff", "--cached", "--name-only")
    if not staged:
        _die("no staged changes remain after git add")

    commit_message = f"Fix issue #{issue_number}"
    _run_git(workspace_dir, "commit", "-m", commit_message)
    current_head = _git_head(workspace_dir)
    if not current_head or current_head == initial_head:
        _die("git commit did not create a new commit")

    _run_git(workspace_dir, "push", "-u", "origin", branch_name)
    pr_url = _create_github_pr(
        title=_issue_solve_pr_title(issue_context, issue_number),
        body=_issue_solve_pr_body(issue_number),
        base_branch=None,
        head_branch=branch_name,
        cwd=workspace_dir,
    )
    print(f"Issue solve pull request created: {pr_url}")


async def _derive_pr_create_text(
    *,
    workspace_dir: Path,
    title: str | None,
    body: str | None,
    base_branch: str | None,
    head_branch: str | None,
    model: str | None,
    reasoning_effort: str | None,
    anvil_binary: Path | None,
    anvil_version: str | None,
    verbose: bool = False,
) -> tuple[str, str]:
    if title and title.strip() and body and body.strip():
        return title.strip(), body.strip()
    text = await _run_anvil_text_prompt(
        workspace_dir=workspace_dir,
        prompt=build_pr_create_prompt(
            title=title,
            body=body,
            base_branch=base_branch,
            head_branch=head_branch,
        ),
        model=model,
        reasoning_effort=reasoning_effort,
        anvil_binary=anvil_binary,
        anvil_version=anvil_version,
        progress_label="PR description",
        verbose=verbose,
    )
    data = _extract_json_object(text)
    final_title = title.strip() if title and title.strip() else _json_string_field(data, "title")
    final_body = body.strip() if body and body.strip() else _json_string_field(data, "body")
    return final_title, final_body


async def run_pr_create(
    workspace_dir: Path,
    title: str | None = None,
    body: str | None = None,
    base_branch: str | None = None,
    head_branch: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    verbose: bool = False,
    anvil_binary: Path | None = None,
    anvil_version: str | None = None,
) -> None:
    """Creates a pull request, using Anvil ACP only to draft missing text."""
    try:
        pr_title, pr_body = await _derive_pr_create_text(
            workspace_dir=workspace_dir,
            title=title,
            body=body,
            base_branch=base_branch,
            head_branch=head_branch,
            model=model,
            reasoning_effort=reasoning_effort,
            anvil_binary=anvil_binary,
            anvil_version=anvil_version,
            verbose=verbose,
        )
        pr_url = _create_github_pr(
            title=pr_title,
            body=pr_body,
            base_branch=base_branch,
            head_branch=head_branch,
            cwd=workspace_dir,
        )
        print(f"Pull request created: {pr_url}")

    except HeadlessAnvilError as e:
        print(f"Anvil ACP error during PR create: {e}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"Anvil ACP error during PR create: {e}", file=sys.stderr)
        sys.exit(1)
    except SystemExit:
        raise
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        sys.exit(1)


async def run_pr_review_job(
    workspace_dir: Path,
    pr_number: int,
    repo_owner: str,
    repo_name: str,
    model: str | None = None,
    reasoning_effort: str | None = None,
    severity_threshold: str | None = None,
    verbose: bool = False,
    anvil_binary: Path | None = None,
    anvil_version: str | None = None,
) -> None:
    """Generate a PR review with Anvil ACP and post it with gh."""
    try:
        pr_title, pr_body, pr_diff = _fetch_pr_review_context(
            repo_owner=repo_owner,
            repo_name=repo_name,
            pr_number=pr_number,
            cwd=workspace_dir,
        )
        review_text = await _run_anvil_text_prompt(
            workspace_dir=workspace_dir,
            prompt=build_pr_review_prompt(
                owner=repo_owner,
                repo=repo_name,
                pr_number=pr_number,
                diff=pr_diff,
                pr_title=pr_title,
                pr_description=pr_body,
                severity_threshold=severity_threshold,
            ),
            model=model,
            reasoning_effort=reasoning_effort,
            anvil_binary=anvil_binary,
            anvil_version=anvil_version,
            progress_label=f"PR review #{pr_number}",
            verbose=verbose,
        )
        review = _extract_json_object(review_text)
        review_body = _format_pr_review_body(review, severity_threshold=severity_threshold)
        _post_github_pr_review(
            repo_owner=repo_owner,
            repo_name=repo_name,
            pr_number=pr_number,
            body=review_body,
            cwd=workspace_dir,
        )
        if verbose:
            print(review_body)
        print(f"PR #{pr_number} review posted.")

    except HeadlessAnvilError as e:
        print(f"Anvil ACP error during PR review job: {e}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"Anvil ACP error during PR review job: {e}", file=sys.stderr)
        sys.exit(1)
    except SystemExit:
        raise
    except Exception as e:
        print(f"Unexpected error during PR review job: {e}", file=sys.stderr)
        sys.exit(1)


async def run_headless_job(
    workspace_dir: Path,
    task_input: str,
    mode: str,
    tags: dict[str, str],
    model: str | None = None,
    reasoning_effort: str | None = None,
    skip_verification: bool | None = None,
    max_issue_fix_attempts: int | None = None,
    verbose: bool = False,
    anvil_binary: Path | None = None,
    anvil_version: str | None = None,
) -> None:
    """Runs a non-interactive job via Anvil ACP and streams events to stdout."""
    manager = HeadlessAcpClient(
        workspace_dir=workspace_dir,
        anvil_binary=anvil_binary,
        anvil_version=anvil_version,
        default_model=model,
    )

    stage = "initializing"
    job_id: str | None = None
    last_state: str | None = None
    error_messages: list[str] = []
    created_issue_url: str | None = None
    token_url_scan_buffer = ""
    answer_chunks: list[str] = []
    spinner_index = 0
    spinner_active = False
    spinner_label = "Creating issue"
    spinner_frames = "|/-\\"
    spinner_enabled = sys.stdout.isatty() and not verbose
    progress_label = {
        "ISSUE_WRITER": "issue create",
        "ISSUE_DIAGNOSE": "issue diagnose",
        "ISSUE": "issue solve",
        "LITE_AGENT": "exec",
    }.get(mode, mode.lower())

    def _extract_message(event: dict[str, Any]) -> str:
        raw = event.get("data")
        if isinstance(raw, str):
            return raw.strip()
        data = safe_data(event)
        for key in ("message", "text", "detail", "error"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key in ("message", "text", "detail", "error"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _record_issue_url(text: str) -> None:
        nonlocal created_issue_url, token_url_scan_buffer
        if created_issue_url or not text:
            return
        token_url_scan_buffer = (token_url_scan_buffer + text)[-8192:]
        match = re.search(r"https://github\.com/[^\s)\]>\"]+/issues/\d+", token_url_scan_buffer)
        if match:
            created_issue_url = match.group(0)

    def _record_issue_url_from_issue_writer_notification(message: str) -> None:
        # Legacy compatibility: older success events emitted:
        # "ISSUE_WRITER: issue created <id> <htmlUrl>"
        if created_issue_url or not message:
            return
        normalized = message.strip()
        if not normalized.startswith("ISSUE_WRITER: issue created"):
            return
        _record_issue_url(normalized)

    def _record_issue_url_from_structured_issue_created(event: dict[str, Any]) -> None:
        if created_issue_url:
            return
        data = safe_data(event)
        issue_url = data.get("issueUrl")
        if isinstance(issue_url, str) and issue_url.strip():
            _record_issue_url(issue_url.strip())

    def _render_spinner() -> None:
        nonlocal spinner_index, spinner_active
        if mode not in {"ISSUE_WRITER", "ISSUE"} or not spinner_enabled:
            return
        frame = spinner_frames[spinner_index % len(spinner_frames)]
        spinner_index += 1
        label = "Solving issue" if mode == "ISSUE" else spinner_label
        sys.stdout.write(f"\r{label}... {frame}")
        sys.stdout.flush()
        spinner_active = True

    def _clear_spinner() -> None:
        nonlocal spinner_active
        if not spinner_enabled or not spinner_active:
            return
        # Calculate max possible width to clear (Issue # + long labels)
        width = len(spinner_label) + 20
        sys.stdout.write("\r" + (" " * width) + "\r")
        sys.stdout.flush()
        spinner_active = False

    def _update_shutdown_context() -> None:
        context_parts = [f"mode={mode}", f"stage={stage}"]
        if job_id:
            context_parts.append(f"job_id={job_id}")
        if last_state:
            context_parts.append(f"last_state={last_state}")
        if error_messages:
            context_parts.append(f"last_error={error_messages[-1]}")
        manager.shutdown_context = ", ".join(context_parts)

    try:
        stage = "starting Anvil"
        _update_shutdown_context()
        _print_progress(f"Starting Anvil for {progress_label}...")
        await manager.start()

        stage = "submitting prompt"
        _update_shutdown_context()
        _render_spinner()
        job_id = f"acp-{uuid.uuid4()}"
        prompt = build_headless_prompt(
            task_input=task_input,
            mode=mode,
            tags=tags,
            skip_verification=skip_verification,
            max_issue_fix_attempts=max_issue_fix_attempts,
        )
        _update_shutdown_context()

        stage = "streaming ACP events"
        _update_shutdown_context()
        _print_progress(f"Submitting {progress_label} task to Anvil...")
        async for event in manager.run_prompt(
            prompt,
            model=model,
            reasoning_effort=reasoning_effort,
        ):
            _render_spinner()
            event_type = event.get("type")
            data = safe_data(event)
            if event_type == "NOTIFICATION":
                message = _extract_message(event)
                if not message:
                    continue
                _record_issue_url_from_issue_writer_notification(message)
                _record_issue_url(message)
                level = str(data.get("level", event.get("level", "INFO"))).strip().upper()
                # LITE_AGENT always shows notifications; others only show WARN/ERROR.
                if (
                    not verbose
                    and mode != "LITE_AGENT"
                    and level not in {"WARN", "WARNING", "ERROR"}
                ):
                    continue
                _clear_spinner()
                print(f"[{level}] {message}")
            elif event_type == "STATE_CHANGE":
                last_state = str(data.get("state", event.get("state", "UNKNOWN")))
                _update_shutdown_context()
                if verbose:
                    _clear_spinner()
                    print(f"Job state: {last_state}")
            elif event_type in {"TOKEN", "LLM_TOKEN"}:
                text = str(data.get("token", event.get("text", "")))
                if text:
                    answer_chunks.append(text)
                _record_issue_url(text)
                # LITE_AGENT always streams tokens; other modes only when verbose.
                if (verbose or mode == "LITE_AGENT") and text:
                    sys.stdout.write(text)
                    sys.stdout.flush()
                continue
            elif event_type == "ERROR":
                message = _extract_message(event) or "Unknown error event"
                _record_issue_url(message)
                error_messages.append(message)
                _update_shutdown_context()
                _clear_spinner()
                print(f"\nError event: {message}", file=sys.stderr)
            elif event_type == "ISSUE_CREATED":
                _record_issue_url_from_structured_issue_created(event)
                if verbose:
                    _clear_spinner()
                    print(f"[ISSUE_CREATED] {data}")
            elif event_type == "COMMAND_RESULT":
                if verbose:
                    _clear_spinner()
                    print(f"[COMMAND_RESULT] {data}")
                _record_issue_url(str(data.get("output", "")))
                _record_issue_url(str(data.get("resultText", "")))
                _record_issue_url(str(data.get("command", "")))
                _record_issue_url(str(data.get("exception", "")))
            elif event_type == "TOOL_OUTPUT":
                if verbose:
                    _clear_spinner()
                    print(f"[TOOL_OUTPUT] {data}")
                _record_issue_url(str(data.get("output", "")))
                _record_issue_url(str(data.get("text", "")))
                _record_issue_url(str(data.get("resultText", "")))

        if is_failure_state(last_state or ""):
            _clear_spinner()
            detail = f" Last error: {error_messages[-1]}" if error_messages else ""
            print(
                f"\n{mode} job ended with state {last_state}.{detail}",
                file=sys.stderr,
            )
            sys.exit(1)

        if error_messages and last_state != "COMPLETED":
            _clear_spinner()
            detail = f" Last error: {error_messages[-1]}"
            observed_state = last_state or "UNKNOWN"
            print(
                f"\n{mode} job ended with errors (last observed state: {observed_state}).{detail}",
                file=sys.stderr,
            )
            sys.exit(1)

        _clear_spinner()
        if mode == "ISSUE_WRITER":
            draft_text = "".join(answer_chunks).strip()
            try:
                draft = _extract_json_object(draft_text)
                issue_title = _json_string_field(draft, "title")
                issue_body = _json_string_field(draft, "body")
            except ValueError as e:
                print(f"Anvil ACP error during {mode} job: {e}", file=sys.stderr)
                sys.exit(1)
            issue_url = _create_github_issue(
                repo_owner=tags["repo_owner"],
                repo_name=tags["repo_name"],
                title=issue_title,
                body=issue_body,
                cwd=workspace_dir,
            )
            print(f"Issue created: {issue_url}")
        elif mode == "ISSUE_DIAGNOSE":
            diagnosis = "".join(answer_chunks).strip()
            if not diagnosis:
                print(
                    f"Anvil ACP error during {mode} job: no agent response text received",
                    file=sys.stderr,
                )
                sys.exit(1)
            try:
                _validate_issue_diagnosis_body(diagnosis)
            except ValueError as e:
                print(f"Anvil ACP error during {mode} job: {e}", file=sys.stderr)
                sys.exit(1)
            diagnosis = _sanitize_issue_diagnosis_body(diagnosis)
            issue_number = int(tags["issue_number"])
            timestamp = datetime.now(UTC).isoformat()
            solve_command = (
                f"brokk issue solve --issue-number {issue_number} "
                f"--repo-owner {tags['repo_owner']} --repo-name {tags['repo_name']}"
            )
            comment_body = f"""
<!-- brokk:diagnosis:v1 timestamp="{timestamp}" -->

## Issue Analysis

{diagnosis}

---

**Next steps:** To fix this issue, run:

`{solve_command}`
""".strip()
            comment_url = _post_github_issue_comment(
                repo_owner=tags["repo_owner"],
                repo_name=tags["repo_name"],
                issue_number=issue_number,
                body=comment_body,
                cwd=workspace_dir,
            )
            print(f"Diagnosis posted: {comment_url}")
        else:
            print("Job finished.")

    except HeadlessAnvilError as e:
        _clear_spinner()
        _update_shutdown_context()
        print(f"Anvil ACP error during {mode} job ({stage}): {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        _clear_spinner()
        _update_shutdown_context()
        print(f"Unexpected error during {mode} job ({stage}): {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        _clear_spinner()
        _update_shutdown_context()
        await manager.stop()


# Commands that don't operate on a workspace and should skip worktree creation
_NON_WORKSPACE_COMMANDS = {"anvil-config", "install", "version"}


def _resolve_worktree_workspace_path(
    workspace_path: Path, repo_root: Path, worktree_path: Path
) -> Path:
    """Map the selected workspace into the corresponding location inside a worktree."""
    try:
        relative_workspace = workspace_path.relative_to(repo_root)
    except ValueError:
        return worktree_path
    return worktree_path / relative_workspace


def _passthrough_command_from_argv(argv: list[str]) -> tuple[str, list[str]] | None:
    """Return passthrough command and args without argparse consuming root flags.

    The acp/mcp subcommands are transparent launchers. argparse root options
    such as --worktree or --anvil-binary must therefore not be interpreted as
    Brokk-owned flags when they appear before those launcher commands.
    """
    root_options_with_values = {"--anvil-binary", "--anvil-version"}
    root_flags = {"--worktree"}
    help_flags = {"-h", "--help"}

    index = 0
    while index < len(argv):
        token = argv[index]
        if token in help_flags:
            return None
        if token == "--":
            index += 1
            break
        if token in root_flags:
            index += 1
            continue
        if token in root_options_with_values:
            index += 2
            continue
        if any(token.startswith(f"{option}=") for option in root_options_with_values):
            index += 1
            continue
        break

    if index >= len(argv):
        return None

    command = argv[index]
    if command not in {"acp", "mcp"}:
        return None

    return command, [*argv[:index], *argv[index + 1 :]]


def main():
    raw_args = sys.argv[1:]
    passthrough_command = _passthrough_command_from_argv(raw_args)
    if passthrough_command is not None:
        command, passthrough_args = passthrough_command
        args = argparse.Namespace(command=command)
        _main_dispatch(args, Path.cwd().resolve(), passthrough_args)
        return

    parser = _build_parser()
    args, unknown = parser.parse_known_args()

    if unknown and args.command not in {"acp", "mcp"}:
        parser.error(f"unrecognized arguments: {' '.join(unknown)}")

    if args.command is None:
        parser.print_help()
        return

    workspace_path = Path.cwd().resolve()

    use_worktree = getattr(args, "worktree", False) and args.command not in _NON_WORKSPACE_COMMANDS
    if use_worktree:
        from brokk_code.git_utils import worktree_context

        repo_root = resolve_workspace_dir(workspace_path)
        with worktree_context(repo_root) as wt_path:
            wt_workspace = _resolve_worktree_workspace_path(workspace_path, repo_root, wt_path)
            _main_dispatch(args, wt_workspace, unknown)
    else:
        _main_dispatch(args, workspace_path, unknown)


def _main_dispatch(
    args: argparse.Namespace,
    workspace_path: Path,
    unknown: list[str],
) -> None:
    """Core command dispatch, extracted to support optional worktree wrapping."""
    if args.command == "install":
        if args.target == "jetbrains":
            args.target = "intellij"
        # Fast-fail validation before prompting for API keys
        if args.plugin and args.target not in {"nvim", "neovim"}:
            print("Error: --plugin is only valid for install targets nvim/neovim", file=sys.stderr)
            sys.exit(1)
        if args.rust and args.native:
            print(
                "Error: --rust and --native are mutually exclusive; pass only one.",
                file=sys.stderr,
            )
            sys.exit(1)
        if args.native:
            print(
                "Warning: --native is deprecated and ignored; `brokk acp` now launches Anvil.",
                file=sys.stderr,
            )
        if args.rust and args.target not in {"zed", "intellij"}:
            print(
                "Error: --rust is only supported for install targets zed/intellij.",
                file=sys.stderr,
            )
            sys.exit(1)
        if args.rust and not args.model:
            print(
                "Error: --rust requires --model (passed to brokk-acp as --default-model).",
                file=sys.stderr,
            )
            sys.exit(1)
        if args.brokk_acp_binary and not args.rust:
            print(
                "Error: --brokk-acp-binary requires --rust.",
                file=sys.stderr,
            )
            sys.exit(1)

        # Rust ACP path: self-contained. The Rust binary connects directly to
        # the user's chosen LLM endpoint and never talks to Brokk's service.
        # brokk-code does NOT build or fetch brokk-acp/bifrost; the user is
        # responsible for installing them. We just resolve their paths.
        if args.rust:
            from brokk_code.rust_acp_install import (
                RustAcpInstallError,
                RustAcpPaths,
                resolve_rust_paths,
            )

            try:
                brokk_acp_path, bifrost_path = resolve_rust_paths(
                    brokk_acp_override=args.brokk_acp_binary,
                )
            except RustAcpInstallError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                sys.exit(1)
            rust_paths = RustAcpPaths(
                brokk_acp=brokk_acp_path,
                bifrost=bifrost_path,
                model=args.model,
                endpoint_url=args.endpoint_url,
                api_key=args.api_key,
            )
            try:
                if args.target == "zed":
                    settings_path = configure_zed_acp_settings(
                        force=args.force, rust_paths=rust_paths
                    )
                    integration = "Zed"
                else:
                    settings_path = configure_intellij_acp_settings(
                        force=args.force, rust_paths=rust_paths
                    )
                    integration = "IntelliJ"
            except (ExistingBrokkCodeEntryError, ValueError) as exc:
                print(f"Error: {exc}", file=sys.stderr)
                sys.exit(1)
            print(f"Wired editor at brokk-acp=`{brokk_acp_path}` bifrost=`{bifrost_path}`")
            print(f"Configured {integration} ACP integration in {settings_path}")
            return

        messages: list[str] = []
        try:
            uv_binary = ensure_uv_ready()
            # Path/str conversions on Windows produce backslashes, but these values
            # are written into JSON configs where we want stable POSIX-style paths.
            uvx_command = str(Path(uv_binary).parent / "uvx").replace("\\", "/")

            if args.target == "zed":
                settings_path = configure_zed_acp_settings(
                    force=args.force,
                    uvx_command=uvx_command,
                    native=False,
                )
                messages = [f"Configured Zed ACP integration in {settings_path}"]
            elif args.target == "intellij":
                settings_path = configure_intellij_acp_settings(
                    force=args.force,
                    uvx_command=uvx_command,
                    native=False,
                )
                messages = [f"Configured IntelliJ ACP integration in {settings_path}"]
            elif args.target in {"nvim", "neovim"}:
                selected_plugin = _resolve_neovim_plugin(plugin=args.plugin)
                if selected_plugin == "codecompanion":
                    settings_path = configure_nvim_codecompanion_acp_settings(force=args.force)
                    patch_result = wire_nvim_plugin_setup(
                        plugin_repo="olimorris/codecompanion.nvim",
                        module_name="brokk.brokk_codecompanion",
                    )
                    messages = [
                        f"Configured Neovim CodeCompanion ACP adapter in {settings_path}",
                        "",
                        "What this is:",
                        "- CodeCompanion is a Neovim AI/chat plugin.",
                        "- Brokk runs as an ACP agent server (`brokk acp`).",
                        "- The generated file wires CodeCompanion -> Brokk over ACP.",
                        "",
                        "Next steps:",
                        "1. Install codecompanion.nvim in Neovim (plugin manager):",
                        "   https://github.com/olimorris/codecompanion.nvim",
                        "2. Read setup docs if needed:",
                        "   https://codecompanion.olimorris.dev/",
                    ]
                    if patch_result.status == "patched":
                        messages.extend(
                            [
                                f"3. Updated {patch_result.path} to load Brokk automatically.",
                            ]
                        )
                    elif patch_result.status == "already_configured":
                        messages.extend(
                            [
                                f"3. {patch_result.path} already loads Brokk.",
                            ]
                        )
                    else:
                        messages.extend(
                            [
                                "3. Auto-wiring skipped to avoid risky edits.",
                                "   Add this in your lazy.nvim spec for codecompanion.nvim:",
                                "   opts = function()",
                                "     return require('brokk.brokk_codecompanion')",
                                "   end",
                            ]
                        )
                    messages.extend(
                        [
                            "4. Use the Brokk adapter in CodeCompanion chat:",
                            "   :CodeCompanionChat adapter=brokk",
                            "   (the generated module sets brokk as the default chat adapter)",
                            "",
                            "Troubleshooting:",
                            "- If you see 'Copilot Adapter: No token found', ",
                            "CodeCompanion is still",
                            "  using its default adapter and your Brokk module is not loaded",
                            "  in setup yet.",
                            "",
                            "Note: this command writes adapter config and may patch init.lua only",
                            "when it",
                            "can do a conservative, safe edit. It does not install Neovim plugins.",
                        ]
                    )
                else:
                    settings_path = configure_nvim_avante_acp_settings(force=args.force)
                    patch_result = wire_nvim_plugin_setup(
                        plugin_repo="yetone/avante.nvim",
                        module_name="brokk.brokk_avante",
                    )
                    messages = [
                        f"Configured Neovim Avante ACP provider in {settings_path}",
                        "",
                        "What this is:",
                        "- Avante is a Neovim AI coding assistant plugin.",
                        "- Brokk runs as an ACP agent server (`brokk acp`).",
                        "- The generated file wires Avante -> Brokk over ACP.",
                        "",
                        "Next steps:",
                        "1. Install avante.nvim in Neovim (plugin manager):",
                        "   https://github.com/yetone/avante.nvim",
                    ]
                    if patch_result.status == "patched":
                        messages.extend(
                            [
                                f"2. Updated {patch_result.path} to load Brokk automatically.",
                            ]
                        )
                    elif patch_result.status == "already_configured":
                        messages.extend(
                            [
                                f"2. {patch_result.path} already loads Brokk.",
                            ]
                        )
                    else:
                        messages.extend(
                            [
                                "2. Auto-wiring skipped to avoid risky edits.",
                                "   Load the generated Brokk provider in your config:",
                                "   local brokk = require('brokk.brokk_avante')",
                                "   require('avante').setup(",
                                "       vim.tbl_deep_extend('force', brokk, {}))",
                            ]
                        )
                    messages.extend(
                        [
                            "3. Use Brokk by setting provider='brokk' in Avante setup",
                            "   (the generated module already does this)",
                            "",
                            "Note: this command writes provider config and may patch init.lua only",
                            "when it",
                            "can do a conservative, safe edit. It does not install Neovim plugins.",
                        ]
                    )
            elif args.target == "mcp":
                claude_settings_path = configure_claude_code_mcp_settings(
                    force=args.force, uvx_command=uvx_command
                )
                codex_settings_path = configure_codex_mcp_settings(
                    force=args.force, uvx_command=uvx_command
                )
                codex_ws_skill = install_codex_mcp_workspace_skill()
                codex_sum_skill = install_codex_mcp_summaries_skill()
                claude_ws_skill = install_claude_mcp_workspace_skill()
                claude_sum_skill = install_claude_mcp_summaries_skill()
                messages = [
                    f"Configured Claude Code MCP integration in {claude_settings_path}",
                    f"Configured Codex MCP integration in {codex_settings_path}",
                    f"Installed Codex MCP workspace skill in {codex_ws_skill}",
                    f"Installed Codex MCP summaries skill in {codex_sum_skill}",
                    f"Installed Claude MCP workspace skill in {claude_ws_skill}",
                    f"Installed Claude MCP summaries skill in {claude_sum_skill}",
                ]
            elif args.target == "codex-plugin":
                install_result = install_codex_local_plugin(
                    force=args.force,
                    uvx_command=uvx_command,
                )
                messages = [
                    f"Installed Codex plugin files in {install_result.plugin_path}",
                    f"Updated Codex marketplace in {install_result.marketplace_path}",
                    (
                        "Restart Codex, open the plugin directory, choose the local "
                        "marketplace, and install Brokk."
                    ),
                ]
            else:
                # Should not happen due to argparse choices
                raise ValueError(f"Unknown target: {args.target}")
            for message in messages:
                print(message)
        except (ExistingBrokkCodeEntryError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        except UvSetupError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)

        return

    if args.command == "version":
        from brokk_code import __version__

        print(f"brokk {__version__}")
        return

    if args.command == "anvil-config":
        if args.reset:
            deleted = delete_anvil_scripting_config()
            message = (
                "Deleted Anvil scripting configuration." if deleted else "No configuration found."
            )
            print(message)
            return
        if args.show:
            print(format_anvil_scripting_config())
            return
        configure_anvil_scripting_interactive(
            workspace_dir=workspace_path,
            anvil_binary=args.anvil_binary,
            anvil_version=args.anvil_version,
        )
        return

    if args.command == "acp":
        run_anvil_acp_server(
            workspace_dir=workspace_path,
            passthrough_args=unknown,
        )
        return

    if args.command == "mcp":
        from brokk_code.bifrost_launcher import run_bifrost_server

        run_bifrost_server(
            workspace_dir=workspace_path,
            passthrough_args=unknown,
        )
        return

    if args.command == "exec":
        workspace_path = resolve_workspace_dir(workspace_path)
        selection = resolve_anvil_selection(
            tool_key="exec",
            model_override=args.model,
            reasoning_override=args.reasoning_effort,
            workspace_dir=workspace_path,
            anvil_binary=args.anvil_binary,
            anvil_version=args.anvil_version,
        )
        asyncio.run(
            run_headless_job(
                workspace_dir=workspace_path,
                task_input=args.prompt,
                model=selection.model,
                reasoning_effort=selection.reasoning_effort,
                mode="LITE_AGENT",
                tags={"mode": "LITE_AGENT"},
                verbose=args.verbose,
                anvil_binary=args.anvil_binary,
                anvil_version=args.anvil_version,
            )
        )
        return

    if args.command == "commit":
        workspace_path = resolve_workspace_dir(workspace_path)
        model = args.model
        reasoning_effort = args.reasoning_effort
        if not (args.message or "").strip():
            selection = resolve_anvil_selection(
                tool_key="commit",
                model_override=args.model,
                reasoning_override=args.reasoning_effort,
                workspace_dir=workspace_path,
                anvil_binary=args.anvil_binary,
                anvil_version=args.anvil_version,
            )
            model = selection.model
            reasoning_effort = selection.reasoning_effort
        asyncio.run(
            run_commit(
                workspace_dir=workspace_path,
                message=args.message,
                model=model,
                reasoning_effort=reasoning_effort,
                verbose=args.verbose,
                anvil_binary=args.anvil_binary,
                anvil_version=args.anvil_version,
            )
        )
        return

    if args.command == "pr":
        if args.pr_command == "create":
            selection = resolve_anvil_selection(
                tool_key="pr_create",
                model_override=args.model,
                reasoning_override=args.reasoning_effort,
                workspace_dir=workspace_path,
                anvil_binary=args.anvil_binary,
                anvil_version=args.anvil_version,
            )
            asyncio.run(
                run_pr_create(
                    workspace_dir=workspace_path,
                    title=args.title,
                    body=args.body,
                    base_branch=args.base,
                    head_branch=args.head,
                    model=selection.model,
                    reasoning_effort=selection.reasoning_effort,
                    verbose=args.verbose,
                    anvil_binary=args.anvil_binary,
                    anvil_version=args.anvil_version,
                )
            )
        if args.pr_command == "review":
            repo_owner = args.repo_owner
            repo_name = args.repo_name
            if not repo_owner or not repo_name:
                inferred_owner, inferred_repo = infer_github_repo_from_remote(workspace_path)
                if not repo_owner:
                    repo_owner = inferred_owner
                if not repo_name:
                    repo_name = inferred_repo

            _validate_github_params(repo_owner, repo_name, "pr review")
            selection = resolve_anvil_selection(
                tool_key="pr_review",
                model_override=args.model,
                reasoning_override=args.reasoning_effort,
                workspace_dir=workspace_path,
                anvil_binary=args.anvil_binary,
                anvil_version=args.anvil_version,
            )

            asyncio.run(
                run_pr_review_job(
                    workspace_dir=workspace_path,
                    pr_number=args.pr_number,
                    repo_owner=repo_owner,
                    repo_name=repo_name,
                    model=selection.model,
                    reasoning_effort=selection.reasoning_effort,
                    severity_threshold=args.severity,
                    verbose=args.verbose,
                    anvil_binary=args.anvil_binary,
                    anvil_version=args.anvil_version,
                )
            )
        return

    if args.command == "issue":
        if args.issue_command == "create":
            _run_issue_command(
                args,
                command_name="issue create",
                mode="ISSUE_WRITER",
                task_input=args.prompt,
                action_label="Issue create",
                tool_key="issue_create",
            )
            return

        if args.issue_command == "diagnose":
            _run_issue_command(
                args,
                command_name="issue diagnose",
                mode="ISSUE_DIAGNOSE",
                task_input=f"Diagnose GitHub Issue #{args.issue_number}",
                action_label="Issue diagnose",
                include_issue_number=True,
                tool_key="issue_diagnose",
            )
            return

        if args.issue_command == "solve":
            _run_issue_solve_command(args)
            return


if __name__ == "__main__":
    main()
