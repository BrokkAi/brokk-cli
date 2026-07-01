import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ORIGIN_URL = "https://github.com/BrokkAi/brokk-cli"
FOUNDEV_AUTHOR = {
    "name": "foundev",
    "email": "86598053+foundev@users.noreply.github.com",
}


def _project_metadata() -> dict[str, object]:
    with (ROOT / "pyproject.toml").open("rb") as pyproject_file:
        return tomllib.load(pyproject_file)["project"]


def test_package_metadata_populates_pypi_project_page() -> None:
    project = _project_metadata()

    assert project["readme"] == "README.md"
    assert (ROOT / project["readme"]).is_file()
    assert project["license"] == "GPL-3.0-only"
    assert project["license-files"] == ["LICENSE"]
    assert (ROOT / "LICENSE").is_file()
    assert project["authors"] == [FOUNDEV_AUTHOR]
    assert project["maintainers"] == [FOUNDEV_AUTHOR]
    assert project["urls"] == {
        "Homepage": ORIGIN_URL,
        "Repository": ORIGIN_URL,
        "Issues": f"{ORIGIN_URL}/issues",
        "Documentation": ORIGIN_URL,
    }

    keywords = project["keywords"]
    assert "brokk" in keywords
    assert "coding-agent" in keywords
    assert "model-context-protocol" in keywords

    classifiers = project["classifiers"]
    assert "Development Status :: 5 - Production/Stable" in classifiers
    assert "Intended Audience :: Developers" in classifiers
    # The license is declared via the SPDX ``license`` expression (asserted
    # above), so no ``License ::`` Trove classifier may be present: PEP 639
    # forbids pairing a License-Expression with license classifiers and PyPI
    # rejects such uploads.
    assert not any(c.startswith("License ::") for c in classifiers)
    assert "Programming Language :: Python :: 3.11" in classifiers
    assert "Programming Language :: Python :: 3.12" in classifiers
    assert "Programming Language :: Python :: 3.13" in classifiers


def test_package_license_file_contains_gplv3_text() -> None:
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")

    assert "GNU GENERAL PUBLIC LICENSE" in license_text
    assert "Version 3, 29 June 2007" in license_text
    assert "https://www.gnu.org/licenses/" in license_text
