"""Persisted model settings for Anvil-backed scripting commands."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TextIO

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

REASONING_CHOICES: tuple[str, ...] = ("low", "medium", "high", "xhigh")


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
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
) -> AnvilToolSelection:
    """Return the effective model/reasoning selection for a scripting command."""
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    config = AnvilScriptingConfig.load()
    if config is None and input_stream.isatty():
        config = configure_anvil_scripting_interactive(
            input_stream=input_stream,
            output_stream=output_stream,
        )

    configured = config.selection_for(tool_key) if config else AnvilToolSelection()
    return AnvilToolSelection(
        model=model_override if model_override is not None else configured.model,
        reasoning_effort=(
            reasoning_override
            if reasoning_override is not None
            else configured.reasoning_effort
        ),
    )


def configure_anvil_scripting_interactive(
    *,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
) -> AnvilScriptingConfig:
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout

    print("Anvil scripting configuration", file=output_stream)
    print("Leave model blank to use Anvil's discovered default.", file=output_stream)
    print("Leave reasoning blank to use the model default.", file=output_stream)
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
                input_stream=input_stream,
                output_stream=output_stream,
            ),
        )
    else:
        selections = {
            tool_key: _ask_selection(
                SCRIPTING_TOOL_LABELS[tool_key],
                input_stream=input_stream,
                output_stream=output_stream,
            )
            for tool_key in SCRIPTING_TOOLS
        }
        config = AnvilScriptingConfig(use_global=False, tool_selections=selections)

    path = config.save()
    print(f"\nSaved Anvil scripting configuration to {path}", file=output_stream)
    return config


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
    input_stream: TextIO,
    output_stream: TextIO,
) -> AnvilToolSelection:
    print(f"\n{label}", file=output_stream)
    model = _ask_text(
        "Model id",
        default=None,
        input_stream=input_stream,
        output_stream=output_stream,
    )
    reasoning = _ask_reasoning_effort(input_stream=input_stream, output_stream=output_stream)
    return AnvilToolSelection(model=model, reasoning_effort=reasoning)


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


def _ask_reasoning_effort(*, input_stream: TextIO, output_stream: TextIO) -> str | None:
    while True:
        print("Reasoning effort:", file=output_stream)
        print("  1. Anvil/model default", file=output_stream)
        for index, value in enumerate(REASONING_CHOICES, start=2):
            print(f"  {index}. {value}", file=output_stream)
        print("  6. Custom", file=output_stream)
        print("Selection [1]: ", end="", file=output_stream, flush=True)
        answer = input_stream.readline()
        if answer == "":
            return None
        value = answer.strip()
        if not value or value == "1":
            return None
        if value in REASONING_CHOICES:
            return value
        if value in {"2", "3", "4", "5"}:
            return REASONING_CHOICES[int(value) - 2]
        if value == "6":
            return _ask_text(
                "Custom reasoning effort",
                default=None,
                input_stream=input_stream,
                output_stream=output_stream,
            )
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
