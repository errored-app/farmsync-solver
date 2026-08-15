import sys
import time
from threading import Lock

from colorama import Fore, Style, init

# Before colorama wraps the stream, not after. The banner rule and the em-dashes
# below do not exist in Windows' legacy cp1252 code page, and colorama writes
# straight through to it — so `python -m src > log.txt` died on the Round 1
# banner while the same command in a console was fine. A daemon that must
# survive a dibycap outage cannot die from being logged instead of watched.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

init()

CORAL = "\033[38;5;203m"
RESET = "\033[39m"

LEVELS = {
    "INFO": Fore.LIGHTCYAN_EX,
    "CAPTCHA": Fore.LIGHTCYAN_EX,
    "SOLVED": Fore.LIGHTGREEN_EX,
    "SUCCESS": Fore.LIGHTGREEN_EX,
    "FAIL": CORAL,
}
RESULTS = {"joined": "SUCCESS", "solved": "SOLVED", "fail": "FAIL"}

# 4Hz. 65 workers finishing dispatches would otherwise rewrite the title
# hundreds of times a second for a human who reads it once.
TITLE_INTERVAL = 0.25

_lock = Lock()


class TitleBar:
    """The tool's live status in the terminal's own title bar.

    Skipped entirely when stdout is not a terminal: `python -m src > log.txt`
    would otherwise collect one OSC escape per dispatch as garbage in the log.

    One Windows caveat worth remembering rather than fighting — colorama's ANSI
    rewriting can swallow OSC sequences on a legacy conhost, while Windows
    Terminal passes them through. If the title renders as junk, it is cosmetic
    and nothing else breaks.
    """

    def __init__(self, stream=None, clock=time.monotonic, interval=TITLE_INTERVAL):
        self.stream = sys.stdout if stream is None else stream
        self.clock = clock
        self.interval = interval
        self.enabled = bool(getattr(self.stream, "isatty", lambda: False)())
        self._last_at = None

    def update(self, text: str) -> None:
        now = self.clock()
        if self._last_at is not None and now - self._last_at < self.interval:
            return
        self._last_at = now
        self._write(text)

    def restore(self) -> None:
        """Clear the title on the way out, throttle or no throttle.

        This is the one update that cannot be dropped: a stale title left on a
        process that has already exited is worse than no title at all.
        """
        self._write("")

    def _write(self, text: str) -> None:
        if not self.enabled:
            return
        # The same lock the log lines take. 65 workers write this, and an
        # escape sequence torn in half by a result line lands in the terminal
        # as visible junk.
        with _lock:
            self.stream.write(f"\033]0;{text}\007")
            self.stream.flush()


class Output:
    @staticmethod
    def banner(text: str) -> None:
        with _lock:
            print(f"{Style.BRIGHT}{Fore.LIGHTCYAN_EX}── {text} ──{Style.RESET_ALL}", flush=True)

    @staticmethod
    def error(text: str) -> None:
        with _lock:
            print(f"  {CORAL}{text}{RESET}", flush=True)

    @staticmethod
    def warn(text: str) -> None:
        """Something the operator should look at, but not a failure."""
        with _lock:
            print(f"  {Fore.LIGHTYELLOW_EX}! {text}{RESET}", flush=True)

    @staticmethod
    def info(text: str) -> None:
        with _lock:
            print(f"  {Style.DIM}{text}{Style.RESET_ALL}", flush=True)

    @staticmethod
    def line(level: str, user: str, detail: str) -> None:
        color = LEVELS[level]
        when = time.strftime("%H:%M:%S")
        with _lock:
            print(f"  {Style.DIM}{when}{Style.RESET_ALL}  {color}[{level}]{RESET:<8}  "
                  f"{user:<20} {Style.DIM}|{Style.RESET_ALL} {color}{detail}{RESET}", flush=True)

    @staticmethod
    def result(user: str, outcome: str, detail: str) -> None:
        Output.line(RESULTS[outcome], user, detail)

    @staticmethod
    def summary(counts: dict) -> None:
        with _lock:
            print(f"\n  {Fore.LIGHTGREEN_EX}{counts['joined']}{RESET} joined  "
                  f"{Fore.LIGHTGREEN_EX}{counts['solved']}{RESET} solved  "
                  f"{CORAL}{counts['fail']}{RESET} fail\n", flush=True)
