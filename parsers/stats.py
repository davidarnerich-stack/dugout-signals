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
    # DS-74: pitching-side contact-quality columns. Feed WEAK/PSKL (weak_pct,
    # babip) and YFIP (fly_ball_pct) directly, and babip also drives the
    # pitching contact_data_available check (requirement #10) — these are the
    # pitching-section counterparts of the batting weak_pct/fly_ball_pct/babip
    # DS-43 already captured; missed at the time because the pitching section
    # wasn't in scope for that pass.
    # DS-103: hhb_pct (110) is weak_pct's counterpart — the share of batted
    # balls that were line drives or hard ground balls. It sits directly
    # between weak_pct and fly_ball_pct and was stepped over by the DS-74
    # pass; without it the contact-quality picture only has its soft half.
    "weak_pct": 109, "hhb_pct": 110, "fly_ball_pct": 114, "babip": 116,
}
PIT_FLOAT = {
    "innings_pitched", "era", "whip", "batting_average_against",
    "p_per_ip", "fip", "strike_pct", "fps_pct", "fpso_pct", "fpsw_pct",
    "fpsh_pct", "bb_per_inn", "sm_pct", "k_per_bf", "k_per_bb",
    "weak_pct", "hhb_pct", "fly_ball_pct", "babip",
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


def _review_copy(p, kind="numberless"):
    """
    Build the lines the review block shows for a player (SPEC §4 body, §5
    "Found in file" row).

    Composed here rather than in the template so the wording is testable and
    stays put. Design's example — 2 AB, 2 H, including a double — is the best
    case; in the real PSW files most flagged players never came to the plate,
    so both lines have to survive having nothing to report. A player with no
    line is exactly the one most easily lost, so the copy still says why they
    matter instead of going blank.

    Two reasons to ask, and they read differently:

      numberless — GameChanger had no jersey number for them.
      new        — they have a number, but no one by that name is on the
                   roster yet. This is how a guest or call-up arrives, and
                   the original block could not see it at all: it keyed the
                   question off the missing number rather than off the player
                   being unfamiliar.
    """
    ab, h  = p["ab"], p["h"]
    pa     = p.get("pa", 0)
    xbh    = ("a home run" if p["hr"] else
              "a triple"   if p["triples"] else
              "a double"   if p["doubles"] else None)

    def n(count, word, plural=None):
        return f"{count} {word if count == 1 else (plural or word + 's')}"

    bits = []
    if ab:
        bits.append(n(ab, "at-bat"))
        if h: bits.append(n(h, "hit"))
    elif pa:
        # A walk, hit by pitch or sacrifice is a plate appearance that is not
        # an at-bat. Reporting only at-bats called a 3-walk game "no batting
        # line", which reads like we failed to parse the row — when in fact
        # the player reached base every time up.
        bits.append(n(pa, "plate appearance"))
        if p.get("bb"):  bits.append(n(p["bb"], "walk"))
        if p.get("hbp"): bits.append(n(p["hbp"], "hit by pitch", "hit by pitches"))
        if p.get("sh"):  bits.append(n(p["sh"], "sacrifice"))
        if p.get("sf"):  bits.append(n(p["sf"], "sacrifice fly", "sacrifice flies"))
    if p["ip"]:
        bits.append(f"{p['ip']} innings pitched")

    if bits:
        line = ", ".join(bits)
        if ab and xbh:
            line += f" — including {xbh}"
        stats = f"{line}. "
    else:
        # Genuinely never came to the plate and never pitched. Say what is
        # true rather than implying the data is missing.
        stats = "They didn't bat or pitch in this game. "

    p["kind"] = kind
    if kind == "new":
        num = f" as #{p['number']}" if p.get("number") else ""
        p["title"] = f"{p['name']} isn't on your roster yet"
        p["body"]  = (f"They appear in this game's file{num}, but no one by "
                      f"that name is on your roster. {stats}Their stats will "
                      f"be imported either way — this just records whether "
                      f"they've joined the team or were a guest.")
    else:
        p["title"] = f"{p['name']} came through without a jersey number"
        p["body"]  = (stats + "Their stats will be imported either way. "
                      "Telling us who they are keeps their numbers together "
                      "across games.")

    cells = []
    if ab:
        cells.append(f"{ab} AB")
        if h:            cells.append(f"{h} H")
        if p["doubles"]: cells.append(f"{p['doubles']} 2B")
        if p["triples"]: cells.append(f"{p['triples']} 3B")
        if p["hr"]:      cells.append(f"{p['hr']} HR")
    elif pa:
        cells.append(f"{pa} PA")
        if p.get("bb"):  cells.append(f"{p['bb']} BB")
        if p.get("hbp"): cells.append(f"{p['hbp']} HBP")
        if p.get("sh"):  cells.append(f"{p['sh']} SAC")
        if p.get("sf"):  cells.append(f"{p['sf']} SF")
    if p["ip"]:          cells.append(f"{p['ip']} IP")
    # "no batting line" was wrong for anyone who walked — reserve the phrase
    # for a player who genuinely never appeared.
    p["stat_line"] = " · ".join(cells) or "did not bat or pitch"
    return p


def preview(file_bytes):
    """
    Parse a stats CSV for the upload preview — no DB access, no writes.
    Returns {pitchers, batters_count, players} for the confirmation screen.

    `players` is every player in the file, carrying the stats and copy the
    review block needs. Which of them the coach is actually asked about is
    decided against the roster in app.py, because that is a database question
    and this stays database-free. The stats travel with the name because the
    review item renders before any write — there is nothing to look up yet.
    """
    text = file_bytes.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))

    pitchers, batters_count, players = [], 0, []
    for r in rows[2:]:
        r += [""] * (200 - len(r))
        number = r[0].strip().strip('"')
        last   = r[1].strip().strip('"')
        first  = r[2].strip().strip('"')
        # The preview must count exactly what the import will write, so this
        # filter has to match process()'s: any of number / first / last makes
        # the row a player. A blank last name is how GameChanger records a
        # call-up nobody entered a surname for.
        if number in ("Totals", "Glossary") or not (number or last or first):
            continue
        batters_count += 1
        ip = parse_num(r[PIT_COLS["innings_pitched"]], as_float=True)

        players.append(_review_copy({
            "name":    f"{first} {last}".strip(),
            "first":   first,
            "last":    last,
            "number":  number,
            "pa":      parse_num(r[BAT_COLS["plate_appearances"]]) or 0,
            "bb":      parse_num(r[BAT_COLS["walks"]])            or 0,
            "hbp":     parse_num(r[BAT_COLS["hit_by_pitch"]])     or 0,
            "sh":      parse_num(r[BAT_COLS["sacrifice_hits"]])   or 0,
            "sf":      parse_num(r[BAT_COLS["sacrifice_flies"]])  or 0,
            "ab":      parse_num(r[BAT_COLS["at_bats"]])    or 0,
            "h":       parse_num(r[BAT_COLS["hits"]])       or 0,
            "doubles": parse_num(r[BAT_COLS["doubles"]])    or 0,
            "triples": parse_num(r[BAT_COLS["triples"]])    or 0,
            "hr":      parse_num(r[BAT_COLS["home_runs"]])  or 0,
            "ip":      r[PIT_COLS["innings_pitched"]].strip() if ip else "",
        }, kind="numberless" if not number else "new"))

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
    return {"pitchers": pitchers, "batters_count": batters_count,
            "players": players}


def _player_key(first_name, last_name):
    return ((first_name or "").strip().lower(), (last_name or "").strip().lower())


def _find_or_create_player(sb, team_id, team_name, number, first, last,
                           by_number, by_name, choices=None, created=None):
    """
    Resolve the player for one stats row, creating them if new (DS-94a).

    Order matters:
      1. jersey number, when the row has one
      2. first + last name within the team — used both when there is no
         number AND when a numbered lookup misses
      3. create, with number NULL when none was given

    Case 2's second half is what stops a duplicate appearing later. A player
    who imports numberless across several games and is then entered in
    GameChanger with a number would otherwise be created a second time,
    stranding the earlier games on the old record — the only situation that
    would ever need a merge. Instead the number is written onto the record
    that already exists.

    `by_number` / `by_name` are the in-memory roster indexes and are updated
    in place, so a name repeated later in the same file resolves to the
    record just created rather than inserting again.

    `choices` carries what the coach answered in the review block, keyed by
    name (DS-99). It only ever affects how a *new* player is labelled — guest
    or not, numbered or not. It cannot stop a player being created and cannot
    stop stats being written; that separation is the whole point of DS-94a,
    where the silent drop was the bug. An unanswered player still lands on the
    roster, just without a number.
    """
    match = by_number.get(number) if number is not None else None

    if match is None:
        match = by_name.get(_player_key(first, last))
        if match is not None and number is not None and match.get("number") is None:
            sb.table("players").update({"number": number}) \
              .eq("player_id", match["player_id"]).execute()
            match["number"] = number
            by_number[number] = match

    if match is not None:
        # The coach's answer has to reach a player who already exists, not
        # only one created on this upload. A numberless player is created by
        # the first game they appear in, so from the second game onward the
        # create branch never runs — answering the review item did nothing at
        # all, which is exactly what happened to Connor McClung across games
        # 2 and 3. Only ever fills a gap: an existing jersey number is left
        # alone, because that record is not the one in question.
        choice = (choices or {}).get(_player_key(first, last)) or {}
        fill = {}
        if match.get("number") is None and choice.get("choice") == "new" and choice.get("number"):
            fill["number"] = parse_num(choice["number"])
        if choice.get("choice") == "guest" and match.get("number") is None:
            fill["is_guest"] = True
        if fill:
            sb.table("players").update(fill).eq("player_id", match["player_id"]).execute()
            match.update(fill)
            if fill.get("number") is not None:
                by_number[fill["number"]] = match
            if created is not None:
                created.append({"name": f"{first} {last}".strip(),
                                "number": fill.get("number"),
                                "is_guest": fill.get("is_guest", False),
                                "updated": True})
        return match["player_id"]

    choice = (choices or {}).get(_player_key(first, last)) or {}
    is_guest = choice.get("choice") == "guest"
    if number is None and choice.get("choice") == "new" and choice.get("number"):
        number = parse_num(choice["number"])

    row = sb.table("players").insert({
        "number": number, "first_name": first, "last_name": last,
        "team_id": team_id, "team_name": team_name, "is_guest": is_guest,
    }).execute().data[0]
    if created is not None:
        created.append({"name": f"{first} {last}".strip(),
                        "number": number, "is_guest": is_guest})
    record = {"player_id": row["player_id"], "number": number,
              "first_name": first, "last_name": last}
    by_name[_player_key(first, last)] = record
    if number is not None:
        by_number[number] = record
    return record["player_id"]


def process(sb, file_bytes, team_id, team_name, game_id=None, filename=None,
            choices=None):
    """
    Process a stats CSV.  game_id takes priority; filename is legacy fallback.

    `choices` is the review block's answers (DS-99), a list of
    {first, last, choice, number}. It labels newly created players and does
    nothing else — no row is skipped and no stat is withheld on account of it.
    """
    choice_map = {_player_key(c.get("first"), c.get("last")): c
                  for c in (choices or [])}
    created_players = []
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
    totals_row = None
    for r in rows[2:]:
        r += [""] * (200 - len(r))
        fc = r[0].strip().strip('"')
        lc = r[1].strip().strip('"')
        if fc == "Totals":
            totals_row = r
            continue
        # A row is a player if it carries ANY identity — number, first name or
        # last name. Requiring a jersey number dropped guests (DS-94a);
        # requiring a last name dropped them right back, because GameChanger
        # records a call-up whose surname nobody entered as `17,,Tristan`.
        # Whichever single field we insist on, the row we lose is a real kid
        # whose stats then go missing from the team's totals. Only a genuinely
        # empty row is not a player.
        if fc == "Glossary" or not (fc or lc or r[2].strip().strip('"')):
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

    # DS-74: capture GameChanger's own team-aggregate "Totals" row verbatim,
    # rather than re-deriving team-level rate stats (BABIP, FB%, games played)
    # by summing/weighting individual player rows — that approximation was
    # verified against the reference workbooks to NOT match GameChanger's own
    # computation closely enough for the metrics layer's 3-decimal-place
    # validation. Reuses the same column-mapping functions as a player row;
    # the Totals row has identical column layout, just team-summed values.
    if totals_row is not None:
        team_totals = {
            "batting": _stat_dict(totals_row, BAT_COLS, BAT_FLOAT),
            "pitching": _pitching_dict(totals_row),
            "fielding": _fielding_dict(totals_row, offset=fld_offset),
        }
        sb.table("games").update({"team_totals": team_totals}).eq("game_id", game_id).execute()

    # (see _find_or_create_player above for the lookup order)
    # Roster loaded once and matched in memory — a youth roster is small, and
    # it keeps name matching out of SQL where a name containing % or _ would
    # behave as a wildcard.
    roster = (sb.table("players")
              .select("player_id, number, first_name, last_name")
              .eq("team_id", team_id).execute()).data or []

    by_number = {p["number"]: p for p in roster if p["number"] is not None}
    by_name   = {_player_key(p["first_name"], p["last_name"]): p for p in roster}

    players_processed = []
    numberless_players = []
    for row in data_rows:
        number = parse_num(row[0], as_float=False)
        last   = row[1].strip().strip('"')
        first  = row[2].strip().strip('"')
        # Any identity is enough — see the row filter above.
        if not (last or first or number is not None):
            continue

        # Lookup order matters (DS-94a):
        #   1. jersey number, when the row has one
        #   2. first + last name within the team — used both when there is no
        #      number and when a numbered lookup misses
        #   3. create, with number NULL when none was given
        #
        # Case 2's second half is what prevents a duplicate appearing later.
        # A player who imports numberless across several games and is then
        # entered in GameChanger with a number would otherwise be created a
        # second time, stranding those earlier games on the old record. The
        # number is written onto the existing record instead.
        player_id = _find_or_create_player(
            sb, team_id, team_name, number, first, last, by_number, by_name,
            choices=choice_map, created=created_players
        )

        if number is None:
            numberless_players.append(f"{first} {last}".strip())

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
        players_processed.append(
            f"#{number} {first} {last}" if number is not None
            else f"(no number) {first} {last}"
        )

    # Name them rather than letting them pass unremarked. Their stats are
    # imported either way — this only tells the coach the roster gained
    # someone whose jersey number GameChanger didn't have (DS-94a). The
    # richer in-flow treatment is DS-99.
    message = f"Stats {game_action} — {len(players_processed)} players processed."
    if numberless_players:
        who = ", ".join(numberless_players)
        message += (f" {len(numberless_players)} without a jersey number "
                    f"({who}) — imported and added to your roster.")

    return {
        "message": message,
        "details": players_processed,
        "numberless_players": numberless_players,
        "created_players": created_players,
    }
