"""Importing this package registers every built-in stage."""

from fdi.pipeline.stages import ocr as ocr  # noqa: F401 - import for its registration side effect
from fdi.pipeline.stages import ping as ping  # noqa: F401 - import for its registration side effect
