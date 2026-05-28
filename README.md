# Brokk Code

## What this project is for

This project is a Python CLI for Brokk editor integrations and non-interactive automation. It launches [Anvil](https://github.com/BrokkAi/anvil) over ACP for agent workflows and [Bifrost](https://github.com/BrokkAi/bifrost) over MCP for code-intelligence tools.

## Getting Started

### Prerequisites

- Python 3.11+

### Installation

**Using uv (recommended):**

```bash
cd brokk-code
uv sync
```

**Using pip:**

```bash
cd brokk-code
pip install -e .
```

### Running

**With uv:**

```bash
uv run brokk --help
```

**With pip installation:**

```bash
brokk --help
```

**Or run directly:**

```bash
python -m brokk_code
```

Running `brokk` without a subcommand prints the help menu.

### ACP Mode

Run the [Anvil](https://github.com/BrokkAi/anvil) ACP server mode over stdio:

```bash
uv run brokk acp
```

This mode is headless and intended for ACP-compatible clients. On first use
brokk-code resolves Anvil in this order: `--anvil-binary` override > `anvil`
on `$PATH` > a downloaded release pinned to the bundled Anvil version.

### Bifrost MCP Mode

Run the [bifrost](https://github.com/BrokkAi/bifrost) (Rust) MCP server over stdio:

```bash
uv run brokk mcp --workspace .
```

On first use brokk-code resolves the binary in this order: `--bifrost-binary` override > `bifrost` on `$PATH` > a downloaded release pinned to `BUNDLED_BIFROST_VERSION` (cached under the platform cache dir, e.g. `~/Library/Caches/Brokk/bifrost/<version>/` on macOS). Bifrost ships native binaries for arm64 macOS, x86_64/aarch64 Linux, and x86_64/aarch64 Windows; Intel macOS is not supported by upstream.

### Editor Integration Installers

You can generate integration settings for supported clients:

```bash
uv run brokk install zed
uv run brokk install intellij
uv run brokk install neovim --plugin codecompanion
uv run brokk install neovim --plugin avante
```

Installers only write client configuration. ACP integrations launch `brokk acp`,
and MCP integrations launch `brokk mcp`; they do not configure GitHub auth or a
Brokk API key.

If you run `brokk install neovim` without `--plugin`, Brokk shows a menu in interactive terminals.

#### Neovim + CodeCompanion

`brokk install neovim --plugin codecompanion` creates a Brokk ACP adapter module at:

`~/.config/nvim/lua/brokk/brokk_codecompanion.lua`

What is CodeCompanion?
- `codecompanion.nvim` is a Neovim plugin for AI chat, code assistance, and agent workflows.
- It can connect to ACP-compatible agent servers.
- Brokk provides one of those agent servers via `brokk acp`.

What this installer does:
- Writes a small adapter module that tells CodeCompanion to start and talk to Brokk over ACP.
- Keeps this Brokk-specific config in a clearly named module (`brokk.brokk_codecompanion`).
- Attempts a conservative auto-wire of `~/.config/nvim/init.lua` when it can safely patch a simple `opts = {}` plugin spec.
- Does **not** install Neovim plugins by itself.

1. Install CodeCompanion:
   - GitHub: <https://github.com/olimorris/codecompanion.nvim>
   - Docs: <https://codecompanion.olimorris.dev/>
2. Ensure Brokk is available on your shell `PATH` as `brokk` (or edit the generated file command).
3. Wire the generated Brokk module into your CodeCompanion plugin setup:

```lua
{
  "olimorris/codecompanion.nvim",
  opts = function()
    return require("brokk.brokk_codecompanion")
  end,
}
```

The generated module sets `interactions.chat.adapter = "brokk"` so Brokk becomes the default for CodeCompanion chat.

If you see `Copilot Adapter: No token found`, CodeCompanion is still using its default adapter and the Brokk module is not loaded yet.

#### Neovim + Avante

`brokk install neovim --plugin avante` creates a Brokk Avante provider module at:

`~/.config/nvim/lua/brokk/brokk_avante.lua`

What is Avante?
- `avante.nvim` is a Neovim AI assistant plugin that supports ACP providers.
- Brokk can be configured as an ACP provider via `brokk acp`.
- Installer behavior is the same: write Brokk module first, then only auto-patch `init.lua` when safe.

1. Install Avante:
   - GitHub: <https://github.com/yetone/avante.nvim>
2. Ensure Brokk is available on your shell `PATH` as `brokk` (or edit the generated file command).
3. Load the generated Brokk provider in your Avante config:

```lua
local brokk = require("brokk.brokk_avante")

require("avante").setup(vim.tbl_deep_extend("force", brokk, {
  -- your existing Avante options
}))
```

### Options

- `--workspace <path>`: Specify the workspace directory (defaults to current directory).
- `brokk acp --anvil-binary <path>`: Specify a custom Anvil binary for ACP mode.
- `brokk acp --anvil-version <version>`: Specify the pinned Anvil version to resolve/download.
- `brokk mcp --bifrost-binary <path>`: Specify a custom Bifrost binary for MCP mode.

### Issue Management (GitHub)

The Python CLI supports creating GitHub issues based on repository evidence via the `issue create` command. This is a read-only operation for the local repository; it uses the GitHub API to post a new issue.

```bash
# Example: Create an issue for a discovered bug
brokk issue create "Describe the NPE in AuthService" \
  --repo-owner acme-corp \
  --repo-name service-api \
  --github-token ghp_yourToken
```

**Required Arguments:**
- `prompt`: A description of the problem or evidence to report.
- `--repo-owner` / `--repo-name`: Target GitHub repository.
- `--github-token`: GitHub PAT (can also be set via `GITHUB_TOKEN` environment variable).

## For Contributors & LLMs

To avoid common mistakes when working on this subproject:

- **Context**: `brokk` is the Python CLI that launches Anvil for ACP and Bifrost for MCP.
- **Do**: Run all Python-related commands (pytest, ruff, uv) from within the `brokk-code/` directory.
- **Don't**: Add Java executor/JBang launch paths back into this package.
- **Tests**: Mock ACP/MCP subprocesses. Do not launch real Anvil, Bifrost, Ollama, or provider services in unit tests.
- **Guidelines**: 
    - See [AGENTS.md](AGENTS.md) for general Python contribution rules.
    - See [brokk_code/AGENTS.md](brokk_code/AGENTS.md) for package-specific details.
