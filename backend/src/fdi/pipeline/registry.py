"""A name -> Stage registry. Duplicate names fail here; topology is the resolver's job."""

from fdi.pipeline.stage import Stage


class StageRegistry:
    def __init__(self) -> None:
        self._stages: dict[str, Stage] = {}

    def register(self, stage: Stage) -> None:
        if stage.name in self._stages:
            raise ValueError(f"stage {stage.name!r} already registered")
        self._stages[stage.name] = stage

    def get(self, name: str) -> Stage:
        return self._stages[name]

    def all(self) -> dict[str, Stage]:
        return dict(self._stages)


REGISTRY = StageRegistry()
