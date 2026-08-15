# FarmsyncSolver

Solves the captchas that stop your farmsync.cloud accounts from joining.

It watches your farm, finds accounts that are sitting idle, and clears their
captchas through dibycap — many at once, around the
clock. It remembers which accounts are banned, which ones just succeeded, and
which ones keep failing, so it does not waste your credit repeating itself.

One `.exe`. No Python, no install, no admin rights.

---

## What you need before you start

| | |
|---|---|
| A dibycap account | Gives you an **API key**. This is what pays for solves. |
| A farmsync.cloud account | Gives you a **bearer token**. This is what lists your accounts. |
| Windows | 64-bit. Windows 10 or newer. |

Both credentials are typed in once, on first run.

## Install

1. Download `FarmsyncSolver.exe` from the
   [latest release](https://github.com/errored-app/farmsync-solver/releases/latest).

2. Put it on your **Desktop**.

   > **Not** `Program Files`. The app updates itself by rewriting its own
   > folder, and that folder needs admin rights. Your Desktop does not.

3. Double-click it.

Windows may warn you about an unrecognised app. That is expected — see
[Troubleshooting](#troubleshooting).

## First run

It asks four questions:

| Question | Answer |
|---|---|
| dibycap API key | Required. Paste it. |
| farmsync bearer token | Required. Paste it. |
| How many accounts at once | Press **Enter** for 45. |
| Discord webhook for credit alerts | Press **Enter** to skip. |

The last two have defaults, so Enter is a valid answer to both. You can change
any of them later without editing files — see [Changing settings](#changing-settings).

Then it starts working. You will see a credit line, a count of accounts
fetched, and a result line per account.

## Every launch after that

You get a short menu:

```
FarmsyncSolver 1.0.0
Settings file: C:\Users\you\AppData\Local\FarmsyncSolver\config.json

[Enter] Start now   [S] Settings   [Q] Quit
starting in 10...
```

Press nothing and it starts on its own after ten seconds. That means you can
launch it and walk away, or put it in your startup folder.

If your API key or farm token is missing, the countdown does not run. The tool
cannot work without them, so it waits for you instead.

## Changing settings

Press **S** at the menu, or run `FarmsyncSolver.exe --settings`.

You can change four things there:

- your dibycap API key
- your farmsync bearer token
- the thread count
- the Discord webhook URL

Keys are shown masked, so you can check which one is loaded without exposing
it on screen. Saving changes only the key you edited and leaves the rest of
the file alone.

Everything else is edited by hand in `config.json`. The menu shows you the
path to it. See [All settings](#all-settings) below.

## Where your files live

Everything that must survive an update sits in
`%LOCALAPPDATA%\FarmsyncSolver\`:

| File | What it is |
|---|---|
| `config.json` | Your credentials and all tuning. Plain text. |
| `state.db` | Per-account memory: bans, grace stamps, failure counts. |
| `updates/` | Download scratch space. Safe to delete. |

Updating never touches that folder.

That folder is the **only** place the tool reads settings from. It will not
pick up a `config.json` sitting next to the `.exe`, or anywhere else on your
machine, even if one is there. So it is safe to keep the `.exe` on your
Desktop alongside your own files.

**To uninstall:** delete the `.exe`. Delete that folder too if you want your
settings and history gone as well.

## Updating

On startup the app asks GitHub whether a newer release exists:

```
Current version: 1.0.0
Latest version:  1.1.0
A new version is available. Update now? [Y/n]
```

Say yes and it downloads the new `.exe`, checks it against the checksum
published with the release, replaces itself, and restarts. If anything goes
wrong it puts the old version back and starts that instead.

Run with `--no-update` to skip the check entirely.

> **On verification.** The checksum is published in the same release as the
> binary. It catches a corrupt or tampered download. It does not defend
> against a compromised GitHub account. Signed releases are planned, not
> current.

## Command line

```
FarmsyncSolver.exe                 run normally
FarmsyncSolver.exe --settings      open the settings screen straight away
FarmsyncSolver.exe --no-update     skip the update check
FarmsyncSolver.exe --grace-report  print the measured captcha grace curve
```

## Troubleshooting

### "Windows protected your PC" / SmartScreen warning

The `.exe` is not code-signed, so Windows does not recognise the publisher.
Click **More info**, then **Run anyway**.

If you want to check the download first, compare its SHA-256 against
`SHA256SUMS.txt` on the release page:

```bat
certutil -hashfile FarmsyncSolver.exe SHA256
```

### "HALTED — dibycap rejected the API key"

```
INVALID_API_KEY
Nothing recovers this on its own.
Start FarmsyncSolver again and press S at the menu to enter a new key.
```

dibycap said no to your key. Usually it expired, or a character got lost when
you pasted it.

Do what it says: close the window, start it again, press **S**, and paste the
key in. Nothing else is needed.

It stops after the first account rather than repeating the same error against
every account you own.

### "dibycap reports 0 solves left"

You are out of credit. The app does **not** exit — it waits, checking every
minute, and picks up on its own the moment you top up. Press **Ctrl-C** if you
would rather stop it.

Set `discord_webhook_url` in settings and it will message you *before* this
happens, at 5,000 solves remaining.

### "farmsync.cloud unreachable"

A network problem, or farmsync is down. It retries on the next refresh by
itself. Nothing is lost.

### Everything is failing at once

If enough dispatches in a row fail on the solver rather than on the accounts,
the app stops dispatching and probes dibycap until it answers again, then
resumes. You do not need to do anything.

### An update failed and left `FarmsyncSolver.exe.old`

Rare, but it says so plainly when it happens:

```
Update failed and rollback failed. The good binary is at ...FarmsyncSolver.exe.old.
Rename it to ...FarmsyncSolver.exe.
```

Do exactly that. Your working copy is that `.old` file.

### "Cannot update in place — this folder is read-only"

The `.exe` is somewhere it cannot rewrite itself, usually `Program Files`. Move
it to your Desktop and updates will work.

## All settings

`%LOCALAPPDATA%\FarmsyncSolver\config.json`. Read once at startup, so a change
takes effect on the next launch.

The four marked **menu** are editable from the settings screen. The rest are
hand-edited, and their defaults are measured rather than guessed — change them
only if you have a reason.

| Key | Default | What it does |
|---|---|---|
| `api_key` | — | dibycap API key. Required. **menu** |
| `farm_token` | — | farmsync.cloud bearer token. Required. **menu** |
| `threads` | 45 | How many accounts to work on at once. Capped by your dibycap plan. **menu** |
| `discord_webhook_url` | `""` | Credit alert destination. Empty disables alerting. **menu** |
| `round_delay` | 60 | Seconds between farm refreshes. `0` runs one pass and exits. |
| `dead_device_minutes` | 30 | Heartbeat age past which a device counts as switched off. |
| `grace_minutes` | 0 | Post-solve suppression window. `0` disables it — see below. |
| `grace_probe_rate` | 0.02 | Fraction of in-grace accounts dispatched anyway. |
| `ban_recheck_minutes` | 120 | How long a ban is trusted before one free re-test. |
| `status_poll_seconds` | 60 | Seconds between credit reads. |
| `alert_below_solves` | 5000 | Discord alert threshold, in solves remaining. |

### Why `grace_minutes` is 0

It looks like a setting you should turn on. It is not.

A dispatch inside the grace window costs nothing *and* is what makes farmsync
actually join the account. Suppressing it saved no money and held working
accounts back for the whole window. Setting it to a non-zero value is a
deliberate act.

`--grace-report` prints the measured survival curve and a suggested value from
your own runs. It reports; it never applies.

---

## For developers

Building from source, running the tests, and cutting a release are covered in
[CONTRIBUTING.md](CONTRIBUTING.md).
