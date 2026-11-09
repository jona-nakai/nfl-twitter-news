"""Who gets pinged where.

A story can go to several channels (e.g. breaking-news + injuries). Each subscriber should be
pinged once per story: in the first of those channels they subscribe to. Discord role mentions
can't express "has role A but not role B", so every member also carries exactly one hidden
"combo" role naming their exact subscription set. The first channel a story hits pings its
plain subscription role; later channels ping only the combo roles that don't overlap any channel
the story already went to.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import combinations

from .config import CHANNEL_KEYS

Combo = frozenset[str]

ALL_COMBOS: tuple[Combo, ...] = tuple(
    frozenset(c) for n in range(1, len(CHANNEL_KEYS) + 1) for c in combinations(CHANNEL_KEYS, n)
)


@dataclass(frozen=True)
class Ping:
    base_role: str | None = None
    combos: tuple[Combo, ...] = ()


def ordered(keys: Iterable[str]) -> list[str]:
    wanted = set(keys)
    return [k for k in CHANNEL_KEYS if k in wanted]


def ping_plan(channels: Sequence[str], already: Iterable[str] = ()) -> list[tuple[str, Ping]]:
    """For each channel (in posting order), the roles to mention. `already` lists channels
    the story was posted to earlier, whose subscribers have been pinged for it."""
    plan = []
    for i, key in enumerate(channels):
        earlier = set(already) | set(channels[:i])
        if not earlier:
            plan.append((key, Ping(base_role=key)))
        else:
            combos = tuple(c for c in ALL_COMBOS if key in c and not c & earlier)
            plan.append((key, Ping(combos=combos)))
    return plan


def combo_key(combo: Combo) -> str:
    return "+".join(ordered(combo))


# Every combo role has the same short name: members never pick them, and the ping line under a
# story can mention up to 16 of them, so short names keep that line small.
COMBO_ROLE_NAME = "🔔"
