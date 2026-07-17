"""Importing this package registers every built-in stage."""

from fdi.pipeline.stages import (
    ast as ast,  # noqa: F401 - import for its registration side effect
)
from fdi.pipeline.stages import (
    normalize as normalize,  # noqa: F401 - import for its registration side effect
)
from fdi.pipeline.stages import ocr as ocr  # noqa: F401 - import for its registration side effect
from fdi.pipeline.stages import ping as ping  # noqa: F401 - import for its registration side effect
from fdi.pipeline.stages import (
    quality as quality,  # noqa: F401 - import for its registration side effect
)
from fdi.pipeline.stages import (
    semantic as semantic,  # noqa: F401 - import for its registration side effect
)
from fdi.pipeline.stages import (
    structure as structure,  # noqa: F401 - import for its registration side effect
)
