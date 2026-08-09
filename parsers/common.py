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


def parse_score_and_opponent(raw: str, team_name: str = "") -> dict:
    """
    Parse the line-score grid and opponent name from already-extracted box
    score PDF text. Shared by extract_game_info_from_pdf (below) and
    parsers/box_scores.py, which used to each hardcode a specific team's
    name here from this app's single-tenant era — that broke for every team
    except the one literal name baked in, silently returning no score/
    opponent match instead of an error. Multi-tenant-safe: takes the coach's
    real team_name as a parameter instead.
    Returns: {opponent_name, our_runs, opponent_runs, is_away}
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

    # 1. Split around our own team's name, tolerant of a trailing-s
    #    mismatch between what a coach typed at onboarding ("...All
    #    Stars") and GameChanger's own formatting of it ("...All Star").
    #    This is the only strategy that can recombine a name that arrived
    #    in two fragments, since it knows exactly where "us" sits.
    if team_name:
        team_pattern = team_name_regex(team_name)
        m = re.search(
            rf"^(.*?)\s*{team_pattern}\s+\d+\s*-\s*\d+\s+(.*?)\s*(?:Away|Home)\b"
            rf"|^(.*?)\s+\d+\s*-\s*\d+\s*{team_pattern}\s*(.*?)\s*(?:Away|Home)\b",
            header, re.I
        )
        if m:
            groups = m.groups()
            # Whichever alternative matched, the two "our name" fragments
            # are groups (1,2) or (3,4) — the unused pair is all None.
            frag_a, frag_b = (groups[0], groups[1]) if groups[0] is not None else (groups[2], groups[3])
            opponent_name = " ".join(p.strip() for p in (frag_a, frag_b) if p and p.strip())

    # 2. Fall back to pure position: the visiting team is always named
    #    first (same convention as the line-score row order above). Won't
    #    recombine a split fragment, but works with no team_name at all.
    if not opponent_name:
        header_m = re.match(r"^(.+?)\s+\d+\s*-\s*\d+\s+(.+?)\s*(?:Away|Home)\b", header)
        if header_m:
            team_a, team_b = header_m.group(1).strip(), header_m.group(2).strip()
            opponent_name = team_b if is_away else team_a

    if opponent_name:
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

    score_info = parse_score_and_opponent(raw, team_name)

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
    """
    existing = (
        sb.table("games")
        .select("game_id")
        .eq("game_date",     game_date)
        .eq("opponent_name", opponent_name)
        .eq("team_id",       team_id)
        .execute()
    )
    if existing.data:
        game_id = existing.data[0]["game_id"]
        update = {}
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


def find_game(sb, game_date: str, opponent_name: str, team_id: str):
    """
    Look up an existing game *without* creating one — mirrors the match logic in
    find_or_create_game so the preview reflects exactly what the write will hit.
    Returns game_id or None.
    """
    if not (game_date and opponent_name):
        return None
    resp = (
        sb.table("games")
        .select("game_id")
        .eq("game_date",     game_date)
        .eq("opponent_name", opponent_name)
        .eq("team_id",       team_id)
        .execute()
    )
    return resp.data[0]["game_id"] if resp.data else None


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
