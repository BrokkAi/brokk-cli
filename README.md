# Brokk Code

Python CLI tooling for Brokk editor integrations, ACP/MCP servers, and headless
repository automation.

The published command is `brokk`. For normal use, run it through `uvx` so the
latest published package is resolved automatically:

```bash
uvx brokk --help
```

Local development still uses `uv run`; see [Development](#development).

## Requirements

- `uv`/`uvx` for normal user-facing commands and generated editor configs.
- Python 3.11+ for local development.
- Java 21+ for Java executor-backed commands such as `exec`, `commit`,
  `issue`, and `pr`.
- `curl` on Unix-like systems if Brokk needs to bootstrap `uv` or `jbang`.

ACP mode launches Anvil and does not use the Java executor. MCP mode launches
bifrost and does not use the Java executor.

## Quick Start

Show the available commands:

```bash
uvx brokk --help
```

Install an editor integration:

```bash
uvx brokk install zed
uvx brokk install intellij
uvx brokk install neovim --plugin codecompanion
uvx brokk install neovim --plugin avante
```

Install MCP integration settings for Claude Code and Codex:

```bash
uvx brokk install mcp
```

Install the local Codex plugin entry:

```bash
uvx brokk install codex-plugin
```

Installers write client configuration only. They do not configure GitHub auth,
do not require a Brokk API key, and do not warm Java/Anvil/bifrost runtime
dependencies. Where supported, generated config launches Brokk as `uvx brokk
...` so clients resolve the current package at runtime.

Use `--force` to replace an existing generated integration entry when the
installer supports it.

## Provider Configuration

Brokk defaults to the Brokk proxy provider. You can inspect or change the
provider stored in `~/.brokk/brokk.properties`:

```bash
uvx brokk provider status
uvx brokk provider brokk
uvx brokk provider custom \
  --url http://localhost:11434/v1 \
  --model llama3.1 \
  --api-key ""
```

The install command can set provider values at the same time:

```bash
uvx brokk install zed \
  --provider custom \
  --provider-url http://localhost:11434/v1 \
  --provider-model llama3.1
```

## ACP Server

Run Anvil as an ACP server over stdio:

```bash
uvx brokk acp --workspace .
```

Resolution order for Anvil is:

1. `--anvil-binary <path>`
2. `anvil` on `PATH`
3. A downloaded release pinned to the bundled Anvil version

Useful ACP options:

```bash
uvx brokk acp --workspace . --default-model gpt-5.2
uvx brokk acp --workspace . --max-turns 20
uvx brokk acp --workspace . --bifrost-binary /path/to/bifrost
```

`brokk acp` rejects Java executor options such as `--jar` and
`--executor-version`.

## MCP Server

Run bifrost as an MCP server over stdio:

```bash
uvx brokk mcp --workspace .
```

Resolution order for bifrost is:

1. `--bifrost-binary <path>`
2. `bifrost` on `PATH`
3. A downloaded release pinned to the bundled bifrost version

`brokk mcp` rejects Java executor options such as `--jar` and
`--executor-version`. Unknown arguments are passed through to bifrost.

## Editor Integrations

### Zed and IntelliJ

```bash
uvx brokk install zed
uvx brokk install intellij
uvx brokk install jetbrains
```

These installers add a custom ACP agent server entry that launches:

```bash
uvx brokk acp
```

`jetbrains` is an alias for `intellij`.

### Neovim + CodeCompanion

```bash
uvx brokk install neovim --plugin codecompanion
```

This writes:

```text
~/.config/nvim/lua/brokk/brokk_codecompanion.lua
```

It creates a CodeCompanion ACP adapter named `brokk`. The installer may patch a
simple `init.lua` plugin spec when it can do so conservatively; otherwise load
the module from your CodeCompanion setup:

```lua
{
  "olimorris/codecompanion.nvim",
  opts = function()
    return require("brokk.brokk_codecompanion")
  end,
}
```

The generated module sets Brokk as the default chat adapter. If CodeCompanion
still reports its default Copilot adapter, the Brokk module is not loaded yet.

### Neovim + Avante

```bash
uvx brokk install neovim --plugin avante
```

This writes:

```text
~/.config/nvim/lua/brokk/brokk_avante.lua
```

Load the generated provider in your Avante config:

```lua
local brokk = require("brokk.brokk_avante")

require("avante").setup(vim.tbl_deep_extend("force", brokk, {
  -- your existing Avante options
}))
```

### Claude Code and Codex MCP

```bash
uvx brokk install mcp
```

This configures Brokk MCP entries for Claude Code and Codex and installs helper
skills/instructions for workspace activation and summaries.

### Codex Plugin

```bash
uvx brokk install codex-plugin
```

This installs local Codex plugin files and adds a local marketplace entry. After
running it, restart Codex, choose the local marketplace, and install Brokk.

### Direct Rust ACP for Zed/IntelliJ

For development or direct Rust ACP usage, Zed and IntelliJ can be wired to
`brokk-acp` instead of `uvx brokk acp`:

```bash
uvx brokk install zed --rust --provider-model gpt-5.2
uvx brokk install intellij --rust --provider-model gpt-5.2
```

In this mode `brokk-acp` and `bifrost` must already be installed and available
on the editor's inherited `PATH`. Use `--brokk-acp-binary <path>` to write an
explicit `brokk-acp` path.

## Headless Repository Commands

These commands use the Java headless executor. Without `--jar`, Brokk uses
JBang to run the bundled executor JAR from the Brokk release channel and will
bootstrap/trust JBang sources if needed.

```bash
uvx brokk exec "Find the likely cause of the failing test"
uvx brokk commit
uvx brokk commit "Fix startup race"
```

GitHub issue commands:

```bash
uvx brokk issue create "Report the flaky checkout failure" \
  --repo-owner acme \
  --repo-name service \
  --github-token "$GITHUB_TOKEN"

uvx brokk issue diagnose \
  --issue-number 123 \
  --repo-owner acme \
  --repo-name service \
  --github-token "$GITHUB_TOKEN"

uvx brokk issue solve \
  --issue-number 123 \
  --repo-owner acme \
  --repo-name service \
  --github-token "$GITHUB_TOKEN"
```

Pull request commands:

```bash
uvx brokk pr create --title "Fix startup race" --body "See commits."
uvx brokk pr review \
  --pr-number 123 \
  --repo-owner acme \
  --repo-name service \
  --github-token "$GITHUB_TOKEN"
```

Common runtime options:

- `--workspace <path>`: workspace directory, defaulting to the current directory.
- `--worktree`: run the command in an isolated git worktree when applicable.
- `--vendor <name>`: set the Java executor's "Other Models" vendor preference.
- `--jar <path>`: run a local executor JAR directly with Java.
- `--executor-version <tag>`: use a specific released executor version.
- `--executor-snapshot` / `--executor-stable`: accepted compatibility flags.

## Development

Clone the repo and create the managed environment:

```bash
uv sync
```

Run the CLI from the checkout:

```bash
uv run brokk --help
uv run brokk version
```

Run tests and linting:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

When changing code, keep generated repository content in English and follow the
project guidance in [AGENTS.md](AGENTS.md) and
[brokk_code/AGENTS.md](brokk_code/AGENTS.md).
