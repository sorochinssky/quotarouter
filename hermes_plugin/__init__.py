"""Hermes plugin shim: makes the `quotarouter` package importable, and registers the backend.

Why a separate directory instead of the repository root: the Hermes loader imports the plugin
directory as `hermes_plugins.<slug>` and puts that directory on the module's `__path__`. If the
plugin directory were the repository root, a root-level `__init__.py` would break pytest
collection (pytest then treats the directory as a package named `__init__`).

Import strategy, in order:

1. `import quotarouter` — the normal case when the package is installed (`pip install -e .`).
2. put the repository root on `sys.path` and retry — the case of a checkout used through a
   symlink: `ln -s /path/to/repo/hermes_plugin ~/.hermes/plugins/quotarouter`.

A symlink `quotarouter -> ../quotarouter` inside this directory would do the same job, but it
fails `hermes plugins validate` with `symlink_escape`: a plugin that follows links out of its
own directory can reach anything on the host. So the import is explicit instead of a link, and
the failure message says what to do.

Note the import must resolve to the same code the CLI uses (`python3 -m quotarouter.cli`);
step 1 and step 2 both end at the repository's own package, never a different copy on `sys.path`.
"""

from __future__ import annotations

import sys
from pathlib import Path

__all__ = ["RouterProvider", "register", "__version__"]


def _import_package():
    try:
        import quotarouter as package  # installed copy or already on sys.path
        return package
    except ImportError:
        pass
    repo_root = Path(__file__).resolve().parent.parent
    if not (repo_root / "quotarouter" / "__init__.py").exists():
        raise ImportError(
            "quotarouter: cannot find the package. Either `pip install quotarouter`, or keep this "
            "plugin directory inside the repository checkout it belongs to, or set PYTHONPATH to "
            f"the directory that contains `quotarouter/` (looked in {repo_root})."
        ) from None
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    import quotarouter as package
    return package


_package = _import_package()
__version__ = _package.__version__

from quotarouter.integrations.hermes import RouterProvider  # noqa: E402  (needs sys.path first)


def register(ctx) -> None:
    """Plugin entry point: registers ONE backend named `quotarouter`."""
    ctx.register_web_search_provider(RouterProvider())
