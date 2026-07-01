import argparse
import sys
from pathlib import Path

from brokk_code.anvil_launcher import run_anvil_acp_server
from brokk_code.avante_config import configure_nvim_avante_acp_settings
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
from brokk_code.zed_config import ExistingBrokkCodeEntryError, configure_zed_acp_settings


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


def _add_common_runtime_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--worktree",
        action="store_true",
        default=False,
        help="Accepted for compatibility; ignored by remaining commands",
    )
    parser.add_argument(
        "--anvil-binary",
        type=Path,
        default=None,
        help="Path to an Anvil binary for ACP passthrough commands",
    )
    parser.add_argument(
        "--anvil-version",
        type=str,
        default=None,
        help="Anvil version to use instead of the pinned release for ACP passthrough commands",
    )
    parser.add_argument(
        "--bifrost-version",
        type=str,
        default=None,
        help="Bifrost version to use instead of the pinned release for MCP passthrough commands",
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
    subparsers.add_parser(
        "mcp",
        help="Run the bifrost MCP server (downloads the pinned release on first use)",
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
            "Wire the editor at the Rust ACP server (brokk-acp) instead of the Python "
            "wrapper. Requires --model. zed/intellij only."
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

    version_parser = subparsers.add_parser("version", help="Print version information")
    _add_common_runtime_args(version_parser)

    return parser


def _passthrough_command_from_argv(
    argv: list[str],
) -> tuple[argparse.Namespace, list[str]] | None:
    """Return passthrough command args with root runtime overrides consumed.

    Only options before the passthrough subcommand are treated as Brokk globals.
    The subcommand tail remains an exact passthrough to Anvil/Bifrost, so e.g.
    `brokk mcp --bifrost-version 0.7.2` still forwards `--bifrost-version` to
    Bifrost while `brokk --bifrost-version 0.7.2 mcp` selects that Bifrost binary.
    """
    root_options_with_values = {
        "--anvil-binary": "anvil_binary",
        "--anvil-version": "anvil_version",
        "--bifrost-version": "bifrost_version",
    }
    root_flags = {"--worktree"}
    help_flags = {"-h", "--help"}
    runtime_args = argparse.Namespace(
        command=None,
        worktree=False,
        anvil_binary=None,
        anvil_version=None,
        bifrost_version=None,
    )

    index = 0
    while index < len(argv):
        token = argv[index]
        if token in help_flags:
            return None
        if token == "--":
            index += 1
            break
        if token in root_flags:
            runtime_args.worktree = True
            index += 1
            continue
        if token in root_options_with_values:
            if index + 1 >= len(argv):
                return None
            value = argv[index + 1]
            attr = root_options_with_values[token]
            setattr(runtime_args, attr, Path(value) if attr == "anvil_binary" else value)
            index += 2
            continue
        matched_option = next(
            (option for option in root_options_with_values if token.startswith(f"{option}=")),
            None,
        )
        if matched_option is not None:
            value = token.split("=", 1)[1]
            attr = root_options_with_values[matched_option]
            setattr(runtime_args, attr, Path(value) if attr == "anvil_binary" else value)
            index += 1
            continue
        break

    if index >= len(argv):
        return None

    command = argv[index]
    if command not in {"acp", "mcp"}:
        return None

    runtime_args.command = command
    return runtime_args, argv[index + 1 :]


def main() -> None:
    raw_args = sys.argv[1:]
    passthrough_command = _passthrough_command_from_argv(raw_args)
    if passthrough_command is not None:
        args, passthrough_args = passthrough_command
        _main_dispatch(args, Path.cwd().resolve(), passthrough_args)
        return

    parser = _build_parser()
    args, unknown = parser.parse_known_args()

    if unknown and args.command not in {"acp", "mcp"}:
        parser.error(f"unrecognized arguments: {' '.join(unknown)}")

    if args.command is None:
        parser.print_help()
        return

    _main_dispatch(args, Path.cwd().resolve(), unknown)


def _main_dispatch(
    args: argparse.Namespace,
    workspace_path: Path,
    unknown: list[str],
) -> None:
    """Core command dispatch."""
    if args.command == "install":
        if args.target == "jetbrains":
            args.target = "intellij"
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
                    ]
                    if patch_result.status == "patched":
                        messages.append(f"Updated {patch_result.path} to load Brokk automatically.")
                    elif patch_result.status == "already_configured":
                        messages.append(f"{patch_result.path} already loads Brokk.")
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
                    ]
                    if patch_result.status == "patched":
                        messages.append(f"Updated {patch_result.path} to load Brokk automatically.")
                    elif patch_result.status == "already_configured":
                        messages.append(f"{patch_result.path} already loads Brokk.")
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

    if args.command == "acp":
        run_anvil_acp_server(
            workspace_dir=workspace_path,
            binary_override=args.anvil_binary,
            version=args.anvil_version,
            passthrough_args=unknown,
        )
        return

    if args.command == "mcp":
        from brokk_code.bifrost_launcher import run_bifrost_server

        run_bifrost_server(
            workspace_dir=workspace_path,
            version=args.bifrost_version,
            passthrough_args=unknown,
        )
        return


if __name__ == "__main__":
    main()
