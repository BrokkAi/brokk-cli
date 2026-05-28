from io import StringIO
from typing import Any

from acp.schema import SessionConfigOptionSelect, SessionConfigSelectOption

import brokk_code.anvil_config as anvil_config_module
from brokk_code.anvil_config import (
    AnvilOptionCatalog,
    AnvilOptionChoice,
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
    input_stream = StringIO("y\ncodex::gpt-5.2\nmedium\n")
    output_stream = StringIO()

    config = configure_anvil_scripting_interactive(
        catalog=AnvilOptionCatalog(
            model_options=[
                AnvilOptionChoice("codex::gpt-5.2", "codex::gpt-5.2"),
            ],
            reasoning_options=[
                AnvilOptionChoice("low", "low"),
                AnvilOptionChoice("medium", "medium"),
            ],
        ),
        input_stream=input_stream,
        output_stream=output_stream,
    )

    assert config.use_global is True
    assert config.global_selection.model == "codex::gpt-5.2"
    assert config.global_selection.reasoning_effort == "medium"
    assert "Saved Anvil scripting configuration" in output_stream.getvalue()


def test_query_anvil_option_catalog_reads_session_config_options(monkeypatch, tmp_path) -> None:
    class FakeHeadlessAcpClient:
        def __init__(self, **kwargs: Any) -> None:
            assert kwargs["workspace_dir"] == tmp_path
            self.config_options = [
                SessionConfigOptionSelect(
                    id="model_selection",
                    name="Model",
                    type="select",
                    currentValue="model-a",
                    options=[
                        SessionConfigSelectOption(name="Model A", value="model-a"),
                        SessionConfigSelectOption(name="Model B", value="model-b"),
                    ],
                ),
                SessionConfigOptionSelect(
                    id="reasoning_effort",
                    name="Reasoning effort",
                    type="select",
                    currentValue="medium",
                    options=[
                        SessionConfigSelectOption(name="Default", value="(default)"),
                        SessionConfigSelectOption(name="Medium", value="medium"),
                    ],
                ),
            ]

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    monkeypatch.setattr(anvil_config_module, "HeadlessAcpClient", FakeHeadlessAcpClient)

    catalog = anvil_config_module.query_anvil_option_catalog(workspace_dir=tmp_path)

    assert catalog.current_model == "model-a"
    assert [choice.value for choice in catalog.model_options] == ["model-a", "model-b"]
    assert catalog.current_reasoning_effort == "medium"
    assert [choice.value for choice in catalog.reasoning_options] == ["(default)", "medium"]


def test_query_anvil_option_catalog_falls_back_on_error(monkeypatch, tmp_path) -> None:
    class FakeHeadlessAcpClient:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def start(self) -> None:
            raise OSError("missing anvil")

        async def stop(self) -> None:
            return None

    output_stream = StringIO()
    monkeypatch.setattr(anvil_config_module, "HeadlessAcpClient", FakeHeadlessAcpClient)

    catalog = anvil_config_module.query_anvil_option_catalog(
        workspace_dir=tmp_path,
        output_stream=output_stream,
    )

    assert catalog.model_options == []
    assert "could not query Anvil config options" in output_stream.getvalue()


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
