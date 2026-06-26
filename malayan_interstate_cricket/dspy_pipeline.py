"""
DSPy-optimized Cricket Scorecard Extraction
=============================================
Uses DSPy to optimize prompts for parsing historical cricket scorecards.

Usage:
    uv run python malayan_interstate_cricket/dspy_pipeline.py --optimize
    uv run python malayan_interstate_cricket/dspy_pipeline.py --run
    uv run python malayan_interstate_cricket/dspy_pipeline.py --run --pages 2-5
"""

import argparse
import json
import sys
from pathlib import Path

import dspy

from scorecards import (
    SYSTEM_PROMPT,
    USER_PROMPT_TEMPLATE,
    PAGE_CACHE_DIR,
    _extract_json,
    normalize_match,
    normalize_player_names,
    validate_bowling_wickets,
    page_url,
)
from validate import validate, ALL_CHECKS, SEVERITY

BASE_DIR = Path(__file__).parent

PARSING_INSTRUCTIONS = USER_PROMPT_TEMPLATE.split("RAW SCORECARD TEXT:")[0].strip()


# ---------------------------------------------------------------------------
# DSPy Signature & Module
# ---------------------------------------------------------------------------

class ScorecardExtraction(dspy.Signature):
    """Parse a historical cricket scorecard from plain text into structured JSON."""

    scorecard_text: str = dspy.InputField(
        desc="Raw text from a historical cricket scorecard page, continuous with no line breaks"
    )
    parsing_instructions: str = dspy.InputField(
        desc="Detailed rules for parsing interleaved innings, dismissal methods, bowling columns, and FOW data, including the JSON schema"
    )
    structured_json: str = dspy.OutputField(
        desc="Complete JSON object following the scorecard schema with match metadata, all innings (batting, bowling, extras, totals, fow), umpires, and notes"
    )


class ScorecardParser(dspy.Module):
    def __init__(self):
        self.extract = dspy.ChainOfThought(ScorecardExtraction)

    def forward(self, scorecard_text, parsing_instructions):
        return self.extract(
            scorecard_text=scorecard_text,
            parsing_instructions=parsing_instructions,
        )


# ---------------------------------------------------------------------------
# Metric
# ---------------------------------------------------------------------------

MAX_SEVERITY = 30


def scorecard_metric(example, prediction, trace=None):
    """DSPy metric: higher is better, range [0, 1].

    Runs the 14 validation checks from validate.py on the parsed scorecard.
    """
    try:
        raw_json = prediction.structured_json
        scorecard = json.loads(_extract_json(raw_json))
        scorecard = normalize_match(scorecard)
        scorecard = normalize_player_names(scorecard)
        scorecard = validate_bowling_wickets(scorecard)
    except Exception:
        return 0.0

    scorecard["page"] = example.get("page", 0)
    issues = validate([scorecard], ALL_CHECKS)

    if not issues:
        return 1.0

    total_severity = sum(i.get("severity", 1) for i in issues)
    return max(0.0, 1.0 - (total_severity / MAX_SEVERITY))


# ---------------------------------------------------------------------------
# Dataset construction
# ---------------------------------------------------------------------------

def _load_page_text(page: int) -> str | None:
    cache_file = PAGE_CACHE_DIR / f"{page}.txt"
    if cache_file.exists():
        text = cache_file.read_text(encoding="utf-8")
        return text if text else None
    return None


def _make_example(scorecard: dict, page_text: str) -> dspy.Example:
    return dspy.Example(
        scorecard_text=page_text,
        parsing_instructions=PARSING_INSTRUCTIONS,
        structured_json=json.dumps(scorecard, ensure_ascii=False),
        page=scorecard["page"],
    ).with_inputs("scorecard_text", "parsing_instructions")


def build_datasets(
    gold_path: Path,
    scorecards_path: Path,
) -> tuple[list, list, list]:
    """Build train/dev/eval splits.

    Returns (gold_train, gold_dev, silver_eval).
    """
    gold_scorecards = json.load(gold_path.open())
    all_scorecards = json.load(scorecards_path.open())

    # Gold: hand-verified examples for few-shot demos
    gold_examples = []
    for sc in gold_scorecards:
        text = _load_page_text(sc["page"])
        if text is None:
            print(f"Warning: no cached text for gold page {sc['page']}, skipping")
            continue
        gold_examples.append(_make_example(sc, text))

    split = len(gold_examples) * 7 // 10
    split = max(split, 1)
    gold_train = gold_examples[:split]
    gold_dev = gold_examples[split:]

    # Silver: validation-clean pages (not in gold) for additional evaluation
    issues_by_page = {}
    all_issues = validate(all_scorecards, ALL_CHECKS)
    for issue in all_issues:
        issues_by_page.setdefault(issue["page"], []).append(issue)

    gold_pages = {sc["page"] for sc in gold_scorecards}
    silver_eval = []
    for sc in all_scorecards:
        page = sc["page"]
        if page in gold_pages or page in issues_by_page:
            continue
        text = _load_page_text(page)
        if text is None:
            continue
        silver_eval.append(_make_example(sc, text))

    print(f"Gold train: {len(gold_train)}, Gold dev: {len(gold_dev)}, Silver eval: {len(silver_eval)}")
    return gold_train, gold_dev, silver_eval


# ---------------------------------------------------------------------------
# Optimization
# ---------------------------------------------------------------------------

def optimize(
    model_name: str,
    gold_path: Path,
    scorecards_path: Path,
    output_path: Path,
    num_trials: int = 15,
):
    lm = dspy.LM(
        f"anthropic/{model_name}",
        max_tokens=64000,
        temperature=0.0,
    )
    dspy.configure(lm=lm)

    gold_train, gold_dev, _ = build_datasets(gold_path, scorecards_path)

    if len(gold_train) < 1:
        raise SystemExit("Need at least 1 gold training example")
    if len(gold_dev) < 1:
        raise SystemExit("Need at least 1 gold dev example")

    optimizer = dspy.MIPROv2(
        metric=scorecard_metric,
        auto=None,
        num_candidates=7,
        init_temperature=0.7,
        max_bootstrapped_demos=3,
        max_labeled_demos=3,
        num_threads=1,
    )

    parser = ScorecardParser()
    optimized = optimizer.compile(
        parser, trainset=gold_train, valset=gold_dev, num_trials=num_trials,
        minibatch=False,
    )
    optimized.save(str(output_path))
    print(f"Saved optimized program to {output_path}")
    return optimized


# ---------------------------------------------------------------------------
# Run optimized pipeline
# ---------------------------------------------------------------------------

def run(
    model_name: str,
    optimized_path: Path,
    output_path: Path,
    page_nums: list[int] | None = None,
):
    lm = dspy.LM(
        f"anthropic/{model_name}",
        max_tokens=64000,
        temperature=0.0,
    )
    dspy.configure(lm=lm)

    parser = ScorecardParser()
    parser.load(str(optimized_path))

    if page_nums is None:
        page_nums = sorted(
            int(f.stem) for f in PAGE_CACHE_DIR.glob("*.txt")
        )

    results = []
    errors = 0

    for page_num in page_nums:
        text = _load_page_text(page_num)
        if text is None:
            print(f"  Page {page_num}: no cached text, skipping")
            continue

        print(f"  Page {page_num} …", end=" ", flush=True)
        try:
            prediction = parser(
                scorecard_text=text,
                parsing_instructions=PARSING_INSTRUCTIONS,
            )
            scorecard = json.loads(_extract_json(prediction.structured_json))
            scorecard = normalize_match(scorecard)
            scorecard = normalize_player_names(scorecard)
            scorecard = validate_bowling_wickets(scorecard)
            scorecard["page"] = page_num
            scorecard["source_url"] = page_url(page_num)
            results.append(scorecard)
            match = scorecard.get("match", {})
            print(
                f"{match.get('team1', '?')} v {match.get('team2', '?')} "
                f"({match.get('date', '?')})"
            )
        except Exception as e:
            errors += 1
            print(f"ERROR: {e}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\nDone. {len(results)} parsed, {errors} errors. Output: {output_path}")


# ---------------------------------------------------------------------------
# Evaluate
# ---------------------------------------------------------------------------

def evaluate(
    model_name: str,
    optimized_path: Path,
    gold_path: Path,
    scorecards_path: Path,
):
    lm = dspy.LM(
        f"anthropic/{model_name}",
        max_tokens=64000,
        temperature=0.0,
    )
    dspy.configure(lm=lm)

    parser = ScorecardParser()
    parser.load(str(optimized_path))

    _, gold_dev, silver_eval = build_datasets(gold_path, scorecards_path)

    for label, dataset in [("Gold dev", gold_dev), ("Silver eval", silver_eval[:20])]:
        if not dataset:
            print(f"  {label}: no examples")
            continue

        scores = []
        for ex in dataset:
            prediction = parser(
                scorecard_text=ex.scorecard_text,
                parsing_instructions=ex.parsing_instructions,
            )
            score = scorecard_metric(ex, prediction)
            scores.append(score)
            print(f"  Page {ex.page}: {score:.2f}")

        avg = sum(scores) / len(scores) if scores else 0
        print(f"  {label} avg score: {avg:.3f} ({len(scores)} examples)\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_page_range(spec: str) -> list[int]:
    pages = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            pages.update(range(int(lo), int(hi) + 1))
        else:
            pages.add(int(part))
    return sorted(pages)


def main():
    parser = argparse.ArgumentParser(
        description="DSPy-optimized cricket scorecard extraction."
    )
    parser.add_argument(
        "--optimize",
        action="store_true",
        help="Run MIPROv2 optimization to produce an optimized program.",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Run the optimized pipeline on pages.",
    )
    parser.add_argument(
        "--evaluate",
        action="store_true",
        help="Evaluate the optimized pipeline on gold/silver datasets.",
    )
    parser.add_argument(
        "--model", "-m",
        default="claude-sonnet-4-6",
        help="Model for DSPy (litellm format, default: claude-sonnet-4-6).",
    )
    parser.add_argument(
        "--gold",
        default="malayan_interstate_cricket/gold_scorecards.json",
        help="Path to hand-verified gold scorecards.",
    )
    parser.add_argument(
        "--scorecards",
        default="malayan_interstate_cricket/site/scorecards.json",
        help="Path to full scorecards file (for silver/dirty splits).",
    )
    parser.add_argument(
        "--optimized",
        default="malayan_interstate_cricket/dspy_optimized.json",
        help="Path to save/load the optimized DSPy program.",
    )
    parser.add_argument(
        "--output", "-o",
        default="malayan_interstate_cricket/scorecards_dspy.json",
        help="Output path for --run results.",
    )
    parser.add_argument(
        "--pages",
        default=None,
        help="Page numbers/ranges for --run (e.g. '1,3,5-10'). Default: all cached.",
    )
    parser.add_argument(
        "--trials",
        type=int,
        default=15,
        help="Number of MIPROv2 optimization trials (default: 15).",
    )
    args = parser.parse_args()

    if not (args.optimize or args.run or args.evaluate):
        parser.print_help()
        return

    if args.optimize:
        optimize(
            model_name=args.model,
            gold_path=Path(args.gold),
            scorecards_path=Path(args.scorecards),
            output_path=Path(args.optimized),
            num_trials=args.trials,
        )

    if args.run:
        optimized_path = Path(args.optimized)
        if not optimized_path.exists():
            raise SystemExit(
                f"No optimized program at {optimized_path}. Run --optimize first."
            )
        page_nums = parse_page_range(args.pages) if args.pages else None
        run(
            model_name=args.model,
            optimized_path=optimized_path,
            output_path=Path(args.output),
            page_nums=page_nums,
        )

    if args.evaluate:
        optimized_path = Path(args.optimized)
        if not optimized_path.exists():
            raise SystemExit(
                f"No optimized program at {optimized_path}. Run --optimize first."
            )
        evaluate(
            model_name=args.model,
            optimized_path=optimized_path,
            gold_path=Path(args.gold),
            scorecards_path=Path(args.scorecards),
        )


if __name__ == "__main__":
    main()
