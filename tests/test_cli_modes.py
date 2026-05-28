import subprocess
import sys
from contextlib import contextmanager
from io import StringIO
from types import SimpleNamespace
from typing import Any

import pytest

import brokk_code.__main__ as main_module
import brokk_code.git_utils as git_utils_module
from brokk_code.anvil_config import AnvilScriptingConfig, AnvilToolSelection

ISSUE_TAGS = {
    "repo_owner": "brokkai",
    "repo_name": "brokk",
}


def _patch_headless_client(
    monkeypatch,
    *,
    events: list[dict[str, Any]] | None = None,
    call_order: list[str] | None = None,
    start_error: Exception | None = None,
    prompt_error: Exception | None = None,
) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    class FakeHeadlessAcpClient:
        def __init__(self, **kwargs: Any) -> None:
            captured["init_kwargs"] = kwargs
            self.shutdown_context: str | None = None

        async def start(self) -> None:
            if call_order is not None:
                call_order.append("start")
            if start_error is not None:
                raise start_error

        async def run_prompt(
            self,
            prompt: str,
            *,
            model: str | None = None,
            reasoning_effort: str | None = None,
        ):
            captured["prompt"] = prompt
            captured["model"] = model
            captured["reasoning_effort"] = reasoning_effort
            if call_order is not None:
                call_order.append("run_prompt")
            if prompt_error is not None:
                raise prompt_error
            for event in events or []:
                yield event

        async def stop(self) -> None:
            if call_order is not None:
                call_order.append("stop")

    monkeypatch.setattr(main_module, "HeadlessAcpClient", FakeHeadlessAcpClient)
    return captured


def _stub_install_warmup(monkeypatch, stub_api_key: bool = True) -> None:
    monkeypatch.setattr(main_module, "ensure_uv_ready", lambda: "/usr/local/bin/uv")
    monkeypatch.setattr(
        main_module,
        "wire_nvim_plugin_setup",
        lambda **_kwargs: SimpleNamespace(status="unsupported", path=None, detail=None),
    )


def test_main_version_subcommand_prints_version(monkeypatch, capsys) -> None:
    """Verify `brokk version` prints the package version and exits cleanly."""
    from brokk_code import __version__

    monkeypatch.setattr(sys, "argv", ["brokk", "version"])

    from brokk_code.__main__ import main

    main()

    captured = capsys.readouterr()
    assert f"brokk {__version__}" in captured.out


def test_main_login_subcommand_is_removed(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["brokk", "login"],
    )

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code == 2


def test_main_logout_subcommand_is_removed(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["brokk", "logout"])

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code == 2


def test_main_login_rejects_api_key_flag(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["brokk", "login", "--api-key", "secret"])
    with pytest.raises(SystemExit) as exc:
        main_module.main()
    assert exc.value.code == 2


def test_main_github_subcommand_is_removed(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["brokk", "github", "login"])
    with pytest.raises(SystemExit) as exc:
        main_module.main()
    assert exc.value.code == 2


def test_main_provider_subcommand_is_removed(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["brokk", "provider", "status"])
    with pytest.raises(SystemExit) as exc:
        main_module.main()
    assert exc.value.code == 2


def test_main_without_command_prints_help(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["brokk"])

    main_module.main()

    captured = capsys.readouterr()
    assert "usage: brokk" in captured.out
    assert "acp" in captured.out
    assert "Launch the interactive app" not in captured.out


def test_main_acp_routes_to_anvil_launcher(monkeypatch, tmp_path) -> None:
    captured: dict[str, Any] = {}

    def fake_run_anvil_acp_server(**kwargs: Any) -> None:
        captured["kwargs"] = kwargs

    monkeypatch.setattr(main_module, "run_anvil_acp_server", fake_run_anvil_acp_server)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brokk",
            "acp",
            "--workspace",
            str(tmp_path),
            "--default-model",
            "claude-haiku-4-5",
            "--bifrost-binary",
            "/opt/bifrost",
        ],
    )

    main_module.main()

    assert captured["kwargs"]["workspace_dir"] == tmp_path.resolve()
    assert captured["kwargs"]["binary_override"] is None
    assert captured["kwargs"]["passthrough_args"] == [
        "--default-model",
        "claude-haiku-4-5",
        "--bifrost-binary",
        "/opt/bifrost",
    ]


def test_main_anvil_config_show(monkeypatch, capsys) -> None:
    AnvilScriptingConfig(
        use_global=True,
        global_selection=AnvilToolSelection(model="configured-model", reasoning_effort="medium"),
    ).save()
    monkeypatch.setattr(sys, "argv", ["brokk", "anvil-config", "--show"])

    main_module.main()

    output = capsys.readouterr().out
    assert "configured-model" in output
    assert "reasoning_effort=medium" in output


def test_main_anvil_config_reset(monkeypatch, capsys) -> None:
    AnvilScriptingConfig(
        use_global=True,
        global_selection=AnvilToolSelection(model="configured-model"),
    ).save()
    monkeypatch.setattr(sys, "argv", ["brokk", "anvil-config", "--reset"])

    main_module.main()

    assert "Deleted Anvil scripting configuration." in capsys.readouterr().out
    assert AnvilScriptingConfig.load() is None


def test_main_acp_native_command_is_removed(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["brokk", "acp-native", "--workspace", str(tmp_path)],
    )

    with pytest.raises(SystemExit) as excinfo:
        main_module.main()

    assert excinfo.value.code == 2
    assert "invalid choice" in capsys.readouterr().err.lower()


def test_main_mcp_routes_to_bifrost_launcher(monkeypatch, tmp_path) -> None:
    captured: dict[str, Any] = {}
    binary = tmp_path / "bifrost"
    binary.write_text("stub")

    from brokk_code import bifrost_launcher as bifrost_launcher_module

    def fake_run_bifrost_server(**kwargs: Any) -> None:
        captured["kwargs"] = kwargs

    monkeypatch.setattr(bifrost_launcher_module, "run_bifrost_server", fake_run_bifrost_server)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brokk",
            "mcp",
            "--workspace",
            str(tmp_path),
            "--bifrost-binary",
            str(binary),
            "--debug",
        ],
    )

    main_module.main()

    assert captured["kwargs"]["workspace_dir"] == tmp_path.resolve()
    assert captured["kwargs"]["binary_override"] == binary
    assert captured["kwargs"]["passthrough_args"] == ["--debug"]


def test_main_bifrost_subcommand_is_removed(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["brokk", "bifrost"])

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code == 2


def test_main_mcp_core_subcommand_is_removed(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["brokk", "mcp-core"])

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code == 2


def test_main_mcp_forwards_unknown_args_as_passthrough(monkeypatch, tmp_path) -> None:
    captured: dict[str, Any] = {}

    from brokk_code import bifrost_launcher as bifrost_launcher_module

    def fake_run_bifrost_server(**kwargs: Any) -> None:
        captured["kwargs"] = kwargs

    monkeypatch.setattr(bifrost_launcher_module, "run_bifrost_server", fake_run_bifrost_server)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brokk",
            "mcp",
            "--workspace",
            str(tmp_path),
            "--help",
            "--custom-flag",
            "value",
        ],
    )

    main_module.main()

    assert captured["kwargs"]["workspace_dir"] == tmp_path.resolve()
    assert captured["kwargs"]["passthrough_args"] == ["--help", "--custom-flag", "value"]


def test_main_mcp_help_forwarding(monkeypatch, tmp_path) -> None:
    """Verify that 'brokk mcp --help' forwards --help as a passthrough arg."""
    captured: dict[str, Any] = {}

    from brokk_code import bifrost_launcher as bifrost_launcher_module

    def fake_run_bifrost_server(**kwargs: Any) -> None:
        captured["kwargs"] = kwargs

    monkeypatch.setattr(bifrost_launcher_module, "run_bifrost_server", fake_run_bifrost_server)
    monkeypatch.setattr(
        sys,
        "argv",
        ["brokk", "mcp", "--help"],
    )

    main_module.main()

    assert captured["kwargs"]["passthrough_args"] == ["--help"]


def test_main_exec_resolves_workspace_to_repo_root(monkeypatch, tmp_path) -> None:
    captured: dict[str, Any] = {"ran": False}
    repo_root = tmp_path / "repo"
    nested_workspace = repo_root / "src" / "pkg"
    nested_workspace.mkdir(parents=True)
    (repo_root / ".git").mkdir()

    async def fake_run_headless_job(**kwargs: Any) -> None:
        captured["kwargs"] = kwargs
        captured["ran"] = True

    monkeypatch.setattr(main_module, "run_headless_job", fake_run_headless_job)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brokk",
            "exec",
            "--workspace",
            str(nested_workspace),
            "Fix the bug",
        ],
    )

    main_module.main()

    assert captured["ran"] is True
    assert captured["kwargs"]["workspace_dir"] == repo_root.resolve()
    assert captured["kwargs"]["mode"] == "LITE_AGENT"
    assert captured["kwargs"]["tags"] == {"mode": "LITE_AGENT"}


def test_main_acp_accepts_legacy_ide_flag_but_ignores_it(monkeypatch, tmp_path) -> None:
    captured: dict[str, Any] = {}

    def fake_run_anvil_acp_server(**kwargs: Any) -> None:
        captured["kwargs"] = kwargs

    monkeypatch.setattr(main_module, "run_anvil_acp_server", fake_run_anvil_acp_server)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brokk",
            "acp",
            "--workspace",
            str(tmp_path),
            "--ide",
            "zed",
        ],
    )

    main_module.main()

    assert captured["kwargs"]["workspace_dir"] == tmp_path.resolve()
    assert "--ide" not in captured["kwargs"]["passthrough_args"]
    assert "zed" not in captured["kwargs"]["passthrough_args"]


def test_main_acp_rejects_extra_positional(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["brokk", "acp", "zed", "--workspace", str(tmp_path)],
    )

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code == 2


def test_main_install_zed_routes_to_installer(monkeypatch, tmp_path, capsys) -> None:
    captured: dict[str, Any] = {}

    def fake_configure_zed_acp_settings(
        *, force: bool = False, settings_path: Any = None, uvx_command: Any = None, **_kw
    ):
        captured["force"] = force
        return tmp_path / ".config" / "zed" / "settings.json"

    monkeypatch.setattr(main_module, "configure_zed_acp_settings", fake_configure_zed_acp_settings)
    _stub_install_warmup(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["brokk", "install", "zed", "--force"])

    main_module.main()

    output = capsys.readouterr().out
    assert captured["force"] is True
    assert "Configured Zed ACP integration" in output


def test_main_install_zed_conflict_exits_nonzero(monkeypatch) -> None:
    def fake_configure_zed_acp_settings(
        *, force: bool = False, settings_path=None, uvx_command=None, **_kw
    ):
        raise main_module.ExistingBrokkCodeEntryError("exists")

    _stub_install_warmup(monkeypatch)
    monkeypatch.setattr(main_module, "configure_zed_acp_settings", fake_configure_zed_acp_settings)
    monkeypatch.setattr(sys, "argv", ["brokk", "install", "zed"])

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code == 1


def test_main_install_zed_invalid_json_exits_nonzero(monkeypatch) -> None:
    def fake_configure_zed_acp_settings(
        *, force: bool = False, settings_path=None, uvx_command=None, **_kw
    ):
        raise ValueError("Could not parse as JSON/JSONC")

    _stub_install_warmup(monkeypatch)
    monkeypatch.setattr(main_module, "configure_zed_acp_settings", fake_configure_zed_acp_settings)
    monkeypatch.setattr(sys, "argv", ["brokk", "install", "zed"])

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code == 1


def test_main_install_intellij_routes_to_installer(monkeypatch, tmp_path, capsys) -> None:
    captured: dict[str, Any] = {}

    def fake_configure_intellij_acp_settings(
        *, force: bool = False, settings_path: Any = None, uvx_command: Any = None, **_kw
    ):
        captured["force"] = force
        return tmp_path / "intellij-config"

    _stub_install_warmup(monkeypatch)
    monkeypatch.setattr(
        main_module, "configure_intellij_acp_settings", fake_configure_intellij_acp_settings
    )
    monkeypatch.setattr(sys, "argv", ["brokk", "install", "intellij", "--force"])

    main_module.main()

    output = capsys.readouterr().out
    assert captured["force"] is True
    assert "Configured IntelliJ ACP integration" in output


def test_main_install_jetbrains_alias_routes_to_intellij_installer(
    monkeypatch, tmp_path, capsys
) -> None:
    captured: dict[str, Any] = {}

    def fake_configure_intellij_acp_settings(
        *, force: bool = False, settings_path: Any = None, uvx_command: Any = None, **_kw
    ):
        captured["force"] = force
        return tmp_path / "intellij-config"

    _stub_install_warmup(monkeypatch)
    monkeypatch.setattr(
        main_module, "configure_intellij_acp_settings", fake_configure_intellij_acp_settings
    )
    monkeypatch.setattr(sys, "argv", ["brokk", "install", "jetbrains", "--force"])

    main_module.main()

    output = capsys.readouterr().out
    assert captured["force"] is True
    assert "Configured IntelliJ ACP integration" in output


def test_main_install_nvim_routes_to_installer(monkeypatch, tmp_path, capsys) -> None:
    captured: dict[str, Any] = {}

    def fake_configure_nvim_codecompanion_acp_settings(
        *, force: bool = False, settings_path: Any = None
    ):
        captured["force"] = force
        return tmp_path / ".config" / "nvim" / "lua" / "brokk" / "brokk_codecompanion.lua"

    _stub_install_warmup(monkeypatch)
    monkeypatch.setattr(
        main_module,
        "configure_nvim_codecompanion_acp_settings",
        fake_configure_nvim_codecompanion_acp_settings,
    )
    monkeypatch.setattr(
        sys, "argv", ["brokk", "install", "nvim", "--plugin", "codecompanion", "--force"]
    )

    main_module.main()

    output = capsys.readouterr().out
    assert captured["force"] is True
    assert "Configured Neovim CodeCompanion ACP adapter" in output


def test_main_install_nvim_conflict_exits_nonzero(monkeypatch) -> None:
    def fake_configure_nvim_codecompanion_acp_settings(*, force: bool = False, settings_path=None):
        raise main_module.ExistingBrokkCodeEntryError("exists")

    _stub_install_warmup(monkeypatch)
    monkeypatch.setattr(
        main_module,
        "configure_nvim_codecompanion_acp_settings",
        fake_configure_nvim_codecompanion_acp_settings,
    )
    monkeypatch.setattr(sys, "argv", ["brokk", "install", "nvim", "--plugin", "codecompanion"])

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code == 1


def test_main_install_neovim_with_plugin_codecompanion_routes_to_installer(
    monkeypatch, tmp_path, capsys
) -> None:
    captured: dict[str, Any] = {}

    def fake_configure_nvim_codecompanion_acp_settings(
        *, force: bool = False, settings_path: Any = None
    ):
        captured["force"] = force
        return tmp_path / ".config" / "nvim" / "lua" / "brokk" / "brokk_codecompanion.lua"

    _stub_install_warmup(monkeypatch)
    monkeypatch.setattr(
        main_module,
        "configure_nvim_codecompanion_acp_settings",
        fake_configure_nvim_codecompanion_acp_settings,
    )
    monkeypatch.setattr(
        sys, "argv", ["brokk", "install", "neovim", "--plugin", "codecompanion", "--force"]
    )

    main_module.main()

    output = capsys.readouterr().out
    assert captured["force"] is True
    assert "Configured Neovim CodeCompanion ACP adapter" in output


def test_main_install_neovim_with_plugin_avante_routes_to_installer(
    monkeypatch, tmp_path, capsys
) -> None:
    captured: dict[str, Any] = {}

    def fake_configure_nvim_avante_acp_settings(*, force: bool = False, settings_path: Any = None):
        captured["force"] = force
        return tmp_path / ".config" / "nvim" / "lua" / "brokk" / "brokk_avante.lua"

    _stub_install_warmup(monkeypatch)
    monkeypatch.setattr(
        main_module,
        "configure_nvim_avante_acp_settings",
        fake_configure_nvim_avante_acp_settings,
    )
    monkeypatch.setattr(
        sys, "argv", ["brokk", "install", "neovim", "--plugin", "avante", "--force"]
    )

    main_module.main()

    output = capsys.readouterr().out
    assert captured["force"] is True
    assert "Configured Neovim Avante ACP provider" in output


def test_main_install_plugin_with_non_neovim_target_exits_nonzero(monkeypatch) -> None:
    _stub_install_warmup(monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        ["brokk", "install", "zed", "--plugin", "avante"],
    )

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code == 1


def test_install_neovim_invalid_selection_skips_key_prompt(monkeypatch) -> None:
    """Verify that invalid Neovim plugin selection fails before writing config."""
    _stub_install_warmup(monkeypatch)

    class FakeTtyInput(StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(sys, "stdin", FakeTtyInput(""))
    monkeypatch.setattr("builtins.input", lambda _prompt="": "99")

    monkeypatch.setattr(sys, "argv", ["brokk", "install", "neovim"])

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code == 1


def test_main_install_neovim_routes_to_installer(monkeypatch, tmp_path, capsys) -> None:
    captured: dict[str, Any] = {}

    def fake_configure_nvim_codecompanion_acp_settings(
        *, force: bool = False, settings_path: Any = None
    ):
        captured["force"] = force
        return tmp_path / ".config" / "nvim" / "lua" / "brokk" / "brokk_codecompanion.lua"

    _stub_install_warmup(monkeypatch)
    monkeypatch.setattr(
        main_module,
        "configure_nvim_codecompanion_acp_settings",
        fake_configure_nvim_codecompanion_acp_settings,
    )
    monkeypatch.setattr(sys, "argv", ["brokk", "install", "neovim", "--force"])

    main_module.main()

    output = capsys.readouterr().out
    assert captured["force"] is True
    assert "Configured Neovim CodeCompanion ACP adapter" in output


def test_main_install_neovim_with_plugin_codecompanion_conflict_exits_nonzero(
    monkeypatch,
) -> None:
    def fake_configure_nvim_codecompanion_acp_settings(*, force: bool = False, settings_path=None):
        raise main_module.ExistingBrokkCodeEntryError("exists")

    _stub_install_warmup(monkeypatch)
    monkeypatch.setattr(
        main_module,
        "configure_nvim_codecompanion_acp_settings",
        fake_configure_nvim_codecompanion_acp_settings,
    )
    monkeypatch.setattr(sys, "argv", ["brokk", "install", "neovim", "--plugin", "codecompanion"])

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code == 1


def test_main_install_verbose_does_not_prefetch_runtime(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(main_module, "ensure_uv_ready", lambda: "/usr/local/bin/uv")

    def fake_configure_zed_acp_settings(
        *, force: bool = False, settings_path=None, uvx_command=None, **_kw
    ):
        return tmp_path / ".config" / "zed" / "settings.json"

    monkeypatch.setattr(main_module, "configure_zed_acp_settings", fake_configure_zed_acp_settings)
    monkeypatch.setattr(sys, "argv", ["brokk", "install", "zed", "-v"])

    main_module.main()

    output = capsys.readouterr().out.strip().splitlines()
    assert any("Configured Zed ACP integration" in line for line in output)
    assert not any("java" in line.lower() for line in output)
    assert not any("--main" in line for line in output)


def test_main_install_intellij_conflict_exits_nonzero(monkeypatch) -> None:
    def fake_configure_intellij_acp_settings(
        *, force: bool = False, settings_path=None, uvx_command=None, **_kw
    ):
        raise main_module.ExistingBrokkCodeEntryError("exists")

    _stub_install_warmup(monkeypatch)
    monkeypatch.setattr(
        main_module, "configure_intellij_acp_settings", fake_configure_intellij_acp_settings
    )
    monkeypatch.setattr(sys, "argv", ["brokk", "install", "intellij"])

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code == 1


def test_main_install_intellij_invalid_json_exits_nonzero(monkeypatch) -> None:
    def fake_configure_intellij_acp_settings(
        *, force: bool = False, settings_path=None, uvx_command=None, **_kw
    ):
        raise ValueError("Could not parse as JSON")

    _stub_install_warmup(monkeypatch)
    monkeypatch.setattr(
        main_module, "configure_intellij_acp_settings", fake_configure_intellij_acp_settings
    )
    monkeypatch.setattr(sys, "argv", ["brokk", "install", "intellij"])

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code == 1


def test_main_install_mcp_routes_to_installer(monkeypatch, tmp_path, capsys) -> None:
    captured: dict[str, Any] = {}

    def fake_configure_claude_code_mcp_settings(
        *,
        force: bool = False,
        settings_path: Any = None,
        uvx_command: Any = None,
    ):
        captured["claude_force"] = force
        captured["claude_uvx_command"] = uvx_command
        return tmp_path / "claude.json"

    def fake_configure_codex_mcp_settings(
        *,
        force: bool = False,
        settings_path: Any = None,
        uvx_command: Any = None,
    ):
        captured["codex_force"] = force
        captured["codex_uvx_command"] = uvx_command
        return tmp_path / "codex.toml"

    def fake_install_codex_mcp_workspace_skill(*, skills_path: Any = None):
        return tmp_path / ".codex" / "skills" / "brokk-mcp-workspace" / "SKILL.md"

    def fake_install_codex_mcp_summaries_skill(*, skills_path: Any = None):
        return tmp_path / ".codex" / "skills" / "brokk-get-summaries" / "SKILL.md"

    def fake_install_claude_mcp_workspace_skill(*, skills_path: Any = None):
        return tmp_path / ".claude" / "skills" / "brokk-mcp-workspace" / "SKILL.md"

    def fake_install_claude_mcp_summaries_skill(*, skills_path: Any = None):
        return tmp_path / ".claude" / "skills" / "brokk-get-summaries" / "SKILL.md"

    monkeypatch.setattr(
        main_module,
        "configure_claude_code_mcp_settings",
        fake_configure_claude_code_mcp_settings,
    )
    monkeypatch.setattr(
        main_module,
        "configure_codex_mcp_settings",
        fake_configure_codex_mcp_settings,
    )
    monkeypatch.setattr(
        main_module,
        "install_codex_mcp_workspace_skill",
        fake_install_codex_mcp_workspace_skill,
    )
    monkeypatch.setattr(
        main_module,
        "install_codex_mcp_summaries_skill",
        fake_install_codex_mcp_summaries_skill,
    )
    monkeypatch.setattr(
        main_module,
        "install_claude_mcp_workspace_skill",
        fake_install_claude_mcp_workspace_skill,
    )
    monkeypatch.setattr(
        main_module,
        "install_claude_mcp_summaries_skill",
        fake_install_claude_mcp_summaries_skill,
    )
    monkeypatch.setattr(main_module, "ensure_uv_ready", lambda: "/usr/local/bin/uv")
    monkeypatch.setattr(
        sys,
        "argv",
        ["brokk", "install", "mcp", "--force"],
    )

    main_module.main()

    output = capsys.readouterr().out
    assert captured["claude_force"] is True
    assert captured["codex_force"] is True
    assert captured["claude_uvx_command"] == "/usr/local/bin/uvx"
    assert captured["codex_uvx_command"] == "/usr/local/bin/uvx"
    assert "Configured Claude Code MCP integration" in output
    assert "Configured Codex MCP integration" in output
    assert "Installed Codex MCP workspace skill" in output
    assert "Installed Codex MCP summaries skill" in output
    assert "Installed Claude MCP workspace skill" in output
    assert "Installed Claude MCP summaries skill" in output


def test_main_install_codex_plugin_routes_to_installer(monkeypatch, tmp_path, capsys) -> None:
    captured: dict[str, Any] = {}

    def fake_install_codex_local_plugin(*, force: bool = False, uvx_command: Any = None):
        captured["force"] = force
        captured["uvx_command"] = uvx_command
        return SimpleNamespace(
            plugin_path=tmp_path / ".codex" / "plugins" / "brokk",
            marketplace_path=tmp_path / ".agents" / "plugins" / "marketplace.json",
        )

    monkeypatch.setattr(main_module, "ensure_uv_ready", lambda: "/usr/local/bin/uv")
    monkeypatch.setattr(main_module, "install_codex_local_plugin", fake_install_codex_local_plugin)
    monkeypatch.setattr(sys, "argv", ["brokk", "install", "codex-plugin", "--force"])

    main_module.main()

    output = capsys.readouterr().out
    assert captured["force"] is True
    assert captured["uvx_command"] == "/usr/local/bin/uvx"
    assert "Installed Codex plugin files" in output
    assert "Updated Codex marketplace" in output
    assert "Restart Codex" in output


def test_main_rejects_removed_tui_commands(monkeypatch) -> None:
    for command in ("resume", "sessions"):
        monkeypatch.setattr(sys, "argv", ["brokk", command])

        with pytest.raises(SystemExit) as exc:
            main_module.main()

        assert exc.value.code == 2


def test_main_issue_create_routes_correctly(monkeypatch, tmp_path) -> None:
    captured: dict[str, Any] = {"ran": False}
    temp_workspace = tmp_path / "temp-create"
    temp_workspace.mkdir()

    async def fake_run_headless_job(**kwargs: Any) -> None:
        captured["kwargs"] = kwargs
        captured["ran"] = True

    @contextmanager
    def fake_temp_checkout(**kwargs: Any):
        captured["checkout_kwargs"] = kwargs
        yield temp_workspace

    monkeypatch.setattr(main_module, "run_headless_job", fake_run_headless_job)
    monkeypatch.setattr(main_module, "_temporary_issue_repo_checkout", fake_temp_checkout)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brokk",
            "issue",
            "create",
            "Broken build",
            "--workspace",
            str(tmp_path),
            "--repo-owner",
            "acme",
            "--repo-name",
            "tools",
            "--model",
            "custom-model",
            "--reasoning-effort",
            "high",
        ],
    )

    main_module.main()

    assert captured["ran"] is True
    assert captured["checkout_kwargs"]["repo_owner"] == "acme"
    assert captured["checkout_kwargs"]["repo_name"] == "tools"
    assert captured["checkout_kwargs"]["action_label"] == "Issue create"
    assert captured["kwargs"]["workspace_dir"] == temp_workspace
    assert captured["kwargs"]["task_input"] == "Broken build"
    assert captured["kwargs"]["mode"] == "ISSUE_WRITER"
    assert captured["kwargs"]["model"] == "custom-model"
    assert captured["kwargs"]["reasoning_effort"] == "high"
    assert captured["kwargs"]["tags"]["repo_owner"] == "acme"
    assert captured["kwargs"]["tags"]["repo_name"] == "tools"


def test_main_issue_create_missing_prompt_exits_nonzero(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["brokk", "issue", "create"],
    )

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code != 0


def test_main_issue_create_rejects_github_token_flag(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brokk",
            "issue",
            "create",
            "Broken build",
            "--github-token",
            "ghp_123",
            "--repo-owner",
            "acme",
            "--repo-name",
            "tools",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code == 2


def test_main_issue_solve_validation_invalid_owner(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brokk",
            "issue",
            "solve",
            "--issue-number",
            "1",
            "--repo-owner",
            "invalid/owner",
            "--repo-name",
            "r",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "Error: Invalid --repo-owner 'invalid/owner'" in err
    assert "^[A-Za-z0-9_.-]+$" in err


def test_main_issue_create_ignores_env_github_token(monkeypatch, tmp_path) -> None:
    captured: dict[str, Any] = {"ran": False}
    temp_workspace = tmp_path / "temp-create-env"
    temp_workspace.mkdir()

    async def fake_run_headless_job(**kwargs: Any) -> None:
        captured["kwargs"] = kwargs
        captured["ran"] = True

    @contextmanager
    def fake_temp_checkout(**kwargs: Any):
        captured["checkout_kwargs"] = kwargs
        yield temp_workspace

    monkeypatch.setattr(main_module, "run_headless_job", fake_run_headless_job)
    monkeypatch.setattr(main_module, "_temporary_issue_repo_checkout", fake_temp_checkout)
    monkeypatch.setenv("GITHUB_TOKEN", "env-token")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brokk",
            "issue",
            "create",
            "Broken build",
            "--repo-owner",
            "acme",
            "--repo-name",
            "tools",
        ],
    )

    main_module.main()

    assert "github_token" not in captured["kwargs"]["tags"]
    assert "github_token" not in captured["checkout_kwargs"]
    assert captured["kwargs"]["workspace_dir"] == temp_workspace
    assert captured["kwargs"]["model"] is None
    assert captured["kwargs"]["reasoning_effort"] is None
    assert captured["kwargs"]["verbose"] is False


def test_temporary_issue_checkout_requires_gh_binary(monkeypatch, capsys) -> None:
    monkeypatch.setattr(main_module.shutil, "which", lambda _name: None)

    with pytest.raises(SystemExit) as exc:
        with main_module._temporary_issue_repo_checkout(
            repo_owner="acme",
            repo_name="tools",
            action_label="Issue create",
        ):
            pass

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "requires the GitHub CLI" in err


def test_temporary_issue_checkout_requires_gh_auth(monkeypatch, capsys) -> None:
    monkeypatch.setattr(main_module.shutil, "which", lambda _name: "/usr/bin/gh")

    def fake_run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=["gh", "auth", "status"],
            stderr="not logged in",
        )

    monkeypatch.setattr(main_module.subprocess, "run", fake_run)

    with pytest.raises(SystemExit) as exc:
        with main_module._temporary_issue_repo_checkout(
            repo_owner="acme",
            repo_name="tools",
            action_label="Issue diagnose",
        ):
            pass

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "requires an authenticated GitHub CLI" in err
    assert "not logged in" in err


def test_main_issue_create_verbose_routes_correctly(monkeypatch, tmp_path) -> None:
    captured: dict[str, Any] = {"ran": False}
    temp_workspace = tmp_path / "temp-create-verbose"
    temp_workspace.mkdir()

    async def fake_run_headless_job(**kwargs: Any) -> None:
        captured["kwargs"] = kwargs
        captured["ran"] = True

    @contextmanager
    def fake_temp_checkout(**kwargs: Any):
        captured["checkout_kwargs"] = kwargs
        yield temp_workspace

    monkeypatch.setattr(main_module, "run_headless_job", fake_run_headless_job)
    monkeypatch.setattr(main_module, "_temporary_issue_repo_checkout", fake_temp_checkout)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brokk",
            "issue",
            "create",
            "Broken build",
            "-v",
            "--repo-owner",
            "acme",
            "--repo-name",
            "tools",
        ],
    )

    main_module.main()

    assert captured["ran"] is True
    assert captured["checkout_kwargs"]["action_label"] == "Issue create"
    assert captured["kwargs"]["workspace_dir"] == temp_workspace
    assert captured["kwargs"]["verbose"] is True


@pytest.mark.asyncio
async def test_run_headless_job_starts_before_prompt(monkeypatch, tmp_path) -> None:
    """Verifies that run_headless_job starts Anvil before submitting the prompt."""
    call_order: list[str] = []
    captured = _patch_headless_client(
        monkeypatch,
        events=[{"type": "STATE_CHANGE", "state": "COMPLETED"}],
        call_order=call_order,
    )

    await main_module.run_headless_job(
        workspace_dir=tmp_path,
        task_input="Test task",
        model="test-model",
        reasoning_effort="high",
        mode="LUTZ",
        tags={},
    )

    assert "start" in call_order
    assert "run_prompt" in call_order
    assert captured["init_kwargs"]["default_model"] == "test-model"
    assert captured["model"] == "test-model"
    assert captured["reasoning_effort"] == "high"

    start_idx = call_order.index("start")
    run_prompt_idx = call_order.index("run_prompt")

    assert start_idx < run_prompt_idx, "start() must be called before run_prompt()"


@pytest.mark.asyncio
async def test_run_headless_job_reports_failed_terminal_state(
    monkeypatch, tmp_path, capsys
) -> None:
    call_order: list[str] = []
    _patch_headless_client(
        monkeypatch,
        call_order=call_order,
        events=[
            {"type": "STATE_CHANGE", "state": "RUNNING"},
            {"type": "ERROR", "message": "GitHub API returned 403"},
            {"type": "STATE_CHANGE", "state": "FAILED"},
        ],
    )

    with pytest.raises(SystemExit) as exc:
        await main_module.run_headless_job(
            workspace_dir=tmp_path,
            task_input="Create issue",
            model="test-model",
            mode="ISSUE_WRITER",
            tags=ISSUE_TAGS,
        )

    captured = capsys.readouterr()
    assert exc.value.code == 1
    assert "Error event: GitHub API returned 403" in captured.err
    assert "ISSUE_WRITER job ended with state FAILED." in captured.err
    assert "Last error: GitHub API returned 403" in captured.err
    assert "stop" in call_order


@pytest.mark.asyncio
async def test_run_headless_job_reports_stage_on_submit_failure(
    monkeypatch, tmp_path, capsys
) -> None:
    call_order: list[str] = []
    _patch_headless_client(
        monkeypatch,
        call_order=call_order,
        prompt_error=main_module.HeadlessAnvilError("401 Unauthorized"),
    )

    with pytest.raises(SystemExit) as exc:
        await main_module.run_headless_job(
            workspace_dir=tmp_path,
            task_input="Create issue",
            model="test-model",
            mode="ISSUE_WRITER",
            tags=ISSUE_TAGS,
        )

    captured = capsys.readouterr()
    assert exc.value.code == 1
    assert "Anvil ACP error during ISSUE_WRITER job (streaming ACP events)" in captured.err
    assert "401 Unauthorized" in captured.err
    assert "stop" in call_order


@pytest.mark.asyncio
async def test_run_headless_job_uses_nested_event_data_for_errors_and_quiet_notifications(
    monkeypatch, tmp_path, capsys
) -> None:
    _patch_headless_client(
        monkeypatch,
        events=[
            {"type": "NOTIFICATION", "data": {"level": "INFO", "message": ""}},
            {"type": "NOTIFICATION", "data": {"level": "WARN", "message": "rate limit near"}},
            {"type": "LLM_TOKEN", "data": {"token": "hello"}},
            {"type": "ERROR", "data": {"message": "executor boom"}},
            {"type": "STATE_CHANGE", "data": {"state": "FAILED"}},
        ],
    )

    with pytest.raises(SystemExit) as exc:
        await main_module.run_headless_job(
            workspace_dir=tmp_path,
            task_input="Create issue",
            model="test-model",
            mode="ISSUE_WRITER",
            tags=ISSUE_TAGS,
        )

    captured = capsys.readouterr()
    assert exc.value.code == 1
    assert "[INFO] None" not in captured.out
    assert "[WARN] rate limit near" in captured.out
    assert "hello" not in captured.out
    assert "Error event: executor boom" in captured.err
    assert "Unknown error event" not in captured.err


@pytest.mark.asyncio
async def test_run_headless_job_verbose_shows_full_event_output(
    monkeypatch, tmp_path, capsys
) -> None:
    _patch_headless_client(
        monkeypatch,
        events=[
            {"type": "NOTIFICATION", "data": {"level": "INFO", "message": "planning"}},
            {"type": "STATE_CHANGE", "data": {"state": "RUNNING"}},
            {"type": "LLM_TOKEN", "data": {"token": "hello"}},
            {"type": "COMMAND_RESULT", "data": {"command": "gh issue create", "output": "ok"}},
            {"type": "TOOL_OUTPUT", "data": {"text": "tool text"}},
            {"type": "STATE_CHANGE", "data": {"state": "COMPLETED"}},
        ],
    )

    await main_module.run_headless_job(
        workspace_dir=tmp_path,
        task_input="Create issue",
        model="test-model",
        mode="ISSUE_WRITER",
        tags=ISSUE_TAGS,
        verbose=True,
    )

    captured = capsys.readouterr()
    assert "[INFO] planning" in captured.out
    assert "Job state: RUNNING" in captured.out
    assert "hello" in captured.out
    assert "[COMMAND_RESULT]" in captured.out
    assert "[TOOL_OUTPUT]" in captured.out


@pytest.mark.asyncio
async def test_run_headless_job_exits_nonzero_on_error_event_without_failed_state(
    monkeypatch, tmp_path, capsys
) -> None:
    _patch_headless_client(
        monkeypatch,
        events=[
            {"type": "NOTIFICATION", "data": {"level": "INFO", "message": "Job started"}},
            {"type": "ERROR", "data": {"message": "parseIssueResponse: invalid JSON"}},
            # Stream ends without a terminal FAILED/CANCELLED state event.
        ],
    )

    with pytest.raises(SystemExit) as exc:
        await main_module.run_headless_job(
            workspace_dir=tmp_path,
            task_input="Create issue",
            model="test-model",
            mode="ISSUE_WRITER",
            tags=ISSUE_TAGS,
        )

    captured = capsys.readouterr()
    assert exc.value.code == 1
    assert "Job finished." not in captured.out
    assert "ISSUE_WRITER job ended with errors (last observed state: UNKNOWN)." in captured.err
    assert "Last error: parseIssueResponse: invalid JSON" in captured.err


@pytest.mark.asyncio
async def test_run_headless_job_prints_issue_created_link_from_suppressed_tokens(
    monkeypatch, tmp_path, capsys
) -> None:
    _patch_headless_client(
        monkeypatch,
        events=[
            {"type": "LLM_TOKEN", "data": {"token": "Created issue: "}},
            {
                "type": "LLM_TOKEN",
                "data": {"token": "https://github.com/brokkai/brokk/issues/123"},
            },
            {"type": "STATE_CHANGE", "data": {"state": "COMPLETED"}},
        ],
    )

    await main_module.run_headless_job(
        workspace_dir=tmp_path,
        task_input="Create issue",
        model="test-model",
        mode="ISSUE_WRITER",
        tags=ISSUE_TAGS,
    )

    captured = capsys.readouterr()
    assert "Issue created: https://github.com/brokkai/brokk/issues/123" in captured.out
    assert "Job submitted:" not in captured.out
    assert "Job finished." not in captured.out


@pytest.mark.asyncio
async def test_run_headless_job_prints_issue_created_link_from_issue_writer_notification(
    monkeypatch, tmp_path, capsys
) -> None:
    _patch_headless_client(
        monkeypatch,
        events=[
            {
                "type": "NOTIFICATION",
                "data": {
                    "level": "INFO",
                    "message": (
                        "ISSUE_WRITER: issue created I_kwDOXYZ "
                        "https://github.com/brokkai/brokk/issues/456"
                    ),
                },
            },
            {"type": "STATE_CHANGE", "data": {"state": "COMPLETED"}},
        ],
    )

    await main_module.run_headless_job(
        workspace_dir=tmp_path,
        task_input="Create issue",
        model="test-model",
        mode="ISSUE_WRITER",
        tags=ISSUE_TAGS,
    )

    captured = capsys.readouterr()
    assert "Issue created: https://github.com/brokkai/brokk/issues/456" in captured.out


@pytest.mark.asyncio
async def test_run_headless_job_prints_issue_created_link_from_tool_output_result_text(
    monkeypatch, tmp_path, capsys
) -> None:
    _patch_headless_client(
        monkeypatch,
        events=[
            {
                "type": "TOOL_OUTPUT",
                "data": {
                    "resultText": "Created: https://github.com/brokkai/brokk/issues/789",
                    "name": "createGitHubIssue",
                    "status": "SUCCESS",
                },
            },
            {"type": "STATE_CHANGE", "data": {"state": "COMPLETED"}},
        ],
    )

    await main_module.run_headless_job(
        workspace_dir=tmp_path,
        task_input="Create issue",
        model="test-model",
        mode="ISSUE_WRITER",
        tags=ISSUE_TAGS,
    )

    captured = capsys.readouterr()
    assert "Issue created: https://github.com/brokkai/brokk/issues/789" in captured.out


@pytest.mark.asyncio
async def test_run_headless_job_prints_issue_created_link_from_structured_issue_created_event(
    monkeypatch, tmp_path, capsys
) -> None:
    _patch_headless_client(
        monkeypatch,
        events=[
            {
                "type": "ISSUE_CREATED",
                "data": {
                    "issueId": "#987",
                    "issueUrl": "https://github.com/brokkai/brokk/issues/987",
                    "repoOwner": "brokkai",
                    "repoName": "brokk",
                },
            },
            {"type": "STATE_CHANGE", "data": {"state": "COMPLETED"}},
        ],
    )

    await main_module.run_headless_job(
        workspace_dir=tmp_path,
        task_input="Create issue",
        model="test-model",
        mode="ISSUE_WRITER",
        tags=ISSUE_TAGS,
    )

    captured = capsys.readouterr()
    assert "Issue created: https://github.com/brokkai/brokk/issues/987" in captured.out


def test_main_issue_diagnose_routes_correctly(monkeypatch, tmp_path) -> None:
    captured: dict[str, Any] = {"ran": False}
    temp_workspace = tmp_path / "temp-diagnose"
    temp_workspace.mkdir()

    async def fake_run_headless_job(**kwargs: Any) -> None:
        captured["kwargs"] = kwargs
        captured["ran"] = True

    @contextmanager
    def fake_temp_workspace(**kwargs: Any):
        captured["temp_workspace_input"] = kwargs
        yield temp_workspace

    monkeypatch.setattr(main_module, "run_headless_job", fake_run_headless_job)
    monkeypatch.setattr(main_module, "_temporary_issue_repo_checkout", fake_temp_workspace)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brokk",
            "issue",
            "diagnose",
            "--issue-number",
            "456",
            "--workspace",
            str(tmp_path),
            "--repo-owner",
            "acme",
            "--repo-name",
            "widgets",
        ],
    )

    main_module.main()

    assert captured["ran"] is True
    assert captured["temp_workspace_input"]["repo_owner"] == "acme"
    assert captured["temp_workspace_input"]["repo_name"] == "widgets"
    assert captured["temp_workspace_input"]["action_label"] == "Issue diagnose"
    assert captured["kwargs"]["mode"] == "ISSUE_DIAGNOSE"
    assert captured["kwargs"]["workspace_dir"] == temp_workspace
    assert captured["kwargs"]["task_input"] == "Diagnose GitHub Issue #456"
    assert captured["kwargs"]["tags"]["issue_number"] == "456"
    assert captured["kwargs"]["tags"]["repo_owner"] == "acme"
    assert captured["kwargs"]["tags"]["repo_name"] == "widgets"


def test_main_issue_solve_routes_correctly(monkeypatch, tmp_path) -> None:
    captured: dict[str, Any] = {"ran": False}
    temp_workspace = tmp_path / "temp-copy"
    temp_workspace.mkdir()

    async def fake_run_headless_job(**kwargs: Any) -> None:
        captured["kwargs"] = kwargs
        captured["ran"] = True

    @contextmanager
    def fake_temp_workspace(**kwargs: Any):
        captured["temp_workspace_input"] = kwargs
        yield temp_workspace

    monkeypatch.setattr(main_module, "run_headless_job", fake_run_headless_job)
    monkeypatch.setattr(main_module, "_temporary_issue_repo_checkout", fake_temp_workspace)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brokk",
            "issue",
            "solve",
            "--issue-number",
            "123",
            "--workspace",
            str(tmp_path),
            "--repo-owner",
            "acme",
            "--repo-name",
            "tools",
            "--skip-verification",
            "--max-issue-fix-attempts",
            "7",
        ],
    )

    main_module.main()

    assert captured["ran"] is True
    assert captured["temp_workspace_input"]["repo_owner"] == "acme"
    assert captured["temp_workspace_input"]["repo_name"] == "tools"
    assert captured["temp_workspace_input"]["action_label"] == "Issue solve"
    assert captured["kwargs"]["mode"] == "ISSUE"
    assert captured["kwargs"]["workspace_dir"] == temp_workspace
    assert captured["kwargs"]["task_input"] == "Resolve GitHub Issue #123"
    assert captured["kwargs"]["tags"]["issue_number"] == "123"
    assert captured["kwargs"]["skip_verification"] is True
    assert captured["kwargs"]["max_issue_fix_attempts"] == 7


def test_main_issue_solve_temp_workspace_cleanup_on_keyboard_interrupt(
    monkeypatch, tmp_path
) -> None:
    captured: dict[str, Any] = {"cleaned": False}
    temp_workspace = tmp_path / "temp-copy"
    temp_workspace.mkdir()

    async def fake_run_headless_job(**kwargs: Any) -> None:
        raise KeyboardInterrupt

    @contextmanager
    def fake_temp_workspace(**kwargs: Any):
        captured["temp_workspace_input"] = kwargs
        try:
            yield temp_workspace
        finally:
            captured["cleaned"] = True

    monkeypatch.setattr(main_module, "run_headless_job", fake_run_headless_job)
    monkeypatch.setattr(main_module, "_temporary_issue_repo_checkout", fake_temp_workspace)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brokk",
            "issue",
            "solve",
            "--issue-number",
            "123",
            "--workspace",
            str(tmp_path),
            "--repo-owner",
            "acme",
            "--repo-name",
            "tools",
        ],
    )

    with pytest.raises(KeyboardInterrupt):
        main_module.main()

    assert captured["temp_workspace_input"]["repo_owner"] == "acme"
    assert captured["cleaned"] is True


def test_main_issue_solve_missing_number_exits_nonzero(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brokk",
            "issue",
            "solve",
            "--repo-owner",
            "acme",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code != 0


def test_main_issue_solve_missing_repo_owner_exits_nonzero(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brokk",
            "issue",
            "solve",
            "--issue-number",
            "123",
            "--repo-name",
            "tools",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code != 0


def test_main_issue_solve_missing_repo_name_exits_nonzero(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brokk",
            "issue",
            "solve",
            "--issue-number",
            "123",
            "--repo-owner",
            "acme",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code != 0


def test_main_issue_solve_invalid_repo_owner_exits_nonzero(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brokk",
            "issue",
            "solve",
            "--issue-number",
            "123",
            "--repo-owner",
            "invalid/owner",
            "--repo-name",
            "tools",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code != 0


def test_main_issue_solve_invalid_repo_name_exits_nonzero(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brokk",
            "issue",
            "solve",
            "--issue-number",
            "123",
            "--repo-owner",
            "acme",
            "--repo-name",
            "invalid/repo",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code != 0


def test_main_issue_create_missing_repo_owner_exits_nonzero(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brokk",
            "issue",
            "create",
            "new issue",
            "--repo-name",
            "tools",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code != 0


def test_main_issue_create_missing_repo_name_exits_nonzero(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brokk",
            "issue",
            "create",
            "new issue",
            "--repo-owner",
            "acme",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code != 0


def test_main_issue_create_invalid_repo_owner_exits_nonzero(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brokk",
            "issue",
            "create",
            "new issue",
            "--repo-owner",
            "invalid/owner",
            "--repo-name",
            "tools",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code != 0


def test_main_issue_create_invalid_repo_name_exits_nonzero(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brokk",
            "issue",
            "create",
            "new issue",
            "--repo-owner",
            "acme",
            "--repo-name",
            "invalid/repo",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code != 0


def test_main_issue_diagnose_missing_repo_owner_exits_nonzero(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brokk",
            "issue",
            "diagnose",
            "--issue-number",
            "123",
            "--repo-name",
            "tools",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code != 0


def test_main_issue_diagnose_missing_repo_name_exits_nonzero(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brokk",
            "issue",
            "diagnose",
            "--issue-number",
            "123",
            "--repo-owner",
            "acme",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code != 0


def test_main_issue_diagnose_invalid_repo_owner_exits_nonzero(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brokk",
            "issue",
            "diagnose",
            "--issue-number",
            "123",
            "--repo-owner",
            "invalid/owner",
            "--repo-name",
            "tools",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code != 0


def test_main_pr_create_routes_correctly(monkeypatch, tmp_path) -> None:
    captured: dict[str, Any] = {"ran": False}

    async def fake_run_pr_create(**kwargs: Any) -> None:
        captured["kwargs"] = kwargs
        captured["ran"] = True

    monkeypatch.setattr(main_module, "run_pr_create", fake_run_pr_create)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brokk",
            "pr",
            "create",
            "--workspace",
            str(tmp_path),
            "--title",
            "My PR Title",
            "--body",
            "PR description here",
            "--base",
            "main",
            "--head",
            "feature-branch",
            "--model",
            "gpt-test",
            "--reasoning-effort",
            "low",
        ],
    )

    main_module.main()

    assert captured["ran"] is True
    assert captured["kwargs"]["workspace_dir"] == tmp_path.resolve()
    assert captured["kwargs"]["title"] == "My PR Title"
    assert captured["kwargs"]["body"] == "PR description here"
    assert captured["kwargs"]["base_branch"] == "main"
    assert captured["kwargs"]["head_branch"] == "feature-branch"
    assert captured["kwargs"]["model"] == "gpt-test"
    assert captured["kwargs"]["reasoning_effort"] == "low"


def test_main_pr_create_omitted_title_body_routes_correctly(monkeypatch, tmp_path) -> None:
    """Verify that omitting title/body still routes to run_pr_create (suggest path)."""
    captured: dict[str, Any] = {"ran": False}

    async def fake_run_pr_create(**kwargs: Any) -> None:
        captured["kwargs"] = kwargs
        captured["ran"] = True

    monkeypatch.setattr(main_module, "run_pr_create", fake_run_pr_create)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brokk",
            "pr",
            "create",
            "--workspace",
            str(tmp_path),
        ],
    )

    main_module.main()

    assert captured["ran"] is True
    assert captured["kwargs"]["title"] is None
    assert captured["kwargs"]["body"] is None


def test_main_pr_create_missing_subcommand_exits_nonzero(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["brokk", "pr", "--workspace", str(tmp_path)],
    )

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code != 0


@pytest.mark.asyncio
async def test_run_pr_create_with_explicit_title_body(monkeypatch, tmp_path, capsys) -> None:
    """Verifies run_pr_create sends explicit title/body in the ACP prompt."""
    call_order: list[str] = []
    captured = _patch_headless_client(
        monkeypatch,
        call_order=call_order,
        events=[
            {
                "type": "LLM_TOKEN",
                "data": {
                    "token": (
                        "PR_CREATE: pull request created https://github.com/test/repo/pull/42"
                    )
                },
            },
            {"type": "STATE_CHANGE", "data": {"state": "COMPLETED"}},
        ],
    )

    await main_module.run_pr_create(
        workspace_dir=tmp_path,
        title="Explicit Title",
        body="Explicit Body",
        model="gpt-test",
        reasoning_effort="high",
    )

    assert "env" not in captured["init_kwargs"]
    assert captured["init_kwargs"]["default_model"] == "gpt-test"
    assert captured["model"] == "gpt-test"
    assert captured["reasoning_effort"] == "high"
    assert "Use this exact pull request title" in captured["prompt"]
    assert "Explicit Title" in captured["prompt"]
    assert "Use this exact pull request body" in captured["prompt"]
    assert "Explicit Body" in captured["prompt"]
    assert "run_prompt" in call_order
    assert "stop" in call_order
    assert "https://github.com/test/repo/pull/42" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_run_pr_create_derives_title_when_missing(monkeypatch, tmp_path) -> None:
    """Verifies run_pr_create asks Anvil to derive missing title in the ACP prompt."""
    captured = _patch_headless_client(
        monkeypatch,
        events=[{"type": "STATE_CHANGE", "data": {"state": "COMPLETED"}}],
    )

    await main_module.run_pr_create(
        workspace_dir=tmp_path,
        title=None,
        body="Explicit Body",
    )

    assert "Derive a clear pull request title" in captured["prompt"]
    assert "Use this exact pull request body" in captured["prompt"]
    assert "Explicit Body" in captured["prompt"]


@pytest.mark.asyncio
async def test_run_pr_create_derives_title_body_and_uses_branches(
    monkeypatch, tmp_path, capsys
) -> None:
    """Verifies run_pr_create passes branch guidance to ACP."""
    captured = _patch_headless_client(
        monkeypatch,
        events=[
            {
                "type": "NOTIFICATION",
                "data": {
                    "message": (
                        "PR_CREATE: pull request created https://github.com/org/repo/pull/777"
                    )
                },
            },
            {"type": "STATE_CHANGE", "data": {"state": "COMPLETED"}},
        ],
    )

    await main_module.run_pr_create(
        workspace_dir=tmp_path,
        title=None,
        body=None,
        base_branch="main",
        head_branch="feature-xyz",
    )

    assert "env" not in captured["init_kwargs"]
    assert "Use `main` as the base branch." in captured["prompt"]
    assert "Use `feature-xyz` as the head branch." in captured["prompt"]
    assert "Derive a clear pull request title" in captured["prompt"]
    assert "Derive a useful Markdown pull request body" in captured["prompt"]
    assert "https://github.com/org/repo/pull/777" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_run_pr_create_executor_error_exits_nonzero(monkeypatch, tmp_path, capsys) -> None:
    """Verifies run_pr_create exits non-zero on ACP error event."""
    call_order: list[str] = []
    _patch_headless_client(
        monkeypatch,
        call_order=call_order,
        events=[
            {"type": "ERROR", "data": {"message": "GitHub API error"}},
            {"type": "STATE_CHANGE", "data": {"state": "FAILED"}},
        ],
    )

    with pytest.raises(SystemExit) as exc:
        await main_module.run_pr_create(
            workspace_dir=tmp_path,
            title="Test",
            body="Test body",
        )

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "Anvil ACP error during PR create" in captured.err
    assert "GitHub API error" in captured.err
    assert "stop" in call_order


def test_main_commit_routes_correctly(monkeypatch, tmp_path) -> None:
    captured: dict[str, Any] = {"ran": False}

    async def fake_run_commit(**kwargs: Any) -> None:
        captured["kwargs"] = kwargs
        captured["ran"] = True

    monkeypatch.setattr(main_module, "run_commit", fake_run_commit)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brokk",
            "commit",
            "Fix the bug",
            "--workspace",
            str(tmp_path),
            "--model",
            "gpt-test",
            "--reasoning-effort",
            "medium",
        ],
    )

    main_module.main()

    assert captured["ran"] is True
    assert captured["kwargs"]["workspace_dir"] == tmp_path.resolve()
    assert captured["kwargs"]["message"] == "Fix the bug"
    assert captured["kwargs"]["model"] == "gpt-test"
    assert captured["kwargs"]["reasoning_effort"] == "medium"


def test_main_commit_no_message_routes_correctly(monkeypatch, tmp_path) -> None:
    captured: dict[str, Any] = {"ran": False}

    async def fake_run_commit(**kwargs: Any) -> None:
        captured["kwargs"] = kwargs
        captured["ran"] = True

    monkeypatch.setattr(main_module, "run_commit", fake_run_commit)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brokk",
            "commit",
            "--workspace",
            str(tmp_path),
        ],
    )

    main_module.main()

    assert captured["ran"] is True
    assert captured["kwargs"]["workspace_dir"] == tmp_path.resolve()
    assert captured["kwargs"]["message"] is None


def test_main_commit_uses_anvil_config_when_flags_omitted(monkeypatch, tmp_path) -> None:
    captured: dict[str, Any] = {"ran": False}
    AnvilScriptingConfig(
        use_global=False,
        tool_selections={
            "commit": AnvilToolSelection(
                model="configured-model",
                reasoning_effort="high",
            )
        },
    ).save()

    async def fake_run_commit(**kwargs: Any) -> None:
        captured["kwargs"] = kwargs
        captured["ran"] = True

    monkeypatch.setattr(main_module, "run_commit", fake_run_commit)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brokk",
            "commit",
            "--workspace",
            str(tmp_path),
        ],
    )

    main_module.main()

    assert captured["ran"] is True
    assert captured["kwargs"]["model"] == "configured-model"
    assert captured["kwargs"]["reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_run_commit_calls_lifecycle_in_order(monkeypatch, tmp_path) -> None:
    """Verifies that run_commit follows the correct lifecycle order."""
    call_order: list[str] = []
    captured = _patch_headless_client(
        monkeypatch,
        call_order=call_order,
        events=[
            {
                "type": "LLM_TOKEN",
                "data": {"token": "COMMIT: committed abc1234567890 Test commit"},
            },
            {"type": "STATE_CHANGE", "data": {"state": "COMPLETED"}},
        ],
    )

    await main_module.run_commit(
        workspace_dir=tmp_path,
        message="My commit message",
        model="gpt-test",
        reasoning_effort="medium",
    )

    assert "start" in call_order
    assert "run_prompt" in call_order
    assert "stop" in call_order
    assert captured["init_kwargs"]["default_model"] == "gpt-test"
    assert captured["model"] == "gpt-test"
    assert captured["reasoning_effort"] == "medium"
    assert "My commit message" in captured["prompt"]
    assert "Use this exact commit message" in captured["prompt"]

    start_idx = call_order.index("start")
    prompt_idx = call_order.index("run_prompt")
    stop_idx = call_order.index("stop")

    assert start_idx < prompt_idx
    assert prompt_idx < stop_idx


@pytest.mark.asyncio
async def test_run_commit_no_changes(monkeypatch, tmp_path, capsys) -> None:
    """Verifies that run_commit handles no changes case."""
    call_order: list[str] = []
    _patch_headless_client(
        monkeypatch,
        call_order=call_order,
        events=[
            {"type": "LLM_TOKEN", "data": {"token": "COMMIT: no changes"}},
            {"type": "STATE_CHANGE", "data": {"state": "COMPLETED"}},
        ],
    )

    await main_module.run_commit(workspace_dir=tmp_path, message=None)

    captured = capsys.readouterr()
    assert "No uncommitted changes" in captured.out
    assert "stop" in call_order


@pytest.mark.asyncio
async def test_run_commit_success_output(monkeypatch, tmp_path, capsys) -> None:
    """Verifies that run_commit prints commit info on success."""
    _patch_headless_client(
        monkeypatch,
        events=[
            {
                "type": "NOTIFICATION",
                "data": {"message": "COMMIT: committed abc1234567890 Fix parser bug"},
            },
            {"type": "STATE_CHANGE", "data": {"state": "COMPLETED"}},
        ],
    )

    await main_module.run_commit(workspace_dir=tmp_path, message="Fix parser bug")

    captured = capsys.readouterr()
    assert "abc1234" in captured.out
    assert "Fix parser bug" in captured.out


@pytest.mark.asyncio
async def test_run_commit_without_marker_or_git_change_exits_nonzero(
    monkeypatch, tmp_path, capsys
) -> None:
    """Verifies that run_commit does not report success from protocol completion alone."""
    _patch_headless_client(
        monkeypatch,
        events=[
            {"type": "LLM_TOKEN", "data": {"token": "finished"}},
            {"type": "STATE_CHANGE", "data": {"state": "COMPLETED"}},
        ],
    )

    with pytest.raises(SystemExit) as exc:
        await main_module.run_commit(workspace_dir=tmp_path, message="Fix parser bug")

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "without reporting or creating a commit" in captured.err


@pytest.mark.asyncio
async def test_run_commit_executor_error_exits_nonzero(monkeypatch, tmp_path, capsys) -> None:
    """Verifies that run_commit exits non-zero on ACP error event."""
    call_order: list[str] = []
    _patch_headless_client(
        monkeypatch,
        call_order=call_order,
        events=[
            {"type": "ERROR", "data": {"message": "Git error"}},
            {"type": "STATE_CHANGE", "data": {"state": "FAILED"}},
        ],
    )

    with pytest.raises(SystemExit) as exc:
        await main_module.run_commit(workspace_dir=tmp_path, message="test")

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "Anvil ACP error during commit" in captured.err
    assert "Git error" in captured.err
    assert "stop" in call_order


def test_main_pr_review_routes_correctly(monkeypatch, tmp_path) -> None:
    captured: dict[str, Any] = {"ran": False}

    async def fake_run_pr_review_job(**kwargs: Any) -> None:
        captured["kwargs"] = kwargs
        captured["ran"] = True

    monkeypatch.setattr(main_module, "run_pr_review_job", fake_run_pr_review_job)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brokk",
            "pr",
            "review",
            "--pr-number",
            "42",
            "--workspace",
            str(tmp_path),
            "--repo-owner",
            "acme",
            "--repo-name",
            "tools",
            "--model",
            "custom-model",
            "--reasoning-effort",
            "medium",
            "--verbose",
        ],
    )

    main_module.main()

    assert captured["ran"] is True
    assert captured["kwargs"]["workspace_dir"] == tmp_path.resolve()
    assert captured["kwargs"]["pr_number"] == 42
    assert captured["kwargs"]["repo_owner"] == "acme"
    assert captured["kwargs"]["repo_name"] == "tools"
    assert captured["kwargs"]["model"] == "custom-model"
    assert captured["kwargs"]["reasoning_effort"] == "medium"
    assert captured["kwargs"]["verbose"] is True


def test_main_pr_review_missing_pr_number_exits_nonzero(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brokk",
            "pr",
            "review",
            "--repo-owner",
            "acme",
            "--repo-name",
            "tools",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code != 0


def test_main_pr_review_missing_repo_owner_without_inference_exits_nonzero(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(main_module, "infer_github_repo_from_remote", lambda _: (None, None))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brokk",
            "pr",
            "review",
            "--pr-number",
            "42",
            "--workspace",
            str(tmp_path),
            "--repo-name",
            "tools",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code == 1


def test_main_pr_review_missing_repo_name_without_inference_exits_nonzero(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(main_module, "infer_github_repo_from_remote", lambda _: (None, None))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brokk",
            "pr",
            "review",
            "--pr-number",
            "42",
            "--workspace",
            str(tmp_path),
            "--repo-owner",
            "acme",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code == 1


def test_main_pr_review_infers_repo_from_https_remote(monkeypatch, tmp_path) -> None:
    captured: dict[str, Any] = {"ran": False}

    async def fake_run_pr_review_job(**kwargs: Any) -> None:
        captured["kwargs"] = kwargs
        captured["ran"] = True

    def fake_subprocess_run(cmd, **kwargs):
        class FakeResult:
            returncode = 0
            stdout = "https://github.com/inferred-owner/inferred-repo.git\n"
            stderr = ""

        return FakeResult()

    monkeypatch.setattr(main_module, "run_pr_review_job", fake_run_pr_review_job)
    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brokk",
            "pr",
            "review",
            "--pr-number",
            "42",
            "--workspace",
            str(tmp_path),
        ],
    )

    main_module.main()

    assert captured["ran"] is True
    assert captured["kwargs"]["repo_owner"] == "inferred-owner"
    assert captured["kwargs"]["repo_name"] == "inferred-repo"


def test_main_pr_review_infers_repo_from_ssh_remote(monkeypatch, tmp_path) -> None:
    captured: dict[str, Any] = {"ran": False}

    async def fake_run_pr_review_job(**kwargs: Any) -> None:
        captured["kwargs"] = kwargs
        captured["ran"] = True

    def fake_subprocess_run(cmd, **kwargs):
        class FakeResult:
            returncode = 0
            stdout = "git@github.com:ssh-owner/ssh-repo.git\n"
            stderr = ""

        return FakeResult()

    monkeypatch.setattr(main_module, "run_pr_review_job", fake_run_pr_review_job)
    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brokk",
            "pr",
            "review",
            "--pr-number",
            "42",
            "--workspace",
            str(tmp_path),
        ],
    )

    main_module.main()

    assert captured["ran"] is True
    assert captured["kwargs"]["repo_owner"] == "ssh-owner"
    assert captured["kwargs"]["repo_name"] == "ssh-repo"


def test_main_pr_review_explicit_params_override_inference(monkeypatch, tmp_path) -> None:
    captured: dict[str, Any] = {"ran": False}

    async def fake_run_pr_review_job(**kwargs: Any) -> None:
        captured["kwargs"] = kwargs
        captured["ran"] = True

    def fake_subprocess_run(cmd, **kwargs):
        class FakeResult:
            returncode = 0
            stdout = "https://github.com/inferred-owner/inferred-repo.git\n"
            stderr = ""

        return FakeResult()

    monkeypatch.setattr(main_module, "run_pr_review_job", fake_run_pr_review_job)
    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brokk",
            "pr",
            "review",
            "--pr-number",
            "42",
            "--workspace",
            str(tmp_path),
            "--repo-owner",
            "explicit-owner",
            "--repo-name",
            "explicit-repo",
        ],
    )

    main_module.main()

    assert captured["ran"] is True
    assert captured["kwargs"]["repo_owner"] == "explicit-owner"
    assert captured["kwargs"]["repo_name"] == "explicit-repo"


def test_main_pr_review_uses_default_anvil_model(monkeypatch, tmp_path) -> None:
    captured: dict[str, Any] = {"ran": False}

    async def fake_run_pr_review_job(**kwargs: Any) -> None:
        captured["kwargs"] = kwargs
        captured["ran"] = True

    monkeypatch.setattr(main_module, "run_pr_review_job", fake_run_pr_review_job)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brokk",
            "pr",
            "review",
            "--pr-number",
            "42",
            "--workspace",
            str(tmp_path),
            "--repo-owner",
            "acme",
            "--repo-name",
            "tools",
        ],
    )

    main_module.main()

    assert captured["kwargs"]["model"] is None
    assert captured["kwargs"]["reasoning_effort"] is None


def test_infer_github_repo_from_remote_https_format(monkeypatch, tmp_path) -> None:
    def fake_subprocess_run(cmd, **kwargs):
        class FakeResult:
            returncode = 0
            stdout = "https://github.com/test-owner/test-repo.git\n"
            stderr = ""

        return FakeResult()

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    owner, repo = git_utils_module.infer_github_repo_from_remote(tmp_path)

    assert owner == "test-owner"
    assert repo == "test-repo"


def test_infer_github_repo_from_remote_https_no_git_suffix(monkeypatch, tmp_path) -> None:
    def fake_subprocess_run(cmd, **kwargs):
        class FakeResult:
            returncode = 0
            stdout = "https://github.com/test-owner/test-repo\n"
            stderr = ""

        return FakeResult()

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    owner, repo = git_utils_module.infer_github_repo_from_remote(tmp_path)

    assert owner == "test-owner"
    assert repo == "test-repo"


def test_infer_github_repo_from_remote_ssh_format(monkeypatch, tmp_path) -> None:
    def fake_subprocess_run(cmd, **kwargs):
        class FakeResult:
            returncode = 0
            stdout = "git@github.com:ssh-owner/ssh-repo.git\n"
            stderr = ""

        return FakeResult()

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    owner, repo = git_utils_module.infer_github_repo_from_remote(tmp_path)

    assert owner == "ssh-owner"
    assert repo == "ssh-repo"


def test_infer_github_repo_from_remote_ssh_no_git_suffix(monkeypatch, tmp_path) -> None:
    def fake_subprocess_run(cmd, **kwargs):
        class FakeResult:
            returncode = 0
            stdout = "git@github.com:ssh-owner/ssh-repo\n"
            stderr = ""

        return FakeResult()

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    owner, repo = git_utils_module.infer_github_repo_from_remote(tmp_path)

    assert owner == "ssh-owner"
    assert repo == "ssh-repo"


def test_infer_github_repo_from_remote_non_github_returns_none(monkeypatch, tmp_path) -> None:
    def fake_subprocess_run(cmd, **kwargs):
        class FakeResult:
            returncode = 0
            stdout = "https://gitlab.com/owner/repo.git\n"
            stderr = ""

        return FakeResult()

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    owner, repo = git_utils_module.infer_github_repo_from_remote(tmp_path)

    assert owner is None
    assert repo is None


def test_infer_github_repo_from_remote_git_failure_returns_none(monkeypatch, tmp_path) -> None:
    def fake_subprocess_run(cmd, **kwargs):
        class FakeResult:
            returncode = 1
            stdout = ""
            stderr = "fatal: not a git repository"

        return FakeResult()

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    owner, repo = git_utils_module.infer_github_repo_from_remote(tmp_path)

    assert owner is None
    assert repo is None


def test_infer_github_repo_from_remote_empty_stdout_returns_none(monkeypatch, tmp_path) -> None:
    def fake_subprocess_run(cmd, **kwargs):
        class FakeResult:
            returncode = 0
            stdout = ""
            stderr = ""

        return FakeResult()

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    owner, repo = git_utils_module.infer_github_repo_from_remote(tmp_path)

    assert owner is None
    assert repo is None


@pytest.mark.asyncio
async def test_run_pr_review_job_submits_anvil_prompt(monkeypatch, tmp_path) -> None:
    call_order: list[str] = []
    captured = _patch_headless_client(
        monkeypatch,
        call_order=call_order,
        events=[{"type": "STATE_CHANGE", "data": {"state": "COMPLETED"}}],
    )

    await main_module.run_pr_review_job(
        workspace_dir=tmp_path,
        pr_number=42,
        repo_owner="test-owner",
        repo_name="test-repo",
        model="gpt-4",
        reasoning_effort="medium",
    )

    assert "start" in call_order
    assert "run_prompt" in call_order
    assert "env" not in captured["init_kwargs"]
    assert captured["init_kwargs"]["default_model"] == "gpt-4"
    assert captured["model"] == "gpt-4"
    assert captured["reasoning_effort"] == "medium"
    assert "test-owner/test-repo" in captured["prompt"]
    assert "pull request #42" in captured["prompt"].lower()


@pytest.mark.asyncio
async def test_run_pr_review_job_exits_nonzero_on_failed_state(
    monkeypatch, tmp_path, capsys
) -> None:
    _patch_headless_client(
        monkeypatch,
        events=[
            {"type": "ERROR", "data": {"message": "GitHub API error"}},
            {"type": "STATE_CHANGE", "data": {"state": "FAILED"}},
        ],
    )

    with pytest.raises(SystemExit) as exc:
        await main_module.run_pr_review_job(
            workspace_dir=tmp_path,
            pr_number=42,
            repo_owner="test-owner",
            repo_name="test-repo",
            model="gpt-4",
        )

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "PR review job ended with state FAILED" in captured.err


def test_install_only_writes_config(monkeypatch, tmp_path) -> None:
    """install command only writes config."""

    def fake_configure_zed_acp_settings(
        *, force: bool = False, uvx_command: str | None = None, **_kwargs
    ):
        return tmp_path / "zed.json"

    monkeypatch.setattr(main_module, "ensure_uv_ready", lambda: "/usr/bin/uv")
    monkeypatch.setattr(main_module, "configure_zed_acp_settings", fake_configure_zed_acp_settings)
    monkeypatch.setattr(
        sys,
        "argv",
        ["brokk", "install", "zed"],
    )

    main_module.main()


def test_install_zed_skips_auth_prompt_without_key(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("BROKK_API_KEY", raising=False)

    monkeypatch.setattr(main_module, "ensure_uv_ready", lambda: "/usr/bin/uv")
    monkeypatch.setattr(
        main_module,
        "configure_zed_acp_settings",
        lambda *, force=False, settings_path=None, uvx_command=None, **_kw: tmp_path / "zed.json",
    )

    monkeypatch.setattr(sys, "argv", ["brokk", "install", "zed"])
    main_module.main()


def test_install_mcp_skips_auth_prompt_without_key(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("BROKK_API_KEY", raising=False)

    monkeypatch.setattr(main_module, "ensure_uv_ready", lambda: "/usr/bin/uv")
    monkeypatch.setattr(
        main_module,
        "configure_claude_code_mcp_settings",
        lambda *, force=False, settings_path=None, uvx_command=None, **_kw: tmp_path / "c.json",
    )
    monkeypatch.setattr(
        main_module,
        "configure_codex_mcp_settings",
        lambda *, force=False, settings_path=None, uvx_command=None, **_kw: tmp_path / "cx.toml",
    )
    monkeypatch.setattr(
        main_module,
        "install_codex_mcp_workspace_skill",
        lambda **_kw: tmp_path / "s1",
    )
    monkeypatch.setattr(
        main_module,
        "install_codex_mcp_summaries_skill",
        lambda **_kw: tmp_path / "s2",
    )
    monkeypatch.setattr(
        main_module,
        "install_claude_mcp_workspace_skill",
        lambda **_kw: tmp_path / "s3",
    )
    monkeypatch.setattr(
        main_module,
        "install_claude_mcp_summaries_skill",
        lambda **_kw: tmp_path / "s4",
    )

    monkeypatch.setattr(sys, "argv", ["brokk", "install", "mcp"])
    main_module.main()


def test_install_zed_with_missing_key_interactive_does_not_read_key(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    """Verify that 'install zed' does not prompt for auth in TTY sessions."""
    monkeypatch.delenv("BROKK_API_KEY", raising=False)

    class FakeTtyInput(StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(sys, "stdin", FakeTtyInput(""))
    _stub_install_warmup(monkeypatch, stub_api_key=False)

    def fake_configure_zed(*args, **kwargs):
        return tmp_path / "zed.json"

    monkeypatch.setattr(main_module, "configure_zed_acp_settings", fake_configure_zed)
    monkeypatch.setattr(sys, "argv", ["brokk", "install", "zed"])

    main_module.main()

    out = capsys.readouterr().out
    assert "Configured Zed ACP" in out
    assert "Saved Brokk API key" not in out


def test_install_intellij_with_missing_key_piped_does_not_read_key(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    """Verify that 'install intellij' ignores stdin auth data."""
    monkeypatch.delenv("BROKK_API_KEY", raising=False)

    monkeypatch.setattr(sys, "stdin", StringIO("piped-key\n"))
    # Ensure it's not detected as TTY
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    _stub_install_warmup(monkeypatch, stub_api_key=False)

    def fake_configure_intellij(*args, **kwargs):
        return tmp_path / "intellij"

    monkeypatch.setattr(main_module, "configure_intellij_acp_settings", fake_configure_intellij)
    monkeypatch.setattr(sys, "argv", ["brokk", "install", "intellij"])

    main_module.main()

    out = capsys.readouterr().out
    assert "Configured IntelliJ ACP" in out
    assert "Saved Brokk API key" not in out


def test_install_mcp_with_missing_key_piped_does_not_read_key(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    """Verify that 'install mcp' ignores stdin auth data."""
    monkeypatch.delenv("BROKK_API_KEY", raising=False)

    monkeypatch.setattr(sys, "stdin", StringIO("mcp-piped-key\n"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    _stub_install_warmup(monkeypatch, stub_api_key=False)
    monkeypatch.setattr(
        main_module,
        "configure_claude_code_mcp_settings",
        lambda **k: tmp_path / "c",
    )
    monkeypatch.setattr(
        main_module,
        "configure_codex_mcp_settings",
        lambda **k: tmp_path / "x",
    )
    monkeypatch.setattr(
        main_module,
        "install_codex_mcp_workspace_skill",
        lambda **_kw: tmp_path / "s1",
    )
    monkeypatch.setattr(
        main_module,
        "install_codex_mcp_summaries_skill",
        lambda **_kw: tmp_path / "s2",
    )
    monkeypatch.setattr(
        main_module,
        "install_claude_mcp_workspace_skill",
        lambda **_kw: tmp_path / "s3",
    )
    monkeypatch.setattr(
        main_module,
        "install_claude_mcp_summaries_skill",
        lambda **_kw: tmp_path / "s4",
    )

    monkeypatch.setattr(sys, "argv", ["brokk", "install", "mcp"])

    main_module.main()

    out = capsys.readouterr().out
    assert "Configured Claude Code MCP" in out
    assert "Configured Codex MCP" in out
    assert "Saved Brokk API key" not in out


def test_install_continues_with_empty_stdin(monkeypatch, tmp_path, capsys) -> None:
    """Verify that install does not require auth data on stdin."""
    monkeypatch.delenv("BROKK_API_KEY", raising=False)

    monkeypatch.setattr(sys, "stdin", StringIO(""))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    monkeypatch.setattr(sys, "argv", ["brokk", "install", "zed"])
    _stub_install_warmup(monkeypatch, stub_api_key=False)
    monkeypatch.setattr(
        main_module,
        "configure_zed_acp_settings",
        lambda *, force=False, settings_path=None, uvx_command=None, **_kw: tmp_path / "zed.json",
    )

    main_module.main()

    assert "Configured Zed ACP" in capsys.readouterr().out


def test_install_continues_when_key_already_configured(monkeypatch, tmp_path, capsys) -> None:
    """Verify that install ignores auth state and only writes config."""
    # Set key in env
    monkeypatch.setenv("BROKK_API_KEY", "existing-env-key")

    _stub_install_warmup(monkeypatch)
    monkeypatch.setattr(main_module, "ensure_uv_ready", lambda: "/usr/bin/uv")
    monkeypatch.setattr(
        main_module,
        "configure_zed_acp_settings",
        lambda *, force=False, settings_path=None, uvx_command=None, **_kw: tmp_path / "z",
    )
    monkeypatch.setattr(sys, "argv", ["brokk", "install", "zed"])

    # Should not raise SystemExit
    main_module.main()

    out = capsys.readouterr().out
    assert "Configured Zed ACP" in out
    assert "Saved Brokk API key" not in out
