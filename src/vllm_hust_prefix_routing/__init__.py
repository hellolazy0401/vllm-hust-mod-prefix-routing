"""PR #173 prefix-routing extraction; see the package documentation."""
from ._version import __version__


def register():
    from .plugin import register as activate
    return activate()
