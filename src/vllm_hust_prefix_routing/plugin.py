"""Discovery validates activation; host source hooks own installation/lifecycle."""
import json
import logging
from pathlib import Path

from .components.registry import COMPONENTS
from .runtime import enabled
from ._version import __version__


def descriptor():
    return json.loads((Path(__file__).parent / "manifests/mod.json").read_text(encoding="utf-8"))


def register():
    if not enabled():
        return
    import os
    if os.getenv("VLLM_HUST_PREFIX_ROUTING_BACKEND") == "runtime023-v1":
        from .adapters.core.runtime023 import ensure_compatible, install_publisher
        ensure_compatible()
        install_publisher()
        return
    configured = [c.component_id for c in COMPONENTS if enabled(c.component_id)]
    if not configured:
        raise ValueError("prefix routing enabled without any component switch")
    from .adapters.core.contract import ensure_compatible
    ensure_compatible()
    logging.getLogger(__name__).info("prefix-routing %s contract checked; configured=%s", __version__, configured)
