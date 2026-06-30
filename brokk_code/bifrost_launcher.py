"""Launch the bifrost MCP server as a subcommand of `brokk`."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from brokk_code.rust_acp_install import (
    BifrostInstallError,
    RustAcpInstallError,
    resolve_bifrost_binary,
)
from brokk_code.workspace import resolve_workspace_dir


def run_bifrost_server(
    *,
    workspace_dir: Path,
    version: str | None = None,
    passthrough_args: list[str] | None = None,
) -> None:
    """Resolve the bifrost binary and exec it with full CLI passthrough.

    Replaces the Python process via os.execvpe on Unix; on Windows runs as a
    child since execvpe loses stdout. Brokk changes into the resolved workspace
    first, then forwards all extra CLI args directly to bifrost unchanged.
    """
    resolved_workspace_dir = resolve_workspace_dir(workspace_dir)

    try:
        bifrost_bin = resolve_bifrost_binary(
            version=version,
            override=None,
            prefer_local=version is None,
        )
    except (RustAcpInstallError, BifrostInstallError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    command = [str(bifrost_bin)]
    if passthrough_args:
        command.extend(passthrough_args)

    os.chdir(resolved_workspace_dir)
    try:
        if sys.platform == "win32":
            result = subprocess.run(command, env=os.environ.copy())
            sys.exit(result.returncode)
        else:
            os.execvpe(str(bifrost_bin), command, os.environ.copy())
    except FileNotFoundError:
        print(
            f"Error: bifrost binary not found at {bifrost_bin}.",
            file=sys.stderr,
        )
        sys.exit(1)
    except OSError as exc:
        print(f"Error: Failed to launch bifrost: {exc}", file=sys.stderr)
        sys.exit(1)
