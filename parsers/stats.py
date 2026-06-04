"""Parse *_Stats.csv files and upsert to Supabase."""
import csv
import io
from .common import TEAM_NAME, SEASON, parse_filename, parse_num

BAT_COLS = {
    "games_played": 3, "plate_appearances": 4, "at_bats": 5,
    "batting_average": 6, "on_base_percentage": 7, "ops": 8, "slugging_percentage": 9,
    "hits": 10, "singles": 11, "doubles": 12, "triples": 13, "home_runs": 14,
    "runs_batted_in": 15, "runs_scored": 16, "walks": 17, "strikeouts": 18,
    "strikeouts_looking": 19, "hit_by_pitch": 20, "sacrifice_hits": 21,
    "sacrifice_flies": 22, "reached_on_error": 23, "fielders_choice": 24,
    "stolen_bases": 25, "stolen_base_percentage": 26, "caught_stealing": 27, "picked_off": 28,
    # New columns
    "quality_at_bats": 29, "hard_hit_balls": 34,
    "left_on_base": 40, "two_out_rbi": 41, "extra_base_hits": 42, "total_bases": 43,
    "pitches_seen": 44, "two_strike_three_plus": 46, "six_plus_pitch_pa": 48,
    "grounded_into_double_play": 51, "grounded_into_triple_play": 52, "catcher_interference": 53,
}
BAT_FLOAT = {"batting_average","on_base_percentage","ops","slugging_percentage","stolen_base_percentage"}

PIT_COLS = {
    "innings_pitched": 54, "games_pitched": 55, "games_started": 56,
    "batters_faced": 57, "total_pitches": 58, "wins": 59, "losses": 60,
    "saves": 61, "save_opportunities": 62, "blown_saves": 63,
    "hits_allowed": 65, "runs_allowed": 66, "earned_runs": 67,
    "walks_allowed": 68, "strikeouts": 69, "strikeouts_looking": 70,
    "hit_batters": 71, "era": 72, "whip": 73, "wild_pitches": 80,
    "batting_average_against": 81, "home_runs_allowed": 112,
    # New columns
    "balks": 75, "pickoffs": 76, "cs_against": 77, "stolen_bases_against": 78,
    "leadoff_outs": 91, "one_two_three_innings": 93,
}
PIT_FLOAT = {"innings_pitched","era","whip","batting_average_against"}

FLD_COLS = {
    "total_chances": 174, "assists": 175, "putouts": 176,
    "fielding_percentage": 177, "errors": 178, "double_plays": 179,
    "triple_plays": 180, "passed_balls": 182, "stolen_bases_allowed": 183,
    "runners_caught_stealing": 185,
    # New columns
    "innings_as_catcher": 181, "stolen_base_attempts": 184,
    "pickoffs": 187, "catcher_interference": 188,
}
FLD_FLOAT = {"fielding_percentage", "innings_as_catcher"}

POS_COLS = {"P":189,"C":190,"1B":191,"2B":192,"3B":193,"SS":194,"LF":195,"CF":196,"RF":197,"SF":198}


def _primary_position(row):
    best_pos, best_inn = None, -1.0
    for pos, col in POS_COLS.items():
        inn = parse_num(row[col], as_float=True) or 0.0
        if inn > best_inn:
            best_inn = inn; best_pos = pos
    return best_pos


def _stat_dict(row, col_map, float_fields):
    d = {}
    for field, col in col_map.items():
        val = parse_num(row[col], as_float=(field in float_fields))
        if val is not None:
            d[field] = val
    return d


def process(sb, file_bytes, game_id=None, filename=None):
    """
    Process a stats CSV.  game_id takes priority; filename is legacy fallback.
    """
    if game_id is None and filename:
        # Legacy: derive game identity from filename
        from .common import parse_num as _pn
        import re as _re
        m = _re.match(r"^(\d{4}-\d{2}-\d{2})-(.+?)_((?:Game)?(\d+))_", filename)
        if not m:
            raise ValueError(f"Cannot identify game from filename: {filename}")
        date    = m.group(1)
        game_num = int(m.group(4))
    elif game_id is not None:
        date = game_num = None  # not needed
    else:
        raise ValueError("Either game_id or filename must be provided")
    text = file_bytes.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))

    data_rows = []
    for r in rows[2:]:
        r += [""] * (200 - len(r))
        fc = r[0].strip().strip('"')
        lc = r[1].strip().strip('"')
        if fc in ("", "Totals", "Glossary") or not lc:
            continue
        data_rows.append(r)

    # Resolve game_id (legacy filename path)
    if game_id is None:
        existing = (sb.table("games").select("game_id")
                    .eq("game_date", date).eq("game_number", game_num)
                    .eq("team_name", TEAM_NAME).execute())
        if existing.data:
            game_id = existing.data[0]["game_id"]
            game_action = "existing"
        else:
            g = sb.table("games").insert({
                "game_date": date, "team_name": TEAM_NAME,
                "game_number": game_num, "year": int(date[:4]),
                "season": SEASON,
            }).execute()
            game_id = g.data[0]["game_id"]
            game_action = "created"
    else:
        game_action = "linked"

    players_processed = []
    for row in data_rows:
        number = parse_num(row[0], as_float=False)
        last   = row[1].strip().strip('"')
        first  = row[2].strip().strip('"')
        if not last:
            continue

        existing_p = (sb.table("players").select("player_id")
                      .eq("number", number).eq("team_name", TEAM_NAME).execute())
        if existing_p.data:
            player_id = existing_p.data[0]["player_id"]
        else:
            p = sb.table("players").insert({
                "number": number, "first_name": first,
                "last_name": last, "team_name": TEAM_NAME,
            }).execute()
            player_id = p.data[0]["player_id"]

        base = {"game_id": game_id, "player_id": player_id}

        if not sb.table("batting_stats").select("stat_id").eq("game_id", game_id).eq("player_id", player_id).execute().data:
            sb.table("batting_stats").insert({**base, **_stat_dict(row, BAT_COLS, BAT_FLOAT)}).execute()

        ip = parse_num(row[PIT_COLS["innings_pitched"]], as_float=True)
        if ip and ip > 0:
            if not sb.table("pitching_stats").select("stat_id").eq("game_id", game_id).eq("player_id", player_id).execute().data:
                sb.table("pitching_stats").insert({**base, **_stat_dict(row, PIT_COLS, PIT_FLOAT)}).execute()

        if not sb.table("fielding_stats").select("stat_id").eq("game_id", game_id).eq("player_id", player_id).execute().data:
            sb.table("fielding_stats").insert({**base, "position": _primary_position(row),
                                               **_stat_dict(row, FLD_COLS, FLD_FLOAT)}).execute()
        players_processed.append(f"#{number} {first} {last}")

    return {
        "message": f"Stats {game_action} — {len(players_processed)} players processed.",
        "details": players_processed,
    }
