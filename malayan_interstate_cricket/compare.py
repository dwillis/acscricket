"""
Compare two scorecard JSON files and pick the better parse per page.

Runs validation checks on each version and decides a per-page winner using
both data completeness and severity-weighted issue scores, so a parse that
omits an innings (or nulls out batting) can't "win" by generating fewer
checkable signals.

Verdict rule per page:
  1. If one parse is materially more complete, it wins outright.
  2. Otherwise the lower severity-weighted issue score wins.
  3. Ties break toward the more complete parse, then toward the original.

Usage:
    uv run python malayan_interstate_cricket/compare.py original.json candidate.json
    uv run python malayan_interstate_cricket/compare.py original.json candidate.json --merge output.json
    uv run python malayan_interstate_cricket/compare.py original.json candidate.json --golden golden.json

Golden mode scores both files against hand-verified ground truth instead of
heuristics. golden.json format:
[
  {"page": 3,
   "innings": [
     {"team": "Selangor", "innings_number": 1, "runs": 108, "wickets": null, "batsmen": 11},
     ...
   ]}
]
(`wickets: null` = all out; any field may be omitted to skip checking it.)
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

from validate import ALL_CHECKS, validate

# A structural completeness gap bigger than this decides the page outright.
COMPLETENESS_TOLERANCE = 3


def structural_completeness(match: dict) -> int:
    """Innings and batting rows only — sections every scorecard must have.

    A gap here means an innings was dropped or its batting nulled out, which
    vetoes the issue-count comparison. Optional sections (bowling, fow) are
    deliberately excluded: old cards legitimately omit them, and a model can
    inflate them by hallucinating rows (e.g. fow numbers misread as bowlers).
    """
    score = 0
    for inn in match.get("innings") or []:
        score += 5  # the innings exists at all
        score += min(len(inn.get("batting") or []), 11)
    return score


def completeness(match: dict) -> int:
    """Full count of checkable signals; used only for tie-breaking."""
    score = structural_completeness(match)
    for inn in match.get("innings") or []:
        score += min(len(inn.get("bowling") or []), 11)
        score += 3 if inn.get("fow") else 0
        score += 2 if (inn.get("total") or {}).get("runs") is not None else 0
        score += 1 if (inn.get("extras") or {}).get("total") is not None else 0
    return score


def weighted_score(issues: list[dict]) -> int:
    return sum(issue.get("severity", 1) for issue in issues)


def decide(orig_match, orig_issues, cand_match, cand_issues) -> str:
    """Return 'original', 'candidate', or 'tie' for one page."""
    orig_struct = structural_completeness(orig_match)
    cand_struct = structural_completeness(cand_match)
    if abs(orig_struct - cand_struct) > COMPLETENESS_TOLERANCE:
        return "candidate" if cand_struct > orig_struct else "original"

    orig_w = weighted_score(orig_issues)
    cand_w = weighted_score(cand_issues)
    if cand_w != orig_w:
        return "candidate" if cand_w < orig_w else "original"

    orig_comp = completeness(orig_match)
    cand_comp = completeness(cand_match)
    if cand_comp != orig_comp:
        return "candidate" if cand_comp > orig_comp else "original"
    return "tie"


def check_deltas(orig_issues: list[dict], cand_issues: list[dict]) -> dict[str, int]:
    """Per-check issue-count delta (candidate minus original), non-zero only."""
    deltas: dict[str, int] = defaultdict(int)
    for issue in cand_issues:
        deltas[issue["check"]] += 1
    for issue in orig_issues:
        deltas[issue["check"]] -= 1
    return {k: v for k, v in deltas.items() if v}


def golden_mismatches(match: dict, golden: dict) -> list[str]:
    """Compare a parsed match against a hand-verified golden entry."""
    mismatches = []
    parsed = {
        ((inn.get("team") or "").strip().lower(), inn.get("innings_number")): inn
        for inn in match.get("innings") or []
    }
    golden_innings = golden.get("innings") or []
    if len(parsed) != len(golden_innings):
        mismatches.append(
            f"innings count: parsed {len(parsed)}, golden {len(golden_innings)}"
        )
    for g in golden_innings:
        key = ((g.get("team") or "").strip().lower(), g.get("innings_number"))
        inn = parsed.get(key)
        label = f"{g.get('team')} inn {g.get('innings_number')}"
        if inn is None:
            mismatches.append(f"{label}: missing from parse")
            continue
        total = inn.get("total") or {}
        if "runs" in g and total.get("runs") != g["runs"]:
            mismatches.append(f"{label}: runs {total.get('runs')} != {g['runs']}")
        if "wickets" in g and total.get("wickets") != g["wickets"]:
            mismatches.append(f"{label}: wickets {total.get('wickets')} != {g['wickets']}")
        if "batsmen" in g and len(inn.get("batting") or []) != g["batsmen"]:
            mismatches.append(
                f"{label}: batsmen {len(inn.get('batting') or [])} != {g['batsmen']}"
            )
    return mismatches


def run_golden(golden_path: Path, orig_by_page: dict, cand_by_page: dict):
    golden_data = json.loads(golden_path.read_text(encoding="utf-8"))
    totals = {"original": 0, "candidate": 0}
    for entry in golden_data:
        page = entry.get("page")
        print(f"\nPage {page}:")
        for name, by_page in (("original", orig_by_page), ("candidate", cand_by_page)):
            match = by_page.get(page)
            if match is None:
                print(f"  {name}: page not present")
                continue
            mismatches = golden_mismatches(match, entry)
            totals[name] += len(mismatches)
            print(f"  {name}: {len(mismatches)} mismatch(es)")
            for m in mismatches:
                print(f"    - {m}")
    print(f"\nGolden totals: original {totals['original']} mismatches, "
          f"candidate {totals['candidate']} mismatches")


def main():
    parser = argparse.ArgumentParser(
        description="Compare two scorecard files and pick the better parse per page."
    )
    parser.add_argument("original", help="Path to the original scorecards.json")
    parser.add_argument("candidate", help="Path to the candidate (re-parsed) scorecards.json")
    parser.add_argument(
        "--merge", "-m", default=None,
        help="Write merged output (best of each) to this path",
    )
    parser.add_argument(
        "--check", default=None,
        help=f"Comma-separated checks to compare on (default: all). Options: {', '.join(ALL_CHECKS)}",
    )
    parser.add_argument(
        "--golden", default=None,
        help="Path to a hand-verified golden.json; scores both files against it instead",
    )
    args = parser.parse_args()

    checks = ALL_CHECKS
    if args.check:
        checks = [c.strip() for c in args.check.split(",")]

    original = json.loads(Path(args.original).read_text(encoding="utf-8"))
    candidate = json.loads(Path(args.candidate).read_text(encoding="utf-8"))

    orig_by_page = {m["page"]: m for m in original if "page" in m}
    cand_by_page = {m["page"]: m for m in candidate if "page" in m}

    if args.golden:
        run_golden(Path(args.golden), orig_by_page, cand_by_page)
        return

    cand_pages = sorted(cand_by_page.keys())

    wins = {"original": 0, "candidate": 0, "tie": 0}
    details = []
    delta_totals: dict[str, int] = defaultdict(int)

    for page in cand_pages:
        if page not in orig_by_page:
            continue

        orig_match = orig_by_page[page]
        cand_match = cand_by_page[page]
        orig_issues = validate([orig_match], checks)
        cand_issues = validate([cand_match], checks)
        orig_comp = completeness(orig_match)
        cand_comp = completeness(cand_match)

        winner = decide(orig_match, orig_issues, cand_match, cand_issues)
        wins[winner] += 1

        deltas = check_deltas(orig_issues, cand_issues)
        for check, delta in deltas.items():
            delta_totals[check] += delta

        match_info = orig_match.get("match", {})
        label = (
            f"{match_info.get('team1', '?')} v {match_info.get('team2', '?')} "
            f"({match_info.get('date', '?')})"
        )

        details.append({
            "page": page,
            "match": label,
            "original_issues": len(orig_issues),
            "candidate_issues": len(cand_issues),
            "original_weighted": weighted_score(orig_issues),
            "candidate_weighted": weighted_score(cand_issues),
            "original_completeness": orig_comp,
            "candidate_completeness": cand_comp,
            "deltas": deltas,
            "winner": winner,
        })

    print(f"Compared {len(details)} pages")
    print(f"  Candidate better: {wins['candidate']}")
    print(f"  Original better:  {wins['original']}")
    print(f"  Tied:             {wins['tie']}")

    if delta_totals:
        print("\nPer-check issue deltas (candidate minus original):")
        for check in ALL_CHECKS:
            if check in delta_totals:
                print(f"  {check:25s} {delta_totals[check]:+d}")
    print()

    for d in details:
        marker = {"candidate": "+", "original": "-", "tie": "="}[d["winner"]]
        delta_str = ""
        if d["deltas"]:
            parts = [f"{c} {v:+d}" for c, v in sorted(d["deltas"].items())]
            delta_str = f"  [{', '.join(parts)}]"
        print(
            f"  {marker} Page {d['page']:>3}: "
            f"original w{d['original_weighted']}/c{d['original_completeness']}, "
            f"candidate w{d['candidate_weighted']}/c{d['candidate_completeness']}"
            f"  ({d['match']}){delta_str}"
        )

    if args.merge:
        merged = dict(orig_by_page)
        replaced = 0
        for d in details:
            if d["winner"] == "candidate":
                merged[d["page"]] = cand_by_page[d["page"]]
                replaced += 1
        # Candidate-only pages (not in original) are included as-is
        added = 0
        for page, match in cand_by_page.items():
            if page not in merged:
                merged[page] = match
                added += 1

        ordered = [merged[k] for k in sorted(merged)]
        Path(args.merge).write_text(
            json.dumps(ordered, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\nMerged output written to {args.merge}")
        print(f"  {replaced} pages replaced with candidate version")
        if added:
            print(f"  {added} candidate-only pages added")


if __name__ == "__main__":
    main()
