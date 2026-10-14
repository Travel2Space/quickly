"""Send-time helpers shared by the send paths in app/jobs.py and the campaign routers."""
from __future__ import annotations

import time
from typing import Hashable

# Process-local de-duplication of operator notifications. The slot scanner re-fires
# a skipped slot every minute; an incident must notify once, not once a minute.
# A restart may repeat a notification once — acceptable.
_open: set[Hashable] = set()             # incidents currently open
_last_sent: dict[Hashable, float] = {}   # key -> monotonic time of the last notification


def notify_once(key: Hashable, min_gap: float = 0.0) -> bool:
    """True when an incident for *key* opens and should be announced.

    Silent while the incident is open (until reset_notify). *min_gap* seconds also
    silence a re-opening soon after the last announcement, so a flapping cause
    cannot notify every minute.
    """
    if key in _open:
        return False
    _open.add(key)
    now = time.monotonic()
    if now - _last_sent.get(key, float("-inf")) < min_gap:
        return False
    _last_sent[key] = now
    return True


def reset_notify(key: Hashable) -> None:
    """The incident is over; the next failure may announce again (subject to min_gap)."""
    _open.discard(key)


def reset_for_tests() -> None:
    _open.clear()
    _last_sent.clear()
