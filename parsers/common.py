"""Shared utilities used by all parsers."""
import csv
import io
import re
from datetime import datetime

TEAM_NAME = "Storm 12U All-Stars"
SEASON    = "Summer 2026"

MONTH_MAP = {
    "January":1,"February":2,"March":3,"April":4,"May":5,"June":6,
    "July":7,"August":8,"September":9,"October":10,"November":11,"December":12,
}


# ── File type detection by content ────────────────────────────────────────────

def detect_file_type(filename: str, file_bytes: bytes) -> str:
    """
    Return 'stats', 'box_score', or 'play_by_play' by inspecting file content.
    Never relies on the filename convention.
    """
    name = filename.lower()

    if name.endswith(".pdf"):
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                text = pdf.pages[0].extract_text() or ""
            if "BATTING" in text and "PITCHING" in text:
                return "box_score"
        except Exception:
            pass
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


# ── Game info extraction from box score PDF ───────────────────────────────────

def extract_game_info_from_pdf(file_bytes: bytes) -> dict:
    """
    Parse a box score PDF and return game metadata — no filename needed.
    Returns: {game_date, opponent_name, storm_runs, opponent_runs, is_away}
    """
    import pdfplumber

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        raw = "\n".join(page.extract_text() or "" for page in pdf.pages)

    # Home / Away
    is_away = bool(re.search(r"\bAway\b", raw[:400]))

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

    # Scores from STRM line in line score
    storm_runs = opp_runs = None
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
                    if "STRM" in m.group(1):
                        storm_runs = nums[-3]
                    else:
                        opp_runs = nums[-3]

    # Opponent name
    header = " ".join(raw[:500].split())
    opponent_name = None
    if is_away:
        pre  = re.search(r"^(.+?)\s+Storm\s+12u\s+Silver\s+All\s+Star", header, re.I)
        post = re.search(
            r"Storm\s+12u\s+Silver\s+All\s+Star\s+\d+\s*-\s*\d+\s+(.+?)\s*(?:Away|Home)",
            header, re.I
        )
        parts = [g.group(1).strip() for g in [pre, post] if g and g.group(1).strip()]
        opponent_name = " ".join(parts) if parts else None
    else:
        m = re.search(
            r"^(.+?)\s+\d+\s*-\s*\d+\s+Storm\s+12u\s+Silver\s+All\s+Star\s*(.*?)\s*Home",
            header, re.I
        )
        if m:
            parts = [m.group(1).strip(), m.group(2).strip()]
            opponent_name = " ".join(p for p in parts if p)

    if opponent_name:
        opponent_name = re.sub(r"-\s+", "-", opponent_name).strip()

    return {
        "game_date":     game_date,
        "opponent_name": opponent_name,
        "storm_runs":    storm_runs,
        "opponent_runs": opp_runs,
        "is_away":       is_away,
    }


# ── Game record helpers ───────────────────────────────────────────────────────

def find_or_create_game(sb, game_date: str, opponent_name: str,
                         storm_runs, opponent_runs, tournament_id,
                         game_number: int, game_type: str = "tournament") -> str:
    """Find an existing game or create one. Returns game_id."""
    existing = (
        sb.table("games")
        .select("game_id")
        .eq("game_date",     game_date)
        .eq("opponent_name", opponent_name)
        .eq("team_name",     TEAM_NAME)
        .execute()
    )
    if existing.data:
        game_id = existing.data[0]["game_id"]
        update = {}
        if storm_runs is not None:
            update["team_runs"]     = storm_runs
            update["opponent_runs"] = opponent_runs
        if tournament_id:
            update["tournament_id"] = tournament_id
        if game_type:
            update["game_type"]     = game_type
        if update:
            sb.table("games").update(update).eq("game_id", game_id).execute()
        return game_id

    year = int(game_date[:4]) if game_date else None
    resp = sb.table("games").insert({
        "game_date":     game_date,
        "team_name":     TEAM_NAME,
        "opponent_name": opponent_name,
        "game_number":   game_number,
        "year":          year,
        "season":        SEASON,
        "team_runs":     storm_runs,
        "opponent_runs": opponent_runs,
        "tournament_id": tournament_id,
        "game_type":     game_type,
    }).execute()
    return resp.data[0]["game_id"]


def find_or_create_tournament(sb, name: str) -> str:
    """Find or create a tournament by name. Returns tournament_id."""
    existing = (
        sb.table("tournaments")
        .select("tournament_id")
        .eq("name",   name)
        .eq("season", SEASON)
        .execute()
    )
    if existing.data:
        return existing.data[0]["tournament_id"]

    resp = sb.table("tournaments").insert({
        "name":   name,
        "season": SEASON,
        "year":   2026,
    }).execute()
    return resp.data[0]["tournament_id"]


def next_game_number(sb, tournament_id: str) -> int:
    """Return the next available game number within a tournament."""
    resp = (
        sb.table("games")
        .select("game_number")
        .eq("tournament_id", tournament_id)
        .eq("team_name",     TEAM_NAME)
        .execute()
    )
    if not resp.data:
        return 1
    return max(r["game_number"] or 0 for r in resp.data) + 1


# ── Player helpers ────────────────────────────────────────────────────────────

def build_player_map(sb):
    resp = (
        sb.table("players")
        .select("player_id,first_name,last_name,number")
        .eq("team_name", TEAM_NAME)
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
