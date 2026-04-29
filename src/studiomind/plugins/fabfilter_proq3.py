"""Re-export shim for backwards compatibility.

The canonical wrapper now lives in
``studiomind.skills.fabfilter_proq3.wrapper``. This module re-exports
everything so callers that still import from
``studiomind.plugins.fabfilter_proq3`` continue to work.

Removed once all callers (notably ``agent/tools.py``) are updated to
import from the skill module directly. Until then, this is the safe
compatibility layer added in P3-B.
"""

from studiomind.skills.fabfilter_proq3.wrapper import *  # noqa: F401,F403
from studiomind.skills.fabfilter_proq3.wrapper import (  # explicit names
    BAND_STRIDE,
    NUM_BANDS,
    OFF_DYN_ENABLED,
    OFF_DYN_RANGE,
    OFF_ENABLED,
    OFF_FREQ,
    OFF_GAIN,
    OFF_Q,
    OFF_SHAPE,
    OFF_SLOPE,
    OFF_SPEAKERS,
    OFF_STEREO,
    OFF_THRESHOLD,
    OFF_USED,
    PLUGIN_NAME,
    band_param_id,
    build_eq_commands,
    freq_to_param,
    gain_to_param,
    param_to_freq,
    param_to_gain,
    param_to_q,
    q_to_param,
)
