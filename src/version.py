"""The one place the application version is defined.

Four things read it and none of them may disagree: the startup banner, the
update checker comparing against the newest GitHub release, the Windows
version resource baked into the .exe by FarmsyncSolver.spec, and the release
workflow, which refuses to publish a tag that does not match.

Bumping it:

  patch  1.0.1  a bug fix. No new config key, no state.db change, nothing the
                operator has to be told.
  minor  1.1.0  a new feature, or a new config key with a working default. An
                existing config.json and an existing state.db still work.
  major  2.0.0  something breaks. A config key removed or renamed, a state.db
                column changing meaning, or a default that would surprise a
                running operator. Say in CHANGELOG.md what they must do.
"""

__version__ = "1.0.0"
