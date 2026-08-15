# Contributing

Everything a user needs is in [README.md](README.md). This file is for working
on the code.

## Running from source

```bat
python -m pip install -r requirements-dev.txt
python -m src
python -m pytest
```

`python -m src` must be run from the repo root. `src/` is a package using
relative imports, and it resolves `input/config.json` relative to its own
file, so `input/` must sit next to `src/`.

From source the tool keeps its files where it always has — `input/config.json`
and `data/state.db`, relative to the repo root — and never checks for updates.

The two layouts do not talk to each other. A packaged build reads and writes
exactly one config, under `%LOCALAPPDATA%`, and will not adopt one found beside
the `.exe`. It used to, as an upgrade path off the source version, and that
failed in the wild: an unrelated `input\config.json` on the operator's Desktop
was picked up silently and the tool ran on credentials nobody had given it.
Moving from source to the packaged build now means typing the two credentials
into the wizard once, and letting `state.db` rebuild.

Copy `input/config.example.json` to `input/config.json` and fill in the two
placeholders. The startup guard rejects any value containing `REPLACE`, so an
unedited copy fails immediately rather than at the first API call.

## Building the .exe

```bat
build.bat
```

Four steps: install build dependencies, run the test suite, freeze with
PyInstaller, print the SHA-256. `.github/workflows/release.yml` runs the same
four in CI, so a local build that passes is a good predictor of a green
release.

Output lands in `dist\FarmsyncSolver.exe`.

`FarmsyncSolver.spec` carries the reasoning for every non-obvious build choice
— why it is a spec file and not a command line, why `console=True` is not
optional, why UPX is off. Read it before changing it.

## Releasing

```bat
REM 1. edit src/version.py       -> __version__ = "1.1.0"
REM 2. edit CHANGELOG.md         -> add the 1.1.0 section at the top
REM 3. build and test locally
build.bat
REM 4. commit, then publish
git commit -am "Release 1.1.0"
publish.bat 1.1.0
```

`publish.bat` rebuilds the public branch from the working tree as a single
fresh commit and force-pushes it with the matching tag. It refuses to run on a
dirty tree, on a leftover `public` or `publish-tmp` branch, on a detached HEAD,
or against any `origin` that is not the distribution repo.

The tag triggers `.github/workflows/release.yml`, which runs the tests, refuses
the release if the tag does not match `src/version.py`, builds the `.exe`,
writes `SHA256SUMS.txt`, and publishes. Existing users are offered the update
on their next start.

**Version numbers.** Patch for a bug fix. Minor for a feature, or a new config
key with a working default. Major when an existing `config.json` or `state.db`
needs the operator to do something — and say what, in the changelog.

## Why the public history is one commit

`git push` publishes every reachable commit. This repository's development
history contains files that name real devices and accounts, so untracking them
at the tip would not have been enough. `publish.bat` builds an orphan branch
from the working tree instead, and everything `.gitignore` excludes stays
excluded.

## Tests

```bat
python -m pytest
```

From the repo root. No network. The pool tests run real threads against real
timeouts, which is the only way to assert a race, so the suite takes about
twenty seconds rather than two.

`tests/conftest.py` solves the import-time config problem: `src/util.py` opens
`input/config.json` at module scope and `src/solver.py` binds its API key at
module scope, so neither can be imported without a config file. The conftest
feeds fake bytes to `open` while importing the *real* module, so the code is
genuinely under test, the suite never reads live credentials, and it works on
a fresh clone.

**Do not import `src` modules above the conftest's shim block.**

Fakes rather than mocks. `FakeSession` replays a scripted queue of responses
and records every call. The `no_sleep` fixture neutralises all three sleep
sites and *records* the durations, so backoff behaviour is asserted rather
than waited on.

## Adding a config key

Four places, in lockstep:

1. `input/config.json` — your local copy
2. `input/config.example.json` — the committed template
3. `src/bootstrap.py` — `DEFAULTS`
4. `README.md` — the settings table

Two tests in `test_bootstrap.py` pin `DEFAULTS` to the example: one on the key
set, one on the values. Miss the example and a fresh clone silently lacks the
key; let the values drift and a fresh clone and a fresh wizard produce
different configurations with nothing saying so.

## What must never be committed

- `input/config.json` — live credentials
- `data/state.db` — per-account memory
- `build/`, `dist/` — build output

All are in `.gitignore`. Check that file rather than this list before adding
anything.
