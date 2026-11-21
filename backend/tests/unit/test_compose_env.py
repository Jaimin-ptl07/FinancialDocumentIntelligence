"""Env-name drift between Settings, the compose allowlist and .env.example.

docker/compose.yml's x-backend-env is the ONLY path a variable takes from the
operator's .env into the api/worker/migrate containers (compose.prod.yml
extends those services, so it inherits the same list). Its header names the
trap: a Settings field missing from the allowlist is inert -- set in .env,
never delivered, silently defaulted -- and the in-container config validator
cannot see it. The reverse drift is louder but just as fatal on deploy day: an
allowlisted name that is not a Settings field is an unknown FDI_ var, which
`extra="forbid"` refuses at boot. These checks catch both on the PR instead.

Read as text, not YAML: what matters is the literal `${...}` each entry
interpolates, and compose.prod.yml's `!reset` tags are not plain YAML anyway.
"""

import re
from pathlib import Path

from fdi.platform.config import Settings

REPO = Path(__file__).resolve().parents[3]


def _read(relative: str) -> str:
    return (REPO / relative).read_text(encoding="utf-8")


# `  FDI_FOO: ${FDI_FOO}` / `  FDI_FOO: '${FDI_FOO:-default}'` inside the
# x-backend-env block: key, then the name the value interpolates.
_ENTRY = re.compile(r"^  (FDI_[A-Z0-9_]+):\s*'?\$\{(FDI_[A-Z0-9_]+)")


def _allowlist() -> dict[str, str]:
    """x-backend-env as {container var: interpolated .env var}."""
    lines = _read("docker/compose.yml").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("x-backend-env:"))
    entries: dict[str, str] = {}
    for line in lines[start + 1 :]:
        if line and not line.startswith(" "):  # next top-level key ends the block
            break
        if m := _ENTRY.match(line):
            entries[m.group(1)] = m.group(2)
    return entries


def _settings_vars() -> set[str]:
    return {f"FDI_{name.upper()}" for name in Settings.model_fields}


def test_every_settings_field_reaches_the_containers() -> None:
    missing = _settings_vars() - _allowlist().keys()
    assert not missing, f"add to docker/compose.yml x-backend-env: {sorted(missing)}"


def test_the_allowlist_names_only_settings_fields() -> None:
    unknown = _allowlist().keys() - _settings_vars()
    assert not unknown, f"not Settings fields, refused at boot: {sorted(unknown)}"


def test_each_entry_interpolates_its_own_name() -> None:
    """`FDI_A: ${FDI_B}` would deliver the operator's B under A's name."""
    crossed = {k: v for k, v in _allowlist().items() if k != v}
    assert not crossed, f"key and interpolated var differ: {crossed}"


def test_env_example_documents_every_allowlisted_var() -> None:
    """The operator builds the deploy-day .env from this file; a var it never
    mentions is one they cannot know to set. Commented-out is fine."""
    example = _read(".env.example")
    documented = set(re.findall(r"^#?\s*(FDI_[A-Z0-9_]+)=", example, re.MULTILINE))
    undocumented = _allowlist().keys() - documented
    assert not undocumented, f"add to .env.example: {sorted(undocumented)}"


def test_production_overrides_name_only_settings_fields() -> None:
    """compose.prod.yml pins topology-bound vars (x-edge-env) over the inherited
    allowlist; a typo there is an unknown FDI_ var the api refuses at boot."""
    prod = _read("docker/compose.prod.yml")
    pinned = set(re.findall(r"^\s+(FDI_[A-Z0-9_]+):", prod, re.MULTILINE))
    assert pinned, "x-edge-env not found -- has compose.prod.yml moved?"
    unknown = pinned - _settings_vars()
    assert not unknown, f"not Settings fields, refused at boot: {sorted(unknown)}"


def test_ci_publishes_the_images_production_pulls() -> None:
    """Deploy day pulls compose.prod.yml's `image:` names; CI must push those."""
    prod = _read("docker/compose.prod.yml")
    pulled = set(re.findall(r"image: (ghcr\.io/[^:\s]+):\$\{IMAGE_TAG", prod))
    published = set(re.findall(r"ref: (ghcr\.io/\S+)", _read(".github/workflows/ci.yml")))
    assert pulled and pulled == published


def test_ci_bakes_the_embedding_model_compose_builds_with() -> None:
    """The baked weights must be the model the runtime asks for, or every new
    container silently re-downloads ~130 MB."""
    compose_default = re.search(
        r"FDI_EMBED_MODEL:\s*\$\{FDI_EMBED_MODEL:-([^}]+)\}", _read("docker/compose.yml")
    )
    ci_arg = re.search(r"FDI_EMBED_MODEL=(\S+)", _read(".github/workflows/ci.yml"))
    assert compose_default and ci_arg
    runtime_default = Settings.model_fields["embed_model"].default
    assert ci_arg.group(1) == compose_default.group(1) == runtime_default
