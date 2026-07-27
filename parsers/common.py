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

    # Line score: two rows appear under the "1 2 3 ..." inning header, one
    # per team. The visiting team's row always comes first (they bat the
    # top of the inning) and the home team's row second — that ordering
    # tells us which row is ours without needing to recognize any specific
    # team abbreviation (GameChanger auto-generates these per team, e.g.
    # "STRM" for Storm, "YANK" for Yankees — not worth trying to predict).
    score_rows = []
    in_score = False
    for line in raw.splitlines():
        s = line.strip()
        if re.match(r"^1\s+2\s+3", s):
            in_score = True
            continue
        if in_score:
            if not s or s.startswith("BATTING"):
                break
            m = re.match(r"^([A-Z0-9]{2,6})\s+([\dX\s]+)$", s)
            if m:
                nums = [int(n) for n in re.findall(r"\d+", m.group(2))]
                if len(nums) >= 3:
                    score_rows.append(nums[-3])

    our_runs = opp_runs = None
    if len(score_rows) >= 2:
        if is_away:
            our_runs, opp_runs = score_rows[0], score_rows[1]
        else:
            opp_runs, our_runs = score_rows[0], score_rows[1]

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
    }


def extract_game_info_from_pdf(file_bytes: bytes, filename: str = "", team_name: str = "") -> dict:
    """
    Parse a box score PDF and return game metadata — no filename needed.
    Returns: {game_date, opponent_name, storm_runs, opponent_runs, is_away}
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
    }


# ── Game record helpers ───────────────────────────────────────────────────────

def find_or_create_game(sb, game_date: str, opponent_name: str,
                         storm_runs, opponent_runs, tournament_id,
                         game_number: int, team_id: str, team_name: str,
                         game_type: str = "tournament") -> str:
    """Find an existing game or create one. Returns game_id."""
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
