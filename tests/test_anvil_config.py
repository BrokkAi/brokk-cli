from io import StringIO

from brokk_code.anvil_config import (
    AnvilScriptingConfig,
    AnvilToolSelection,
    configure_anvil_scripting_interactive,
    delete_anvil_scripting_config,
    format_anvil_scripting_config,
    resolve_anvil_selection,
)


def test_anvil_scripting_config_roundtrip() -> None:
    config = AnvilScriptingConfig(
        use_global=False,
        tool_selections={
            "commit": AnvilToolSelection(model="codex::gpt-5.2", reasoning_effort="medium"),
            "pr_create": AnvilToolSelection(model="openrouter::model", reasoning_effort="high"),
        },
    )

    path = config.save()
    loaded = AnvilScriptingConfig.load(path)

    assert loaded is not None
    assert loaded.use_global is False
    assert loaded.selection_for("commit").model == "codex::gpt-5.2"
    assert loaded.selection_for("commit").reasoning_effort == "medium"
    assert loaded.selection_for("pr_create").reasoning_effort == "high"


def test_resolve_anvil_selection_uses_config_and_overrides() -> None:
    AnvilScriptingConfig(
        use_global=True,
        global_selection=AnvilToolSelection(
            model="configured-model",
            reasoning_effort="medium",
        ),
    ).save()

    selection = resolve_anvil_selection(tool_key="commit")
    overridden = resolve_anvil_selection(
        tool_key="commit",
        model_override="flag-model",
        reasoning_override="high",
    )

    assert selection.model == "configured-model"
    assert selection.reasoning_effort == "medium"
    assert overridden.model == "flag-model"
    assert overridden.reasoning_effort == "high"


def test_resolve_anvil_selection_without_config_non_tty_returns_empty() -> None:
    input_stream = StringIO("")

    selection = resolve_anvil_selection(
        tool_key="commit",
        input_stream=input_stream,
        output_stream=StringIO(),
    )

    assert selection.model is None
    assert selection.reasoning_effort is None


def test_configure_anvil_scripting_interactive_global() -> None:
    input_stream = StringIO("y\ncodex::gpt-5.2\n3\n")
    output_stream = StringIO()

    config = configure_anvil_scripting_interactive(
        input_stream=input_stream,
        output_stream=output_stream,
    )

    assert config.use_global is True
    assert config.global_selection.model == "codex::gpt-5.2"
    assert config.global_selection.reasoning_effort == "medium"
    assert "Saved Anvil scripting configuration" in output_stream.getvalue()


def test_format_and_delete_anvil_scripting_config() -> None:
    AnvilScriptingConfig(
        use_global=True,
        global_selection=AnvilToolSelection(model="m", reasoning_effort="low"),
    ).save()

    output = format_anvil_scripting_config()
    deleted = delete_anvil_scripting_config()
    missing = delete_anvil_scripting_config()

    assert "model=m" in output
    assert "reasoning_effort=low" in output
    assert deleted is True
    assert missing is False
