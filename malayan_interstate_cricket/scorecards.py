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

--vision parses the scanned page image instead of the flattened HTML text.
The archive's FlippingBook viewer exposes full-resolution page scans at
files/assets/common/page-textlayers/page{NNNN}_1.png (zero-padded to 4
digits), which preserve the original table layout — this avoids the whole
class of column-assignment errors (swapped innings totals, misattributed
bowling figures) that come from reading a 2D scorecard as flattened text:
    uv run python malayan_interstate_cricket/scorecards.py --vision --pages 302
    uv run python malayan_interstate_cricket/scorecards.py --vision --batch --pages 94,106,302
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

from names import dismissal_tally, lookup_tally

BASE_URL = (
    "https://archive.acscricket.com/research/rm/"
    "malayan_interstate_cricket_1899-1957/"
    "rm_malayan_interstate_cricket_scorecards"
)

DEFAULT_MODEL_ID = "claude-sonnet-4.6"
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


VISION_SYSTEM_PROMPT = """\
You are an expert cricket scorecard parser. You read a scanned image of a \
historical cricket scorecard and extract structured data. Respond ONLY with \
a JSON object — no markdown fences, no prose."""

VISION_USER_PROMPT = """\
The attached image is a scan of a historical cricket scorecard page. Its \
layout is a real table: batting entries for one team form a column block, \
extras and totals sit below each innings' batting, and bowling/fall-of-wickets \
appear in a table below with separate columns per innings. Use the visual \
layout to assign each number to the correct innings and column — do not treat \
this as a flattened stream of numbers.

RULES:
- Players marked with * are captain, + are wicketkeeper
- Dismissal methods: "b" = bowled, "c" = caught, "c&b" or "c ... b" = caught \
and bowled, "lbw" = leg before wicket, "st" = stumped, "run out" = run out, \
"ht wkt" = hit wicket, "not out" = not out, "retired" = retired
- A "(N)" before a 2nd-innings dismissal indicates the batting position changed
- Bowling columns are headed O M R W, sometimes with nb (no-balls) and w (wides)
- fall of wickets (fow) values are CUMULATIVE RUN TOTALS at which each wicket \
fell, one ascending sequence per innings — not wicket numbers
- "[unknown]" means the information is not available in the source
- IGNORE any "Made with FlippingBook" text — do NOT include it in notes
- VALIDATE bowling wickets using this TWO-PASS approach:
  PASS 1: Scan every batting dismissal in an innings to build a wicket tally \
per bowler for that innings. Any dismissal containing "b <Bowler>" credits \
that bowler with a wicket: "b Fox", "c X b Fox", "c&b Fox", "lbw b Fox", \
"st X b Fox", "ht wkt b Fox" all give Fox one wicket. "run out" and "not out" \
give no bowler a wicket.
  PASS 2: Use the Pass 1 tally as the AUTHORITATIVE wicket count in the \
bowling section. If the image's bowling figures are faint or ambiguous, set \
overs/maidens/runs to null but ALWAYS set wickets from the dismissal tally.

Return a JSON object with this structure:
{
  "match": {
    "team1": "string",
    "team2": "string",
    "venue": "string",
    "date": "string (as written)",
    "result": "string"
  },
  "innings": [
    {
      "team": "string",
      "innings_number": 1,
      "batting": [
        {
          "name": "string",
          "captain": true/false,
          "wicketkeeper": true/false,
          "dismissal": "string (e.g. 'c Fielder b Bowler', 'not out', 'b Bowler', 'run out')",
          "runs": number
        }
      ],
      "extras": {
        "total": number,
        "detail": "string or null (e.g. 'b 4, lb 2')"
      },
      "total": {
        "runs": number,
        "wickets": number or null (null if all out),
        "declared": false
      },
      "fow": [number] or null,
      "bowling": [
        {
          "name": "string",
          "overs": "string (e.g. '12.3')",
          "maidens": number,
          "runs": number,
          "wickets": number,
          "noballs": number or null,
          "wides": number or null
        }
      ]
    }
  ],
  "umpires": ["string"] or null,
  "toss": "string or null",
  "close_of_play": "string or null",
  "balls_per_over": number or null,
  "notes": ["string"] or null
}

Rules:
- Include ALL innings (some matches have 1, 2, 3, or 4 innings)
- Extras "total" is the number shown; "detail" is the breakdown if available
- For "total", wickets is null when team is all out (all 10 wickets fell)
- Include umpires as an array, even if only one is known
- Include all notes at the bottom of the scorecard
- Do NOT include navigation text, FlippingBook text, or page numbers
- If a section is not present in the image, use null"""


PAGE_CACHE_DIR = Path(__file__).parent / "pages"
PAGE_IMAGE_CACHE_DIR = Path(__file__).parent / "page_images"

IMAGE_ASSET_BASE = (
    "https://archive.acscricket.com/research/rm/"
    "malayan_interstate_cricket_1899-1957/"
    "rm_malayan_interstate_cricket_scorecards/files/assets/"
    "common/page-textlayers"
)


def page_url(page: int) -> str:
    return f"{BASE_URL}/index.html" if page == 1 else f"{BASE_URL}/{page}/index.html"


def page_image_url(page: int) -> str:
    return f"{IMAGE_ASSET_BASE}/page{page:04d}_1.png"


def fetch_page_image(client: httpx.Client, page: int) -> bytes | None:
    """Fetch (or read cached) full-resolution scorecard image for a page.

    These are the FlippingBook text-layer PNGs, which preserve the original
    tabular layout that the flattened HTML text loses — batting columns,
    interleaved-innings bowling tables, and fall-of-wickets grids.
    """
    cache_file = PAGE_IMAGE_CACHE_DIR / f"{page}.png"
    if cache_file.exists():
        data = cache_file.read_bytes()
        return data if data else None

    try:
        resp = client.get(page_image_url(page), follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        print(f"  HTTP error fetching image for page {page}: {e}")
        return None

    data = resp.content
    if data:
        PAGE_IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(data)
    return data if data else None


def fetch_page_text(client: httpx.Client, page: int) -> str | None:
    cache_file = PAGE_CACHE_DIR / f"{page}.txt"
    if cache_file.exists():
        text = cache_file.read_text(encoding="utf-8")
        return text if text else None

    try:
        resp = client.get(page_url(page), follow_redirects=True)
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
    if text:
        PAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(text, encoding="utf-8")
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


def validate_bowling_wickets(scorecard: dict) -> dict:
    """Patch bowling wickets to match dismissal counts from batting data.

    Only patches when the dismissal evidence is unambiguous. A bowler with
    no tally entry gets 0 only when every dismissal in the innings is known;
    otherwise the parsed value is left alone (validation will flag any
    disagreement). The model's original value is preserved as
    `wickets_reported` when overwritten.
    """
    for innings in scorecard.get("innings") or []:
        batting = innings.get("batting") or []
        tally = dismissal_tally(batting)
        dismissals_complete = bool(batting) and all(
            (b.get("dismissal") or "").strip().lower() not in ("", "[unknown]", "unknown")
            for b in batting
        )

        for bowl in innings.get("bowling") or []:
            if not bowl.get("name"):
                continue
            matched = lookup_tally(tally, bowl["name"])
            if matched is None:
                # Absent or ambiguous-surname tally entry: only treat as 0
                # when we saw every dismissal and the surname isn't ambiguous
                key_surname = bowl["name"].rsplit(" ", 1)[-1].lower()
                ambiguous = sum(
                    1 for k in tally if k.rsplit(" ", 1)[-1] == key_surname
                ) > 1
                if ambiguous or not dismissals_complete:
                    continue
                matched = 0
            if bowl.get("wickets") != matched:
                if bowl.get("wickets") is not None:
                    bowl["wickets_reported"] = bowl["wickets"]
                bowl["wickets"] = matched

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
    for innings in scorecard.get("innings") or []:
        for bat in innings.get("batting") or []:
            if not bat.get("name"):
                continue
            clean, is_capt, is_wk = _fix_name_markers(bat["name"])
            clean = _fix_initials(clean)
            bat["name"] = clean
            if is_capt:
                bat["captain"] = True
            if is_wk:
                bat["wicketkeeper"] = True

        for bowl in innings.get("bowling") or []:
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
    for innings in scorecard.get("innings") or []:
        for bat in innings.get("batting") or []:
            name = bat.get("name", "")
            if " " in name:
                surname = name.rsplit(" ", 1)[1]
                key = surname.lower()
                if key not in full_names:
                    full_names[key] = []
                if name not in full_names[key]:
                    full_names[key].append(name)

    # For each surname-only bowler, try to resolve
    for innings in scorecard.get("innings") or []:
        # Build per-innings batting roster for disambiguation
        innings_batters = set()
        for bat in innings.get("batting") or []:
            innings_batters.add(bat.get("name", ""))

        # Also check the opposing innings (bowlers bowl at the OTHER team)
        # The batting team for this bowling section is the team that batted
        # in this innings — the bowlers are from the other team.
        # So we need batters from the SAME innings (to find who the bowler
        # was bowling at) and bowler's teammates from OTHER innings.
        other_batters = set()
        bowling_team = None
        for other_inn in scorecard.get("innings") or []:
            if other_inn is not innings:
                for bat in other_inn.get("batting") or []:
                    other_batters.add(bat.get("name", ""))

        for bowl in innings.get("bowling") or []:
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


def _top_level_objects(raw: str) -> list[str]:
    """Find complete top-level {...} objects via brace-matching (string- and
    escape-aware), so embedded braces inside quoted strings don't confuse
    depth tracking."""
    objs = []
    depth = 0
    start = None
    in_string = False
    escape = False
    for i, ch in enumerate(raw):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                objs.append(raw[start : i + 1])
                start = None
    return objs


def _extract_json(raw: str) -> str:
    """Pull the JSON object out of a model response that may include prose
    before/after it, wrap it in a markdown fence, or (occasionally, in
    vision mode) contain several full re-attempts in sequence — in which
    case the last complete object is the model's final answer."""
    raw = raw.strip()
    # Prefer fenced ```json blocks anywhere in the response; take the last
    fences = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if fences:
        return fences[-1]
    objs = _top_level_objects(raw)
    if objs:
        return objs[-1]
    # Fallback: everything from the first { to the last }
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        return raw[start : end + 1]
    return raw


def parse_scorecard(model, text: str) -> dict:
    prompt = USER_PROMPT_TEMPLATE.format(text=text)
    response = model.prompt(prompt, system=SYSTEM_PROMPT, thinking=False)
    scorecard = json.loads(_extract_json(response.text()))
    scorecard = normalize_match(scorecard)
    scorecard = normalize_player_names(scorecard)
    return validate_bowling_wickets(scorecard)


def parse_scorecard_vision(model, image_bytes: bytes) -> dict:
    attachment = llm.Attachment(content=image_bytes, type="image/png")
    response = model.prompt(
        VISION_USER_PROMPT,
        attachments=[attachment],
        system=VISION_SYSTEM_PROMPT,
        thinking=False,
    )
    scorecard = json.loads(_extract_json(response.text()))
    scorecard = normalize_match(scorecard)
    scorecard = normalize_player_names(scorecard)
    return validate_bowling_wickets(scorecard)


def _anthropic_model_id(model_arg: str) -> str:
    """Convert an llm model alias/ID to a raw Anthropic API model ID.

    e.g. 'claude-sonnet-4.6'  -> 'claude-sonnet-4-6'
         'anthropic/claude-3-5-haiku-latest' -> 'claude-3-5-haiku-latest'
    """
    mid = model_arg
    if mid.startswith("anthropic/"):
        mid = mid[len("anthropic/"):]
    # llm uses dots in version aliases (4.6); Anthropic API uses hyphens (4-6)
    mid = re.sub(r"(\d+)\.(\d+)", r"\1-\2", mid)
    return mid


def _process_batch_text(raw_text: str) -> dict:
    """Parse a batch result text string into a normalised scorecard dict."""
    scorecard = json.loads(_extract_json(raw_text))
    scorecard = normalize_match(scorecard)
    scorecard = normalize_player_names(scorecard)
    return validate_bowling_wickets(scorecard)


def _batch_collect_results(
    client,
    batch_id: str,
    existing: dict[int, dict],
    output_path: Path,
) -> tuple[int, int]:
    """Stream batch results and merge into *existing*. Returns (parsed, errors)."""
    total_parsed = 0
    failed_pages: list[int] = []
    failures_dir = output_path.parent / "failures"

    def _dump_failure(page_num: int, raw_text: str):
        failures_dir.mkdir(parents=True, exist_ok=True)
        (failures_dir / f"page-{page_num}.txt").write_text(raw_text, encoding="utf-8")

    for result in client.messages.batches.results(batch_id):
        page_num = int(result.custom_id.split("-", 1)[1])
        if result.result.type == "succeeded":
            message = result.result.message
            raw_text = message.content[0].text if message.content else ""
            if message.stop_reason == "max_tokens":
                failed_pages.append(page_num)
                _dump_failure(page_num, raw_text)
                print(
                    f"  page {page_num}: TRUNCATED at max_tokens "
                    f"({message.usage.output_tokens} output tokens) — raw saved"
                )
                continue
            try:
                scorecard = _process_batch_text(raw_text)
                scorecard["page"] = page_num
                scorecard["source_url"] = page_url(page_num)
                existing[page_num] = scorecard
                total_parsed += 1
                match = scorecard.get("match", {})
                print(
                    f"  page {page_num}: "
                    f"{match.get('team1', '?')} v {match.get('team2', '?')} "
                    f"({match.get('date', '?')})"
                )
            except Exception as e:
                failed_pages.append(page_num)
                _dump_failure(page_num, raw_text)
                print(f"  page {page_num}: parse error: {e} — raw saved")
        else:
            failed_pages.append(page_num)
            print(f"  page {page_num}: {result.result.type}")
    _save(output_path, existing)

    if failed_pages:
        pages_arg = ",".join(str(p) for p in sorted(failed_pages))
        print(f"\n{len(failed_pages)} page(s) failed; raw responses in {failures_dir}/")
        print(f"Retry with: --batch --pages {pages_arg}")
    return total_parsed, len(failed_pages)


def _poll_batch(client, batch_id: str, poll_interval: int) -> None:
    """Block until the batch reaches 'ended' status, printing progress."""
    while True:
        batch = client.messages.batches.retrieve(batch_id)
        counts = batch.request_counts
        print(
            f"  {batch.processing_status}: "
            f"processing={counts.processing} "
            f"succeeded={counts.succeeded} "
            f"errored={counts.errored}"
        )
        if batch.processing_status == "ended":
            break
        time.sleep(poll_interval)


def run_batch(
    model_arg: str,
    page_nums: list[int],
    existing: dict[int, dict],
    output_path: Path,
    http_client: httpx.Client,
    force: bool = False,
    poll_interval: int = 60,
    vision: bool = False,
) -> None:
    """Fetch pages, submit as an Anthropic Message Batch, poll, and collect."""
    try:
        import anthropic as _anthropic
        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
        from anthropic.types.messages.batch_create_params import Request as BatchRequest
    except ImportError:
        raise SystemExit(
            "anthropic package not found. "
            "Install it with: pip install anthropic"
        )

    api_key = llm.get_key("", "anthropic", "ANTHROPIC_API_KEY")
    client = _anthropic.Anthropic(api_key=api_key)
    api_model_id = _anthropic_model_id(model_arg)

    pages_to_fetch = [
        p for p in page_nums if force or p not in existing
    ]
    requests: list = []

    if vision:
        import base64

        print(f"Fetching images for {len(pages_to_fetch)} page(s)…")
        for page_num in pages_to_fetch:
            image_bytes = fetch_page_image(http_client, page_num)
            if not image_bytes:
                print(f"  page {page_num}: no image content — skipping")
                continue
            requests.append(
                BatchRequest(
                    custom_id=f"page-{page_num}",
                    params=MessageCreateParamsNonStreaming(
                        model=api_model_id,
                        max_tokens=32000,
                        system=VISION_SYSTEM_PROMPT,
                        messages=[{
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": VISION_USER_PROMPT,
                                    "cache_control": {"type": "ephemeral"},
                                },
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/png",
                                        "data": base64.b64encode(image_bytes).decode("ascii"),
                                    },
                                },
                            ],
                        }],
                    ),
                )
            )
    else:
        print(f"Fetching text for {len(pages_to_fetch)} page(s)…")
        for page_num in pages_to_fetch:
            text = fetch_page_text(http_client, page_num)
            if not text:
                print(f"  page {page_num}: no text content — skipping")
                continue
            # Split the prompt so the ~2.5k-token instruction prefix is shared
            # and cacheable across all requests; only the scorecard text varies.
            instructions = USER_PROMPT_TEMPLATE.split("{text}")[0]
            requests.append(
                BatchRequest(
                    custom_id=f"page-{page_num}",
                    params=MessageCreateParamsNonStreaming(
                        model=api_model_id,
                        max_tokens=32000,
                        system=SYSTEM_PROMPT,
                        messages=[{
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": instructions,
                                    "cache_control": {"type": "ephemeral"},
                                },
                                {"type": "text", "text": text},
                            ],
                        }],
                    ),
                )
            )

    if not requests:
        print("Nothing to submit.")
        return

    print(f"Submitting batch of {len(requests)} request(s) (model: {api_model_id})…")
    batch = client.messages.batches.create(requests=requests)
    print(f"Batch ID : {batch.id}")
    print(f"(Resume with --batch-id {batch.id} if interrupted)")

    _poll_batch(client, batch.id, poll_interval)

    total_parsed, total_errors = _batch_collect_results(
        client, batch.id, existing, output_path
    )
    print(f"\nDone. {total_parsed} scorecards parsed, {total_errors} errors.")
    print(f"Total scorecards in {output_path}: {len(existing)}")


def resume_batch(
    batch_id: str,
    existing: dict[int, dict],
    output_path: Path,
    poll_interval: int = 60,
) -> None:
    """Poll and collect results for an already-submitted batch."""
    try:
        import anthropic as _anthropic
    except ImportError:
        raise SystemExit(
            "anthropic package not found. "
            "Install it with: pip install anthropic"
        )

    client = _anthropic.Anthropic(api_key=llm.get_key("", "anthropic", "ANTHROPIC_API_KEY"))
    print(f"Polling batch {batch_id}…")
    _poll_batch(client, batch_id, poll_interval)

    total_parsed, total_errors = _batch_collect_results(
        client, batch_id, existing, output_path
    )
    print(f"\nDone. {total_parsed} scorecards parsed, {total_errors} errors.")
    print(f"Total scorecards in {output_path}: {len(existing)}")


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
    parser.add_argument(
        "--fetch-only",
        action="store_true",
        help="Fetch and cache page texts without parsing. Useful for populating the cache.",
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help="Submit all pages as an Anthropic Message Batch (50%% cost, async).",
    )
    parser.add_argument(
        "--vision",
        action="store_true",
        help="Parse from the scanned page image instead of flattened HTML text. "
        "Preserves table layout for interleaved innings/bowling/fow.",
    )
    parser.add_argument(
        "--batch-id",
        metavar="BATCH_ID",
        default=None,
        help="Poll and collect results for an already-submitted batch ID.",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=60,
        metavar="SECONDS",
        help="Seconds between batch status polls (default: 60).",
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

    if args.fetch_only:
        http_client = httpx.Client(
            timeout=30,
            headers={"User-Agent": "ACSCricket-Scorecard-Parser/1.0"},
        )
        if args.pages:
            page_nums = sorted(parse_page_range(args.pages))
        else:
            max_page = detect_max_page(http_client)
            page_nums = list(range(1, max_page + 1))
        cached = 0
        fetched = 0
        for page_num in page_nums:
            cache_file = PAGE_CACHE_DIR / f"{page_num}.txt"
            if cache_file.exists():
                cached += 1
                continue
            text = fetch_page_text(http_client, page_num)
            if text:
                fetched += 1
                print(f"  Fetched page {page_num}")
            else:
                print(f"  No content for page {page_num}")
            time.sleep(0.5)
        http_client.close()
        print(f"Done. {fetched} fetched, {cached} already cached.")
        return

    # --batch-id: resume an existing batch — no model or page fetching needed
    if args.batch_id:
        if existing:
            print(f"Resuming: {len(existing)} page(s) already in {output_path}")
        resume_batch(args.batch_id, existing, output_path, args.poll_interval)
        return

    try:
        model = llm.get_model(args.model)
    except llm.UnknownModelError:
        raise SystemExit(
            f"Unknown model: {args.model!r}. Run 'llm models' to see available models."
        )

    if existing:
        print(f"Resuming: {len(existing)} page(s) already in {output_path}")

    http_client = httpx.Client(
        timeout=30,
        headers={"User-Agent": "ACSCricket-Scorecard-Parser/1.0"},
    )

    if args.pages:
        page_nums = sorted(parse_page_range(args.pages))
    else:
        max_page = detect_max_page(http_client)
        page_nums = list(range(1, max_page + 1))
        print(f"Detected {max_page} pages")

    # --batch: submit all pages as an Anthropic Message Batch
    if args.batch:
        print(f"Output: {output_path}")
        run_batch(
            args.model,
            page_nums,
            existing,
            output_path,
            http_client,
            force=args.force,
            poll_interval=args.poll_interval,
            vision=args.vision,
        )
        http_client.close()
        return

    print(f"Model : {args.model}")
    print(f"Output: {output_path}")
    print(f"Pages : {len(page_nums)}")
    if args.vision:
        print("Mode  : vision (scanned page image)")

    total_parsed = 0
    total_errors = 0

    for page_num in page_nums:
        if page_num in existing and not args.force:
            print(f"  Skipping page {page_num} (already processed)")
            continue

        if args.vision:
            print(f"  Fetching image for page {page_num} …", end=" ", flush=True)
            image_bytes = fetch_page_image(http_client, page_num)
            if not image_bytes:
                print("no image content")
                continue
        else:
            print(f"  Fetching page {page_num} …", end=" ", flush=True)
            text = fetch_page_text(http_client, page_num)
            if not text:
                print("no text content")
                continue

        print("parsing …", end=" ", flush=True)
        error = None
        scorecard = None

        for attempt in range(RETRY_ATTEMPTS + 1):
            try:
                if args.vision:
                    scorecard = parse_scorecard_vision(model, image_bytes)
                else:
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
            scorecard["source_url"] = page_url(page_num)
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
    http_client.close()

    print(f"\nDone. {total_parsed} scorecards parsed, {total_errors} errors.")
    print(f"Total scorecards in {output_path}: {len(existing)}")


def _save(path: Path, data: dict[int, dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = [data[k] for k in sorted(data)]
    path.write_text(json.dumps(ordered, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
