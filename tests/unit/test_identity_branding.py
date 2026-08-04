"""H-001 identity and inherited-branding regression tests."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
UPSTREAM_NAME = "Rus" + "tam"
UPSTREAM_SURNAME = "Zoki" + "rov"
UPSTREAM_ATTRIBUTION = (
    "> This project was built upon the original repository: "
    f"[{UPSTREAM_NAME}-Z/hamroh](https://github.com/{UPSTREAM_NAME}-Z/hamroh)."
)
INHERITED_PUBLIC_NAMES = (
    ("rus" + "tam " + UPSTREAM_SURNAME.casefold()),
    ("rus" + "tamz.com"),
    ("hamroh " + "harness"),
    ("lu" + "na"),
    ("nodi" + "ra"),
    ("mir" + "zo"),
    ("dil" + "ya"),
    ("clau" + "dir"),
    ("bot" + "_xona"),
    ("self" + "-reflection"),
    ("reminder" + "-format"),
    ("render" + "-style"),
    ("house " + "style"),
    ("<this is a " + "reminder>"),
    ("self/" + "learn" + "ings.md"),
)


def _text_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file())


def test_effective_identity_names_shahnoza_and_jonibek() -> None:
    prompt = "\n".join(
        [
            (ROOT / "prompts/system.md").read_text(encoding="utf-8"),
            (ROOT / "prompts/project.md.example").read_text(encoding="utf-8"),
        ]
    ).casefold()

    assert "shahnoza" in prompt
    assert "jonibek sharipov" in prompt
    assert "developed" in prompt
    assert "customized" in prompt
    for forbidden in INHERITED_PUBLIC_NAMES:
        assert forbidden not in prompt


def test_model_loaded_surfaces_have_no_inherited_personas() -> None:
    surfaces = (ROOT / "prompts", ROOT / "skills", ROOT / "memories")
    for surface in surfaces:
        for path in _text_files(surface):
            text = path.read_text(encoding="utf-8").casefold()
            for forbidden in INHERITED_PUBLIC_NAMES:
                assert forbidden not in text, f"{forbidden!r} remains in {path}"


def test_readme_has_one_exact_upstream_attribution() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert readme.count(UPSTREAM_ATTRIBUTION) == 1
    upstream_lines = [
        line
        for line in readme.splitlines()
        if UPSTREAM_NAME.casefold() in line.casefold()
    ]
    assert upstream_lines == [UPSTREAM_ATTRIBUTION]


def test_license_preserves_upstream_notice_and_credits_modifications() -> None:
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")

    assert f"Copyright (c) 2026 {UPSTREAM_NAME} {UPSTREAM_SURNAME}" in license_text
    assert "Modifications Copyright (c) 2026 Jonibek Sharipov" in license_text
