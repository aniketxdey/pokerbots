"""Bot isolation helpers: per-call time limits, error capture, no network, muted output.

Time limit modes (cfg.time_mode):
  'hard'  - (default, matches the official rules) the call is interrupted the moment it
            exceeds the limit and the bot auto-checks/folds. Uses SIGALRM/setitimer, so
            it works on Linux/macOS; on Windows it silently degrades to 'soft'.
  'soft'  - the call runs to completion, but if it took longer than the limit its
            answer is discarded and the bot auto-checks/folds.
  'off'   - time is measured and reported but never enforced.
"""
import io
import os
import re
import signal
import socket
import sys
import time
import traceback

HAS_ALARM = hasattr(signal, "setitimer") and hasattr(signal, "SIGALRM")


class BotTimeout(BaseException):
    """Raised inside a bot when it runs past its time limit.

    Subclasses BaseException so that bots using `except Exception:` cannot swallow it.
    """


_armed = False


def _alarm_handler(signum, frame):
    global _armed
    if _armed:
        _armed = False
        raise BotTimeout()


def install_alarm():
    if HAS_ALARM:
        signal.signal(signal.SIGALRM, _alarm_handler)


def _no_network(*a, **k):
    raise PermissionError("network access is disabled for bots (official rules §3.6)")


def disable_network():
    """Bots run without network access, like the official sandbox."""
    socket.socket = _no_network          # type: ignore
    socket.create_connection = _no_network  # type: ignore
    socket.getaddrinfo = _no_network     # type: ignore


def mute_output():
    """Bots love print(); swallow it so it doesn't slow the simulation down."""
    devnull = open(os.devnull, "w")
    sys.stdout = devnull
    sys.stderr = devnull


class BotStats:
    __slots__ = ("decisions", "timeouts", "errors", "invalid", "time_total", "time_max",
                 "callback_timeouts", "error_samples")

    def __init__(self):
        self.decisions = 0
        self.timeouts = 0
        self.errors = 0
        self.invalid = 0
        self.time_total = 0.0
        self.time_max = 0.0
        self.callback_timeouts = 0
        self.error_samples = []

    def merge(self, other):
        self.decisions += other.decisions
        self.timeouts += other.timeouts
        self.errors += other.errors
        self.invalid += other.invalid
        self.time_total += other.time_total
        self.time_max = max(self.time_max, other.time_max)
        self.callback_timeouts += other.callback_timeouts
        for e in other.error_samples:
            if len(self.error_samples) < 5 and e not in self.error_samples:
                self.error_samples.append(e)

    def to_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}

    @classmethod
    def from_dict(cls, d):
        s = cls()
        for k in cls.__slots__:
            if k in d:
                setattr(s, k, d[k])
        return s


TIMEOUT = object()
ERROR = object()


def timed_call(fn, args, limit_s, mode, stats, is_decision):
    """Call fn(*args) under the time limit. Returns the result, or TIMEOUT / ERROR."""
    global _armed
    use_alarm = mode == "hard" and HAS_ALARM and limit_s > 0
    t0 = time.perf_counter()
    try:
        if use_alarm:
            _armed = True
            signal.setitimer(signal.ITIMER_REAL, limit_s)
        try:
            result = fn(*args)
        finally:
            if use_alarm:
                _armed = False
                signal.setitimer(signal.ITIMER_REAL, 0)
    except BotTimeout:
        result = TIMEOUT
    except Exception:  # bot crashed
        stats.errors += 1
        if len(stats.error_samples) < 5:
            tb = traceback.format_exc(limit=3).strip().splitlines()
            msg = tb[-1] if tb else "error"
            loc = next((l.strip() for l in reversed(tb) if l.strip().startswith("File")), "")
            loc = re.sub(r'File "([^"]*)"', lambda m: 'File "' + os.path.basename(m.group(1)) + '"', loc)
            sample = f"{msg}  [{loc}]" if loc else msg
            if sample not in stats.error_samples:
                stats.error_samples.append(sample)
        result = ERROR
    dt = time.perf_counter() - t0
    if is_decision:
        stats.decisions += 1
        stats.time_total += dt
        if dt > stats.time_max:
            stats.time_max = dt
    over = mode != "off" and limit_s > 0 and dt > limit_s
    if result is TIMEOUT or (over and result is not ERROR):
        if is_decision:
            stats.timeouts += 1
        else:
            stats.callback_timeouts += 1
        return TIMEOUT
    return result
