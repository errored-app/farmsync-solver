"""The version is load-bearing in four places, so its shape is pinned.

src/update.py compares it against a GitHub tag, FarmsyncSolver.spec turns it
into a Windows version resource, CI refuses to publish a tag that disagrees
with it, and the startup banner prints it. A value like "1.0" or "1.0.0-beta"
breaks the first three silently.
"""

from src import version


def test_version_is_exactly_three_dotted_integers():
    parts = version.__version__.split(".")
    assert len(parts) == 3, version.__version__
    assert all(part.isdigit() for part in parts), version.__version__


def test_version_has_no_leading_v():
    """The git tag carries the 'v'; the constant does not. CI compares them."""
    assert not version.__version__.lower().startswith("v")
