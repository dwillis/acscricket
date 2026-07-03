"""Unit tests for the scorecard pipeline, seeded with real failure shapes
observed in production: prose-before-JSON responses, max_tokens truncation,
null batting/bowling sections, and ambiguous surname matching.

Run with: uv run --with pytest pytest malayan_interstate_cricket/tests/
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import compare
import scorecards
import validate
from names import bowler_from_dismissal, dismissal_tally, lookup_tally


# ---------------------------------------------------------------------------
# _extract_json — the model-response shapes that broke production
# ---------------------------------------------------------------------------

class TestExtractJson:
    def test_bare_json(self):
        assert json.loads(scorecards._extract_json('{"a": 1}')) == {"a": 1}

    def test_fenced_json(self):
        raw = '```json\n{"a": 1}\n```'
        assert json.loads(scorecards._extract_json(raw)) == {"a": 1}

    def test_prose_then_fenced_json(self):
        # The page 1/2 failure shape: PASS-1 reasoning before the fence
        raw = (
            "I'll parse this carefully.\n\n**PASS 1 — tally:** {not json} prose\n\n"
            '```json\n{"match": {"team1": "Perak"}}\n```'
        )
        assert json.loads(scorecards._extract_json(raw)) == {
            "match": {"team1": "Perak"}
        }

    def test_prose_then_unfenced_json(self):
        raw = 'Here you go:\n{"a": {"b": 2}}\nDone.'
        assert json.loads(scorecards._extract_json(raw)) == {"a": {"b": 2}}

    def test_truncated_json_fails_loudly(self):
        # max_tokens truncation: extraction returns something, but json fails
        raw = '```json\n{"match": {"team1": "Perak", "innings": [{"name"'
        with pytest.raises(json.JSONDecodeError):
            json.loads(scorecards._extract_json(raw))


# ---------------------------------------------------------------------------
# names helpers
# ---------------------------------------------------------------------------

class TestNames:
    @pytest.mark.parametrize("dismissal,expected", [
        ("b Fox", "Fox"),
        ("c Ingall b Fox", "Fox"),
        ("c&b Stewart", "Stewart"),
        ("lbw b R Thomasz", "R Thomasz"),
        ("st Walker b Hughes", "Hughes"),
        ("ht wkt b Disbrowe", "Disbrowe"),
        ("not out", None),
        ("retired hurt", None),
        ("run out", None),
        ("run out (Hawkins>Ryan)", None),
        ("", None),
    ])
    def test_bowler_from_dismissal(self, dismissal, expected):
        assert bowler_from_dismissal(dismissal) == expected

    def test_dismissal_tally(self):
        batting = [
            {"dismissal": "b Fox"},
            {"dismissal": "c X b Fox"},
            {"dismissal": "b Stewart"},
            {"dismissal": "not out"},
        ]
        assert dismissal_tally(batting) == {"fox": 2, "stewart": 1}

    def test_lookup_exact(self):
        assert lookup_tally({"r thomasz": 2}, "R Thomasz") == 2

    def test_lookup_surname_fallback(self):
        assert lookup_tally({"r thomasz": 2}, "Thomasz") == 2

    def test_lookup_ambiguous_surname_returns_none(self):
        tally = {"r thomasz": 2, "fa thomasz": 1}
        assert lookup_tally(tally, "Thomasz") is None

    def test_lookup_absent_returns_none(self):
        assert lookup_tally({"fox": 1}, "Stewart") is None


# ---------------------------------------------------------------------------
# scorecards post-processing
# ---------------------------------------------------------------------------

class TestFixInitials:
    @pytest.mark.parametrize("raw,expected", [
        ("M T Retnam", "MT Retnam"),
        ("AWWanless", "AW Wanless"),
        ("SGAMaartensz", "SGA Maartensz"),
        ("EW Moss", "EW Moss"),
    ])
    def test_fix_initials(self, raw, expected):
        assert scorecards._fix_initials(raw) == expected


class TestValidateBowlingWickets:
    def _scorecard(self, bowling):
        return {"innings": [{
            "team": "A", "innings_number": 1,
            "batting": [
                {"name": "X One", "dismissal": "b R Thomasz", "runs": 5},
                {"name": "X Two", "dismissal": "b FA Thomasz", "runs": 5},
                {"name": "X Three", "dismissal": "not out", "runs": 5},
            ],
            "bowling": bowling,
        }]}

    def test_exact_match_patches_and_preserves_reported(self):
        sc = self._scorecard([{"name": "R Thomasz", "wickets": 2}])
        out = scorecards.validate_bowling_wickets(sc)
        bowl = out["innings"][0]["bowling"][0]
        assert bowl["wickets"] == 1
        assert bowl["wickets_reported"] == 2

    def test_ambiguous_surname_left_alone(self):
        sc = self._scorecard([{"name": "Thomasz", "wickets": 3}])
        out = scorecards.validate_bowling_wickets(sc)
        assert out["innings"][0]["bowling"][0]["wickets"] == 3

    def test_absent_with_complete_dismissals_zeroed(self):
        sc = self._scorecard([{"name": "Nobody", "wickets": 4}])
        out = scorecards.validate_bowling_wickets(sc)
        assert out["innings"][0]["bowling"][0]["wickets"] == 0

    def test_absent_with_unknown_dismissals_left_alone(self):
        sc = self._scorecard([{"name": "Nobody", "wickets": 4}])
        sc["innings"][0]["batting"][0]["dismissal"] = "[unknown]"
        out = scorecards.validate_bowling_wickets(sc)
        assert out["innings"][0]["bowling"][0]["wickets"] == 4

    def test_null_innings_sections_dont_crash(self):
        sc = {"innings": [{"team": "A", "batting": None, "bowling": None}]}
        scorecards.validate_bowling_wickets(sc)
        scorecards.normalize_player_names(sc)


# ---------------------------------------------------------------------------
# validate checks
# ---------------------------------------------------------------------------

def _innings(**kwargs):
    base = {
        "team": "Selangor", "innings_number": 1,
        "batting": [
            {"name": f"P{i}", "dismissal": "b Fox", "runs": 10} for i in range(10)
        ] + [{"name": "P10", "dismissal": "not out", "runs": 5}],
        "extras": {"total": 5, "detail": "b 3, lb 2"},
        "total": {"runs": 110, "wickets": None, "declared": False},
        "fow": list(range(10, 110, 10)),
        "bowling": [{"name": "Fox", "overs": "12.3", "maidens": 2,
                     "runs": 50, "wickets": 10}],
    }
    base.update(kwargs)
    return base


class TestValidateChecks:
    def test_clean_innings_passes(self):
        assert validate.check_batting_total(_innings()) == []
        assert validate.check_missing_section(_innings()) == []
        assert validate.check_dismissed_count(_innings()) == []
        assert validate.check_duplicate_player(_innings()) == []
        assert validate.check_fow_max(_innings()) == []
        assert validate.check_extras_detail(_innings()) == []

    def test_missing_section_null_batting(self):
        issues = validate.check_missing_section(_innings(batting=None))
        assert any("batting" in i["message"] for i in issues)

    def test_missing_section_no_total(self):
        issues = validate.check_missing_section(_innings(total=None))
        assert any("total" in i["message"] for i in issues)

    def test_dismissal_wickets_collapses_to_one_issue(self):
        # Two bowlers both wrong -> still a single scored issue
        inn = _innings(bowling=[
            {"name": "Fox", "wickets": 3},
            {"name": "Stranger", "wickets": 4},
        ])
        issues = validate.check_dismissal_wickets(inn)
        assert len(issues) == 1
        assert "Fox" in issues[0]["message"]
        assert "Stranger" in issues[0]["message"]

    def test_dismissed_count_over(self):
        inn = _innings(total={"runs": 110, "wickets": 5})
        issues = validate.check_dismissed_count(inn)
        assert issues and issues[0]["numeric"] == 5

    def test_duplicate_player(self):
        inn = _innings()
        inn["batting"][1]["name"] = "P0"
        issues = validate.check_duplicate_player(inn)
        assert issues and "p0" in issues[0]["message"]

    def test_fow_max(self):
        inn = _innings(fow=[10, 200])
        issues = validate.check_fow_max(inn)
        assert issues and issues[0]["numeric"] == 90

    def test_extras_detail_mismatch(self):
        inn = _innings(extras={"total": 9, "detail": "b 3, lb 2"})
        issues = validate.check_extras_detail(inn)
        assert issues and issues[0]["numeric"] == -4

    def test_innings_structure_wrong_team(self):
        match = {
            "match": {"team1": "Selangor", "team2": "Perak",
                      "result": "Selangor won by 2 wickets"},
            "innings": [_innings(team="Penang")],
        }
        issues = validate.check_innings_structure(match)
        assert any("Penang" in i["message"] for i in issues)

    def test_innings_structure_innings_victory_count(self):
        match = {
            "match": {"team1": "A", "team2": "B",
                      "result": "A won by an innings and 10 runs"},
            "innings": [_innings(team="A"), _innings(team="B"),
                        _innings(team="B", innings_number=2),
                        _innings(team="A", innings_number=2)],
        }
        issues = validate.check_innings_structure(match)
        assert any("innings victory" in i["message"] for i in issues)

    def test_overs_sanity(self):
        match = {
            "balls_per_over": 6,
            "innings": [_innings(bowling=[
                {"name": "Fox", "overs": "12.7", "wickets": 1}])],
        }
        issues = validate.check_overs_sanity(match)
        assert issues and "12.7" in issues[0]["message"]

    def test_bowling_plausibility_maidens_exceed_overs(self):
        inn = _innings(bowling=[
            {"name": "Fox", "overs": "2.0", "maidens": 5, "runs": 10, "wickets": 1},
        ])
        issues = validate.check_bowling_plausibility(inn)
        assert any("maidens exceeds" in i["message"] for i in issues)

    def test_bowling_plausibility_runs_exceed_total(self):
        inn = _innings(bowling=[
            {"name": "Fox", "overs": "10.0", "maidens": 0, "runs": 200, "wickets": 1},
        ])
        issues = validate.check_bowling_plausibility(inn)
        assert any("runs conceded" in i["message"] for i in issues)

    def test_bowling_plausibility_wickets_exceed_dismissed(self):
        inn = _innings(bowling=[
            {"name": "Fox", "overs": "10.0", "maidens": 0, "runs": 20, "wickets": 20},
        ])
        issues = validate.check_bowling_plausibility(inn)
        assert any("exceed dismissed batsmen" in i["message"] for i in issues)

    def test_bowling_plausibility_clean_passes(self):
        assert validate.check_bowling_plausibility(_innings()) == []

    def test_result_margin_runs_mismatch(self):
        match = {
            "match": {"team1": "A", "team2": "B", "result": "A won by 50 runs"},
            "innings": [
                _innings(team="A", innings_number=1, total={"runs": 100, "wickets": None}),
                _innings(team="B", innings_number=1, total={"runs": 90, "wickets": None}),
            ],
        }
        issues = validate.check_result_margin(match)
        assert issues and "50-run margin" in issues[0]["message"]

    def test_result_margin_innings_victory_uses_summed_totals(self):
        match = {
            "match": {"team1": "A", "team2": "B", "result": "A won by an innings and 10 runs"},
            "innings": [
                _innings(team="A", innings_number=1, total={"runs": 200, "wickets": None}),
                _innings(team="B", innings_number=1, total={"runs": 90, "wickets": None}),
                _innings(team="B", innings_number=2, total={"runs": 100, "wickets": None}),
            ],
        }
        assert validate.check_result_margin(match) == []

    def test_result_margin_decided_on_first_innings_ignores_second(self):
        # Real shape from page 118: Singapore won by 50 runs on the first
        # innings alone; Singapore's 2nd innings (125/7) must not count.
        match = {
            "match": {"team1": "Singapore", "team2": "Melaka", "result": "Singapore won by 50 runs"},
            "notes": ["Match was played over one day with the result decided on the first innings"],
            "innings": [
                _innings(team="Singapore", innings_number=1, total={"runs": 147, "wickets": None}),
                _innings(team="Melaka", innings_number=1, total={"runs": 97, "wickets": None}),
                _innings(team="Singapore", innings_number=2, total={"runs": 125, "wickets": 7}),
            ],
        }
        assert validate.check_result_margin(match) == []

    def test_result_margin_skips_incomplete_totals(self):
        match = {
            "match": {"team1": "A", "team2": "B", "result": "A won by 50 runs"},
            "innings": [
                _innings(team="A", innings_number=1, total={"runs": None, "wickets": None}),
                _innings(team="B", innings_number=1, total={"runs": 90, "wickets": None}),
            ],
        }
        assert validate.check_result_margin(match) == []

    def test_sparse_source_halves_arithmetic_severity(self):
        match = {
            "page": 1, "match": {}, "notes": ["Bowling analyses are not known"],
            "innings": [_innings(total={"runs": 999, "wickets": None})],
        }
        issues = validate.validate([match], validate.ALL_CHECKS)
        bt = [i for i in issues if i["check"] == "batting_total"]
        assert bt and bt[0]["severity"] == 1  # halved from 3
        assert bt[0]["sparse_source"] is True

    def test_validate_attaches_severity(self):
        match = {"page": 1, "match": {}, "innings": [_innings(batting=None)]}
        issues = validate.validate([match], validate.ALL_CHECKS)
        missing = [i for i in issues if i["check"] == "missing_section"]
        assert missing and all(i["severity"] == 5 for i in missing)


# ---------------------------------------------------------------------------
# compare verdicts — omission must not win
# ---------------------------------------------------------------------------

class TestCompare:
    def test_completeness_counts_signals(self):
        full = {"innings": [_innings()]}
        empty = {"innings": [{"team": "A", "batting": None, "bowling": None}]}
        assert compare.completeness(full) > compare.completeness(empty)

    def test_duplicated_innings_does_not_win_on_completeness(self):
        # Real page-58 shape: one parse duplicates a team's 2nd innings as a
        # 3rd/4th object with identical data, inflating naive completeness.
        # A correct, non-duplicated 4-innings parse must not lose to it.
        duplicated = {
            "page": 58, "match": {"team1": "A", "team2": "B", "result": "A won by 9 runs"},
            "innings": [
                _innings(team="A", innings_number=1, total={"runs": 168, "wickets": None}),
                _innings(team="B", innings_number=2, total={"runs": 160, "wickets": None}),
                _innings(team="A", innings_number=3, total={"runs": 148, "wickets": None}),
                _innings(team="B", innings_number=3, total={"runs": 147, "wickets": None}),
                _innings(team="A", innings_number=4, total={"runs": 148, "wickets": None}),
            ],
        }
        correct = {
            "page": 58, "match": {"team1": "A", "team2": "B", "result": "A won by 9 runs"},
            "innings": [
                _innings(team="A", innings_number=1, total={"runs": 168, "wickets": None}),
                _innings(team="A", innings_number=2, total={"runs": 148, "wickets": None}),
                _innings(team="B", innings_number=1, total={"runs": 160, "wickets": None}),
                _innings(team="B", innings_number=2, total={"runs": 147, "wickets": None}),
            ],
        }
        assert compare.structural_completeness(duplicated) == compare.structural_completeness(correct)
        dup_issues = validate.validate([duplicated], validate.ALL_CHECKS)
        correct_issues = validate.validate([correct], validate.ALL_CHECKS)
        winner = compare.decide(duplicated, dup_issues, correct, correct_issues)
        assert winner == "candidate"

    def test_omission_does_not_win(self):
        # Candidate dropped an innings entirely but has zero issues;
        # original is complete with one minor arithmetic issue.
        original = {
            "page": 1, "match": {"team1": "A", "team2": "B"},
            "innings": [_innings(team="A"), _innings(team="B")],
        }
        original["innings"][0]["total"]["runs"] = 111  # off-by-one issue
        candidate = {
            "page": 1, "match": {"team1": "A", "team2": "B"},
            "innings": [_innings(team="B")],
        }
        orig_issues = validate.validate([original], validate.ALL_CHECKS)
        cand_issues = validate.validate([candidate], validate.ALL_CHECKS)
        assert len(cand_issues) <= len(orig_issues)  # the trap the old metric fell into
        winner = compare.decide(original, orig_issues, candidate, cand_issues)
        assert winner == "original"

    def test_hallucinated_bowling_does_not_win_on_completeness(self):
        # The page-2 shape: original fabricated extra bowling rows (earning
        # many issues), candidate honestly parsed the sparse card cleanly.
        # Equal batting structure -> weighted issues must decide.
        original = {"page": 2, "match": {"team1": "A", "team2": "B"},
                    "innings": [_innings(team="A")]}
        original["innings"][0]["bowling"] = [
            {"name": f"Ghost{i}", "overs": None, "maidens": None,
             "runs": None, "wickets": 2} for i in range(8)
        ]
        candidate = {"page": 2, "match": {"team1": "A", "team2": "B"},
                     "innings": [_innings(team="A")]}
        orig_issues = validate.validate([original], validate.ALL_CHECKS)
        cand_issues = validate.validate([candidate], validate.ALL_CHECKS)
        assert compare.completeness(original) > compare.completeness(candidate)
        winner = compare.decide(original, orig_issues, candidate, cand_issues)
        assert winner == "candidate"

    def test_equal_completeness_weighted_issues_decide(self):
        original = {"page": 1, "match": {"team1": "A", "team2": "B"},
                    "innings": [_innings(team="A")]}
        original["innings"][0]["total"]["runs"] = 111
        candidate = {"page": 1, "match": {"team1": "A", "team2": "B"},
                     "innings": [_innings(team="A")]}
        orig_issues = validate.validate([original], validate.ALL_CHECKS)
        cand_issues = validate.validate([candidate], validate.ALL_CHECKS)
        winner = compare.decide(original, orig_issues, candidate, cand_issues)
        assert winner == "candidate"

    def test_golden_mismatches(self):
        match = {"innings": [_innings()]}
        golden = {"page": 1, "innings": [
            {"team": "Selangor", "innings_number": 1,
             "runs": 110, "wickets": None, "batsmen": 11},
        ]}
        assert compare.golden_mismatches(match, golden) == []
        golden["innings"][0]["runs"] = 99
        assert len(compare.golden_mismatches(match, golden)) == 1
