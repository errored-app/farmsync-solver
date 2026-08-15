# Changelog

Notable changes per release. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[semantic versioning](https://semver.org/).

## [1.0.1] - 2026-08-15

### Removed

- **Migration from beside the `.exe`.** The first run no longer copies a
  `config.json`, `input\config.json`, `state.db`, or `data\state.db` found in
  the `.exe`'s own folder. It ran in the wild and failed: the `.exe` sat on a
  Desktop that already held an unrelated folder named `input` with a
  `config.json` in it, and the tool started on credentials the operator had
  never given it — no wizard, no error, just a rejected key and a thread count
  nobody chose. The folder holding the `.exe` belongs to the operator, not the
  tool. Moving from the source version now means typing the two credentials
  into the wizard once, and letting `state.db` rebuild.

### Changed

- A rejected API key, a rejected farm token, and an unset credential now say
  "start again and press S at the menu" instead of naming a key to edit inside
  `config.json`. The settings screen already does the edit, and the operator
  who hits these messages is the least equipped to be sent to a text editor.

### Added

- The `.exe` carries an icon.

## [1.0.0] - 2026-08-15

First packaged release. Behaviour is unchanged from the source tool; what is
new is how it is delivered.

### Added

- Single-file Windows executable. No Python, pip, virtualenv, or git needed.
- First-run wizard: asks for the dibycap key and the farmsync token, writes a
  complete config, and starts.
- Persistent user data in `%LOCALAPPDATA%\FarmsyncSolver\` — `config.json` and
  `state.db` — so updating never touches settings or per-account memory.
- Migration: the first run of the `.exe` inside an existing project folder
  copies `input/config.json` and `data/state.db` forward. Originals are left
  alone.
- Self-update from GitHub Releases, with SHA-256 verification, rollback on a
  failed swap, and `--no-update` to skip the check.
- `src/version.py` as the single source of truth for the version, enforced
  against the git tag by CI.

### Changed

- Errors naming the config file now print its real path instead of the literal
  `input/config.json`, which does not exist in a packaged install.
- A source run with no `input/config.json` opens the first-run wizard instead
  of raising at import. Better for a person, worse for a script: a run with
  closed stdin now prints "Setup abandoned" and exits quietly where it used to
  fail loudly.

### Notes

- The release checksum lives in the same release as the binary, so it catches
  a corrupt or tampered download but not a compromised GitHub account. Signed
  releases are a planned improvement.
- Credentials are stored in plain text under `%LOCALAPPDATA%`, which Windows
  already restricts to the single user account.
