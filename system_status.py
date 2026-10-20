"""
`get_system_status`: a small health readout of the machine Jarvis runs on.

MOCK TELEMETRY, on purpose. CPU load and memory use are made up (seeded random
numbers in a plausible range) so the rich-reply path can be shown end to end
before a real source is wired in; the only real number is uptime, measured
from when this module was imported (about when the brain started). Because of
that the widget is sent with `sample: true`, and the page labels it Sample.
Replace `collect()` with real readings (psutil, the OS) and drop `sample` when
the numbers are real.

Three things come out of one snapshot:
  - widget_data(): the data of a `system_status` widget, in the shape
    TripPlanner_Web/components/jarvis/widgets/SystemStatusWidget.tsx validates;
  - web_result(): what the model is told when the widget is on screen;
  - spoken_summary(): what the model is given when there is no screen (voice),
    so it can say the numbers out loud.

Dependency-free apart from the standard library.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

_STARTED_AT = time.monotonic()

# Load above these is "degraded" / "down" in the widget.
_DEGRADED_PCT = 70.0
_DOWN_PCT = 90.0


@dataclass(frozen=True)
class Snapshot:
    cpu_pct: float
    cpu_cores: int
    memory_used_gb: float
    memory_total_gb: float
    uptime_seconds: int
    taken_at: datetime

    @property
    def memory_pct(self) -> float:
        return 100.0 * self.memory_used_gb / self.memory_total_gb


def collect(rng: Optional[random.Random] = None, now: Optional[datetime] = None) -> Snapshot:
    """One mock reading. Pass `rng` / `now` for a repeatable one (tests)."""
    rng = rng or random.Random()
    return Snapshot(
        cpu_pct=round(rng.uniform(8, 62), 1),
        cpu_cores=8,
        memory_used_gb=round(rng.uniform(4.5, 11.5), 1),
        memory_total_gb=16.0,
        uptime_seconds=int(time.monotonic() - _STARTED_AT),
        taken_at=now or datetime.now(timezone.utc),
    )


def _level(pct: float) -> str:
    if pct >= _DOWN_PCT:
        return "down"
    if pct >= _DEGRADED_PCT:
        return "degraded"
    return "operational"


def format_uptime(seconds: int) -> str:
    """"3 h 12 min", "45 min", "20 s" - the two largest units that are not zero."""
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days} d {hours} h"
    if hours:
        return f"{hours} h {minutes} min"
    if minutes:
        return f"{minutes} min"
    return f"{secs} s"


def widget_data(snapshot: Snapshot) -> dict[str, Any]:
    """The `data` of a system_status widget: one row each for CPU, memory and uptime."""
    return {
        "updatedAt": snapshot.taken_at.isoformat().replace("+00:00", "Z"),
        "services": [
            {
                "name": "CPU",
                "status": _level(snapshot.cpu_pct),
                "note": f"{snapshot.cpu_pct:g}% load across {snapshot.cpu_cores} cores",
            },
            {
                "name": "Memory",
                "status": _level(snapshot.memory_pct),
                "note": f"{snapshot.memory_used_gb:g} of {snapshot.memory_total_gb:g} GB in use ({snapshot.memory_pct:.0f}%)",
            },
            {
                "name": "Uptime",
                "status": "operational",
                "note": f"Up {format_uptime(snapshot.uptime_seconds)} since the brain started",
            },
        ],
    }


def spoken_summary(snapshot: Snapshot) -> str:
    """The readout as a tool result for the voice assistant, which has no screen to show it on."""
    return (
        f"System status (sample numbers, not live readings): CPU at {snapshot.cpu_pct:g} percent "
        f"of {snapshot.cpu_cores} cores, memory {snapshot.memory_used_gb:g} of "
        f"{snapshot.memory_total_gb:g} gigabytes in use, up for {format_uptime(snapshot.uptime_seconds)}. "
        "Tell the user in one or two short sentences, and say these are sample numbers."
    )


def web_result(snapshot: Snapshot) -> str:
    """The tool result when the web chat is showing the widget: the model should not repeat what is on screen."""
    return (
        "DONE: a System status card (CPU, memory, uptime - SAMPLE numbers, not live readings) is "
        "now shown to the user in the chat. Do not list the numbers; answer in ONE short sentence "
        f"on the overall picture ({'everything looks normal' if _level(max(snapshot.cpu_pct, snapshot.memory_pct)) == 'operational' else 'something is running hot'}) "
        "and say they are sample numbers."
    )
