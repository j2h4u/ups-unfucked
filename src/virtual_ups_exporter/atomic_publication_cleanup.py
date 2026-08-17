"""Exact-owned temporary-file cleanup for bounded virtual-UPS publication."""

from __future__ import annotations

import os
import signal
import stat
import threading
from pathlib import Path


def cleanup_atomic_publication(
    temporary: Path,
    descriptor: int | None,
    temporary_identity: tuple[int, int] | None,
    primary_error: BaseException | None,
) -> None:
    """Close and unlink only the exact temporary owned by this write.

    SIGALRM is deferred only across this bounded close/lstat/unlink sequence.
    A pending deadline is delivered after cleanup, while a primary write error
    remains the exception reported to the caller.
    """
    alarm_fired = False
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})

    def defer_alarm(_signum: int, _frame: object) -> None:
        nonlocal alarm_fired
        alarm_fired = True

    if threading.current_thread() is not threading.main_thread():
        try:
            cleanup_errors = _collect_cleanup_errors(
                temporary,
                descriptor,
                temporary_identity,
            )
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        _finish_cleanup(cleanup_errors, primary_error, alarm_fired=False)
        return

    try:
        signal.signal(signal.SIGALRM, defer_alarm)
        cleanup_errors = _collect_cleanup_errors(
            temporary,
            descriptor,
            temporary_identity,
        )
    finally:
        alarm_fired = alarm_fired or signal.SIGALRM in signal.sigpending()
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        signal.signal(signal.SIGALRM, previous_handler)

    _finish_cleanup(cleanup_errors, primary_error, alarm_fired)


def _collect_cleanup_errors(
    temporary: Path,
    descriptor: int | None,
    temporary_identity: tuple[int, int] | None,
) -> list[OSError]:
    cleanup_errors: list[OSError] = []
    cleanup_error = _close_descriptor(descriptor)
    if cleanup_error is not None:
        cleanup_errors.append(cleanup_error)
    cleanup_error = _unlink_owned_temp(temporary, temporary_identity)
    if cleanup_error is not None:
        cleanup_errors.append(cleanup_error)
    return cleanup_errors


def _finish_cleanup(
    cleanup_errors: list[OSError],
    primary_error: BaseException | None,
    alarm_fired: bool,
) -> None:
    if cleanup_errors:
        if primary_error is not None:
            for cleanup_error in cleanup_errors:
                primary_error.add_note(f"publication cleanup failed: {cleanup_error}")
        else:
            raise cleanup_errors[0]
    if alarm_fired and primary_error is None:
        os.kill(os.getpid(), signal.SIGALRM)


def _close_descriptor(descriptor: int | None) -> OSError | None:
    if descriptor is None:
        return None
    try:
        os.close(descriptor)
    except OSError as exc:
        return exc
    return None


def _unlink_owned_temp(
    temporary: Path,
    temporary_identity: tuple[int, int] | None,
) -> OSError | None:
    if temporary_identity is None:
        return None
    try:
        info = temporary.lstat()
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != temporary_identity:
            return None
        temporary.unlink()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return exc
    return None
