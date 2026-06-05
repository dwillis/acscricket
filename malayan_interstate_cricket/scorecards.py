"""
Malayan Interstate Cricket Scorecard Parser
=============================================
Fetches scorecard pages from the ACS Cricket archive and parses the
text content into structured JSON using an LLM.

Each page at:
  https://archive.acscricket.com/research/rm/malayan_interstate_cricket_1899-1957/
  rm_malayan_interstate_cricket_scorecards/{page}/index.html

contains a plain-text scorecard inside <div id="text-container">.

Usage:
    uv run python malayan_interstate_cricket/scorecards.py
    uv run python malayan_interstate_cricket/scorecards.py --pages 2-5
    uv run python malayan_interstate_cricket/scorecards.py --model gemini-2.5-flash
    uv run python malayan_interstate_cricket/scorecards.py --output scorecards.json
"""

import argparse
import html
import json
import re
import time
from pathlib import Path

import httpx
import llm
from bs4 import BeautifulSoup

BASE_URL = (
    "https://archive.acscricket.com/research/rm/"
    "malayan_interstate_cricket_1899-1957/"
    "rm_malayan_interstate_cricket_scorecards"
)

DEFAULT_MODEL_ID = "gpt-4o-mini"
RATE_LIMIT_DELAY = 1.0
RETRY_ATTEMPTS = 1
RETRY_BACKOFF = 5.0
MAX_PAGE = 355

SYSTEM_PROMPT = """\
You are an expert cricket scorecard parser. You read raw text from historical \
cricket scorecards and extract structured data. Respond ONLY with a JSON object \
— no markdown fences, no prose."""

USER_PROMPT_TEMPLATE = """\
Below is the raw text extracted from a historical cricket scorecard page. \
Parse it into a structured JSON object.

The text is a continuous string with no line breaks. It contains:
- Match header (teams, venue, date, result)
- Batting for each team (1st and optionally 2nd innings, interleaved on same line per batsman)
- Extras and totals per innings
- Bowling figures per innings
- Fall of wickets (fow)
- Match metadata (umpires, toss, close of play, balls per over, notes)

CRITICAL PARSING RULES FOR INTERLEAVED INNINGS:
- Each batsman line contains BOTH their 1st and 2nd innings data on the SAME line
- Example: "SCG Fox lbw b Disbrowe 4 b Neubronner 22" means:
  - 1st innings: lbw b Disbrowe, scored 4
  - 2nd innings: b Neubronner, scored 22
- You MUST create SEPARATE innings objects for each innings
- A typical 2-innings match has 4 innings objects (Team1 1st, Team1 2nd, Team2 1st, Team2 2nd)
- A match where a team batted only once (innings defeat) has 3 innings objects
- The extras line shows two numbers for two innings, e.g. "Extras 3 3" then "55 104" = totals
- "(8 wickets)" after a total means not all out

OTHER RULES:
- Players marked with * are captain, + are wicketkeeper
- Dismissal methods: "b" = bowled, "c" = caught, "c&b" or "c ... b" = caught and bowled, \
"lbw" = leg before wicket, "st" = stumped, "run out" = run out, "ht wkt" = hit wicket, \
"not out" = not out, "retired" = retired
- Numbers after dismissal info are runs scored
- "(N)" before a dismissal in 2nd innings indicates batting position changed from 1st innings
- Bowling columns are O M R W (Overs Maidens Runs Wickets), sometimes with nb (no-balls) and w (wides)
- Bowling figures appear under team headers: "Selangor O M R W" means Selangor's bowlers bowling in that innings
- CRITICAL: The bowling section often has INTERLEAVED columns for multiple innings, \
e.g. "O M R W nb w O M R W nb w" (two innings side by side). The numbers after each \
bowler's name fill these columns left to right. With two innings, a bowler with full data \
has 8+ numbers; a bowler with partial data has fewer.
- SPARSE BOWLING DATA: When a bowler has very few numbers (1-3) relative to the column \
headers, the data is likely INCOMPLETE. In this case:
  - CROSS-CHECK against the batting dismissals to determine wickets. Count how many \
batsmen were dismissed "b <Bowler>", "c ... b <Bowler>", "c&b <Bowler>", "st ... b <Bowler>", \
or "lbw b <Bowler>" in each innings. This gives the true wicket count.
  - If a bowler has only 2 numbers and there are 2 innings columns, those numbers are \
likely wickets per innings (1st, 2nd), NOT overs and maidens.
  - If a bowler has only 1 number, it is likely their wickets for a single innings.
  - Set any column you cannot determine to null rather than guessing.
  - The dismissal-derived wicket count is AUTHORITATIVE — if the numbers in the bowling \
section conflict with the dismissal count, trust the dismissals.
- fow = fall of wickets. The data appears as columns: wicket number, then cumulative run totals \
for each innings. E.g. "fow Sel (1) Pen (1) Sel (2) 1 15 11 4 2 51 93 11" means \
wicket 1 fell at 15 runs in Sel(1), 11 in Pen(1), 4 in Sel(2); wicket 2 at 51, 93, 11 respectively.
- fow numbers are often INTERLEAVED with or adjacent to bowling data. Do not confuse \
fow run totals with bowling figures. fow values tend to be ascending sequences within \
each innings column.
- fow values are CUMULATIVE RUN TOTALS at which each wicket fell, NOT wicket numbers
- fow data may appear interleaved between bowling sections
- Extras include byes (b), leg byes (lb), wides (w), no-balls (nb)
- "[unknown]" means the information is not available
- IGNORE any "Made with FlippingBook" text — do NOT include it in notes

Return a JSON object with this structure:
{{
  "match": {{
    "team1": "string",
    "team2": "string",
    "venue": "string",
    "date": "string (as written)",
    "result": "string"
  }},
  "innings": [
    {{
      "team": "string",
      "innings_number": 1,
      "batting": [
        {{
          "name": "string",
          "captain": true/false,
          "wicketkeeper": true/false,
          "dismissal": "string (e.g. 'c Fielder b Bowler', 'not out', 'b Bowler', 'run out')",
          "runs": number
        }}
      ],
      "extras": {{
        "total": number,
        "detail": "string or null (e.g. 'b 4, lb 2')"
      }},
      "total": {{
        "runs": number,
        "wickets": number or null (null if all out),
        "declared": false
      }},
      "fow": [number] or null,
      "bowling": [
        {{
          "name": "string",
          "overs": "string (e.g. '12.3')",
          "maidens": number,
          "runs": number,
          "wickets": number,
          "noballs": number or null,
          "wides": number or null
        }}
      ]
    }}
  ],
  "umpires": ["string"] or null,
  "toss": "string or null",
  "close_of_play": "string or null",
  "balls_per_over": number or null,
  "notes": ["string"] or null
}}

Rules:
- Include ALL innings (some matches have 1, 2, 3, or 4 innings)
- If fall of wickets data is present (fow), include it as an array of cumulative run totals
- If fow includes partnership details, just extract the run totals
- Extras "total" is the number shown; "detail" is the breakdown if available
- For "total", wickets is null when team is all out (all 10 wickets fell)
- Include umpires as an array, even if only one is known
- Include all notes at the bottom of the scorecard
- Do NOT include navigation text, FlippingBook text, or page numbers
- If a section is not present in the text, use null
- VALIDATE bowling wickets using this TWO-PASS approach:
  PASS 1: Before filling in the bowling section, scan ALL batting dismissals to build \
a wicket tally per bowler per innings. Any dismissal containing "b <Bowler>" credits \
that bowler with a wicket: "b Fox", "c X b Fox", "c&b Fox", "lbw b Fox", "st X b Fox", \
"ht wkt b Fox" all give Fox one wicket. "run out" and "not out" give no bowler a wicket.
  PASS 2: Use the tally from Pass 1 as the AUTHORITATIVE wicket count in the bowling \
section. If the bowling numbers in the text are ambiguous or sparse, set overs/maidens/runs \
to null but ALWAYS set wickets from the dismissal tally — never guess wickets from \
the bowling number stream alone.

RAW SCORECARD TEXT:
{text}"""


def fetch_page_text(client: httpx.Client, page: int) -> str | None:
    url = f"{BASE_URL}/index.html" if page == 1 else f"{BASE_URL}/{page}/index.html"
    try:
        resp = client.get(url, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        print(f"  HTTP error for page {page}: {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    container = soup.find("div", id="text-container")
    if not container:
        return None

    text = container.get_text(separator=" ", strip=True)
    text = html.unescape(text)
    return text if text else None


def detect_max_page(client: httpx.Client) -> int:
    """Try to detect the last page from the nav links on page 2."""
    url = f"{BASE_URL}/2/index.html"
    try:
        resp = client.get(url, follow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        nav = soup.find("div", class_="nav-links")
        if nav:
            links = nav.find_all("a")
            pages = []
            for link in links:
                href = link.get("href", "")
                m = re.search(r"/(\d+)/?$", href)
                if m:
                    pages.append(int(m.group(1)))
            if pages:
                return max(pages)
    except Exception:
        pass
    return MAX_PAGE


def _bowler_from_dismissal(dismissal: str) -> str | None:
    """Extract the bowler's name from a batting dismissal string."""
    d = dismissal.strip()
    if not d or d.lower() in ("not out", "retired"):
        return None
    if "run out" in d.lower():
        return None
    m = re.search(r"\bb\s+(.+)$", d, re.IGNORECASE)
    return m.group(1).strip() if m else None


def _normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip()).lower()


def validate_bowling_wickets(scorecard: dict) -> dict:
    """Patch bowling wickets to match dismissal counts from batting data."""
    for innings in scorecard.get("innings", []):
        tally: dict[str, int] = {}
        for bat in innings.get("batting", []):
            bowler = _bowler_from_dismissal(bat.get("dismissal") or "")
            if bowler:
                key = _normalize_name(bowler)
                tally[key] = tally.get(key, 0) + 1

        for bowl in innings.get("bowling", []):
            if not bowl.get("name"):
                continue
            key = _normalize_name(bowl["name"])
            matched = tally.get(key)
            if matched is None:
                for tally_key, count in tally.items():
                    if tally_key.endswith(key) or key.endswith(tally_key):
                        matched = count
                        break
            bowl["wickets"] = matched if matched is not None else 0

    return scorecard


def _fix_name_markers(name: str) -> tuple[str, bool, bool]:
    """Strip */+ captain/keeper markers, return (clean_name, is_captain, is_keeper)."""
    captain = False
    keeper = False
    n = name
    while n and n[0] in "*+":
        if n[0] == "*":
            captain = True
        else:
            keeper = True
        n = n[1:]
    return n.strip(), captain, keeper


def _fix_initials(name: str) -> str:
    """Normalize initial spacing: 'M T Retnam' -> 'MT Retnam', 'AWWanless' -> 'AW Wanless'."""
    # Spaced initials: "M T Retnam" -> "MT Retnam"
    name = re.sub(r"^([A-Z])\s([A-Z])\s", r"\1\2 ", name)
    # Stuck initials: uppercase-only prefix run into a capitalized surname
    # e.g. "AWWanless" -> "AW Wanless", "SGAMaartensz" -> "SGA Maartensz"
    if " " not in name:
        m = re.match(r"^([A-Z]+)([A-Z][a-z].*)$", name)
        if m:
            name = f"{m.group(1)} {m.group(2)}"
    return name


def normalize_player_names(scorecard: dict) -> dict:
    """Normalize player names within a single scorecard.

    1. Strip */+ markers from names, set captain/keeper flags instead.
    2. Fix initial spacing issues.
    3. Resolve surname-only bowler names using match context.
    """
    # --- Pass 1 & 2: Clean all names (markers + initials) ---
    for innings in scorecard.get("innings", []):
        for bat in innings.get("batting", []):
            if not bat.get("name"):
                continue
            clean, is_capt, is_wk = _fix_name_markers(bat["name"])
            clean = _fix_initials(clean)
            bat["name"] = clean
            if is_capt:
                bat["captain"] = True
            if is_wk:
                bat["wicketkeeper"] = True

        for bowl in innings.get("bowling", []):
            if not bowl.get("name"):
                continue
            clean, _, _ = _fix_name_markers(bowl["name"])
            clean = _fix_initials(clean)
            bowl["name"] = clean

    # Also clean names inside dismissal strings (fielder/bowler references
    # don't need fixing — they're already surname-only in the text)

    # --- Pass 3: Resolve surname-only bowler names ---
    # Build a set of full names from batting across ALL innings in this match
    full_names: dict[str, list[str]] = {}  # surname -> [full names]
    for innings in scorecard.get("innings", []):
        for bat in innings.get("batting", []):
            name = bat.get("name", "")
            if " " in name:
                surname = name.rsplit(" ", 1)[1]
                key = surname.lower()
                if key not in full_names:
                    full_names[key] = []
                if name not in full_names[key]:
                    full_names[key].append(name)

    # For each surname-only bowler, try to resolve
    for innings in scorecard.get("innings", []):
        # Build per-innings batting roster for disambiguation
        innings_batters = set()
        for bat in innings.get("batting", []):
            innings_batters.add(bat.get("name", ""))

        # Also check the opposing innings (bowlers bowl at the OTHER team)
        # The batting team for this bowling section is the team that batted
        # in this innings — the bowlers are from the other team.
        # So we need batters from the SAME innings (to find who the bowler
        # was bowling at) and bowler's teammates from OTHER innings.
        other_batters = set()
        bowling_team = None
        for other_inn in scorecard.get("innings", []):
            if other_inn is not innings:
                for bat in other_inn.get("batting", []):
                    other_batters.add(bat.get("name", ""))

        for bowl in innings.get("bowling", []):
            name = bowl.get("name", "")
            if " " in name:
                continue  # Already a full name
            key = name.lower()
            candidates = full_names.get(key, [])
            if len(candidates) == 1:
                bowl["name"] = candidates[0]
            elif len(candidates) > 1:
                # Disambiguate: the bowler is NOT batting in this innings
                # (they're on the fielding team), so prefer candidates
                # who appear in OTHER innings' batting
                non_batting = [c for c in candidates if c not in innings_batters]
                if len(non_batting) == 1:
                    bowl["name"] = non_batting[0]
                else:
                    # Try: who appears in other innings as a batter?
                    in_other = [c for c in candidates if c in other_batters]
                    if len(in_other) == 1:
                        bowl["name"] = in_other[0]
                    # else: leave as surname — can't disambiguate

    return scorecard


def normalize_match(scorecard: dict) -> dict:
    """Fix common schema variants in the match object."""
    match = scorecard.get("match", {})

    # Handle "teams" field instead of team1/team2
    if "teams" in match and "team1" not in match:
        teams_str = match.pop("teams")
        parts = re.split(r"\s+v(?:s\.?)?\s+", teams_str, maxsplit=1)
        match["team1"] = parts[0].strip() if len(parts) > 0 else "?"
        match["team2"] = parts[1].strip() if len(parts) > 1 else "?"

    return scorecard


def parse_scorecard(model, text: str) -> dict:
    prompt = USER_PROMPT_TEMPLATE.format(text=text)
    response = model.prompt(prompt, system=SYSTEM_PROMPT)
    raw = response.text()
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    scorecard = json.loads(raw)
    scorecard = normalize_match(scorecard)
    scorecard = normalize_player_names(scorecard)
    return validate_bowling_wickets(scorecard)


def parse_page_range(spec: str) -> set[int]:
    pages = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            pages.update(range(int(lo), int(hi) + 1))
        elif part:
            pages.add(int(part))
    return pages


def main():
    parser = argparse.ArgumentParser(
        description="Fetch and parse Malayan Interstate Cricket scorecards."
    )
    parser.add_argument("--model", "-m", default=DEFAULT_MODEL_ID)
    parser.add_argument("--output", "-o", default="malayan_interstate_cricket/scorecards.json")
    parser.add_argument(
        "--pages",
        default=None,
        help="Comma-separated page numbers or ranges, e.g. '1,3,5-10'.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=RATE_LIMIT_DELAY,
        help=f"Delay between LLM calls in seconds (default: {RATE_LIMIT_DELAY})",
    )
    parser.add_argument(
        "-f", "--force",
        action="store_true",
        help="Re-parse pages even if they already exist in the output file.",
    )
    parser.add_argument(
        "--normalize-only",
        action="store_true",
        help="Re-run name normalization on existing data without re-parsing.",
    )
    args = parser.parse_args()

    output_path = Path(args.output)

    existing: dict[int, dict] = {}
    if output_path.exists():
        try:
            data = json.loads(output_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and "page" in item:
                        existing[item["page"]] = item
            elif isinstance(data, dict):
                existing = {int(k): v for k, v in data.items()}
        except Exception:
            pass

    if args.normalize_only:
        if not existing:
            raise SystemExit(f"No data to normalize in {output_path}")
        print(f"Normalizing {len(existing)} scorecards…")
        for page_num, sc in existing.items():
            sc = normalize_match(sc)
            sc = normalize_player_names(sc)
            sc = validate_bowling_wickets(sc)
            existing[page_num] = sc
        _save(output_path, existing)
        print("Done.")
        return

    all_models = {m.model_id: m for m in llm.get_models()}
    if args.model not in all_models:
        raise SystemExit(
            f"Unknown model: {args.model!r}. Run 'llm models' to see available models."
        )
    model = all_models[args.model]

    if existing:
        print(f"Resuming: {len(existing)} page(s) already in {output_path}")

    client = httpx.Client(
        timeout=30,
        headers={"User-Agent": "ACSCricket-Scorecard-Parser/1.0"},
    )

    if args.pages:
        page_nums = sorted(parse_page_range(args.pages))
    else:
        max_page = detect_max_page(client)
        page_nums = list(range(1, max_page + 1))
        print(f"Detected {max_page} pages")

    print(f"Model : {args.model}")
    print(f"Output: {output_path}")
    print(f"Pages : {len(page_nums)}")

    total_parsed = 0
    total_errors = 0

    for page_num in page_nums:
        if page_num in existing and not args.force:
            print(f"  Skipping page {page_num} (already processed)")
            continue

        print(f"  Fetching page {page_num} …", end=" ", flush=True)
        text = fetch_page_text(client, page_num)
        if not text:
            print("no text content")
            continue

        print("parsing …", end=" ", flush=True)
        error = None
        scorecard = None

        for attempt in range(RETRY_ATTEMPTS + 1):
            try:
                scorecard = parse_scorecard(model, text)
                break
            except json.JSONDecodeError as e:
                error = f"JSON parse error: {e}"
                break
            except Exception as e:
                error = str(e)
                if attempt < RETRY_ATTEMPTS:
                    time.sleep(RETRY_BACKOFF)
                    continue
                break

        if scorecard:
            scorecard["page"] = page_num
            scorecard["source_url"] = f"{BASE_URL}/{page_num}/index.html"
            existing[page_num] = scorecard
            total_parsed += 1
            match = scorecard.get("match", {})
            print(
                f"{match.get('team1', '?')} v {match.get('team2', '?')} "
                f"({match.get('date', '?')})"
            )

            if total_parsed % 5 == 0:
                _save(output_path, existing)
        else:
            total_errors += 1
            print(f"ERROR: {error}")

        time.sleep(args.delay)

    _save(output_path, existing)
    client.close()

    print(f"\nDone. {total_parsed} scorecards parsed, {total_errors} errors.")
    print(f"Total scorecards in {output_path}: {len(existing)}")


def _save(path: Path, data: dict[int, dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = [data[k] for k in sorted(data)]
    path.write_text(json.dumps(ordered, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
