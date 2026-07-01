from brokk_code import __main__ as brokk_main


def test_passthrough_parser_consumes_global_bifrost_version() -> None:
    parsed = brokk_main._passthrough_command_from_argv(
        ["--bifrost-version", "0.7.2", "mcp", "--server", "searchtools"]
    )

    assert parsed is not None
    args, passthrough_args = parsed
    assert args.command == "mcp"
    assert args.bifrost_version == "0.7.2"
    assert passthrough_args == ["--server", "searchtools"]


def test_passthrough_parser_consumes_equals_form_global_bifrost_version() -> None:
    parsed = brokk_main._passthrough_command_from_argv(
        ["--bifrost-version=0.7.2", "mcp", "--server", "searchtools"]
    )

    assert parsed is not None
    args, passthrough_args = parsed
    assert args.command == "mcp"
    assert args.bifrost_version == "0.7.2"
    assert passthrough_args == ["--server", "searchtools"]


def test_passthrough_parser_leaves_subcommand_bifrost_version_as_passthrough() -> None:
    parsed = brokk_main._passthrough_command_from_argv(
        ["mcp", "--bifrost-version", "0.7.2", "--server", "searchtools"]
    )

    assert parsed is not None
    args, passthrough_args = parsed
    assert args.command == "mcp"
    assert args.bifrost_version is None
    assert passthrough_args == ["--bifrost-version", "0.7.2", "--server", "searchtools"]


def test_passthrough_parser_consumes_global_anvil_runtime_options(tmp_path) -> None:
    binary = tmp_path / "anvil"
    parsed = brokk_main._passthrough_command_from_argv(
        [
            "--anvil-binary",
            binary.as_posix(),
            "--anvil-version=0.17.0",
            "acp",
            "--default-model",
            "claude-haiku-4-5",
        ]
    )

    assert parsed is not None
    args, passthrough_args = parsed
    assert args.command == "acp"
    assert args.anvil_binary == binary
    assert args.anvil_version == "0.17.0"
    assert passthrough_args == ["--default-model", "claude-haiku-4-5"]


def test_main_dispatch_passes_bifrost_version_to_mcp(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    def fake_run_bifrost_server(**kwargs: object) -> None:
        captured.update(kwargs)

    import brokk_code.bifrost_launcher as bifrost_launcher

    monkeypatch.setattr(bifrost_launcher, "run_bifrost_server", fake_run_bifrost_server)

    args = brokk_main.argparse.Namespace(command="mcp", bifrost_version="0.7.2")
    brokk_main._main_dispatch(args, tmp_path, ["--server", "searchtools"])

    assert captured == {
        "workspace_dir": tmp_path,
        "version": "0.7.2",
        "passthrough_args": ["--server", "searchtools"],
    }


def test_main_dispatch_passes_anvil_globals_to_acp(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}
    binary = tmp_path / "anvil"

    def fake_run_anvil_acp_server(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(brokk_main, "run_anvil_acp_server", fake_run_anvil_acp_server)

    args = brokk_main.argparse.Namespace(
        command="acp",
        anvil_binary=binary,
        anvil_version="0.17.0",
    )
    brokk_main._main_dispatch(args, tmp_path, ["--default-model", "claude-haiku-4-5"])

    assert captured == {
        "workspace_dir": tmp_path,
        "binary_override": binary,
        "version": "0.17.0",
        "passthrough_args": ["--default-model", "claude-haiku-4-5"],
    }
