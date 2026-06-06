"""
Arithmetic cross-checks for Malayan Interstate Cricket scorecards.

Loads scorecards.json and runs validation checks on every innings,
reporting issues grouped by match.

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

DEFAULT_INPUT = "site/scorecards.json"

NON_DISMISSALS = {"not out", "retired", "retired hurt", "absent", "did not bat", "[unknown]", "unknown", ""}


def _int(val, default=0) -> int:
    """Coerce a value to int, handling strings and None."""
    if val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default

ALL_CHECKS = [
    "batting_total",
    "dismissal_wickets",
    "batsmen_count",
    "invalid_dismissal",
    "fow_ascending",
    "fow_count",
    "fow_final_value",
]


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
    batting = innings.get("batting", [])
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


def check_batting_total(innings: dict) -> list[dict]:
    batting = innings.get("batting", [])
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


def _bowler_from_dismissal(dismissal: str) -> str | None:
    d = dismissal.strip()
    if not d or d.lower() in ("not out", "retired", "retired hurt"):
        return None
    if "run out" in d.lower():
        return None
    m = re.search(r"\bb\s+(.+)$", d, re.IGNORECASE)
    return re.sub(r"\s+", " ", m.group(1).strip()).lower() if m else None


def check_dismissal_wickets(innings: dict) -> list[dict]:
    issues = []
    batting = innings.get("batting", [])
    bowling = innings.get("bowling", [])

    tally: dict[str, int] = {}
    for b in batting:
        bowler = _bowler_from_dismissal(b.get("dismissal") or "")
        if bowler:
            tally[bowler] = tally.get(bowler, 0) + 1

    dismissal_total = sum(tally.values())
    bowling_total = sum(_int(bw.get("wickets")) for bw in bowling)

    if dismissal_total != bowling_total:
        issues.append({
            "check": "dismissal_wickets",
            "message": (
                f"bowling figures total {bowling_total}w "
                f"but {dismissal_total} bowler-credited dismissals"
            ),
            "numeric": bowling_total - dismissal_total,
        })

    for bw in bowling:
        name = re.sub(r"\s+", " ", (bw.get("name") or "").strip().lower())
        if not name:
            continue
        bw_wickets = _int(bw.get("wickets"))
        d_wickets = tally.get(name)
        if d_wickets is None:
            surname = name.rsplit(" ", 1)[-1] if " " in name else name
            for dk, dv in tally.items():
                if dk.endswith(surname) or surname == dk:
                    d_wickets = dv
                    break
        if d_wickets is None:
            d_wickets = 0
        if bw_wickets != d_wickets:
            issues.append({
                "check": "dismissal_wickets",
                "message": (
                    f"{bw.get('name', '?')}: bowling says {bw_wickets}w "
                    f"but dismissals say {d_wickets}w"
                ),
                "numeric": bw_wickets - d_wickets,
            })

    return issues


def check_batsmen_count(innings: dict) -> list[dict]:
    n = len(innings.get("batting", []))
    if n > 11:
        return [{
            "check": "batsmen_count",
            "message": f"{n} batsmen (expected <= 11)",
            "numeric": n - 11,
        }]
    return []


def check_invalid_dismissal(innings: dict) -> list[dict]:
    issues = []
    for b in innings.get("batting", []):
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


CHECK_FUNCS = {
    "batting_total": check_batting_total,
    "dismissal_wickets": check_dismissal_wickets,
    "batsmen_count": check_batsmen_count,
    "invalid_dismissal": check_invalid_dismissal,
    "fow_ascending": check_fow_ascending,
    "fow_count": check_fow_count,
    "fow_final_value": check_fow_final_value,
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

        for innings in match.get("innings", []):
            label = _innings_label(innings)
            sparseness = _innings_sparseness(innings)
            for check_name in checks:
                func = CHECK_FUNCS[check_name]
                for issue in func(innings):
                    all_issues.append({
                        "page": page,
                        "source_url": source_url,
                        "match": match_label,
                        "innings": label,
                        "sparseness": sparseness,
                        **issue,
                    })
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
            sparse = issue.get("sparseness", 0)
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
        unknown = [c for c in checks if c not in CHECK_FUNCS]
        if unknown:
            print(f"Error: unknown check(s): {', '.join(unknown)}", file=sys.stderr)
            print(f"Available: {', '.join(ALL_CHECKS)}", file=sys.stderr)
            sys.exit(1)

    issues = validate(data, checks)

    if args.csv:
        csv_path = Path(args.csv)
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["page", "match", "innings", "category", "discrepancy", "numeric", "sparseness"])
            for issue in issues:
                writer.writerow([
                    issue["page"],
                    issue["match"],
                    issue["innings"],
                    issue["check"],
                    issue["message"],
                    issue.get("numeric", ""),
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
