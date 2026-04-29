"""Re-export shim for backwards compatibility.

The canonical wrapper now lives in
``studiomind.skills.fruity_compressor.wrapper``. This module re-exports
everything so callers that still import from
``studiomind.plugins.fruity_compressor`` continue to work.

Removed once all callers (notably ``agent/tools.py``) are updated to
import from the skill module directly. Until then, this is the safe
compatibility layer added in P3-B.
"""

from studiomind.skills.fruity_compressor.wrapper import *  # noqa: F401,F403
from studiomind.skills.fruity_compressor.wrapper import (  # explicit re-exports
    ATTACK_MAX_MS,
    ATTACK_MIN_MS,
    GAIN_MAX_DB,
    GAIN_MIN_DB,
    KNEE_HARD,
    KNEE_SMOOTH,
    KNEES,
    KNEES_REVERSE,
    NUM_PARAMS,
    PARAM_ATTACK,
    PARAM_GAIN,
    PARAM_RATIO,
    PARAM_RELEASE,
    PARAM_THRESHOLD,
    PARAM_TYPE,
    PLUGIN_NAME,
    RATIO_MAX,
    RATIO_MIN,
    RELEASE_MAX_MS,
    RELEASE_MIN_MS,
    THRESHOLD_MAX_DB,
    THRESHOLD_MIN_DB,
    CompressorState,
    attack_to_param,
    build_compressor_commands,
    decode_state,
    gain_to_param,
    knee_to_param,
    param_to_attack,
    param_to_gain,
    param_to_knee,
    param_to_ratio,
    param_to_release,
    param_to_threshold,
    ratio_to_param,
    release_to_param,
    threshold_to_param,
)
