import sys
import time
from threading import Thread

from . import credit, dispatch, health as health_mod, progress as progress_mod
from . import pool, solver, state as state_mod
from .credit import CreditAlerter, Depletion
from .farmsync import Farmsync, FarmsyncError
from .health import SolverHealth
from .output import Output, TitleBar
from .paths import config_file
from .progress import Progress
from .roblox import Roblox
from .state import State
from .thread_lock import lock
from .util import Util
from .version import __version__

# How long Ctrl-C waits for workers that are mid-dispatch before giving up on
# them. Only reached on an interrupt — a normal exit waits as long as it takes.
SHUTDOWN_GRACE_SECONDS = 2.0


def main() -> None:
    config = Util.config()
    state = State()

    # Read-only, needs no credentials, so it runs before the startup guard.
    if "--grace-report" in sys.argv:
        grace_report(state)
        return

    # Both messages name the menu rather than the file. `settings.parse`
    # already rejects a placeholder in the wizard and the editor, so reaching
    # here at all means the file was written by hand — but the operator who
    # hit it is still the one least equipped to be sent back to a text editor.
    for key, label in (("api_key", "dibycap API key"),
                       ("farm_token", "farmsync token")):
        if "REPLACE" in config[key]:
            Output.error(f"No {label} has been set yet.")
            Output.error("Start FarmsyncSolver again and press S at the menu "
                         "to enter it.")
            Output.error(f"Settings file: {config_file()}")
            return

    settings = {
        "threads": config["threads"],
        "round_delay": config["round_delay"],
        "dead_device_minutes": config.get("dead_device_minutes",
                                          dispatch.DEAD_DEVICE_MINUTES),
        "grace_minutes": config.get("grace_minutes", dispatch.GRACE_MINUTES),
        "grace_probe_rate": config.get("grace_probe_rate", dispatch.GRACE_PROBE_RATE),
        "ban_recheck_minutes": config.get("ban_recheck_minutes",
                                          dispatch.BAN_RECHECK_MINUTES),
        "status_poll_seconds": config.get("status_poll_seconds",
                                          credit.STATUS_POLL_SECONDS),
    }
    farm = Farmsync(config["farm_token"])

    health = SolverHealth()
    depletion = Depletion()
    alerter = CreditAlerter(config.get("discord_webhook_url", ""),
                            config.get("alert_below_solves", credit.ALERT_BELOW_SOLVES),
                            on_error=lambda e: Output.warn(f"discord webhook failed: {e}"))
    progress = Progress(time.time())
    titlebar = TitleBar()
    # The producer owns the credit readout and the workers only read it. A cell
    # rather than a closure variable because `serve` writes it from a different
    # frame, and the workers now outlive every refresh that produced one.
    shared = {"status": None}

    def dispatched(outcome):
        """One dispatch finished: fold it into the session and repaint the title.

        Called from all 65 workers, which is why `TitleBar` throttles itself
        rather than trusting the caller to.
        """
        progress.record(outcome)
        titlebar.update(progress_mod.title_text(
            progress, time.time(), shared["status"], depletion))

    pruned = state.prune(time.time())
    if pruned:
        Output.info(f"pruned {pruned} account rows farmsync has not listed "
                    f"in {state_mod.RETENTION_DAYS} days")

    print(f"FarmsyncSolver {__version__}  |  threads={settings['threads']}  "
          f"grace={settings['grace_minutes']}m")

    work = pool.WorkQueue()
    try:
        serve(farm, state, work, health, alerter, depletion, progress, titlebar,
              settings, dispatched, shared)
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        titlebar.restore()
        state.close()


def serve(farm, state, work, health, alerter, depletion, progress, titlebar,
          settings, dispatched, shared=None):
    """Refresh farmsync on a cadence; let a permanent pool drain the queue.

    The round barrier is gone. It made every worker wait for the slowest one in
    its batch before anything queued next could start, and the two paths differ
    by more than an order of magnitude — a 1.8-second in-grace dispatch behind a
    47-second median solve. `round_delay` is now the refresh interval rather
    than dead time bolted onto the end of each pass.

    The producer runs here on the calling thread, so a `KeyboardInterrupt` still
    lands where the caller can print and shut down cleanly.
    """
    counts = {"joined": 0, "solved": 0, "fail": 0}
    workers = []
    shared = {"status": None} if shared is None else shared
    latest_status = None
    last_status_at = 0.0
    # `round_delay <= 0` means one refresh and exit — a documented mode that a
    # persistent pool has no natural end for, so it is spelled out here.
    one_shot = settings["round_delay"] <= 0
    threads = settings["threads"]
    refresh_n = 0
    interrupted = False

    try:
        while True:
            refresh_n += 1
            Output.banner(f"Refresh {refresh_n}")

            now = time.time()
            if latest_status is None or \
                    now - last_status_at >= settings["status_poll_seconds"]:
                last_status_at = now
                latest_status = poll_credit(health, depletion, now)
            # The status poll doubles as the liveness probe, so a dead key
            # surfaces here before a single account is dispatched against it —
            # and this is also where the pool is answered after an outage it
            # stopped itself on.
            keep_running, latest_status = react_to_health(
                health, alerter, depletion, latest_status,
                settings["status_poll_seconds"], wait=not one_shot)
            if not keep_running:
                break
            if latest_status is not None:
                # The plan's cap is authoritative; `threads` still throttles down.
                threads = min(settings["threads"],
                              latest_status["max_concurrent"] or settings["threads"])
                if not credit.has_credit(latest_status):
                    latest_status = park(alerter, depletion, latest_status,
                                         settings["status_poll_seconds"])
            shared["status"] = latest_status

            # After the health check, not before: workers exit when the breaker
            # opens or the wallet empties, so the pool has to be topped back up
            # once that has been answered. The round loop got this for free by
            # building a fresh pool every pass.
            workers = ensure_workers(workers, threads, work, counts, state,
                                     health, dispatched)

            # The walk is one HTTP call per device and prints nothing of its
            # own — minutes of it on a farm of any size — landing as a
            # silent gap directly under the Refresh banner, which is
            # indistinguishable from a hung process. Said before the wait, not
            # after it, or it explains nothing while the operator is watching.
            Output.info("fetching accounts from farmsync...")
            fetch_started = time.monotonic()
            try:
                accounts = farm.solvable_accounts()
            except FarmsyncError as e:
                Output.error(f"farmsync.cloud unreachable: {e}")
                if one_shot:
                    break
                time.sleep(max(settings["round_delay"], 10))
                continue
            # Only on the success path: this line is the evidence the walk
            # finished, and printing it after the error above would contradict it.
            Output.info(f"fetched {len(accounts):,} accounts in "
                        f"{time.monotonic() - fetch_started:.0f}s")

            now = time.time()
            # Stamping every account this refresh listed is what lets pruning
            # spot rows for accounts that no longer exist. Accounts filtered out
            # upstream by `running: true` are not stamped and will eventually be
            # pruned; that costs one free dispatch to re-learn if they return.
            state.mark_seen([a.get("id") for a in accounts], now)

            plan = dispatch.plan_dispatch(
                accounts, state.load(), now=now,
                dead_device_minutes=settings["dead_device_minutes"],
                grace_minutes=settings["grace_minutes"],
                grace_probe_rate=settings["grace_probe_rate"],
                ban_recheck_minutes=settings["ban_recheck_minutes"])
            composition = plan.summary()
            Output.info(composition)
            if latest_status is not None:
                alerter.check(latest_status, depletion, pool=composition)
            if plan.counts["ban_rechecks"]:
                Output.info(f"re-testing {plan.counts['ban_rechecks']} banned accounts "
                            f"(free) in case they were repaired in farmsync")
            for name, age, active in plan.unclassified:
                if age is None:
                    Output.warn(f"{name}: no heartbeat field — cannot tell if it is live")
                else:
                    Output.warn(f"{name}: heartbeat {age:.0f}m stale but still reports "
                                f"{active} active accounts — not suppressed")

            posted = work.submit(plan.queued)
            held = len(plan.queued) - posted
            if held:
                # Not a warning: an account the previous refresh has not
                # finished is exactly what the dedup exists to hold back.
                Output.info(f"queued {posted}, held {held} already in flight "
                            f"or waiting")
            progress.start_pass(work.pending)
            titlebar.update(progress_mod.title_text(
                progress, time.time(), latest_status, depletion))
            Output.info(progress_mod.session_line(
                progress, time.time(), latest_status))

            if one_shot:
                break
            # The second silent stretch. "workers keep running" rather than
            # "waiting" because that is what happens: `round_delay` is the
            # refresh interval, not dead time bolted onto the end of a pass, and
            # the pool is draining the queue throughout it.
            Output.info(f"next refresh in {settings['round_delay']:g}s "
                        f"— workers keep running")
            time.sleep(settings["round_delay"])
    except KeyboardInterrupt:
        interrupted = True
        raise
    finally:
        # Always, including on Ctrl-C: an open queue leaves every worker waiting
        # for a producer that has gone.
        work.close()
        # A normal end waits for the pool, because that is what makes one-shot
        # mode dispatch everything it queued. Ctrl-C does not: a worker mid-solve
        # can be inside a 180-poll wait, and under the round loop the operator
        # got their prompt back at once. Workers are daemon threads, so whatever
        # is still running dies with the process.
        deadline = time.monotonic() + SHUTDOWN_GRACE_SECONDS
        for t in workers:
            t.join(timeout=max(0.0, deadline - time.monotonic())
                   if interrupted else None)
        Output.summary(counts)
        Output.info(progress_mod.session_line(progress, time.time(), latest_status))
        # A daemon that leaves this loop has already been told why by
        # `react_to_health`; a one-shot run has no next refresh to reach it. The
        # round loop got this for free by answering health once more after the
        # pool joined, and losing it means a live dibycap 503 exits with a
        # screenful of failures and nothing saying what happened.
        if one_shot and health.stopped():
            report_stop(health)


def report_stop(health) -> None:
    """Name the condition that stopped the pool, without acting on it.

    Deliberately not `react_to_health`: this runs after the workers have gone,
    so parking or probing here would hold a finished run open for a pool that
    no longer exists.
    """
    if health.halt_reason:
        Output.banner("STOPPED — dibycap rejected the API key")
        Output.error(f"{health.halt_reason}")
        Output.error("Start FarmsyncSolver again and press S at the menu to "
                     "enter a new key.")
        Output.error(f"Settings file: {config_file()}")
    elif health.out_of_credit:
        Output.banner("STOPPED — out of solving credit")
        Output.error("dibycap reports 0 solves left. Top up and run again.")
    else:
        Output.banner("STOPPED — dibycap is not answering")
        Output.error(f"{health.threshold} dispatches in a row failed on the "
                     f"service, not the accounts. Nothing was charged for them. "
                     f"Try again in a few minutes.")


def ensure_workers(workers, size, work, counts, state, health, dispatched):
    """Top the pool back up to `size` live threads, and return them.

    Workers are permanent in the ordinary case — they outlive every refresh.
    They only ever exit for a reason `react_to_health` has already dealt with,
    so refilling here is how the pool comes back after an outage rather than
    staying empty for the rest of the run.
    """
    alive = [t for t in workers if t.is_alive()]
    for _ in range(size - len(alive)):
        worker = Roblox(lock, work, counts, state, health, dispatched)
        thread = Thread(target=worker.check, daemon=True)
        thread.start()
        alive.append(thread)
    return alive


def poll_credit(health, depletion, now):
    """Read dibycap's credit and fold it into the burn-rate average.

    One failed read is reported and skipped rather than fatal — losing a credit
    readout must not cost a round of solving. It still feeds `health`, because
    this call is the liveness probe as well as the credit readout: a rejected
    key surfaces here before any account is dispatched against it, and a run of
    failures here is the same outage the workers would hit.
    """
    try:
        status = solver.status()
    except Exception as e:
        health.record_failure(e)
        Output.warn(f"credit check failed: {e}")
        return None
    health.record_success()
    depletion.sample(status["estimated_solves"] or 0, now)
    Output.info(credit.credit_line(status, depletion))
    return status


def park(alerter, depletion, status, poll_seconds):
    """Stop dispatching at zero credit and wait for a top-up.

    Per the operator's instruction this never exits: the process stays up, the
    webhook fires because credit reaches zero exactly when nobody is watching
    the terminal, and solving resumes on its own.
    """
    Output.banner("PARKED — out of solving credit")
    Output.error("dibycap reports 0 solves left. Top up and this resumes on "
                 "its own — nothing needs restarting.")
    alerter.notify_parked(status)

    resumed = credit.wait_for_top_up(
        solver.status, poll_seconds=poll_seconds,
        on_error=lambda e: Output.warn(f"credit check failed while parked: {e}"))
    depletion.sample(resumed["estimated_solves"] or 0, time.time())
    Output.info(f"credit restored — {resumed['estimated_solves']:,} solves. resuming.")
    return resumed


def react_to_health(health, alerter, depletion, status, poll_seconds, wait=True):
    """Answer whatever stopped the pool. Returns (keep running, latest status).

    Three conditions, three different answers — which is the whole reason
    `health.py` splits one `TERMINAL` tuple into four:

      * a rejected key never repairs itself, so say so once and stop;
      * an empty wallet parks and waits for a top-up;
      * a sick service pauses dispatch and probes until it answers.

    Checked in that order. A key that will never work must not end up sitting in
    a probe loop because an outage happened to be recorded beside it.
    """
    if health.halt_reason:
        Output.banner("HALTED — dibycap rejected the API key")
        Output.error(f"{health.halt_reason}")
        Output.error("Nothing recovers this on its own.")
        Output.error("Start FarmsyncSolver again and press S at the menu to "
                     "enter a new key.")
        Output.error(f"Settings file: {config_file()}")
        return False, status

    if health.out_of_credit:
        status = park(alerter, depletion, status or {}, poll_seconds)
        health.clear()

    if health.open:
        Output.banner("PAUSED — dibycap is not answering")
        Output.error(f"{health.threshold} dispatches in a row failed on the "
                     f"service, not the accounts. Holding the pool.")
        if not wait:
            # One-shot runs (`round_delay <= 0`) do one pass and exit; sitting
            # on a probe loop would silently turn that into a daemon.
            return False, status
        health.recover(solver.status,
                       first_delay=health_mod.PROBE_DELAY_SECONDS,
                       on_error=lambda e: Output.warn(f"dibycap still down: {e}"))
        Output.info("dibycap is answering again — resuming")

    return True, status


def grace_report(state) -> None:
    """Print the measured survival curve for the captcha grace window.

    Every dispatch already reveals the answer — `solve_ms == 0` means the
    account was still in grace, `solve_ms > 0` means it had expired — so the
    real window is measured as a side effect of normal work.
    """
    histogram = state.grace_histogram()
    if not histogram:
        Output.info("no grace observations yet — run a few passes first")
        return

    print("\n  age (min)   probes   re-captchaed")
    for b in histogram:
        share = b["recaptchaed"] / b["probes"] if b["probes"] else 0.0
        print(f"  {state_mod.bucket_label(b['age_bucket']):>9}   "
              f"{b['probes']:>6}   {b['recaptchaed']:>6}  {share:>5.0%}")

    recommended = state_mod.recommend_grace_minutes(histogram)
    if recommended is None:
        Output.info("\nnot enough evidence yet to recommend a grace_minutes")
    else:
        Output.info(f"\nsuggested grace_minutes: {recommended} "
                    f"(edit {config_file()} yourself — this is never applied)")
