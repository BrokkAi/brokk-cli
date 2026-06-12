import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import brokk_code.__main__ as main_module


def test_main_version_subcommand_prints_version(monkeypatch, capsys) -> None:
    from brokk_code import __version__

    monkeypatch.setattr(sys, "argv", ["brokk", "version"])

    main_module.main()

    assert f"brokk {__version__}" in capsys.readouterr().out


def test_main_without_command_prints_help(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["brokk"])

    main_module.main()

    output = capsys.readouterr().out
    assert "usage: brokk" in output
    assert "acp" in output
    assert "mcp" in output
    assert "vk" in output


def test_main_acp_routes_to_anvil_launcher(monkeypatch, tmp_path) -> None:
    captured: dict[str, Any] = {}

    def fake_run_anvil_acp_server(**kwargs: Any) -> None:
        captured["kwargs"] = kwargs

    monkeypatch.setattr(main_module, "run_anvil_acp_server", fake_run_anvil_acp_server)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brokk",
            "acp",
            "--default-model",
            "gpt-5",
            "--bifrost-binary",
            "/opt/bifrost",
        ],
    )

    main_module.main()

    assert captured["kwargs"]["workspace_dir"] == tmp_path.resolve()
    assert captured["kwargs"]["passthrough_args"] == [
        "--default-model",
        "gpt-5",
        "--bifrost-binary",
        "/opt/bifrost",
    ]


def test_main_acp_does_not_consume_root_position_runtime_flags(monkeypatch, tmp_path) -> None:
    captured: dict[str, Any] = {}

    def fake_run_anvil_acp_server(**kwargs: Any) -> None:
        captured["kwargs"] = kwargs

    monkeypatch.setattr(main_module, "run_anvil_acp_server", fake_run_anvil_acp_server)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brokk",
            "--worktree",
            "--anvil-binary",
            "anvil-bin",
            "--anvil-version=9.9.9",
            "acp",
            "--default-model",
            "gpt-5",
        ],
    )

    main_module.main()

    assert captured["kwargs"]["workspace_dir"] == tmp_path.resolve()
    assert captured["kwargs"]["passthrough_args"] == [
        "--worktree",
        "--anvil-binary",
        "anvil-bin",
        "--anvil-version=9.9.9",
        "--default-model",
        "gpt-5",
    ]


def test_main_mcp_routes_to_bifrost_launcher(monkeypatch, tmp_path) -> None:
    captured: dict[str, Any] = {}

    def fake_run_bifrost_server(**kwargs: Any) -> None:
        captured["kwargs"] = kwargs

    import brokk_code.bifrost_launcher as bifrost_launcher

    monkeypatch.setattr(bifrost_launcher, "run_bifrost_server", fake_run_bifrost_server)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["brokk", "mcp", "--server", "searchtools"])

    main_module.main()

    assert captured["kwargs"]["workspace_dir"] == tmp_path.resolve()
    assert captured["kwargs"]["passthrough_args"] == ["--server", "searchtools"]


def test_main_vk_passthrough_forwards_args(monkeypatch, tmp_path) -> None:
    captured: dict[str, Any] = {}

    def fake_run_valkyrie(args: list[str], **kwargs: Any) -> None:
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setattr(main_module, "_run_valkyrie", fake_run_valkyrie)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["brokk", "vk", "issue", "12", "--plan"])

    main_module.main()

    assert captured["args"] == ["issue", "12", "--plan"]
    assert captured["kwargs"]["workspace_dir"] == tmp_path.resolve()


def test_main_exec_delegates_to_valkyrie_run(monkeypatch, tmp_path) -> None:
    captured: dict[str, Any] = {}

    def fake_run_valkyrie(args: list[str], **kwargs: Any) -> None:
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setattr(main_module, "_run_valkyrie", fake_run_valkyrie)
    monkeypatch.setattr(main_module, "resolve_workspace_dir", lambda path: path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["brokk", "exec", "fix parser", "--verbose"])

    main_module.main()

    assert captured["args"] == ["run", "fix parser", "--verbose", "--repo", str(tmp_path)]


def test_main_issue_solve_delegates_to_valkyrie_issue(monkeypatch, tmp_path) -> None:
    captured: dict[str, Any] = {}

    def fake_run_valkyrie(args: list[str], **_kwargs: Any) -> None:
        captured["args"] = args

    monkeypatch.setattr(main_module, "_run_valkyrie", fake_run_valkyrie)
    monkeypatch.setattr(main_module, "resolve_workspace_dir", lambda path: path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        ["brokk", "issue", "solve", "--issue-number", "42", "--skip-verification"],
    )

    main_module.main()

    assert captured["args"] == [
        "issue",
        "42",
        "--write",
        "--skip-validation",
        "--repo",
        str(tmp_path),
    ]


def test_main_pr_review_url_delegates_to_valkyrie_pr_plan(monkeypatch, tmp_path) -> None:
    captured: dict[str, Any] = {}

    def fake_run_valkyrie(args: list[str], **_kwargs: Any) -> None:
        captured["args"] = args

    monkeypatch.setattr(main_module, "_run_valkyrie", fake_run_valkyrie)
    monkeypatch.setattr(main_module, "resolve_workspace_dir", lambda path: path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        ["brokk", "pr", "review", "https://github.com/BrokkAi/valkyrie/pull/17"],
    )

    main_module.main()

    assert captured["args"] == ["pr", "17", "--plan", "--repo", str(tmp_path)]


def test_resolve_valkyrie_command_prefers_override(tmp_path) -> None:
    binary = tmp_path / "vk"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")

    assert main_module._resolve_valkyrie_command(binary) == [str(binary)]


def test_run_valkyrie_exits_with_child_status(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        main_module,
        "_resolve_valkyrie_command",
        lambda _override=None: ["vk"],
    )

    def fake_run(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(SystemExit) as exc:
        main_module._run_valkyrie(["doctor"], workspace_dir=tmp_path)

    assert exc.value.code == 7


def test_install_zed_uses_uvx(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "ensure_uv_ready", lambda: "/usr/local/bin/uv")
    monkeypatch.setattr(
        main_module,
        "configure_zed_acp_settings",
        lambda **kwargs: Path("/tmp/zed-settings.json"),
    )
    args = SimpleNamespace(
        command="install",
        target="zed",
        plugin=None,
        native=False,
        rust=False,
        brokk_acp_binary=None,
        force=False,
        verbose=False,
        model=None,
        endpoint_url=None,
        api_key="",
    )

    main_module._run_install(args)
