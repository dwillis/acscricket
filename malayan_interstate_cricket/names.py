"""Shared player-name and dismissal helpers.

Used by both scorecards.py (post-parse patching) and validate.py
(arithmetic cross-checks) so the two can't drift apart.
"""

import re

NON_DISMISSALS = {
    "not out", "retired", "retired hurt", "absent", "did not bat",
    "[unknown]", "unknown", "",
}


def normalize_name(name: str) -> str:
    """Collapse whitespace and lowercase for use as a lookup key."""
    return re.sub(r"\s+", " ", name.strip()).lower()


def surname(name: str) -> str:
    """Last whitespace-separated token of a name."""
    return name.rsplit(" ", 1)[-1]


def bowler_from_dismissal(dismissal: str) -> str | None:
    """Extract the bowler's name from a batting dismissal string.

    Returns None for non-dismissals, run outs, and dismissals that
    don't credit a bowler.
    """
    d = dismissal.strip()
    if not d or d.lower() in NON_DISMISSALS:
        return None
    if "run out" in d.lower():
        return None
    m = re.search(r"\bb\s+(.+)$", d, re.IGNORECASE)
    return m.group(1).strip() if m else None


def dismissal_tally(batting: list[dict]) -> dict[str, int]:
    """Count bowler-credited wickets per normalized bowler name."""
    tally: dict[str, int] = {}
    for bat in batting or []:
        bowler = bowler_from_dismissal(bat.get("dismissal") or "")
        if bowler:
            key = normalize_name(bowler)
            tally[key] = tally.get(key, 0) + 1
    return tally


def lookup_tally(tally: dict[str, int], name: str) -> int | None:
    """Find the tally entry for a (normalized) bowler name.

    Falls back to surname matching, but returns None when the surname is
    ambiguous (two tally entries share it — e.g. R Thomasz vs FA Thomasz)
    rather than guessing.
    """
    key = normalize_name(name)
    if key in tally:
        return tally[key]
    s = surname(key)
    matches = [k for k in tally if surname(k) == s]
    if len(matches) == 1:
        return tally[matches[0]]
    return None
