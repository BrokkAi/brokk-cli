# Brokk Code Agent Guide

## What this project is for

This project is a Python CLI for Brokk editor integrations and server passthrough wrappers. It launches Anvil over ACP for editor agent workflows and Bifrost over MCP for code-intelligence tools.

## Environment & Requirements

- **Python Version**: 3.11 or higher is required.
- **Key Dependencies**:
  - `httpx`: For downloading pinned Anvil/Bifrost releases.

## Communication Architecture

This project no longer launches the Java executor or JBang.
- ACP mode (`brokk acp`) launches Anvil directly over stdio.
- `brokk acp` is a full passthrough wrapper around `anvil`. "Passthrough" here means Brokk may resolve the binary path and `chdir` into the resolved workspace first, but it must not rewrite, normalize, drop, reorder, or add CLI arguments before invoking Anvil.
- `brokk acp` must not consume any Brokk-owned runtime flags. Flags such as `--worktree`, `--anvil-binary`, `--anvil-version`, `--default-model`, `--max-turns`, `--bifrost-binary`, `--llm-idle-timeout-secs`, `--no-wasm-sandbox`, and `--ide` must be forwarded unchanged to Anvil when present.
- MCP mode (`brokk mcp`) launches Bifrost directly over stdio.
- `brokk mcp` is a full passthrough wrapper around `bifrost`. "Passthrough" here means Brokk may resolve the binary path and `chdir` into the resolved workspace first, but it must not rewrite, normalize, drop, reorder, or add CLI arguments before invoking Bifrost.
- In particular, do not inject implicit MCP defaults such as `--root`, `--server`, `searchtools`, or any other Bifrost mode/subcommand when handling `brokk mcp`. If the user wants those arguments, they must appear explicitly in the args passed to `brokk mcp`.
- `brokk mcp` must not expose a Brokk-level `--bifrost-binary` override. If `--bifrost-binary` appears after `brokk mcp`, treat it as a literal Bifrost CLI argument and pass it through unchanged.
- `brokk mcp` must not consume any Brokk runtime flags at all. Flags such as `--worktree`, `--anvil-binary`, and `--anvil-version` are invalid as Brokk-owned options on this subcommand and must be forwarded unchanged to Bifrost when present.
- There is no Python ACP SDK client in this package. Do not reintroduce Python ACP prompt submission or Python-owned repository automation without an explicit product decision.
- For ACP mode startup, use `wait_live()`/`wait_ready()` only as a liveness probe; it no longer depends on session preload.
- In ACP mode, do NOT emit context snapshots after prompt completion. This feature was removed because inconsistent Markdown and data URI support across ACP clients (e.g., IntelliJ vs. Zed) led to poor rendering of token bars and resource blocks.

## Code Style & Standards

- **PEP 8**: Follow standard Python style guidelines.
- **Linting**: Use `ruff` for linting and formatting.
- **Type Hints**: Use type hints for all function signatures and complex variables.
- **Naming**: Use `snake_case` for variables and functions, and `PascalCase` for classes.

## Testing

ALWAYS RUN TESTS WHEN MAKING CHANGES!

- **Framework**: Use `pytest` for all tests.
- **Command**: Run tests with `uv run pytest` so the project-managed environment is always used.
- **Location**: Place tests in the `tests/` directory.
- Prefer in-process or mocked subprocess tests. Do not launch real Anvil, Bifrost, Ollama, or provider services in unit tests.

## Project Structure

- `brokk_code/`: Main package directory. (See [brokk_code/AGENTS.md](brokk_code/AGENTS.md) for subtree rules).
- `__main__.py`: CLI parsing and command dispatch.
- `anvil_launcher.py`: Anvil binary resolution and ACP stdio launch.
- `bifrost_launcher.py`: Bifrost binary resolution and MCP stdio launch.
