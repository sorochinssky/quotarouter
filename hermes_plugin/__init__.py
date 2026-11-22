"""Hermes plugin shim: a directory from which the `quotarouter` package is importable.

Why a separate directory instead of the repository root: the Hermes loader imports the plugin
directory as `hermes_plugins.<slug>` and puts it on the module's `__path__`. If the plugin
directory were the repository root, a root-level `__init__.py` would break pytest collection
(pytest then treats that directory as a package named `__init__`). Here there is only the shim
and a symlink `quotarouter -> ../quotarouter`.

The relative import is mandatory: an absolute one (`from quotarouter.integrations... import`)
would resolve through `sys.path` and could pick up a different copy of the package than the
one in this repository.

Install by symlinking this directory into the profile's plugin directory:

    ln -s /path/to/repo/hermes_plugin ~/.hermes/plugins/quotarouter

Then both the plugin and the CLI (`python3 -m quotarouter.cli`) see the same code, because the
`quotarouter` symlink points at the package directory itself.
"""

from __future__ import annotations

from .quotarouter import __version__
from .quotarouter.integrations.hermes import RouterProvider

__all__ = ["RouterProvider", "register", "__version__"]


def register(ctx) -> None:
    """Plugin entry point: registers ONE backend named `quotarouter`."""
    ctx.register_web_search_provider(RouterProvider())
