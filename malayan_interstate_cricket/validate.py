"""
Arithmetic cross-checks for Malayan Interstate Cricket scorecards.

Loads scorecards.json and runs validation checks on every innings,
reporting issues grouped by match. Each issue carries a `severity`
(higher = more serious) and, where meaningful, a `numeric` delta.

Usage:
    uv run python malayan_interstate_cricket/validate.py
    uv run python malayan_interstate_cricket/validate.py --json
    uv run python malayan_interstate_cricket/validate.py --check batting_total,fow_ascending
"""

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from names import (
    NON_DISMISSALS,
    dismissal_tally,
    lookup_tally,
    normalize_name,
    surname,
)

DEFAULT_INPUT = "site/scorecards.json"


def _int(val, default=0) -> int:
    """Coerce a value to int, handling strings and None."""
    if val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


# Severity scale: 5 structural omission, 4 structure mismatch,
# 3 hard arithmetic, 2 secondary arithmetic, 1 cosmetic/noisy.
SEVERITY = {
    "missing_section": 5,
    "innings_structure": 4,
    "batting_total": 3,
    "batsmen_count": 3,
    "dismissed_count": 3,
    "dismissal_wickets": 2,
    "duplicate_player": 2,
    "fow_ascending": 2,
    "fow_count": 2,
    "fow_final_value": 2,
    "fow_max": 2,
    "extras_detail": 2,
    "invalid_dismissal": 1,
    "overs_sanity": 1,
}

ALL_CHECKS = list(SEVERITY)


def _innings_label(innings: dict) -> str:
    team = innings.get("team", "?")
    num = innings.get("innings_number", "?")
    return f"{team} inn {num}"


def _innings_sparseness(innings: dict) -> float:
    """Return 0.0 (complete) to 1.0 (empty) based on missing data.

    Components (each 0–1, equally weighted):
      - batsmen: fraction missing out of 11
      - dismissals: fraction of batsmen with no/unknown dismissal
      - bowling: 1 if no bowling data, else 0
      - extras: 1 if extras total is None, else 0
    """
    batting = innings.get("batting") or []
    n_bat = len(batting)

    bat_score = max(0, 11 - n_bat) / 11

    if n_bat > 0:
        unknown = sum(
            1 for b in batting
            if not b.get("dismissal")
            or b["dismissal"].strip().lower() in ("[unknown]", "unknown")
        )
        dismissal_score = unknown / n_bat
    else:
        dismissal_score = 1.0

    bowling_score = 1.0 if not innings.get("bowling") else 0.0
    extras_score = 1.0 if (innings.get("extras") or {}).get("total") is None else 0.0

    return round((bat_score + dismissal_score + bowling_score + extras_score) / 4, 2)


# ---------------------------------------------------------------------------
# Per-innings checks: func(innings) -> list of issue dicts
# ---------------------------------------------------------------------------

def check_missing_section(innings: dict) -> list[dict]:
    """An innings with null/empty batting or no total is a parse failure,
    not a clean innings — flag it so omission is penalized, not rewarded."""
    issues = []
    if not (innings.get("batting") or []):
        issues.append({
            "check": "missing_section",
            "message": "batting section is null/empty",
            "numeric": None,
        })
    total = innings.get("total")
    if not total or total.get("runs") is None:
        issues.append({
            "check": "missing_section",
            "message": "total is missing",
            "numeric": None,
        })
    return issues


def check_batting_total(innings: dict) -> list[dict]:
    batting = innings.get("batting") or []
    if not batting:
        return []  # covered by missing_section
    extras_total = _int((innings.get("extras") or {}).get("total"))
    declared_total = _int((innings.get("total") or {}).get("runs"))
    bat_sum = sum(_int(b.get("runs")) for b in batting)
    computed = bat_sum + extras_total
    if computed != declared_total:
        diff = computed - declared_total
        sign = "+" if diff > 0 else ""
        return [{
            "check": "batting_total",
            "message": f"batting ({bat_sum}) + extras ({extras_total}) = {computed}, but total is {declared_total} ({sign}{diff})",
            "numeric": diff,
        }]
    return []


def check_dismissal_wickets(innings: dict) -> list[dict]:
    """One issue per innings: bowling wickets vs dismissal-credited wickets.

    Per-bowler mismatches are detailed in the message but scored once, so a
    single systemic name-matching problem doesn't swamp the issue count.
    """
    batting = innings.get("batting") or []
    bowling = innings.get("bowling") or []
    tally = dismissal_tally(batting)

    dismissal_total = sum(tally.values())
    bowling_total = sum(_int(bw.get("wickets")) for bw in bowling)

    details = []
    for bw in bowling:
        name = (bw.get("name") or "").strip()
        if not name or bw.get("wickets") is None:
            continue
        d_w = lookup_tally(tally, name)
        if d_w is None:
            s = surname(normalize_name(name))
            if sum(1 for k in tally if surname(k) == s) > 1:
                continue  # ambiguous surname — can't attribute, don't guess
            d_w = 0
        bw_w = _int(bw.get("wickets"))
        if bw_w != d_w:
            details.append(f"{name}: bowling {bw_w}w vs dismissals {d_w}w")

    if dismissal_total == bowling_total and not details:
        return []

    message = (
        f"bowling figures total {bowling_total}w "
        f"but {dismissal_total} bowler-credited dismissals"
    )
    if details:
        message += " (" + "; ".join(details) + ")"
    return [{
        "check": "dismissal_wickets",
        "message": message,
        "numeric": bowling_total - dismissal_total,
    }]


def check_batsmen_count(innings: dict) -> list[dict]:
    n = len(innings.get("batting") or [])
    if n > 11:
        return [{
            "check": "batsmen_count",
            "message": f"{n} batsmen (expected <= 11)",
            "numeric": n - 11,
        }]
    return []


def check_dismissed_count(innings: dict) -> list[dict]:
    """Count of dismissed batsmen must match the declared wicket count.

    Catches innings interleaving errors (1st/2nd innings dismissals swapped)
    and missing batsmen. Under-counts are only flagged for full lineups,
    since sparse old cards legitimately omit batsmen.
    """
    batting = innings.get("batting") or []
    if not batting:
        return []
    n_dismissed = sum(
        1 for b in batting
        if (b.get("dismissal") or "").strip().lower() not in NON_DISMISSALS
    )
    declared = (innings.get("total") or {}).get("wickets")
    expected = 10 if declared is None else _int(declared)

    if n_dismissed > expected or (n_dismissed < expected and len(batting) >= 11):
        wkt_display = "all out (10)" if declared is None else str(declared)
        return [{
            "check": "dismissed_count",
            "message": f"{n_dismissed} dismissed batsmen but wickets={wkt_display}",
            "numeric": n_dismissed - expected,
        }]
    return []


def check_duplicate_player(innings: dict) -> list[dict]:
    issues = []
    for section in ("batting", "bowling"):
        seen: dict[str, int] = {}
        for entry in innings.get(section) or []:
            name = normalize_name(entry.get("name") or "")
            if name:
                seen[name] = seen.get(name, 0) + 1
        dupes = [n for n, c in seen.items() if c > 1]
        if dupes:
            issues.append({
                "check": "duplicate_player",
                "message": f"duplicate names in {section}: {', '.join(dupes)}",
                "numeric": None,
            })
    return issues


def check_invalid_dismissal(innings: dict) -> list[dict]:
    issues = []
    for b in innings.get("batting") or []:
        d = (b.get("dismissal") or "").strip()
        if not d:
            continue
        if d.lower() in NON_DISMISSALS:
            continue
        if d[0].isdigit():
            issues.append({
                "check": "invalid_dismissal",
                "message": f"{b.get('name', '?')}: dismissal is \"{d}\" (starts with number)",
                "numeric": None,
            })
    return issues


def check_fow_ascending(innings: dict) -> list[dict]:
    fow = innings.get("fow")
    if not fow or not isinstance(fow, list):
        return []
    nums = [x for x in fow if isinstance(x, (int, float))]
    for i in range(1, len(nums)):
        if nums[i] < nums[i - 1]:
            return [{
                "check": "fow_ascending",
                "message": f"FOW not ascending: {fow}",
                "numeric": None,
            }]
    return []


def check_fow_count(innings: dict) -> list[dict]:
    fow = innings.get("fow")
    if not fow or not isinstance(fow, list):
        return []
    total = innings.get("total") or {}
    declared_wickets = total.get("wickets")
    if declared_wickets is None:
        expected = 10
    else:
        expected = declared_wickets

    n_fow = len([x for x in fow if isinstance(x, (int, float))])
    if n_fow != expected:
        wkt_display = "all out (10)" if declared_wickets is None else str(declared_wickets)
        return [{
            "check": "fow_count",
            "message": f"{n_fow} FOW entries but wickets={wkt_display}",
            "numeric": n_fow - expected,
        }]
    return []


def check_fow_final_value(innings: dict) -> list[dict]:
    fow = innings.get("fow")
    if not fow or not isinstance(fow, list):
        return []
    total = innings.get("total") or {}
    declared_wickets = total.get("wickets")
    if declared_wickets is not None:
        return []
    declared_runs = _int(total.get("runs"))
    nums = [x for x in fow if isinstance(x, (int, float))]
    if not nums:
        return []
    last_fow = nums[-1]
    if last_fow != declared_runs:
        return [{
            "check": "fow_final_value",
            "message": f"last FOW is {last_fow} but all-out total is {declared_runs}",
            "numeric": last_fow - declared_runs,
        }]
    return []


def check_fow_max(innings: dict) -> list[dict]:
    """No FOW value can exceed the innings total."""
    fow = innings.get("fow")
    if not fow or not isinstance(fow, list):
        return []
    runs = (innings.get("total") or {}).get("runs")
    if runs is None:
        return []
    nums = [x for x in fow if isinstance(x, (int, float))]
    too_big = [x for x in nums if x > _int(runs)]
    if too_big:
        return [{
            "check": "fow_max",
            "message": f"FOW value(s) {too_big} exceed innings total {runs}",
            "numeric": max(too_big) - _int(runs),
        }]
    return []


def check_extras_detail(innings: dict) -> list[dict]:
    """When the extras breakdown is given, it must sum to the extras total."""
    extras = innings.get("extras") or {}
    detail = extras.get("detail")
    total = extras.get("total")
    if not detail or total is None:
        return []
    nums = [int(n) for n in re.findall(r"\d+", detail)]
    if not nums:
        return []
    if sum(nums) != _int(total):
        return [{
            "check": "extras_detail",
            "message": f"extras detail \"{detail}\" sums to {sum(nums)} but total is {total}",
            "numeric": sum(nums) - _int(total),
        }]
    return []


# ---------------------------------------------------------------------------
# Match-level checks: func(match) -> list of issue dicts (with "innings" label)
# ---------------------------------------------------------------------------

def check_innings_structure(match: dict) -> list[dict]:
    """Structural sanity for the match as a whole: teams, innings counts,
    result consistency, bowlers not batting in the same innings."""
    issues = []
    info = match.get("match") or {}
    innings_list = match.get("innings") or []
    teams = {
        normalize_name(info.get(k) or "")
        for k in ("team1", "team2")
        if info.get(k)
    }

    per_team: dict[str, int] = {}
    for innings in innings_list:
        label = _innings_label(innings)
        team = normalize_name(innings.get("team") or "")
        if teams and team and team not in teams:
            issues.append({
                "check": "innings_structure",
                "innings": label,
                "message": f"innings team \"{innings.get('team')}\" matches neither {info.get('team1')!r} nor {info.get('team2')!r}",
                "numeric": None,
            })
        per_team[team] = per_team.get(team, 0) + 1

        num = innings.get("innings_number")
        if num not in (1, 2, None):
            issues.append({
                "check": "innings_structure",
                "innings": label,
                "message": f"innings_number is {num} (expected 1 or 2)",
                "numeric": None,
            })

        batters = {
            normalize_name(b.get("name") or "")
            for b in innings.get("batting") or []
        }
        bowling_batters = [
            bw.get("name") for bw in innings.get("bowling") or []
            if normalize_name(bw.get("name") or "") in batters
        ]
        if bowling_batters:
            issues.append({
                "check": "innings_structure",
                "innings": label,
                "message": f"bowler(s) also in same innings' batting: {', '.join(bowling_batters)}",
                "numeric": None,
            })

    for team, count in per_team.items():
        if count > 2:
            issues.append({
                "check": "innings_structure",
                "innings": "match",
                "message": f"{count} innings for one team (expected <= 2)",
                "numeric": count - 2,
            })

    result = (info.get("result") or "").lower()
    n = len(innings_list)
    if "won by an innings" in result and n != 3:
        issues.append({
            "check": "innings_structure",
            "innings": "match",
            "message": f"result is an innings victory but {n} innings present (expected 3)",
            "numeric": n - 3,
        })
    elif re.search(r"won by \d+ (run|wicket)", result) and n == 3:
        issues.append({
            "check": "innings_structure",
            "innings": "match",
            "message": f"result \"{info.get('result')}\" implies an even innings count but 3 innings present",
            "numeric": None,
        })
    return issues


def check_overs_sanity(match: dict) -> list[dict]:
    """The fractional part of an overs figure must be a legal ball count
    for the era's balls-per-over (5, 6, or 8 in this dataset)."""
    bpo = _int(match.get("balls_per_over"), default=6) or 6
    issues = []
    for innings in match.get("innings") or []:
        bad = []
        for bw in innings.get("bowling") or []:
            overs = bw.get("overs")
            if overs is None:
                continue
            m = re.match(r"^\d+\.(\d+)$", str(overs).strip())
            if m and int(m.group(1)) >= bpo:
                bad.append(f"{bw.get('name', '?')}: {overs}")
        if bad:
            issues.append({
                "check": "overs_sanity",
                "innings": _innings_label(innings),
                "message": f"overs with illegal ball count for {bpo}-ball overs: {'; '.join(bad)}",
                "numeric": None,
            })
    return issues


CHECK_FUNCS = {
    "missing_section": check_missing_section,
    "batting_total": check_batting_total,
    "dismissal_wickets": check_dismissal_wickets,
    "batsmen_count": check_batsmen_count,
    "dismissed_count": check_dismissed_count,
    "duplicate_player": check_duplicate_player,
    "invalid_dismissal": check_invalid_dismissal,
    "fow_ascending": check_fow_ascending,
    "fow_count": check_fow_count,
    "fow_final_value": check_fow_final_value,
    "fow_max": check_fow_max,
    "extras_detail": check_extras_detail,
}

MATCH_CHECK_FUNCS = {
    "innings_structure": check_innings_structure,
    "overs_sanity": check_overs_sanity,
}


def validate(data: list[dict], checks: list[str]) -> list[dict]:
    all_issues = []
    for match in data:
        page = match.get("page", "?")
        source_url = match.get("source_url", "")
        match_info = match.get("match", {})
        match_label = (
            f"{match_info.get('team1', '?')} v {match_info.get('team2', '?')} "
            f"({match_info.get('date', '?')})"
        )

        def add(issue: dict, innings_label: str, sparseness):
            all_issues.append({
                "page": page,
                "source_url": source_url,
                "match": match_label,
                "innings": innings_label,
                "sparseness": sparseness,
                "severity": SEVERITY.get(issue.get("check"), 1),
                **issue,
            })

        for innings in match.get("innings") or []:
            label = _innings_label(innings)
            sparseness = _innings_sparseness(innings)
            for check_name in checks:
                func = CHECK_FUNCS.get(check_name)
                if not func:
                    continue
                for issue in func(innings):
                    add(issue, label, sparseness)

        for check_name in checks:
            mfunc = MATCH_CHECK_FUNCS.get(check_name)
            if not mfunc:
                continue
            for issue in mfunc(match):
                add(issue, issue.pop("innings", "match"), "")

    return all_issues


def print_report(issues: list[dict]):
    counts = defaultdict(int)
    by_page = defaultdict(list)
    for issue in issues:
        counts[issue["check"]] += 1
        by_page[issue["page"]].append(issue)

    for page in sorted(by_page):
        page_issues = by_page[page]
        match_label = page_issues[0]["match"]
        print(f"\nPage {page}: {match_label}")
        for issue in page_issues:
            sparse = issue.get("sparseness") or 0
            sparse_tag = f" [sparse={sparse}]" if sparse > 0 else ""
            print(f"  [{issue['check']}] {issue['innings']}{sparse_tag}: {issue['message']}")

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    total = 0
    for check in ALL_CHECKS:
        c = counts.get(check, 0)
        total += c
        if c:
            print(f"  {check:25s} {c:>5d}")
    print(f"  {'TOTAL':25s} {total:>5d}")

    # Pages needing reparse: have both batting_total and dismissal_wickets issues
    reparse_pages = sorted(
        page for page, page_issues in by_page.items()
        if any(i["check"] == "batting_total" for i in page_issues)
        and any(i["check"] == "dismissal_wickets" for i in page_issues)
    )
    if reparse_pages:
        print(f"\nPages with both batting_total and dismissal_wickets issues ({len(reparse_pages)}):")
        print(",".join(str(p) for p in reparse_pages))


def main():
    parser = argparse.ArgumentParser(description="Validate scorecards.json")
    parser.add_argument(
        "--input", "-i", default=DEFAULT_INPUT,
        help=f"Path to scorecards.json (default: {DEFAULT_INPUT})",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output results as JSON",
    )
    parser.add_argument(
        "--csv", default=None,
        help="Write issues to a CSV file at this path",
    )
    parser.add_argument(
        "--check", default=None,
        help=f"Comma-separated list of checks to run (default: all). Options: {', '.join(ALL_CHECKS)}",
    )
    args = parser.parse_args()

    path = Path(args.input)
    if not path.exists():
        print(f"Error: {path} not found", file=sys.stderr)
        sys.exit(1)

    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        print("Error: expected a JSON array", file=sys.stderr)
        sys.exit(1)

    checks = ALL_CHECKS
    if args.check:
        checks = [c.strip() for c in args.check.split(",")]
        unknown = [c for c in checks if c not in CHECK_FUNCS and c not in MATCH_CHECK_FUNCS]
        if unknown:
            print(f"Error: unknown check(s): {', '.join(unknown)}", file=sys.stderr)
            print(f"Available: {', '.join(ALL_CHECKS)}", file=sys.stderr)
            sys.exit(1)

    issues = validate(data, checks)

    if args.csv:
        csv_path = Path(args.csv)
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["page", "match", "innings", "category", "discrepancy", "numeric", "severity", "sparseness"])
            for issue in issues:
                writer.writerow([
                    issue["page"],
                    issue["match"],
                    issue["innings"],
                    issue["check"],
                    issue["message"],
                    issue.get("numeric", ""),
                    issue.get("severity", ""),
                    issue.get("sparseness", ""),
                ])
        print(f"Wrote {len(issues)} issues to {csv_path}")

    if args.json:
        json.dump(issues, sys.stdout, indent=2, ensure_ascii=False)
        print()
    elif not args.csv:
        if issues:
            print_report(issues)
        else:
            print("No issues found.")

    sys.exit(1 if issues else 0)


if __name__ == "__main__":
    main()
