"""Parse *_Stats.csv files and upsert to Supabase."""
import csv
import io
from .common import SEASON, parse_num

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
    "balks": 75, "pickoffs": 76, "cs_against": 77, "stolen_bases_against": 78,
    "leadoff_outs": 91, "one_two_three_innings": 93,
    # Pitch-quality columns added June 2026 (indices header-verified vs Storm CSV)
    "p_per_ip": 88, "fip": 95, "strike_pct": 96, "fps_pct": 97,
    "fpso_pct": 98, "fpsw_pct": 99, "fpsh_pct": 100, "bb_per_inn": 101,
    "zero_bb_inn": 102, "sm_pct": 106, "k_per_bf": 107, "k_per_bb": 108,
}
PIT_FLOAT = {
    "innings_pitched", "era", "whip", "batting_average_against",
    "p_per_ip", "fip", "strike_pct", "fps_pct", "fpso_pct", "fpsw_pct",
    "fpsh_pct", "bb_per_inn", "sm_pct", "k_per_bf", "k_per_bb",
}

# Rate/percentage stats GameChanger writes as 0.00 when there's no data — store
# those as NULL. zero_bb_inn and k_per_bf are excluded: 0 is a real value there.
PIT_NULL_IF_ZERO = {
    "strike_pct", "fps_pct", "fpso_pct", "fpsw_pct", "fpsh_pct",
    "sm_pct", "k_per_bb", "bb_per_inn", "p_per_ip", "fip",
}

FLD_COLS = {
    "total_chances": 174, "assists": 175, "putouts": 176,
    "fielding_percentage": 177, "errors": 178, "double_plays": 179,
    "triple_plays": 180, "passed_balls": 182, "stolen_bases_allowed": 183,
    "runners_caught_stealing": 185,
    # New columns
    "innings_as_catcher": 181,
    "pickoffs": 187, "catcher_interference": 188,
    # NOTE: column 184 ("SB-ATT") is intentionally NOT mapped here — GameChanger
    # exports it as a combined string like "38-42" (allowed-attempts), not a
    # plain number, so parse_num() silently returns None for it every time.
    # stolen_base_attempts is derived instead, in _fielding_dict() below, from
    # the two columns that already parse cleanly. See DS-71.
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


def _pitching_dict(row):
    """
    Build the pitching_stats payload, including the June-2026 pitch-quality
    columns, NULL rules, and the two derived raw counts GameChanger doesn't
    export.  Every managed key is emitted (None included) so an UPSERT cleanly
    overwrites stale values on re-upload.
    """
    d = {}
    for field, col in PIT_COLS.items():
        val = parse_num(row[col], as_float=(field in PIT_FLOAT))
        # GameChanger writes 0.00 for these rate stats when there's no data.
        if field in PIT_NULL_IF_ZERO and val == 0:
            val = None
        d[field] = val

    # K/BB is undefined when no walks were issued.
    if not d.get("walks_allowed"):
        d["k_per_bb"] = None

    # GC exports the percentages but not the raw strike counts — derive them.
    tp, sp = d.get("total_pitches"), d.get("strike_pct")
    bf, fp = d.get("batters_faced"), d.get("fps_pct")
    d["strikes_thrown"]      = round(tp * sp / 100) if (tp and sp) else None
    d["first_pitch_strikes"] = round(bf * fp / 100) if (bf and fp) else None
    return d


def _fielding_dict(row):
    """
    Build the fielding_stats payload. stolen_base_attempts is derived rather
    than parsed from GameChanger's own SB-ATT column (184), which exports a
    combined string like "38-42" that parse_num() can't read — see DS-71.
    stolen_bases_allowed and runners_caught_stealing are both plain numeric
    columns already, so their sum is the attempts count; verified against
    real data (38 + 4 = 42, matching the export's own "38-42"). Always
    written explicitly, never omitted, so a catcher with zero attempts
    stores 0 rather than relying on the column default.
    """
    d = _stat_dict(row, FLD_COLS, FLD_FLOAT)
    d["stolen_base_attempts"] = (d.get("stolen_bases_allowed") or 0) + (d.get("runners_caught_stealing") or 0)
    return d


def preview(file_bytes):
    """
    Parse a stats CSV for the upload preview — no DB access, no writes.
    Returns {pitchers: [...], batters_count: N} for the confirmation screen.
    """
    text = file_bytes.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))

    pitchers, batters_count = [], 0
    for r in rows[2:]:
        r += [""] * (200 - len(r))
        number = r[0].strip().strip('"')
        last   = r[1].strip().strip('"')
        first  = r[2].strip().strip('"')
        if number in ("", "Totals", "Glossary") or not last:
            continue
        batters_count += 1
        ip = parse_num(r[PIT_COLS["innings_pitched"]], as_float=True)
        if ip and ip > 0:
            pitchers.append({
                "number":  number,
                "name":    f"{first} {last}".strip(),
                "ip":      r[PIT_COLS["innings_pitched"]].strip(),
                "bf":      parse_num(r[PIT_COLS["batters_faced"]]),
                "pitches": parse_num(r[PIT_COLS["total_pitches"]]),
                "s_pct":   parse_num(r[PIT_COLS["strike_pct"]], as_float=True),
                "fps_pct": parse_num(r[PIT_COLS["fps_pct"]],    as_float=True),
            })
    return {"pitchers": pitchers, "batters_count": batters_count}


def process(sb, file_bytes, team_id, team_name, game_id=None, filename=None):
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
                    .eq("team_id", team_id).execute())
        if existing.data:
            game_id = existing.data[0]["game_id"]
            game_action = "existing"
        else:
            g = sb.table("games").insert({
                "game_date": date, "team_id": team_id, "team_name": team_name,
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
                      .eq("number", number).eq("team_id", team_id).execute())
        if existing_p.data:
            player_id = existing_p.data[0]["player_id"]
        else:
            p = sb.table("players").insert({
                "number": number, "first_name": first,
                "last_name": last, "team_id": team_id, "team_name": team_name,
            }).execute()
            player_id = p.data[0]["player_id"]

        base = {"game_id": game_id, "player_id": player_id, "team_id": team_id}

        sb.table("batting_stats").upsert(
            {**base, **_stat_dict(row, BAT_COLS, BAT_FLOAT)},
            on_conflict="game_id,player_id",
        ).execute()

        ip = parse_num(row[PIT_COLS["innings_pitched"]], as_float=True)
        if ip and ip > 0:
            sb.table("pitching_stats").upsert(
                {**base, **_pitching_dict(row)},
                on_conflict="game_id,player_id",
            ).execute()

        sb.table("fielding_stats").upsert(
            {**base, "position": _primary_position(row),
             **_fielding_dict(row)},
            on_conflict="game_id,player_id",
        ).execute()
        players_processed.append(f"#{number} {first} {last}")

    return {
        "message": f"Stats {game_action} — {len(players_processed)} players processed.",
        "details": players_processed,
    }
