import argparse
import os
import shutil
import subprocess
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
from brokk_code.workspace import resolve_workspace_dir
from brokk_code.zed_config import ExistingBrokkCodeEntryError, configure_zed_acp_settings

_NON_WORKSPACE_COMMANDS = {"install", "version"}
_VALKYRIE_LOCAL_CHECKOUT = Path.home() / "code" / "valkyrie"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Brokk Code wrapper for Anvil, Bifrost, and Valkyrie"
    )
    _add_common_runtime_args(parser)
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("acp", help="Run Anvil in ACP server mode", add_help=False)
    subparsers.add_parser(
        "mcp",
        help="Run the Bifrost MCP server",
        add_help=False,
    )
    subparsers.add_parser(
        "vk",
        help="Run vk directly",
        add_help=False,
    )
    subparsers.add_parser("valkyrie", help=argparse.SUPPRESS, add_help=False)

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
            "Only used for install targets nvim/neovim."
        ),
    )
    install_parser.add_argument(
        "--native",
        action="store_true",
        default=False,
        help="[Deprecated] Native is now the default; this flag is ignored.",
    )
    install_parser.add_argument(
        "--rust",
        action="store_true",
        default=False,
        help="Wire the editor at brokk-acp and bifrost instead of brokk acp.",
    )
    install_parser.add_argument(
        "--brokk-acp-binary",
        type=Path,
        default=None,
        help="Write this brokk-acp path into the editor's agent server config.",
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
        help="Accepted for compatibility.",
    )
    install_parser.add_argument(
        "--model",
        default=None,
        help="Model name for --rust editor config",
    )
    install_parser.add_argument(
        "--endpoint-url",
        default=None,
        help="Endpoint URL for --rust config",
    )
    install_parser.add_argument("--api-key", default="", help="API key for --rust editor config")

    subparsers.add_parser("version", help="Print version information")

    exec_parser = subparsers.add_parser("exec", help="Run a local task through Valkyrie")
    _add_valkyrie_compat_args(exec_parser)
    exec_parser.add_argument("prompt", help="The task to execute")

    commit_parser = subparsers.add_parser("commit", help="Ask Valkyrie to commit current changes")
    _add_valkyrie_compat_args(commit_parser)
    commit_parser.add_argument("message", nargs="?", default=None, help="Commit message")

    issue_parser = subparsers.add_parser("issue", help="Delegate GitHub issue work to Valkyrie")
    issue_subparsers = issue_parser.add_subparsers(dest="issue_command", required=True)
    issue_create_parser = issue_subparsers.add_parser("create", help="Create a GitHub issue")
    _add_valkyrie_compat_args(issue_create_parser)
    issue_create_parser.add_argument("prompt", help="Description of the issue to create")
    issue_diagnose_parser = issue_subparsers.add_parser("diagnose", help="Plan an issue diagnosis")
    _add_valkyrie_compat_args(issue_diagnose_parser)
    issue_diagnose_parser.add_argument("--issue-number", type=int, required=True)
    issue_solve_parser = issue_subparsers.add_parser("solve", help="Fix an existing GitHub issue")
    _add_valkyrie_compat_args(issue_solve_parser)
    issue_solve_parser.add_argument("--issue-number", type=int, required=True)
    issue_solve_parser.add_argument("--skip-verification", action="store_true", default=False)
    issue_solve_parser.add_argument("--max-issue-fix-attempts", type=int, default=None)
    issue_solve_parser.add_argument("--build-settings", default=None)

    pr_parser = subparsers.add_parser("pr", help="Delegate pull request work to Valkyrie")
    pr_subparsers = pr_parser.add_subparsers(dest="pr_command", required=True)
    pr_create_parser = pr_subparsers.add_parser("create", help="Create a pull request")
    _add_valkyrie_compat_args(pr_create_parser)
    pr_create_parser.add_argument("--title", default=None)
    pr_create_parser.add_argument("--body", default=None)
    pr_create_parser.add_argument("--base", default=None)
    pr_create_parser.add_argument("--head", default=None)
    pr_review_parser = pr_subparsers.add_parser("review", help="Review a pull request")
    _add_valkyrie_compat_args(pr_review_parser)
    pr_review_parser.add_argument("pr_target", nargs="?", default=None)
    pr_review_parser.add_argument("--pr-number", type=int, default=None)
    pr_review_parser.add_argument("--severity", choices=["CRITICAL", "HIGH", "MEDIUM", "LOW"])

    for name in ("run", "plan", "defaults", "status", "logs", "diff", "doctor"):
        subparsers.add_parser(name, help=f"Run `vk {name}`", add_help=False)

    return parser


def _add_common_runtime_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--worktree",
        action="store_true",
        default=False,
        help="Create an isolated git worktree for workspace commands",
    )
    parser.add_argument(
        "--anvil-binary",
        type=Path,
        default=None,
        help="Compatibility option for Anvil-backed commands",
    )
    parser.add_argument(
        "--anvil-version",
        type=str,
        default=None,
        help="Compatibility option for Anvil-backed commands",
    )
    parser.add_argument(
        "--vk-binary",
        type=Path,
        default=None,
        dest="vk_binary",
        help="Path to a vk binary",
    )
    parser.add_argument(
        "--valkyrie-binary",
        type=Path,
        default=None,
        dest="vk_binary",
        help=argparse.SUPPRESS,
    )


def _add_valkyrie_compat_args(parser: argparse.ArgumentParser) -> None:
    _add_common_runtime_args(parser)
    parser.add_argument("--model", default=None, help="Accepted for compatibility; not passed yet")
    parser.add_argument(
        "--reasoning-effort",
        default=None,
        help="Accepted for compatibility; not passed yet",
    )
    parser.add_argument("--repo-owner", default=None, help="Accepted for compatibility")
    parser.add_argument("--repo-name", default=None, help="Accepted for compatibility")
    parser.add_argument("-v", "--verbose", action="store_true", default=False)


def _passthrough_command_from_argv(argv: list[str]) -> tuple[str, list[str]] | None:
    root_options_with_values = {
        "--anvil-binary",
        "--anvil-version",
        "--vk-binary",
        "--valkyrie-binary",
    }
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
    if command not in {"acp", "mcp", "vk", "valkyrie"}:
        return None

    return command, [*argv[:index], *argv[index + 1 :]]


def main() -> None:
    raw_args = sys.argv[1:]
    passthrough_command = _passthrough_command_from_argv(raw_args)
    if passthrough_command is not None:
        command, passthrough_args = passthrough_command
        _main_dispatch(argparse.Namespace(command=command), Path.cwd().resolve(), passthrough_args)
        return

    parser = _build_parser()
    args, unknown = parser.parse_known_args()
    if args.command is None:
        parser.print_help()
        return
    if unknown and args.command not in _valkyrie_passthrough_commands():
        parser.error(f"unrecognized arguments: {' '.join(unknown)}")

    workspace_path = Path.cwd().resolve()
    use_worktree = getattr(args, "worktree", False) and args.command not in _NON_WORKSPACE_COMMANDS
    if use_worktree:
        from brokk_code.git_utils import worktree_context

        repo_root = resolve_workspace_dir(workspace_path)
        with worktree_context(repo_root) as worktree_path:
            _main_dispatch(
                args,
                _resolve_worktree_workspace_path(workspace_path, repo_root, worktree_path),
                unknown,
            )
    else:
        _main_dispatch(args, workspace_path, unknown)


def _main_dispatch(args: argparse.Namespace, workspace_path: Path, unknown: list[str]) -> None:
    if args.command == "install":
        _run_install(args)
        return

    if args.command == "version":
        from brokk_code import __version__

        print(f"brokk {__version__}")
        return

    if args.command == "acp":
        run_anvil_acp_server(workspace_dir=workspace_path, passthrough_args=unknown)
        return

    if args.command == "mcp":
        from brokk_code.bifrost_launcher import run_bifrost_server

        run_bifrost_server(workspace_dir=workspace_path, passthrough_args=unknown)
        return

    valkyrie_args = _valkyrie_args_for_namespace(args, workspace_path, unknown)
    _run_valkyrie(
        valkyrie_args,
        workspace_dir=workspace_path,
        override=getattr(args, "vk_binary", None),
    )


def _run_install(args: argparse.Namespace) -> None:
    if args.target == "jetbrains":
        args.target = "intellij"
    if args.plugin and args.target not in {"nvim", "neovim"}:
        print("Error: --plugin is only valid for install targets nvim/neovim", file=sys.stderr)
        sys.exit(1)
    if args.rust and args.native:
        print("Error: --rust and --native are mutually exclusive; pass only one.", file=sys.stderr)
        sys.exit(1)
    if args.native:
        print(
            "Warning: --native is deprecated and ignored; `brokk acp` launches Anvil.",
            file=sys.stderr,
        )
    if args.rust and args.target not in {"zed", "intellij"}:
        print("Error: --rust is only supported for install targets zed/intellij.", file=sys.stderr)
        sys.exit(1)
    if args.rust and not args.model:
        print("Error: --rust requires --model.", file=sys.stderr)
        sys.exit(1)
    if args.brokk_acp_binary and not args.rust:
        print("Error: --brokk-acp-binary requires --rust.", file=sys.stderr)
        sys.exit(1)

    if args.rust:
        _run_rust_install(args)
        return

    try:
        uv_binary = ensure_uv_ready()
        uvx_command = str(Path(uv_binary).parent / "uvx").replace("\\", "/")
        messages = _install_target(args, uvx_command=uvx_command)
        for message in messages:
            print(message)
    except (ExistingBrokkCodeEntryError, ValueError, UvSetupError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


def _run_rust_install(args: argparse.Namespace) -> None:
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
            settings_path = configure_zed_acp_settings(force=args.force, rust_paths=rust_paths)
            integration = "Zed"
        else:
            settings_path = configure_intellij_acp_settings(force=args.force, rust_paths=rust_paths)
            integration = "IntelliJ"
    except (ExistingBrokkCodeEntryError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"Wired editor at brokk-acp=`{brokk_acp_path}` bifrost=`{bifrost_path}`")
    print(f"Configured {integration} ACP integration in {settings_path}")


def _install_target(args: argparse.Namespace, *, uvx_command: str) -> list[str]:
    if args.target == "zed":
        path = configure_zed_acp_settings(force=args.force, uvx_command=uvx_command, native=False)
        return [f"Configured Zed ACP integration in {path}"]
    if args.target == "intellij":
        path = configure_intellij_acp_settings(
            force=args.force,
            uvx_command=uvx_command,
            native=False,
        )
        return [f"Configured IntelliJ ACP integration in {path}"]
    if args.target in {"nvim", "neovim"}:
        return _install_neovim(args)
    if args.target == "mcp":
        claude_settings_path = configure_claude_code_mcp_settings(
            force=args.force,
            uvx_command=uvx_command,
        )
        codex_settings_path = configure_codex_mcp_settings(
            force=args.force,
            uvx_command=uvx_command,
        )
        return [
            f"Configured Claude Code MCP integration in {claude_settings_path}",
            f"Configured Codex MCP integration in {codex_settings_path}",
            f"Installed Codex MCP workspace skill in {install_codex_mcp_workspace_skill()}",
            f"Installed Codex MCP summaries skill in {install_codex_mcp_summaries_skill()}",
            f"Installed Claude MCP workspace skill in {install_claude_mcp_workspace_skill()}",
            f"Installed Claude MCP summaries skill in {install_claude_mcp_summaries_skill()}",
        ]
    if args.target == "codex-plugin":
        result = install_codex_local_plugin(force=args.force, uvx_command=uvx_command)
        return [
            f"Installed Codex plugin files in {result.plugin_path}",
            f"Updated Codex marketplace in {result.marketplace_path}",
            (
                "Restart Codex, open the plugin directory, choose the local marketplace, "
                "and install Brokk."
            ),
        ]
    raise ValueError(f"Unknown target: {args.target}")


def _install_neovim(args: argparse.Namespace) -> list[str]:
    plugin = _resolve_neovim_plugin(plugin=args.plugin)
    if plugin == "codecompanion":
        settings_path = configure_nvim_codecompanion_acp_settings(force=args.force)
        patch_result = wire_nvim_plugin_setup(
            plugin_repo="olimorris/codecompanion.nvim",
            module_name="brokk.brokk_codecompanion",
        )
        plugin_name = "CodeCompanion"
    else:
        settings_path = configure_nvim_avante_acp_settings(force=args.force)
        patch_result = wire_nvim_plugin_setup(
            plugin_repo="yetone/avante.nvim",
            module_name="brokk.brokk_avante",
        )
        plugin_name = "Avante"

    messages = [f"Configured Neovim {plugin_name} ACP integration in {settings_path}"]
    if patch_result.status == "patched":
        messages.append(f"Updated {patch_result.path} to load Brokk automatically.")
    elif patch_result.status == "already_configured":
        messages.append(f"{patch_result.path} already loads Brokk.")
    else:
        messages.append("Auto-wiring skipped to avoid risky edits.")
    return messages


def _resolve_neovim_plugin(*, plugin: str | None) -> str:
    if plugin:
        return plugin
    if not sys.stdin.isatty():
        return "codecompanion"
    print("Choose a Neovim plugin integration:")
    print("1. CodeCompanion (ACP adapter)")
    print("2. Avante (ACP provider)")
    choice = input("Selection [1/2] (default: 1): ").strip().lower()
    if choice in {"", "1", "codecompanion"}:
        return "codecompanion"
    if choice in {"2", "avante"}:
        return "avante"
    raise ValueError(f"Invalid plugin selection: '{choice}'")


def _valkyrie_passthrough_commands() -> set[str]:
    return {"vk", "valkyrie", "run", "plan", "defaults", "status", "logs", "diff", "doctor"}


def _valkyrie_args_for_namespace(
    args: argparse.Namespace,
    workspace_path: Path,
    unknown: list[str],
) -> list[str]:
    command = args.command
    if command in {"vk", "valkyrie"}:
        return unknown
    if command in _valkyrie_passthrough_commands():
        return [command, *unknown]
    if command == "exec":
        return _with_repo(["run", args.prompt, *_verbose_arg(args)], workspace_path)
    if command == "commit":
        prompt = "Commit the current repository changes."
        if args.message:
            prompt = f"{prompt} Use this commit message: {args.message}"
        return _with_repo(
            ["run", prompt, "--commit", "--skip-validation", *_verbose_arg(args)],
            workspace_path,
        )
    if command == "issue":
        return _issue_valkyrie_args(args, workspace_path)
    if command == "pr":
        return _pr_valkyrie_args(args, workspace_path)
    raise SystemExit(f"Unhandled command: {command}")


def _issue_valkyrie_args(args: argparse.Namespace, workspace_path: Path) -> list[str]:
    if args.issue_command == "create":
        return _with_repo(
            [
                "run",
                f"Draft a GitHub issue from this request: {args.prompt}",
                "--no-write",
                *_verbose_arg(args),
            ],
            workspace_path,
        )
    if args.issue_command == "diagnose":
        return _with_repo(
            ["issue", str(args.issue_number), "--plan", *_verbose_arg(args)],
            workspace_path,
        )
    if args.issue_command == "solve":
        valkyrie_args = ["issue", str(args.issue_number), "--write", *_verbose_arg(args)]
        if args.skip_verification:
            valkyrie_args.append("--skip-validation")
        return _with_repo(valkyrie_args, workspace_path)
    raise SystemExit(f"Unhandled issue command: {args.issue_command}")


def _pr_valkyrie_args(args: argparse.Namespace, workspace_path: Path) -> list[str]:
    if args.pr_command == "create":
        prompt_parts = ["Prepare a pull request for the current branch."]
        if args.title:
            prompt_parts.append(f"Title: {args.title}")
        if args.body:
            prompt_parts.append(f"Body: {args.body}")
        if args.base:
            prompt_parts.append(f"Base branch: {args.base}")
        if args.head:
            prompt_parts.append(f"Head branch: {args.head}")
        return _with_repo(
            ["run", "\n".join(prompt_parts), "--open-pr", *_verbose_arg(args)],
            workspace_path,
        )
    if args.pr_command == "review":
        pr_number = _resolve_pr_number(args)
        return _with_repo(["pr", str(pr_number), "--plan", *_verbose_arg(args)], workspace_path)
    raise SystemExit(f"Unhandled PR command: {args.pr_command}")


def _resolve_pr_number(args: argparse.Namespace) -> int:
    if args.pr_number is not None:
        return args.pr_number
    if args.pr_target:
        tail = args.pr_target.rstrip("/").split("/")[-1]
        try:
            return int(tail)
        except ValueError:
            pass
    print("Error: A pull request number or GitHub pull request URL is required", file=sys.stderr)
    sys.exit(1)


def _verbose_arg(args: argparse.Namespace) -> list[str]:
    return ["--verbose"] if getattr(args, "verbose", False) else []


def _with_repo(args: list[str], workspace_path: Path) -> list[str]:
    return [*args, "--repo", str(resolve_workspace_dir(workspace_path))]


def _run_valkyrie(
    args: list[str],
    *,
    workspace_dir: Path,
    override: Path | None = None,
) -> None:
    command = _resolve_valkyrie_command(override)
    try:
        completed = subprocess.run(command + args, cwd=workspace_dir, env=os.environ.copy())
    except OSError as exc:
        print(f"Error: failed to launch Valkyrie: {exc}", file=sys.stderr)
        sys.exit(1)
    if completed.returncode != 0:
        sys.exit(completed.returncode)


def _resolve_valkyrie_command(override: Path | None = None) -> list[str]:
    if override is not None:
        if not override.exists():
            print(f"Error: vk binary not found: {override}", file=sys.stderr)
            sys.exit(1)
        return [str(override)]

    env_override = os.environ.get("VK_BINARY") or os.environ.get("VALKYRIE_BINARY")
    if env_override:
        return _resolve_valkyrie_command(Path(env_override))

    path_binary = shutil.which("vk")
    if path_binary:
        return [path_binary]

    local_binary = _VALKYRIE_LOCAL_CHECKOUT / "target" / "debug" / _valkyrie_binary_name()
    if local_binary.exists():
        return [str(local_binary)]

    manifest = _VALKYRIE_LOCAL_CHECKOUT / "Cargo.toml"
    if manifest.exists() and shutil.which("cargo"):
        return ["cargo", "run", "--quiet", "--manifest-path", str(manifest), "--bin", "vk", "--"]

    print(
        "Error: vk is required for this command. Install `vk`, set VK_BINARY, "
        "pass --vk-binary, or keep a checkout at ~/code/valkyrie.",
        file=sys.stderr,
    )
    sys.exit(1)


def _valkyrie_binary_name() -> str:
    return "vk.exe" if os.name == "nt" else "vk"


def _resolve_worktree_workspace_path(
    workspace_path: Path,
    repo_root: Path,
    worktree_path: Path,
) -> Path:
    try:
        relative_workspace = workspace_path.relative_to(repo_root)
    except ValueError:
        return worktree_path
    return worktree_path / relative_workspace


if __name__ == "__main__":
    main()
