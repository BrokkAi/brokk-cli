"""Persisted model settings for Anvil-backed scripting commands."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, TextIO

from brokk_code.anvil_launcher import BUNDLED_ANVIL_VERSION
from brokk_code.headless_anvil import (
    ANVIL_MODEL_CONFIG_ID,
    ANVIL_REASONING_EFFORT_CONFIG_ID,
    HeadlessAcpClient,
    HeadlessAnvilError,
)
from brokk_code.settings import get_global_config_dir

CONFIG_VERSION = 1
CONFIG_FILENAME = "anvil-scripting.json"

SCRIPTING_TOOLS: tuple[str, ...] = (
    "exec",
    "commit",
    "issue_create",
    "issue_diagnose",
    "issue_solve",
    "pr_create",
    "pr_review",
)

SCRIPTING_TOOL_LABELS: dict[str, str] = {
    "exec": "brokk exec",
    "commit": "brokk commit",
    "issue_create": "brokk issue create",
    "issue_diagnose": "brokk issue diagnose",
    "issue_solve": "brokk issue solve",
    "pr_create": "brokk pr create",
    "pr_review": "brokk pr review",
}


@dataclass
class AnvilToolSelection:
    model: str | None = None
    reasoning_effort: str | None = None

    @classmethod
    def from_dict(cls, data: object) -> "AnvilToolSelection":
        if not isinstance(data, dict):
            return cls()
        return cls(
            model=_clean_optional_string(data.get("model")),
            reasoning_effort=_clean_optional_string(data.get("reasoning_effort")),
        )

    def is_empty(self) -> bool:
        return self.model is None and self.reasoning_effort is None


@dataclass
class AnvilOptionChoice:
    value: str
    name: str
    description: str | None = None


@dataclass
class AnvilOptionCatalog:
    model_options: list[AnvilOptionChoice] = field(default_factory=list)
    reasoning_options: list[AnvilOptionChoice] = field(default_factory=list)
    current_model: str | None = None
    current_reasoning_effort: str | None = None


@dataclass
class AnvilScriptingConfig:
    version: int = CONFIG_VERSION
    use_global: bool = True
    global_selection: AnvilToolSelection = field(default_factory=AnvilToolSelection)
    tool_selections: dict[str, AnvilToolSelection] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> "AnvilScriptingConfig | None":
        config_path = path or anvil_scripting_config_file()
        if not config_path.exists():
            return None
        try:
            with config_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        selections = data.get("tool_selections", {})
        parsed_selections = {
            key: AnvilToolSelection.from_dict(value)
            for key, value in selections.items()
            if key in SCRIPTING_TOOLS
        }
        return cls(
            version=CONFIG_VERSION,
            use_global=bool(data.get("use_global", True)),
            global_selection=AnvilToolSelection.from_dict(data.get("global_selection")),
            tool_selections=parsed_selections,
        )

    def save(self, path: Path | None = None) -> Path:
        config_path = path or anvil_scripting_config_file()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = config_path.with_suffix(".tmp")
        with temp_path.open("w", encoding="utf-8") as f:
            json.dump(self.to_json_dict(), f, indent=2)
            f.write("\n")
        temp_path.replace(config_path)
        return config_path

    def to_json_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["version"] = CONFIG_VERSION
        if self.use_global:
            data["tool_selections"] = {}
        return data

    def selection_for(self, tool_key: str) -> AnvilToolSelection:
        if self.use_global:
            return self.global_selection
        return self.tool_selections.get(tool_key, AnvilToolSelection())


def anvil_scripting_config_file() -> Path:
    return get_global_config_dir() / CONFIG_FILENAME


def resolve_anvil_selection(
    *,
    tool_key: str,
    model_override: str | None = None,
    reasoning_override: str | None = None,
    workspace_dir: Path | None = None,
    anvil_binary: Path | None = None,
    anvil_version: str = BUNDLED_ANVIL_VERSION,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
) -> AnvilToolSelection:
    """Return the effective model/reasoning selection for a scripting command."""
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    config = AnvilScriptingConfig.load()
    if config is None and input_stream.isatty():
        config = configure_anvil_scripting_interactive(
            workspace_dir=workspace_dir,
            anvil_binary=anvil_binary,
            anvil_version=anvil_version,
            input_stream=input_stream,
            output_stream=output_stream,
        )

    configured = config.selection_for(tool_key) if config else AnvilToolSelection()
    return AnvilToolSelection(
        model=model_override if model_override is not None else configured.model,
        reasoning_effort=(
            reasoning_override if reasoning_override is not None else configured.reasoning_effort
        ),
    )


def configure_anvil_scripting_interactive(
    *,
    workspace_dir: Path | None = None,
    anvil_binary: Path | None = None,
    anvil_version: str = BUNDLED_ANVIL_VERSION,
    catalog: AnvilOptionCatalog | None = None,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
) -> AnvilScriptingConfig:
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    if catalog is None:
        catalog = query_anvil_option_catalog(
            workspace_dir=workspace_dir,
            anvil_binary=anvil_binary,
            anvil_version=anvil_version,
            output_stream=output_stream,
        )

    print("Anvil scripting configuration", file=output_stream)
    if catalog.model_options:
        print("Models and reasoning choices were read from Anvil ACP.", file=output_stream)
    else:
        print("No model picker came back from Anvil; model input is manual.", file=output_stream)
    if not catalog.reasoning_options:
        print(
            "No reasoning picker came back from Anvil for the active model.",
            file=output_stream,
        )
    print("", file=output_stream)

    use_global = _ask_yes_no(
        "Use one model and reasoning effort for all scripting commands?",
        default=True,
        input_stream=input_stream,
        output_stream=output_stream,
    )

    if use_global:
        config = AnvilScriptingConfig(
            use_global=True,
            global_selection=_ask_selection(
                "All scripting commands",
                catalog=catalog,
                input_stream=input_stream,
                output_stream=output_stream,
            ),
        )
    else:
        selections = {
            tool_key: _ask_selection(
                SCRIPTING_TOOL_LABELS[tool_key],
                catalog=catalog,
                input_stream=input_stream,
                output_stream=output_stream,
            )
            for tool_key in SCRIPTING_TOOLS
        }
        config = AnvilScriptingConfig(use_global=False, tool_selections=selections)

    path = config.save()
    print(f"\nSaved Anvil scripting configuration to {path}", file=output_stream)
    return config


def query_anvil_option_catalog(
    *,
    workspace_dir: Path | None = None,
    anvil_binary: Path | None = None,
    anvil_version: str = BUNDLED_ANVIL_VERSION,
    output_stream: TextIO | None = None,
) -> AnvilOptionCatalog:
    workspace = workspace_dir or Path.cwd()
    try:
        return asyncio.run(
            _query_anvil_option_catalog(
                workspace_dir=workspace,
                anvil_binary=anvil_binary,
                anvil_version=anvil_version,
            )
        )
    except (HeadlessAnvilError, OSError, RuntimeError) as exc:
        if output_stream is not None:
            print(f"Warning: could not query Anvil config options: {exc}", file=output_stream)
        return AnvilOptionCatalog()


async def _query_anvil_option_catalog(
    *,
    workspace_dir: Path,
    anvil_binary: Path | None,
    anvil_version: str,
) -> AnvilOptionCatalog:
    client = HeadlessAcpClient(
        workspace_dir=workspace_dir,
        anvil_binary=anvil_binary,
        anvil_version=anvil_version,
    )
    try:
        await client.start()
        return _catalog_from_config_options(client.config_options)
    finally:
        await client.stop()


def format_anvil_scripting_config(config: AnvilScriptingConfig | None = None) -> str:
    config = config or AnvilScriptingConfig.load()
    if config is None:
        return "No Anvil scripting configuration found."
    lines = [f"Anvil scripting configuration v{config.version}"]
    if config.use_global:
        lines.append("Mode: one selection for all scripting commands")
        lines.append(f"All: {_format_selection(config.global_selection)}")
    else:
        lines.append("Mode: per-command selections")
        for tool_key in SCRIPTING_TOOLS:
            selection = config.selection_for(tool_key)
            lines.append(f"{SCRIPTING_TOOL_LABELS[tool_key]}: {_format_selection(selection)}")
    return "\n".join(lines)


def delete_anvil_scripting_config(path: Path | None = None) -> bool:
    config_path = path or anvil_scripting_config_file()
    if not config_path.exists():
        return False
    config_path.unlink()
    return True


def _ask_selection(
    label: str,
    *,
    catalog: AnvilOptionCatalog,
    input_stream: TextIO,
    output_stream: TextIO,
) -> AnvilToolSelection:
    print(f"\n{label}", file=output_stream)
    model = _ask_model(
        catalog=catalog,
        input_stream=input_stream,
        output_stream=output_stream,
    )
    reasoning = _ask_reasoning_effort(
        catalog=catalog,
        input_stream=input_stream,
        output_stream=output_stream,
    )
    return AnvilToolSelection(model=model, reasoning_effort=reasoning)


def _ask_model(
    *,
    catalog: AnvilOptionCatalog,
    input_stream: TextIO,
    output_stream: TextIO,
) -> str | None:
    if not catalog.model_options:
        return _ask_text(
            "Model id (blank for Anvil default)",
            default=None,
            input_stream=input_stream,
            output_stream=output_stream,
        )
    choices = [
        AnvilOptionChoice("", "Anvil default"),
        *catalog.model_options,
        AnvilOptionChoice("__custom__", "Custom model id"),
    ]
    value = _ask_choice(
        "Model",
        choices=choices,
        default_value=catalog.current_model or "",
        input_stream=input_stream,
        output_stream=output_stream,
    )
    if value == "__custom__":
        return _ask_text(
            "Custom model id",
            default=None,
            input_stream=input_stream,
            output_stream=output_stream,
        )
    return value or None


def _ask_text(
    prompt: str,
    *,
    default: str | None,
    input_stream: TextIO,
    output_stream: TextIO,
) -> str | None:
    suffix = f" [{default}]" if default else ""
    print(f"{prompt}{suffix}: ", end="", file=output_stream, flush=True)
    value = input_stream.readline()
    if value == "":
        return default
    value = value.strip()
    return value or default


def _ask_yes_no(
    prompt: str,
    *,
    default: bool,
    input_stream: TextIO,
    output_stream: TextIO,
) -> bool:
    marker = "Y/n" if default else "y/N"
    while True:
        print(f"{prompt} [{marker}]: ", end="", file=output_stream, flush=True)
        answer = input_stream.readline()
        if answer == "":
            return default
        normalized = answer.strip().lower()
        if not normalized:
            return default
        if normalized in {"y", "yes"}:
            return True
        if normalized in {"n", "no"}:
            return False
        print("Please answer yes or no.", file=output_stream)


def _ask_reasoning_effort(
    *,
    catalog: AnvilOptionCatalog,
    input_stream: TextIO,
    output_stream: TextIO,
) -> str | None:
    if not catalog.reasoning_options:
        return _ask_text(
            "Reasoning effort (blank for model default)",
            default=None,
            input_stream=input_stream,
            output_stream=output_stream,
        )
    choices = [
        AnvilOptionChoice("", "Model default"),
        *catalog.reasoning_options,
        AnvilOptionChoice("__custom__", "Custom reasoning effort"),
    ]
    value = _ask_choice(
        "Reasoning effort",
        choices=choices,
        default_value=catalog.current_reasoning_effort or "",
        input_stream=input_stream,
        output_stream=output_stream,
    )
    if value == "__custom__":
        return _ask_text(
            "Custom reasoning effort",
            default=None,
            input_stream=input_stream,
            output_stream=output_stream,
        )
    return value or None


def _ask_choice(
    label: str,
    *,
    choices: list[AnvilOptionChoice],
    default_value: str,
    input_stream: TextIO,
    output_stream: TextIO,
) -> str:
    default_index = _choice_default_index(choices, default_value)
    while True:
        print(f"{label}:", file=output_stream)
        for index, choice in enumerate(choices, start=1):
            marker = " (current)" if index == default_index else ""
            print(f"  {index}. {choice.name}{marker}", file=output_stream)
        print(f"Selection [{default_index}]: ", end="", file=output_stream, flush=True)
        answer = input_stream.readline()
        if answer == "":
            return choices[default_index - 1].value
        value = answer.strip()
        if not value:
            return choices[default_index - 1].value
        if value.isdigit() and 1 <= int(value) <= len(choices):
            return choices[int(value) - 1].value
        for choice in choices:
            if value == choice.value:
                return choice.value
        print("Please choose a listed option.", file=output_stream)


def _format_selection(selection: AnvilToolSelection) -> str:
    model = selection.model or "Anvil default"
    reasoning = selection.reasoning_effort or "model default"
    return f"model={model}, reasoning_effort={reasoning}"


def _clean_optional_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _catalog_from_config_options(config_options: list[Any]) -> AnvilOptionCatalog:
    model_option = _find_select_config_option(config_options, ANVIL_MODEL_CONFIG_ID)
    reasoning_option = _find_select_config_option(
        config_options,
        ANVIL_REASONING_EFFORT_CONFIG_ID,
    )
    return AnvilOptionCatalog(
        model_options=_choices_from_select_option(model_option),
        reasoning_options=_choices_from_select_option(reasoning_option),
        current_model=_clean_optional_string(getattr(model_option, "current_value", None)),
        current_reasoning_effort=_normalize_reasoning_default(
            getattr(reasoning_option, "current_value", None)
        ),
    )


def _find_select_config_option(config_options: list[Any], config_id: str) -> Any | None:
    for option in config_options:
        if getattr(option, "id", None) == config_id and getattr(option, "type", None) == "select":
            return option
    return None


def _choices_from_select_option(option: Any | None) -> list[AnvilOptionChoice]:
    if option is None:
        return []
    choices: list[AnvilOptionChoice] = []
    for raw in getattr(option, "options", []) or []:
        value = getattr(raw, "value", None)
        name = getattr(raw, "name", None)
        if not isinstance(value, str) or not isinstance(name, str):
            continue
        choices.append(
            AnvilOptionChoice(
                value=value,
                name=name,
                description=getattr(raw, "description", None),
            )
        )
    return choices


def _normalize_reasoning_default(value: object) -> str | None:
    cleaned = _clean_optional_string(value)
    if cleaned == "(default)":
        return None
    return cleaned


def _choice_default_index(choices: list[AnvilOptionChoice], default_value: str) -> int:
    for index, choice in enumerate(choices, start=1):
        if choice.value == default_value:
            return index
    return 1
