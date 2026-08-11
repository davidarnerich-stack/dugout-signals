"""Shared utilities used by all parsers."""
import csv
import io
import re
from datetime import datetime

SEASON    = "Summer 2026"

MONTH_MAP = {
    "January":1,"February":2,"March":3,"April":4,"May":5,"June":6,
    "July":7,"August":8,"September":9,"October":10,"November":11,"December":12,
    "Jan":1,"Feb":2,"Mar":3,"Apr":4,"Jun":6,"Jul":7,"Aug":8,
    "Sep":9,"Oct":10,"Nov":11,"Dec":12,
}


def parse_date_from_gc_filename(filename: str):
    """
    Extract game date from a GameChanger box score filename.
    Handles formats like: ..._Jun_6_2026.pdf  or  ..._May_31_2026.pdf
    """
    m = re.search(
        r"_(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)_(\d{1,2})_(\d{4})",
        filename
    )
    if m:
        month = MONTH_MAP.get(m.group(1), 0)
        day   = int(m.group(2))
        year  = int(m.group(3))
        if month:
            return f"{year:04d}-{month:02d}-{day:02d}"
    return None


# ── File type detection by content ────────────────────────────────────────────

def detect_file_type(filename: str, file_bytes: bytes) -> str:
    """
    Return 'stats', 'box_score', or 'play_by_play' by inspecting file content.
    Never relies on the filename convention.
    """
    name = filename.lower()

    if name.endswith(".pdf"):
        # Try content-based detection first. BATTING and PITCHING aren't
        # always on the same page — a full 12+ batter roster can push
        # PITCHING onto page 2 — so check across all pages, not just page 1.
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                text = "\n".join(page.extract_text() or "" for page in pdf.pages)
            if "BATTING" in text and "PITCHING" in text:
                return "box_score"
        except Exception:
            pass
        # Filename fallback: GameChanger box score filenames always contain "_vs_"
        # e.g. Storm12uSilverAllStar_vs_Ventura12UAllStars26SilverAlonzo_Jun_6_2026.pdf
        if "_vs_" in filename:
            return "box_score"
        return "unknown"

    if name.endswith(".csv"):
        try:
            text = file_bytes.decode("utf-8-sig")
            reader = csv.reader(io.StringIO(text))
            rows   = [r for r in reader if any(r)]
            # Row 1 (0-indexed) should be the column header row with 150+ columns
            if len(rows) > 1 and len(rows[1]) > 100:
                hdr = rows[1]
                if "Number" in hdr[:5] and "Last" in hdr[:5] and "AVG" in hdr[:10]:
                    return "stats"
        except Exception:
            pass
        return "unknown"

    if name.endswith(".docx"):
        return "play_by_play"   # legacy DOCX support

    return "unknown"


def team_name_regex(team_name: str) -> str:
    """
    Build a whitespace-tolerant, trailing-s-tolerant regex pattern that matches
    a team's full name the way GameChanger tends to print it (e.g. a coach-
    entered "...All Stars" vs. GameChanger's own "...All Star"). Shared by
    box score opponent/score detection and play-by-play batting-team detection
    — anywhere we need to recognize "is this text talking about our team?"
    without hardcoding any specific team's name.
    """
    words = team_name.split()
    if not words:
        return ""
    last = words[-1]
    words[-1] = (re.escape(last[:-1]) + "s?") if last.endswith("s") else (re.escape(last) + "s?")
    return r"\s+".join(re.escape(w) if i < len(words) - 1 else w for i, w in enumerate(words))


# ── Game info extraction from box score PDF ───────────────────────────────────

def parse_title_names(page) -> dict | None:
    """
    Read both team names out of the box score title using word coordinates
    (DS-93). Returns {"left": str, "right": str} or None.

    The flattened text stream cannot be used for this. When both names wrap
    to two lines the extractor emits them row by row, interleaving them:

        PSW Summer 26   TBD- 08/05/26, 7:00
        7 - 8
        Hilighters      PM

    Reading "the text after the score" then yields "Hilighters PM" — the
    second line of both names concatenated, half ours and half theirs.

    Spatially it is unambiguous: the score sits between two clean columns of
    words. Cluster the title words either side of it and read each column in
    reading order, and the full names come back intact.

    Left is the visiting team, right the home team — the same convention the
    line-score rows follow. Which one is ours comes from the venue marker,
    not from matching any name.
    """
    try:
        words = page.extract_words(x_tolerance=2, y_tolerance=2)
    except Exception:
        return None
    if not words:
        return None

    # The venue marker ends the title area.
    venue_top = next((w["top"] for w in words
                      if w["text"].strip(" .,") in ("Away", "Home")), None)
    if venue_top is None:
        return None
    title = [w for w in words if w["top"] < venue_top - 1]
    if not title:
        return None

    # The score block: a hyphen flanked by the two run totals. Anchor on the
    # digits immediately either side of the hyphen rather than the whole row
    # — a team name can share the score's row (it does on Storm's box
    # scores), and taking the row's full extent would swallow that name and
    # leave one side empty.
    hyphen = next((w for w in title if w["text"].strip() in ("-", "–", "—")), None)
    if hyphen is None:
        return None
    same_row = [w for w in title if abs(w["top"] - hyphen["top"]) < 5]
    before = [w for w in same_row if w["x1"] <= hyphen["x0"] and w["text"].strip().isdigit()]
    after  = [w for w in same_row if w["x0"] >= hyphen["x1"] and w["text"].strip().isdigit()]
    if not before or not after:
        return None
    score_left  = max(before, key=lambda w: w["x1"])["x0"]   # nearest digits left
    score_right = min(after,  key=lambda w: w["x0"])["x1"]   # nearest digits right

    def render(cluster):
        # Reading order: row by row, left to right within a row.
        ordered = sorted(cluster, key=lambda w: (round(w["top"] / 3), w["x0"]))
        return " ".join(w["text"] for w in ordered).strip()

    left  = render([w for w in title if w["x1"] <= score_left])
    right = render([w for w in title if w["x0"] >= score_right])
    if not left or not right:
        return None
    return {"left": left, "right": right}


def parse_line_score(raw: str) -> dict | None:
    """
    Parse the full line-score grid from box score PDF text — the official,
    authoritative record of runs per inning (DS-91).

    Runs per inning must come from here and not from play-by-play. Youth
    games are shortened by mercy rules (a per-inning run cap), time limits,
    and drop-dead innings that revert to the last complete inning; none of
    those appear in the play-by-play stream, and scorekeepers additionally
    issue mid-game corrections. The box score already accounts for all of it.

    Row order is the visiting team first, home team second — verified across
    both home and away games. That ordering, combined with the venue marker
    parsed separately, is what identifies which row is ours; no team-name
    matching is involved.

    Cells are ints, or the literal "X" where a team did not bat (a home team
    ahead after the top of the final inning). "X" is preserved in place so
    later innings are not shifted.

    Returns {innings, rows: [visitor, home], consistent} or None when the
    grid can't be parsed. `consistent` is False when a row's cells don't sum
    to its R — a signal the grid should not be trusted or rendered.
    """
    innings: list[str] = []
    rows: list[dict] = []
    in_score = False

    for line in raw.splitlines():
        s = line.strip()
        if not in_score:
            # Header row: "1 2 3 4 R H E"
            if re.match(r"^1\s+2\s+3", s):
                toks = s.split()
                if "R" in toks:
                    innings = toks[:toks.index("R")]
                    in_score = True
            continue

        if not s or s.startswith("BATTING"):
            break
        # Data row: "PSWS 0 4 3 0 7 5 1"  /  "PSWS 1 0 5 0 X 6 0 1"
        m = re.match(r"^([A-Z0-9]{2,6})\s+([\dX\s]+)$", s)
        if not m:
            continue
        toks = m.group(2).split()
        if len(toks) < 4:          # need at least one inning plus R/H/E
            continue
        cells_raw, rhe = toks[:-3], toks[-3:]
        cells = [t if t.upper() == "X" else int(t) for t in cells_raw]
        rows.append({
            "abbr":  m.group(1),
            "cells": cells,
            "r": int(rhe[0]) if rhe[0].isdigit() else None,
            "h": int(rhe[1]) if rhe[1].isdigit() else None,
            "e": int(rhe[2]) if rhe[2].isdigit() else None,
        })

    if len(rows) < 2 or not innings:
        return None

    rows = rows[:2]
    # Pad/trim so every row lines up with the inning header.
    for row in rows:
        row["cells"] = (row["cells"] + [""] * len(innings))[:len(innings)]

    consistent = all(
        row["r"] is not None
        and sum(c for c in row["cells"] if isinstance(c, int)) == row["r"]
        for row in rows
    )
    return {"innings": innings, "rows": rows, "consistent": consistent}


def parse_score_and_opponent(raw: str, title_names: dict | None = None) -> dict:
    """
    Parse the line score and opponent name from box score PDF text.

    Team identity here is entirely positional — the venue marker says
    whether we were the visiting team, and the visiting team is always the
    first line-score row and the left-hand title name. The coach's own team
    name is deliberately not used: GameChanger's name for a team routinely
    differs from what the coach entered at onboarding, and matching on it
    failed silently rather than loudly (DS-92, DS-93).

    `title_names` comes from parse_title_names() and is strongly preferred
    for the opponent, since it is the only source that survives a team name
    wrapping to two lines.

    Returns: {opponent_name, our_runs, opponent_runs, is_away, line_score}
    """
    is_away = bool(re.search(r"\bAway\b", raw[:400]))

    # Runs come from the full line score — one parser, one source of truth.
    # The visiting team's row is always first; the venue marker above says
    # whether that row is ours. No team-name matching involved.
    line_score = parse_line_score(raw)
    our_runs = opp_runs = None
    if line_score:
        visitor_r = line_score["rows"][0]["r"]
        home_r    = line_score["rows"][1]["r"]
        our_runs, opp_runs = (visitor_r, home_r) if is_away else (home_r, visitor_r)

    # Opponent name. The header reads "{Team A} {score} - {score} {Team B}
    # {Home|Away}" — but pdfplumber's text extraction can interleave a
    # wrapped second line of one team's name (e.g. "AllStars" wrapping
    # under "Sierra Madre 12u Gold") to AFTER the other team's name in the
    # raw text stream, not where it visually reads. So a name can arrive
    # in two separate fragments, one on each side of whichever team isn't
    # split. Two strategies, tried in order:
    header = " ".join(raw[:500].split())
    opponent_name = None

    # 1. Preferred: names read from the title by word coordinates, which is
    #    the only approach that survives a name wrapping to two lines
    #    (DS-93). Left is the visiting team; the venue marker says which
    #    side is ours. No name matching involved.
    if title_names:
        # Taken verbatim. GameChanger's "TBD- 08/05/26, 7:00 PM" placeholder
        # must be stored exactly as printed so it can be recognised later
        # (DS-95) and shown back as evidence.
        opponent_name = (title_names["right"] if is_away else title_names["left"]).strip()

    # 2. Fall back to pure position in the flattened text: the visiting team
    #    is named first (same convention as the line-score row order above).
    #    Cannot recombine a wrapped name, so it is a last resort only.
    if not opponent_name:
        header_m = re.match(r"^(.+?)\s+\d+\s*-\s*\d+\s+(.+?)\s*(?:Away|Home)\b", header)
        if header_m:
            team_a, team_b = header_m.group(1).strip(), header_m.group(2).strip()
            opponent_name = team_b if is_away else team_a
            # Only the flattened-text path needs this: extraction there can
            # leave a stray space inside a hyphenated name. Never applied to
            # coordinate-read names, which come back already intact.
            opponent_name = re.sub(r"-\s+", "-", opponent_name).strip()

    return {
        "opponent_name": opponent_name,
        "our_runs":      our_runs,
        "opponent_runs": opp_runs,
        "is_away":       is_away,
        "line_score":    line_score,
    }


def extract_game_info_from_pdf(file_bytes: bytes, filename: str = "", team_name: str = "") -> dict:
    """
    Parse a box score PDF and return game metadata — no filename needed.
    Returns: {game_date, opponent_name, storm_runs, opponent_runs, is_away,
              line_score}
    """
    import pdfplumber

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        raw = "\n".join(page.extract_text() or "" for page in pdf.pages)
        # Title names need word coordinates, so they must be read while the
        # document is still open (DS-93).
        title_names = parse_title_names(pdf.pages[0]) if pdf.pages else None

    # Game date: "Sunday May 31, 2026" or "Saturday May 16, 2026"
    game_date = None
    date_m = re.search(
        r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+"
        r"(\w+)\s+(\d{1,2}),?\s+(\d{4})",
        raw
    )
    if date_m:
        try:
            month = MONTH_MAP.get(date_m.group(1), 0)
            day   = int(date_m.group(2))
            year  = int(date_m.group(3))
            if month:
                game_date = f"{year:04d}-{month:02d}-{day:02d}"
        except Exception:
            pass

    score_info = parse_score_and_opponent(raw, title_names)

    # Fallback: parse date from GC filename if content extraction failed
    if not game_date and filename:
        game_date = parse_date_from_gc_filename(filename)

    return {
        "game_date":     game_date,
        "opponent_name": score_info["opponent_name"],
        "storm_runs":    score_info["our_runs"],
        "opponent_runs": score_info["opponent_runs"],
        "is_away":       score_info["is_away"],
        "line_score":    score_info["line_score"],
    }


# ── Game record helpers ───────────────────────────────────────────────────────

def resolve_game(sb, team_id: str, game_date: str, *, is_away=None,
                 team_runs=None, opponent_runs=None, line_score=None,
                 opponent_name=None):
    """
    Find the stored game this upload refers to, or None if it is a new one
    (DS-100). The single identity decision — both the write path and the
    overwrite check call this, so the preview cannot disagree with the write.

    Identity used to be (game_date, opponent_name, team_id). `opponent_name`
    is *parsed*, so improving the parser silently changed it: DS-93 corrected
    "TBD-07/15/26, 7:00 PSW Summer 26" to "TBD- 07/15/26, 7:00 PM", both
    lookups missed together, a duplicate game was inserted, and the overwrite
    warning never fired. Anything a parser produces or a coach can edit is
    unsafe as a key.

    So candidates are narrowed by the two facts that cannot drift — the team
    and the date — and then chosen between using observed facts, strongest
    first:

      1. an identical line score. Inning-by-inning runs for both teams
         matching exactly is conclusive.
      2. the same final score and venue.
      3. the same opponent name. Kept last so existing games still match on a
         re-upload that predates the newer evidence.

    When nothing matches, this returns None and the caller creates a second
    game. That is deliberate. The tempting shortcut — "one game already
    exists on this date, so it must be that one" — silently merges the two
    halves of a doubleheader into a single record, destroying one of them.
    A surplus game is visible in the reports list and can be deleted; a
    merge cannot be undone or even noticed.
    """
    resp = (
        sb.table("games")
        .select("game_id, opponent_name, is_away, team_runs, opponent_runs, line_score")
        .eq("team_id", team_id).eq("game_date", game_date)
        .execute()
    )
    candidates = resp.data or []
    if not candidates:
        return None

    def _cells(ls):
        """Comparable shape for a line score, ignoring how it was stored."""
        if isinstance(ls, str):
            import json as _json
            try:
                ls = _json.loads(ls)
            except ValueError:
                return None
        if not isinstance(ls, dict):
            return None
        return [(r.get("cells"), r.get("r")) for r in ls.get("rows") or []] or None

    mine = _cells(line_score)
    if mine:
        for c in candidates:
            if _cells(c.get("line_score")) == mine:
                return c

    if team_runs is not None and opponent_runs is not None:
        for c in candidates:
            if (c.get("team_runs") == team_runs
                    and c.get("opponent_runs") == opponent_runs
                    and (is_away is None or c.get("is_away") is None
                         or c.get("is_away") == is_away)):
                return c

    if opponent_name:
        for c in candidates:
            if (c.get("opponent_name") or "").strip() == opponent_name.strip():
                return c

    return None



def find_or_create_game(sb, game_date: str, opponent_name: str,
                         storm_runs, opponent_runs, tournament_id,
                         game_number: int, team_id: str, team_name: str,
                         game_type: str = "tournament",
                         is_away=None, line_score=None) -> str:
    """Find an existing game or create one. Returns game_id.

    is_away / line_score come straight from the box score PDF (DS-91) and
    are the authoritative record of venue and runs per inning. Both are
    written on create and refreshed on re-upload, so re-processing a game
    corrects a previously bad line score rather than leaving it stale.

    Identity is resolved by resolve_game (DS-100), the same call the
    overwrite check uses — the preview and the write cannot disagree.
    """
    match = resolve_game(
        sb, team_id, game_date,
        is_away=is_away, team_runs=storm_runs, opponent_runs=opponent_runs,
        line_score=line_score, opponent_name=opponent_name,
    )
    if match:
        game_id = match["game_id"]
        update = {}
        # A re-upload is also how a corrected opponent name reaches an
        # existing game, now that the name is no longer part of identity.
        if opponent_name:
            update["opponent_name"] = opponent_name
        if storm_runs is not None:
            update["team_runs"]     = storm_runs
            update["opponent_runs"] = opponent_runs
            if storm_runs > opponent_runs:  update["result"] = 'W'
            elif storm_runs < opponent_runs: update["result"] = 'L'
            else:                            update["result"] = 'T'
        if tournament_id:
            update["tournament_id"] = tournament_id
        if game_type:
            update["game_type"]     = game_type
        if is_away is not None:
            update["is_away"]       = is_away
        if line_score is not None:
            update["line_score"]    = line_score
        if update:
            sb.table("games").update(update).eq("game_id", game_id).execute()
        return game_id

    def calc_result(sr, or_):
        if sr is None or or_ is None: return None
        if sr > or_:  return 'W'
        if sr < or_:  return 'L'
        return 'T'

    year = int(game_date[:4]) if game_date else None
    resp = sb.table("games").insert({
        "game_date":     game_date,
        "team_id":       team_id,
        "team_name":     team_name,
        "opponent_name": opponent_name,
        "game_number":   game_number,
        "year":          year,
        "season":        SEASON,
        "team_runs":     storm_runs,
        "opponent_runs": opponent_runs,
        "tournament_id": tournament_id,
        "game_type":     game_type,
        "result":        calc_result(storm_runs, opponent_runs),
        "is_away":       is_away,
        "line_score":    line_score,
    }).execute()
    return resp.data[0]["game_id"]


def find_game(sb, game_date: str, team_id: str, *, is_away=None,
              team_runs=None, opponent_runs=None, line_score=None,
              opponent_name=None):
    """
    Look up an existing game *without* creating one — the overwrite check.

    Delegates to resolve_game so it cannot drift from what the write will do
    (DS-100). Previously the two mirrored each other by having the same code
    written twice, which held right up until the shared assumption changed:
    a corrected opponent name made both miss, so the coach got a duplicate
    game and no warning that anything was being replaced.

    Returns game_id or None.
    """
    if not game_date:
        return None
    match = resolve_game(
        sb, team_id, game_date,
        is_away=is_away, team_runs=team_runs, opponent_runs=opponent_runs,
        line_score=line_score, opponent_name=opponent_name,
    )
    return match["game_id"] if match else None


def find_or_create_tournament(sb, name: str, team_id: str) -> str:
    """Find or create a tournament by name, scoped to the coach's team.
    Returns tournament_id."""
    existing = (
        sb.table("tournaments")
        .select("tournament_id")
        .eq("name",    name)
        .eq("season",  SEASON)
        .eq("team_id", team_id)
        .execute()
    )
    if existing.data:
        return existing.data[0]["tournament_id"]

    resp = sb.table("tournaments").insert({
        "name":    name,
        "season":  SEASON,
        "year":    2026,
        "team_id": team_id,
    }).execute()
    return resp.data[0]["tournament_id"]


def next_game_number(sb, tournament_id: str, team_id: str) -> int:
    """Return the next available game number within a tournament."""
    resp = (
        sb.table("games")
        .select("game_number")
        .eq("tournament_id", tournament_id)
        .eq("team_id",       team_id)
        .execute()
    )
    if not resp.data:
        return 1
    return max(r["game_number"] or 0 for r in resp.data) + 1


# ── Player helpers ────────────────────────────────────────────────────────────

def build_player_map(sb, team_id: str):
    resp = (
        sb.table("players")
        .select("player_id,first_name,last_name,number")
        .eq("team_id", team_id)
        .execute()
    )
    pmap = {}
    for p in resp.data:
        pid = p["player_id"]
        fn  = p.get("first_name") or ""
        ln  = p.get("last_name")  or ""
        for key in [f"{fn} {ln}", f"{fn[0]} {ln}" if fn else None, ln]:
            if key and key.strip():
                pmap[key.strip()] = pid
    return pmap


def resolve_player(name, player_map):
    if not name:
        return None
    if name in player_map:
        return player_map[name]
    for part in reversed(name.strip().split()):
        if part in player_map:
            return player_map[part]
    return None


def parse_num(val, as_float=False):
    s = str(val).strip()
    if s in ("-", "", "N/A", "None"):
        return None
    try:
        f = float(s.replace("%", ""))
        return f if as_float else int(f)
    except (ValueError, TypeError):
        return None
