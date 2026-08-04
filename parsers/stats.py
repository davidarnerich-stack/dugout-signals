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
    # DS-43: remaining batting columns. All pre-computed by GameChanger and
    # exported as their own columns — column mapping only, no calculation.
    "quality_at_bat_pct": 30, "pa_per_bb": 31, "bb_per_k": 32, "contact_pct": 33,
    "line_drive_pct": 35, "fly_ball_pct": 36, "ground_ball_pct": 37,
    "babip": 38, "batting_avg_risp": 39, "pitches_per_pa": 45,
    "two_strike_three_plus_pct": 47, "six_plus_pitch_pa_pct": 49, "ab_per_hr": 50,
}
BAT_FLOAT = {
    "batting_average","on_base_percentage","ops","slugging_percentage","stolen_base_percentage",
    "quality_at_bat_pct","pa_per_bb","bb_per_k","contact_pct","line_drive_pct","fly_ball_pct",
    "ground_ball_pct","babip","batting_avg_risp","pitches_per_pa","two_strike_three_plus_pct",
    "six_plus_pitch_pa_pct","ab_per_hr",
}

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

# DS-43: per-pitch-type detail block (cols 118 onward). Baseball carries 6
# pitch types (FB/CT/CB/SL/CH/OS), softball carries 10 (FB/CH/RB/DB/SC/CB/
# DC/KB/KC/OS) — different sets, different column counts (30 vs 56), and the
# column layout differs by sport rather than being a fixed offset like
# Fielding is. Rather than hardcoding two per-sport tables, this is walked
# directly from the CSV's own header row (row 1) at parse time: a block
# starts wherever a bare code is immediately followed by "<code>S" (e.g.
# "FB" then "FBS"), which self-validates without needing a fixed whitelist
# of pitch-type codes — robust to a pitch type this file has never shown us.
#
# Column 82-87 carries a FIXED 6-slot velocity block present in every file
# regardless of sport (softball files carry unused MPHCT/MPHSL columns).
# Types not in that fixed set (softball's RB/DB/SC/DC/KB/KC) carry their own
# inline MPH column as a 6th column in their block instead of borrowing from
# here — see PITCH_TYPE_TRAP note in _parse_pitch_type_detail().
MPH_SHARED = {"FB": 82, "CT": 83, "CB": 84, "SL": 85, "CH": 86, "OS": 87}
PITCH_TYPE_BLOCK_START = 118

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
# FLD_COLS and POS_COLS above are indexed against the softball layout, where
# Fielding starts at column 174. Baseball's pitch-type detail block (30 cols)
# is 26 columns shorter than softball's (56 cols), which shifts Fielding and
# everything after it left to column 148 — reading FLD_COLS/POS_COLS
# unadjusted on a baseball file silently reads past the real data into the
# parser's own end-of-row padding. See DS-78.
FLD_BASELINE_COL = 174  # where "Fielding" sits in the softball layout FLD_COLS/POS_COLS were built against


def _section_offset(banner_row, section_label, baseline_col):
    """
    Locate `section_label` (e.g. "Fielding") in the CSV's own banner row
    (row 0) and return how far it sits from `baseline_col`, rather than
    trusting a hardcoded index. 0 for softball, -26 for baseball, on current
    real exports — but derived from the file itself so it doesn't silently
    break if GameChanger's column counts shift again. Falls back to 0 (the
    softball baseline, i.e. today's pre-fix behavior) if the label can't be
    found, rather than raising on an unexpected export.
    """
    for i, cell in enumerate(banner_row):
        if cell.strip() == section_label:
            return i - baseline_col
    return 0


def _primary_position(row, offset=0):
    best_pos, best_inn = None, -1.0
    for pos, col in POS_COLS.items():
        inn = parse_num(row[col + offset], as_float=True) or 0.0
        if inn > best_inn:
            best_inn = inn; best_pos = pos
    return best_pos


def _stat_dict(row, col_map, float_fields, offset=0):
    d = {}
    for field, col in col_map.items():
        val = parse_num(row[col + offset], as_float=(field in float_fields))
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


def _fielding_dict(row, offset=0):
    """
    Build the fielding_stats payload. `offset` corrects FLD_COLS for baseball
    files — see DS-78 / _section_offset(). stolen_base_attempts is derived
    rather than parsed from GameChanger's own SB-ATT column (184), which
    exports a combined string like "38-42" that parse_num() can't read — see
    DS-71. stolen_bases_allowed and runners_caught_stealing are both plain
    numeric columns already, so their sum is the attempts count; verified
    against real data (38 + 4 = 42, matching the export's own "38-42").
    Always written explicitly, never omitted, so a catcher with zero
    attempts stores 0 rather than relying on the column default.
    """
    d = _stat_dict(row, FLD_COLS, FLD_FLOAT, offset=offset)
    d["stolen_base_attempts"] = (d.get("stolen_bases_allowed") or 0) + (d.get("runners_caught_stealing") or 0)
    return d


def _parse_pitch_type_detail(header_row, row, fielding_start_col):
    """
    Build the pitch_type_detail JSONB payload for pitching_stats — see DS-43.

    Walks columns 118 .. fielding_start_col-1 using the CSV's own header row
    (row 1) rather than a hardcoded per-sport table, so baseball's 6 pitch
    types and softball's 10 don't need two separate maps. A block starts
    wherever a bare code (e.g. "FB") is immediately followed by "<code>S"
    (e.g. "FBS") — self-validating, not a fixed whitelist.

    Sub-field labels confirmed against GameChanger's own glossary (per DS-43
    AC3 — not guessed) — every export embeds it as a row the app already
    discards as non-player data (row 15 in a real file; matched here by
    content, not position, since its row number isn't guaranteed stable):
        FB    = "Number of pitches thrown as Fastballs"           -> thrown
        FBS   = "Number of Fastballs thrown for strikes"          -> strikes
        FBS%  = "Percentage of Fastballs thrown for strikes"      -> strike_pct
        FBSW% = "Percentage of Fastballs swung at"                -> swing_pct
        FBSM% = "Percentage of Fastballs swung at and missed"     -> swing_miss_pct
        MPHFB = "Fastball average velocity"                       -> mph
    (Same pattern for every other pitch-type prefix.)

    Only pitch types the pitcher actually threw (thrown > 0) are included —
    intentionally sparse, not one key per pitch type this sport supports.
    """
    detail = {}
    col = PITCH_TYPE_BLOCK_START
    while col < fielding_start_col:
        code = header_row[col].strip() if col < len(header_row) else ""
        next_label = header_row[col + 1].strip() if col + 1 < len(header_row) else ""
        if not code or next_label != f"{code}S":
            col += 1  # not a recognized block start; skip forward defensively
            continue

        thrown = parse_num(row[col])
        if not thrown:
            # Not thrown this game — omit rather than store an all-zero block.
            width = 6 if (col + 5 < fielding_start_col
                          and header_row[col + 5].strip() == f"MPH{code}") else 5
            col += width
            continue

        has_inline_mph = (col + 5 < fielding_start_col
                           and header_row[col + 5].strip() == f"MPH{code}")
        if has_inline_mph:
            mph = parse_num(row[col + 5], as_float=True)
            width = 6
        else:
            mph_col = MPH_SHARED.get(code)
            mph = parse_num(row[mph_col], as_float=True) if mph_col is not None else None
            width = 5

        detail[code] = {
            "thrown":          thrown,
            "strikes":         parse_num(row[col + 1]),
            "strike_pct":      parse_num(row[col + 2], as_float=True),
            "swing_pct":       parse_num(row[col + 3], as_float=True),
            "swing_miss_pct":  parse_num(row[col + 4], as_float=True),
            "mph":             mph,
        }
        col += width

    return detail


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
    fld_offset = _section_offset(rows[0] if rows else [], "Fielding", FLD_BASELINE_COL)
    fielding_start_col = FLD_BASELINE_COL + fld_offset
    header_row = rows[1] if len(rows) > 1 else []

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
                {**base, **_pitching_dict(row),
                 "pitch_type_detail": _parse_pitch_type_detail(header_row, row, fielding_start_col)},
                on_conflict="game_id,player_id",
            ).execute()

        sb.table("fielding_stats").upsert(
            {**base, "position": _primary_position(row, offset=fld_offset),
             **_fielding_dict(row, offset=fld_offset)},
            on_conflict="game_id,player_id",
        ).execute()
        players_processed.append(f"#{number} {first} {last}")

    return {
        "message": f"Stats {game_action} — {len(players_processed)} players processed.",
        "details": players_processed,
    }
