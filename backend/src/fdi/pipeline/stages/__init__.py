"""Importing this package registers every built-in stage."""

from fdi.pipeline.stages import (
    ast as ast,  # noqa: F401 - import for its registration side effect
)
from fdi.pipeline.stages import (
    basis as basis,  # noqa: F401 - import for its registration side effect
)
from fdi.pipeline.stages import (
    chunks as chunks,  # noqa: F401 - import for its registration side effect
)
from fdi.pipeline.stages import (
    embedding as embedding,  # noqa: F401 - import for its registration side effect
)
from fdi.pipeline.stages import (
    fog_mapping as fog_mapping,  # noqa: F401 - import for its registration side effect
)
from fdi.pipeline.stages import (
    fog_objects as fog_objects,  # noqa: F401 - import for its registration side effect
)
from fdi.pipeline.stages import (
    fog_reason as fog_reason,  # noqa: F401 - import for its registration side effect
)
from fdi.pipeline.stages import (
    graph as graph,  # noqa: F401 - import for its registration side effect
)
from fdi.pipeline.stages import (
    graphload as graphload,  # noqa: F401 - import for its registration side effect
)
from fdi.pipeline.stages import (
    index as index,  # noqa: F401 - import for its registration side effect
)
from fdi.pipeline.stages import (
    knowledge as knowledge,  # noqa: F401 - import for its registration side effect
)
from fdi.pipeline.stages import (
    linking as linking,  # noqa: F401 - import for its registration side effect
)
from fdi.pipeline.stages import (
    normalize as normalize,  # noqa: F401 - import for its registration side effect
)
from fdi.pipeline.stages import (
    note_index as note_index,  # noqa: F401 - import for its registration side effect
)
from fdi.pipeline.stages import ocr as ocr  # noqa: F401 - import for its registration side effect
from fdi.pipeline.stages import (
    ontology as ontology,  # noqa: F401 - import for its registration side effect
)
from fdi.pipeline.stages import ping as ping  # noqa: F401 - import for its registration side effect
from fdi.pipeline.stages import (
    provenance as provenance,  # noqa: F401 - import for its registration side effect
)
from fdi.pipeline.stages import (
    quality as quality,  # noqa: F401 - import for its registration side effect
)
from fdi.pipeline.stages import (
    semantic as semantic,  # noqa: F401 - import for its registration side effect
)
from fdi.pipeline.stages import (
    statement_index as statement_index,  # noqa: F401 - import for its registration side effect
)
from fdi.pipeline.stages import (
    structure as structure,  # noqa: F401 - import for its registration side effect
)
