# Brokk Code Agent Guide

## What this project is for

This project is a Python CLI for Brokk editor integrations and non-interactive automation. It launches Anvil over ACP for agent workflows and Bifrost over MCP for code-intelligence tools.

## Environment & Requirements

- **Python Version**: 3.11 or higher is required.
- **Key Dependencies**:
  - `agent-client-protocol`: For ACP client/server communication.
  - `httpx`: For downloading pinned Anvil/Bifrost releases.

## Communication Architecture

This project no longer launches the Java executor or JBang.
- ACP mode (`brokk acp`) launches Anvil directly over stdio.
- MCP mode (`brokk mcp`) launches Bifrost directly over stdio.
- Headless commands (`exec`, `issue`, `pr`, `commit`) submit ACP prompts to Anvil through the Python ACP SDK.
- For commands that touch GitHub, Anvil is only used to generate text. The Python CLI performs the actual `gh` calls and must validate that the expected issue, comment, review, or pull request was created.
- Do not add GitHub credential environment variable names, credential flags, or credential forwarding logic anywhere in this repository. GitHub authentication belongs to the `gh` CLI/configuration, and Anvil must not receive GitHub auth environment.
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
- `headless_anvil.py`: ACP prompt construction and headless Anvil client.
- `anvil_launcher.py`: Anvil binary resolution and ACP stdio launch.
- `bifrost_launcher.py`: Bifrost binary resolution and MCP stdio launch.
