"""A name -> Stage registry. Duplicate names fail here; topology is the resolver's job."""

from typing import Protocol, cast

from fdi.pipeline.stage import Stage, StageContext


class _Registrable(Protocol):
    """The 4 members every stage module actually defines at class-definition
    time -- `Stage` minus `key_material`. `key_material` has a default
    implementation on `Stage` (see stage.py), but mypy's structural check
    still requires it on any type checked AGAINST `Stage` directly, which
    would force all ~20 existing stage classes to declare it for no
    behavioural benefit. Registering through this narrower protocol instead
    keeps that requirement off every stage module; `run_stage` still calls
    `stage.key_material(...)` through a `contextlib.suppress(Exception)`
    guard, which is exactly what covers a stage that has none."""

    name: str
    version: int
    deps: tuple[str, ...]

    async def run(self, ctx: StageContext) -> object: ...


class StageRegistry:
    def __init__(self) -> None:
        self._stages: dict[str, Stage] = {}

    def register(self, stage: _Registrable) -> None:
        if stage.name in self._stages:
            raise ValueError(f"stage {stage.name!r} already registered")
        self._stages[stage.name] = cast(Stage, stage)

    def get(self, name: str) -> Stage:
        return self._stages[name]

    def all(self) -> dict[str, Stage]:
        return dict(self._stages)


REGISTRY = StageRegistry()
