"""
Pull all data needed to generate a tournament analysis report.
Returns a structured dict that gets passed to the AI and rendered into tables.
"""

import re

# A ball in play is one struck into fair territory. Imported from the parser
# so the report and the parse cannot disagree about what counts as one.
from parsers.play_by_play import BALL_IN_PLAY_RESULTS

_FIELDER_ABBREV = re.compile(r"^(?P<pos>.*?)\s+(?P<initial>[A-Za-zÀ-ÿ])\.?\s+(?P<last>[A-Za-zÀ-ÿ'’\-]+)$")


def _resolve_fielder_name(location: str, players) -> str:
    """
    Expand GameChanger's abbreviated fielder name against the real roster.

    Play-by-play records positions as "left fielder R Yamada-Harivandi" — an
    initial and a surname. Handing that to a narrative invites it to guess at
    the full name, and it did: Reuben came back as "Rohan".

    Resolves the initial and surname to the rostered player when exactly one
    matches. When the surname is shared, or unknown, the name is dropped and
    only the position is kept — a fielder identified solely by position is
    accurate; an invented first name is not.
    """
    if not location:
        return location
    m = _FIELDER_ABBREV.match(location)
    if not m:
        return location

    pos     = m.group("pos").strip()
    initial = m.group("initial").lower()
    last    = m.group("last").lower()

    matches = [p for p in players
               if (p.get("last_name") or "").strip().lower() == last
               and (p.get("first_name") or "").strip().lower().startswith(initial)]
    if len(matches) == 1:
        p = matches[0]
        return f"{pos} {p['first_name']} {p['last_name']}".strip()
    return pos or location


def _ordered_rows(us: dict, opponent: dict, is_away) -> list:
    """
    Box score row order: visiting team on top, home team below — the
    convention every printed box score follows (DS-91).

    Whether our team is home or away is set by the schedule and read from
    the box score's venue marker, so either row can be ours. Our team keeps
    its accent wherever it lands; `is_us` is what drives that in the
    template, so ordering and emphasis stay independent.

    `is_away` may be None on games ingested before the venue was persisted;
    those fall back to home, matching the previous behaviour.
    """
    if is_away:
        return [dict(us, is_us=True), dict(opponent, is_us=False)]
    return [dict(opponent, is_us=False), dict(us, is_us=True)]


def _distinct_short_names(name_a, name_b, fallback_a="A", fallback_b="B"):
    """
    Line-score short labels for two teams shown side by side. Taking each
    name's first word breaks when both teams share one (e.g. two Sierra
    Madre-league teams: "Madre Yankees" vs "Madre Twins" both shorten to
    "Madre") — fall back to each name's last word when the first-word
    shorthand would collide, then to the full name if even that collides.
    """
    words_a = name_a.split() if name_a else []
    words_b = name_b.split() if name_b else []
    short_a = words_a[0] if words_a else fallback_a
    short_b = words_b[0] if words_b else fallback_b
    if short_a.lower() == short_b.lower() and short_a:
        short_a = words_a[-1] if len(words_a) > 1 else short_a
        short_b = words_b[-1] if len(words_b) > 1 else short_b
        if short_a.lower() == short_b.lower():
            short_a = name_a or fallback_a
            short_b = name_b or fallback_b
    return short_a, short_b


def get_tournament_data(sb, tournament_id: str, team_id: str, team_name: str) -> dict:
    """
    Return a complete data payload for one tournament.
    All numbers come from Supabase — nothing calculated here.
    """

    # ── Tournament metadata ───────────────────────────────────────────────
    t = sb.table("tournaments").select("*").eq("tournament_id", tournament_id).execute()
    tournament = t.data[0] if t.data else {}

    # ── Games in tournament ───────────────────────────────────────────────
    games_resp = (
        sb.table("games")
        .select("game_id,game_date,game_number,opponent_name,team_runs,opponent_runs,result")
        .eq("tournament_id", tournament_id)
        .eq("team_id", team_id)
        .order("game_date")
        .order("game_number")
        .execute()
    )
    games = games_resp.data

    # Record summary
    wins   = sum(1 for g in games if g["result"] == "W")
    losses = sum(1 for g in games if g["result"] == "L")
    ties   = sum(1 for g in games if g["result"] == "T")
    rs     = sum(g["team_runs"]     or 0 for g in games)
    ra     = sum(g["opponent_runs"] or 0 for g in games)
    game_ids = [g["game_id"] for g in games]

    # ── Batting order / pitcher usage per game ────────────────────────────
    pitcher_usage = {}
    for g in games:
        ps = (
            sb.table("pitching_stats")
            .select("player_id,innings_pitched")
            .eq("game_id", g["game_id"])
            .execute()
        )
        players_resp = sb.table("players").select("player_id,first_name,last_name,number").eq("team_id", team_id).execute()
        pid_to_name = {p["player_id"]: f"{p['first_name'][0]}. {p['last_name']} #{p['number']}" for p in players_resp.data}
        pitchers = [f"{pid_to_name.get(p['player_id'], '?')} {p['innings_pitched']} IP" for p in ps.data if p["innings_pitched"] and float(p["innings_pitched"]) > 0]
        pitcher_usage[g["game_id"]] = ", ".join(pitchers)

    # ── Tournament batting stats ──────────────────────────────────────────
    # Sum batting_stats rows for all games in this tournament
    bat_rows = (
        sb.table("batting_stats")
        .select("*")
        .in_("game_id", game_ids)
        .execute()
    ).data

    # Group by player_id and sum
    from collections import defaultdict
    bat_by_player = defaultdict(lambda: defaultdict(float))
    int_fields = {"games_played","plate_appearances","at_bats","hits","singles","doubles",
                  "triples","home_runs","runs_batted_in","runs_scored","walks","strikeouts",
                  "strikeouts_looking","hit_by_pitch","stolen_bases","caught_stealing",
                  "quality_at_bats","hard_hit_balls","left_on_base","two_out_rbi",
                  "extra_base_hits","total_bases","pitches_seen","grounded_into_double_play"}
    for row in bat_rows:
        pid = row["player_id"]
        for f in int_fields:
            bat_by_player[pid][f] += (row.get(f) or 0)

    # Attach player info and calculate rates
    players_resp = sb.table("players").select("*").eq("team_id", team_id).execute()
    pid_to_player = {p["player_id"]: p for p in players_resp.data}

    batting_stats = []
    for pid, sums in bat_by_player.items():
        p = pid_to_player.get(pid, {})
        ab = sums["at_bats"] or 0
        pa = sums["plate_appearances"] or 0
        h  = sums["hits"] or 0
        bb = sums["walks"] or 0
        hbp = sums["hit_by_pitch"] or 0
        sf  = 0
        tb  = sums["total_bases"] or 0
        k   = sums["strikeouts"] or 0
        stl = sums["stolen_bases"] or 0      # renamed: 'sb' would shadow the Supabase client
        cs  = sums["caught_stealing"] or 0

        avg = round(h / ab, 3) if ab > 0 else 0.0
        obp_den = ab + bb + hbp + sf
        obp = round((h + bb + hbp) / obp_den, 3) if obp_den > 0 else 0.0
        slg = round(tb / ab, 3) if ab > 0 else 0.0
        ops = round(obp + slg, 3)
        bb_k = round(bb / k, 2) if k > 0 else None

        batting_stats.append({
            "number":        p.get("number"),
            "name":          f"{p.get('first_name','?')} {p.get('last_name','?')}",
            "player_id":     pid,
            "pa": pa, "ab": ab, "h": h,
            "doubles":       int(sums["doubles"]),
            "triples":       int(sums["triples"]),
            "hr":            int(sums["home_runs"]),
            "rbi":           int(sums["runs_batted_in"]),
            "r":             int(sums["runs_scored"]),
            "bb":            int(bb),
            "k":             int(k),
            "sb":            int(stl),
            "cs":            int(cs),
            "qab":           int(sums["quality_at_bats"]),
            "avg": _fmt_avg(avg), "obp": _fmt_avg(obp),
            "slg": _fmt_avg(slg), "ops": _fmt_avg(ops),
            "bb_k": str(bb_k) if bb_k is not None else "—",
        })
    batting_stats.sort(key=lambda x: x["number"] or 99)

    # Team batting totals
    team_ab  = sum(b["ab"]  for b in batting_stats)
    team_h   = sum(b["h"]   for b in batting_stats)
    team_bb  = sum(b["bb"]  for b in batting_stats)
    team_hbp = sum(b["pa"] - b["ab"] - b["bb"] for b in batting_stats)  # approx
    team_k   = sum(b["k"]   for b in batting_stats)
    team_sb  = sum(b["sb"]  for b in batting_stats)
    team_cs  = sum(b["cs"]  for b in batting_stats)
    team_tb  = sum(int((b.get("h",0) - b.get("doubles",0) - b.get("triples",0) - b.get("hr",0))
                       + 2*b.get("doubles",0) + 3*b.get("triples",0) + 4*b.get("hr",0))
                  for b in batting_stats)
    t_avg = round(team_h / team_ab, 3) if team_ab else 0
    t_obp_den = team_ab + team_bb + team_hbp
    t_obp = round((team_h + team_bb + team_hbp) / t_obp_den, 3) if t_obp_den else 0
    t_slg = round(team_tb / team_ab, 3) if team_ab else 0
    t_ops = round(t_obp + t_slg, 3)
    t_bbk = round(team_bb / team_k, 2) if team_k else 0
    sb_pct = round(team_sb / (team_sb + team_cs) * 100) if (team_sb + team_cs) else 0

    team_batting = {
        "avg": _fmt_avg(t_avg), "obp": _fmt_avg(t_obp),
        "ops": _fmt_avg(t_ops), "bb_k": str(t_bbk),
        "sb": team_sb, "cs": team_cs, "sb_pct": sb_pct,
    }

    # ── Pitching stats ────────────────────────────────────────────────────
    pit_rows = (
        sb.table("pitching_stats")
        .select("*")
        .in_("game_id", game_ids)
        .execute()
    ).data

    # Per-game pitching (for game-by-game tables)
    game_id_to_game = {g["game_id"]: g for g in games}
    pit_by_player_game = defaultdict(list)
    for row in pit_rows:
        if (row.get("innings_pitched") or 0) > 0:
            g = game_id_to_game.get(row["game_id"], {})
            pit_by_player_game[row["player_id"]].append({
                "game_number": g.get("game_number"),
                "opponent":    g.get("opponent_name", "?"),
                "ip":   row.get("innings_pitched", 0),
                "bf":   row.get("batters_faced", 0),
                "pitches": row.get("total_pitches", 0),
                "h":    row.get("hits_allowed", 0),
                "r":    row.get("runs_allowed", 0),
                "er":   row.get("earned_runs", 0),
                "bb":   row.get("walks_allowed", 0),
                "k":    row.get("strikeouts", 0),
                "fps_pct": row.get("fps_pct"),
            })

    # Tournament totals per pitcher
    from metrics.compute import parse_innings_to_outs, corrected_innings

    # DS-82: innings_pitched is GameChanger's thirds notation ("5.2" = 5 2/3
    # innings, 17 outs) — summing it as a raw float across games is wrong
    # twice over (5.2 + 5.2 = 10.4, not the correct 11 1/3 innings / 34
    # outs). Summed separately from the other counting stats as an integer
    # out count; corrected innings is derived only at the final division.
    pit_outs = defaultdict(int)
    pit_totals = defaultdict(lambda: defaultdict(float))
    for row in pit_rows:
        pid = row["player_id"]
        for f in ["games_pitched","batters_faced","total_pitches",
                  "hits_allowed","runs_allowed","earned_runs","walks_allowed",
                  "strikeouts","hit_batters","wild_pitches"]:
            pit_totals[pid][f] += (row.get(f) or 0)
        pit_outs[pid] += parse_innings_to_outs(row.get("innings_pitched")) or 0

    pitching_stats = []
    for pid, sums in pit_totals.items():
        p = pid_to_player.get(pid, {})
        outs = pit_outs[pid]
        ip_corrected = corrected_innings(outs) or 0
        ip_display = f"{outs // 3}.{outs % 3}"  # reconstruct thirds notation from the true out count
        er   = sums["earned_runs"]
        bb   = sums["walks_allowed"]
        h    = sums["hits_allowed"]
        k    = sums["strikeouts"]
        hbp  = sums["hit_batters"]
        era  = round(er * 6 / ip_corrected, 2) if ip_corrected else 0
        whip = round((bb + h) / ip_corrected, 2) if ip_corrected else 0
        pitching_stats.append({
            "number": p.get("number"),
            "name":   f"{p.get('first_name','?')} {p.get('last_name','?')}",
            "player_id": pid,
            "ip":  ip_display, "gp": int(sums["games_pitched"]),
            "bf":  int(sums["batters_faced"]),
            "pitches": int(sums["total_pitches"]),
            "h": int(h), "r": int(sums["runs_allowed"]),
            "er": int(er), "bb": int(bb), "k": int(k),
            "era": era, "whip": whip,
            "game_log": sorted(pit_by_player_game.get(pid, []), key=lambda x: x.get("game_number") or 0),
        })
    pitching_stats.sort(key=lambda x: x["number"] or 99)

    # ── Fielding stats ────────────────────────────────────────────────────
    fld_rows = (
        sb.table("fielding_stats")
        .select("*")
        .in_("game_id", game_ids)
        .execute()
    ).data

    fld_by_player = defaultdict(lambda: defaultdict(float))
    fld_positions = defaultdict(list)
    for row in fld_rows:
        pid = row["player_id"]
        if row.get("position"):
            fld_positions[pid].append(row["position"])
        for f in ["total_chances","assists","putouts","errors","double_plays",
                  "passed_balls","stolen_bases_allowed","stolen_base_attempts",
                  "runners_caught_stealing"]:
            fld_by_player[pid][f] += (row.get(f) or 0)

    fielding_stats = []
    for pid, sums in fld_by_player.items():
        p = pid_to_player.get(pid, {})
        tc = sums["total_chances"]
        e  = sums["errors"]
        fpct = round((tc - e) / tc, 3) if tc > 0 else 1.0
        from collections import Counter
        pos_counts = Counter(fld_positions[pid])
        primary_pos = pos_counts.most_common(1)[0][0] if pos_counts else "—"
        fielding_stats.append({
            "number": p.get("number"),
            "name":   f"{p.get('first_name','?')} {p.get('last_name','?')}",
            "player_id": pid,
            "position": primary_pos,
            "tc": int(tc), "e": int(e), "fpct": _fmt_avg(fpct),
            "pb": int(sums["passed_balls"]),
            "sb_allowed": int(sums["stolen_bases_allowed"]),
            "sba": int(sums["stolen_base_attempts"]),
            "cs": int(sums["runners_caught_stealing"]),
        })
    fielding_stats.sort(key=lambda x: x["number"] or 99)

    team_tc = sum(f["tc"] for f in fielding_stats)
    team_e  = sum(f["e"]  for f in fielding_stats)
    team_fpct = round((team_tc - team_e) / team_tc, 3) if team_tc else 1.0

    # ── Base running ──────────────────────────────────────────────────────
    # From the stats CSV, matching what GameChanger scored (DS-101). These
    # were previously counted from base_running_events, which is parsed from
    # play-by-play prose and over-counts: the prose describes advances the
    # scorer does not credit as steals ("advances on defensive indifference").
    #
    # This report already carried both numbers. report.html and the summary
    # narrative used the CSV totals while the baserunning narrative used the
    # play-by-play ones, so the prose could contradict the table printed
    # directly above it. One source now.
    sb_count  = team_sb
    cs_count  = team_cs
    sb_pct_br = sb_pct

    # ── Spring baseline ───────────────────────────────────────────────────
    spring_resp = (
        sb.table("player_season_stats")
        .select("player_id,batting_average,on_base_percentage,ops,stolen_bases,stolen_base_percentage")
        .eq("season", "Spring 2026")
        .execute()
    )
    spring_by_pid = {r["player_id"]: r for r in spring_resp.data}

    # Add spring baseline to batting_stats
    for b in batting_stats:
        sp = spring_by_pid.get(b["player_id"], {})
        b["spring_avg"] = _fmt_avg(sp.get("batting_average") or 0)
        b["spring_obp"] = _fmt_avg(sp.get("on_base_percentage") or 0)
        b["spring_ops"] = _fmt_avg(sp.get("ops") or 0)
        # Trend
        try:
            diff = float(b["avg"].strip(".")) - float(b["spring_avg"].strip("."))
        except Exception:
            diff = 0
        b["trend"] = "↑ Above" if diff > 0.03 else ("↓ Below" if diff < -0.03 else "→ Flat")

    # ── Previous tournament data ──────────────────────────────────────────
    # (all games not in this tournament, before its first game)
    first_game_date = games[0]["game_date"] if games else "2099-01-01"
    prev_games_resp = (
        sb.table("games")
        .select("game_id")
        .eq("team_id", team_id)
        .eq("game_type", "tournament")
        .lt("game_date", first_game_date)
        .execute()
    )
    prev_game_ids = [g["game_id"] for g in prev_games_resp.data]

    prev_batting = {}
    if prev_game_ids:
        prev_bat_rows = (
            sb.table("batting_stats")
            .select("player_id,at_bats,hits,walks,hit_by_pitch,total_bases,strikeouts")
            .in_("game_id", prev_game_ids)
            .execute()
        ).data
        prev_by_pid = defaultdict(lambda: defaultdict(float))
        for row in prev_bat_rows:
            for f in ["at_bats","hits","walks","hit_by_pitch","total_bases","strikeouts"]:
                prev_by_pid[row["player_id"]][f] += (row.get(f) or 0)
        for pid, sums in prev_by_pid.items():
            ab = sums["at_bats"]
            h  = sums["hits"]
            bb = sums["walks"]
            hbp = sums["hit_by_pitch"]
            tb  = sums["total_bases"]
            avg = round(h / ab, 3) if ab else 0
            obp_d = ab + bb + hbp
            obp = round((h + bb + hbp) / obp_d, 3) if obp_d else 0
            slg = round(tb / ab, 3) if ab else 0
            prev_batting[pid] = {
                "avg": _fmt_avg(avg), "obp": _fmt_avg(obp),
                "ops": _fmt_avg(round(obp + slg, 3)),
            }

    for b in batting_stats:
        prev = prev_batting.get(b["player_id"], {})
        b["prior_avg"] = prev.get("avg", "—")
        b["prior_obp"] = prev.get("obp", "—")
        b["prior_ops"] = prev.get("ops", "—")
        if prev:
            try:
                diff2 = float(b["avg"].strip(".")) - float(b["prior_avg"].strip("."))
                b["prior_trend"] = "↑ Above" if diff2 > 0.03 else ("↓ Below" if diff2 < -0.03 else "→ Flat")
            except Exception:
                b["prior_trend"] = "—"
        else:
            b["prior_trend"] = "—"

    # ── Tournament-over-tournament comparison ─────────────────────────────
    all_tournaments_resp = (
        sb.table("tournaments")
        .select("tournament_id,name")
        .eq("season", tournament.get("season", "Summer 2026"))
        .eq("team_id", team_id)
        .execute()
    )
    tourney_comparison = []
    for t_item in sorted(all_tournaments_resp.data, key=lambda x: x["name"]):
        t_games = (
            sb.table("games")
            .select("game_id,team_runs,opponent_runs,result")
            .eq("tournament_id", t_item["tournament_id"])
            .eq("team_id", team_id)
            .execute()
        ).data
        if not t_games:
            continue
        t_game_ids = [g["game_id"] for g in t_games]
        t_w = sum(1 for g in t_games if g["result"] == "W")
        t_l = sum(1 for g in t_games if g["result"] == "L")
        t_t = sum(1 for g in t_games if g["result"] == "T")
        t_rs = sum(g["team_runs"] or 0 for g in t_games)
        t_ra = sum(g["opponent_runs"] or 0 for g in t_games)
        # Quick batting avg
        t_bat = sb.table("batting_stats").select("at_bats,hits,walks,hit_by_pitch").in_("game_id", t_game_ids).execute().data
        t_ab = sum(r.get("at_bats",0) or 0 for r in t_bat)
        t_h  = sum(r.get("hits",0) or 0 for r in t_bat)
        t_avg = _fmt_avg(round(t_h / t_ab, 3) if t_ab else 0)
        tourney_comparison.append({
            "name": t_item["name"],
            "record": f"{t_w}–{t_l}{'–'+str(t_t) if t_t else ''}",
            "rs_ra": f"{t_rs} / {t_ra}",
            "avg": t_avg,
            "is_current": t_item["tournament_id"] == tournament_id,
        })

    return {
        "tournament":         tournament,
        "tournament_name":    tournament.get("name", "Tournament"),
        "season":             tournament.get("season", "Summer 2026"),
        "team_name":          team_name,
        "record":             f"{wins}–{losses}{'–'+str(ties) if ties else ''}",
        "wins": wins, "losses": losses, "ties": ties,
        "runs_scored": rs, "runs_allowed": ra,
        "games":              games,
        "pitcher_usage":      pitcher_usage,
        "batting_stats":      batting_stats,
        "team_batting":       team_batting,
        "pitching_stats":     pitching_stats,
        "fielding_stats":     fielding_stats,
        "team_fpct":          _fmt_avg(team_fpct),
        "team_errors":        team_e,
        "errors_per_game":    round(team_e / len(games), 1) if games else 0,
        "sb_total":           sb_count,
        "cs_total":           cs_count,
        "sb_pct":             sb_pct_br,
        "tourney_comparison": tourney_comparison,
    }


def _team_batting_line(sb, game_ids: list) -> dict:
    """Team-level batting rate stats across a set of games. Used for the
    single-game report's last-3-games / season-to-date trend comparisons."""
    if not game_ids:
        return {"avg": "—", "obp": "—", "ops": "—", "games": 0}
    rows = sb.table("batting_stats").select("at_bats,hits,walks,hit_by_pitch,total_bases").in_("game_id", game_ids).execute().data
    ab = sum(r.get("at_bats") or 0 for r in rows)
    h  = sum(r.get("hits") or 0 for r in rows)
    bb = sum(r.get("walks") or 0 for r in rows)
    hbp = sum(r.get("hit_by_pitch") or 0 for r in rows)
    tb = sum(r.get("total_bases") or 0 for r in rows)
    avg = round(h / ab, 3) if ab else 0.0
    obp_den = ab + bb + hbp
    obp = round((h + bb + hbp) / obp_den, 3) if obp_den else 0.0
    slg = round(tb / ab, 3) if ab else 0.0
    return {"avg": _fmt_avg(avg), "obp": _fmt_avg(obp), "ops": _fmt_avg(round(obp + slg, 3)), "games": len(game_ids)}


def _order_pitchers(sb, game_id, pitching_stats):
    """
    Put pitchers in the order they actually appeared, and record the innings
    each one worked.

    They used to be sorted by innings pitched, descending. That is not an
    order at all, and the narrative read it as one — on the 2026-08-11 report
    it had the third pitcher "closing out the game" when he in fact worked the
    3rd, and the second "entering in the 3rd" when he finished. Nobody had
    told the model the sequence, so it inferred one from the list it was given.

    The play-by-play knows: every plate appearance carries the inning and the
    pitcher who threw it. Where there is no play-by-play we fall back to the
    starter first, then innings descending — and say nothing about sequence,
    because we do not know it.
    """
    pa_rows = (sb.table("plate_appearances")
               .select("inning, half_inning, pa_sequence, pitcher_player_id")
               .eq("game_id", game_id).order("pa_sequence").execute().data or [])

    first_seen, innings_for = {}, {}
    for r in pa_rows:
        pid = r.get("pitcher_player_id")
        if not pid:
            continue                      # opponent's pitcher, or unparsed
        first_seen.setdefault(pid, r.get("pa_sequence") or 0)
        innings_for.setdefault(pid, set()).add(r.get("inning"))

    for p in pitching_stats:
        inns = sorted(i for i in innings_for.get(p["player_id"], set()) if i)
        p["appeared_in_innings"] = inns          # [] when unknown
        p["innings_label"] = (
            f"{_ordinal_int(inns[0])}" if len(inns) == 1 else
            f"{_ordinal_int(inns[0])}–{_ordinal_int(inns[-1])}" if inns else ""
        )

    if first_seen:
        pitching_stats.sort(key=lambda x: first_seen.get(x["player_id"], 10**6))
    else:
        from metrics.compute import parse_innings_to_outs
        pitching_stats.sort(
            key=lambda x: (0 if x.get("gs") else 1,
                           -(parse_innings_to_outs(x["ip"]) or 0)))


def _ordinal_int(n) -> str:
    if not n:
        return ""
    n = int(n)
    suf = "th" if 10 <= n % 100 <= 20 else {1:"st",2:"nd",3:"rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def get_single_game_data(sb, game_id: str, team_id: str, team_name: str) -> dict:
    """
    Return a complete data payload for one game — header block (structured,
    never AI-generated), season context (this game's ordinal position plus
    last-3-games / season-to-date aggregates for trend narration), and this
    game's batting/pitching/fielding/baserunning detail. All numbers come
    from Supabase; the AI layer (reports/generate.py) only writes the
    narrative around them.
    """
    from collections import defaultdict

    g = sb.table("games").select("*").eq("game_id", game_id).eq("team_id", team_id).execute()
    if not g.data:
        raise ValueError(f"Game {game_id} not found for this team.")
    game = g.data[0]

    bat_rows = sb.table("batting_stats").select("*").eq("game_id", game_id).execute().data
    pit_rows = sb.table("pitching_stats").select("*").eq("game_id", game_id).execute().data
    fld_rows = sb.table("fielding_stats").select("*").eq("game_id", game_id).execute().data
    players_resp = sb.table("players").select("*").eq("team_id", team_id).execute()
    pid_to_player = {p["player_id"]: p for p in players_resp.data}

    team_resp = sb.table("teams").select("age_level").eq("id", team_id).limit(1).execute()
    age_level = (team_resp.data[0].get("age_level") if team_resp.data else None) or "12U"

    def name_of(pid):
        p = pid_to_player.get(pid, {})
        return f"{p.get('first_name','?')} {p.get('last_name','?')}", p.get("number")

    batting_stats = []
    for row in bat_rows:
        name, number = name_of(row["player_id"])
        ab, h  = row.get("at_bats") or 0, row.get("hits") or 0
        bb, hbp = row.get("walks") or 0, row.get("hit_by_pitch") or 0
        tb = row.get("total_bases") or 0
        avg = round(h / ab, 3) if ab else 0.0
        obp_den = ab + bb + hbp
        obp = round((h + bb + hbp) / obp_den, 3) if obp_den else 0.0
        slg = round(tb / ab, 3) if ab else 0.0
        batting_stats.append({
            "player_id": row["player_id"], "number": number, "name": name,
            # Plate appearances and HBP travel with at-bats. Supplying AB
            # alone makes a walk-heavy line look self-contradictory — a
            # narrative given "0-for-1, 2 BB" wrote "2 walks in his lone
            # plate appearance", trying to reconcile figures that only
            # disagree because the reconciling number was withheld.
            "pa": row.get("plate_appearances") or 0,
            "hbp": hbp,
            "ab": ab, "h": h, "doubles": row.get("doubles") or 0,
            "triples": row.get("triples") or 0, "hr": row.get("home_runs") or 0,
            "rbi": row.get("runs_batted_in") or 0, "r": row.get("runs_scored") or 0,
            "bb": bb, "k": row.get("strikeouts") or 0, "sb": row.get("stolen_bases") or 0,
            "cs": row.get("caught_stealing") or 0, "roe": row.get("reached_on_error") or 0,
            "avg": _fmt_avg(avg), "obp": _fmt_avg(obp), "slg": _fmt_avg(slg),
            "ops": _fmt_avg(round(obp + slg, 3)),
        })
    batting_stats.sort(key=lambda x: x["number"] or 99)

    from metrics.compute import parse_innings_to_outs, corrected_innings

    pitching_stats = []
    for row in pit_rows:
        name, number = name_of(row["player_id"])
        ip, er = row.get("innings_pitched") or 0, row.get("earned_runs") or 0
        bb, h  = row.get("walks_allowed") or 0, row.get("hits_allowed") or 0
        # DS-82: GameChanger's thirds notation ("5.2" = 5 2/3 innings, 17
        # outs) is not decimal — divide ERA/WHIP by the corrected innings
        # value, never the raw string-as-float. `ip` itself stays the raw
        # value for display; it's already correct thirds notation for a
        # single game, only the division was wrong.
        ip_corrected = corrected_innings(parse_innings_to_outs(ip)) or 0
        era  = round(er * 6 / ip_corrected, 2) if ip_corrected else 0
        whip = round((bb + h) / ip_corrected, 2) if ip_corrected else 0
        pitching_stats.append({
            "player_id": row["player_id"], "number": number, "name": name,
            "ip": ip, "h": h, "r": row.get("runs_allowed") or 0, "er": er,
            "bb": bb, "k": row.get("strikeouts") or 0, "era": era, "whip": whip,
            "gs": row.get("games_started") or 0,
            "hbp": row.get("hit_batters") or 0,
            "bf":  row.get("batters_faced") or 0,
            # DS-103: command and contact quality. These were captured on
            # upload but never reached the report, which is why a narrative
            # once asserted a pitcher "was generating whiffs" with nothing
            # to base it on. Left as None where GameChanger recorded nothing,
            # so the prompt can omit rather than invent.
            "strike_pct": row.get("strike_pct"),   # share of pitches for strikes
            "fps_pct":    row.get("fps_pct"),      # first-pitch strikes
            "sm_pct":     row.get("sm_pct"),       # swing-and-miss / whiff%
            "weak_pct":   row.get("weak_pct"),     # batted balls weakly hit
            "hhb_pct":    row.get("hhb_pct"),      # batted balls hit hard
            "p_per_ip":   row.get("p_per_ip"),     # pitch efficiency
        })
    _order_pitchers(sb, game_id, pitching_stats)

    # `position` is one label per player per game, but players move around —
    # on 2026-08-11 the catcher's label sat on a player who also played third,
    # and a pitcher's error was reported "at catcher". GameChanger gives
    # innings by position, never chances or errors by position, so which
    # position a given putout or error happened at is simply not in the data.
    #
    # So the label is carried as `listed_position` — usable for "who caught"
    # (innings_as_catcher is real) but never for attributing a play. The
    # prompt is told not to place a chance or an error at a position.
    fielding_stats = []
    for row in fld_rows:
        name, number = name_of(row["player_id"])
        tc, e = row.get("total_chances") or 0, row.get("errors") or 0
        fpct = round((tc - e) / tc, 3) if tc else 1.0
        fielding_stats.append({
            "player_id": row["player_id"], "number": number, "name": name,
            "listed_position": row.get("position") or "—",
            "innings_caught": row.get("innings_as_catcher") or 0,
            "tc": tc, "e": e,
            "fpct": _fmt_avg(fpct), "pb": row.get("passed_balls") or 0,
            "sb_allowed": row.get("stolen_bases_allowed") or 0,
        })
    fielding_stats.sort(key=lambda x: x["number"] or 99)

    # ── Catching (DS-75) ──────────────────────────────────────────────────
    # Scope rule: player catching at single-game grain gets counting stats
    # only (~1-8 innings caught per game quantizes any rate to noise) — see
    # metrics.yml's catching bucket. Team-level rates are fine and drive the
    # age-band headline switch (pb_per_inning at 8U-10U, cs_pct at 12U+).
    from metrics.compute import compute_catching_metrics
    catcher_rows = [r for r in fld_rows if (r.get("innings_as_catcher") or 0) > 0]
    catching_stats = []
    for row in catcher_rows:
        name, number = name_of(row["player_id"])
        catching_stats.append({
            "player_id": row["player_id"], "number": number, "name": name,
            "innings": row.get("innings_as_catcher"),
            "pb": row.get("passed_balls") or 0,
            "sb_allowed": row.get("stolen_bases_allowed") or 0,
            "cs": row.get("runners_caught_stealing") or 0,
            "pik": row.get("pickoffs") or 0,
            "ci": row.get("catcher_interference") or 0,
        })
    catching_stats.sort(key=lambda x: x["number"] or 99)

    catching_outs = sum(parse_innings_to_outs(r.get("innings_as_catcher")) or 0 for r in catcher_rows)
    catching_team_totals = {
        "innings_as_catcher": f"{catching_outs // 3}.{catching_outs % 3}",
        "passed_balls": sum(r.get("passed_balls") or 0 for r in catcher_rows),
        "stolen_bases_allowed": sum(r.get("stolen_bases_allowed") or 0 for r in catcher_rows),
        "runners_caught_stealing": sum(r.get("runners_caught_stealing") or 0 for r in catcher_rows),
    }
    catching_metrics = compute_catching_metrics(catching_team_totals)
    catching_summary = {
        "headline_metric": "pb_per_inning" if (age_level or "").strip().upper() in ("8U", "10U") else "cs_pct",
        "innings_caught": catching_metrics.get("innings_caught"),
        "pb_per_inning": catching_metrics.get("pb_per_inning"),
        "cs_pct": catching_metrics.get("cs_pct"),
        "passed_balls": catching_team_totals["passed_balls"],
        "attempts": catching_team_totals["stolen_bases_allowed"] + catching_team_totals["runners_caught_stealing"],
        "cs": catching_team_totals["runners_caught_stealing"],
    }

    # ── Header block (structured, never AI-generated) ───────────────────
    team_h = sum(b["h"] for b in batting_stats)
    team_e = sum(f["e"] for f in fielding_stats)
    # Opponent H/E aren't tracked as their own stat rows anywhere in this
    # schema — derived instead from what our own stats already capture
    # about them: hits our pitchers allowed = opponent hits; times our
    # batters reached on an opponent error = opponent errors. Both are
    # available from the CSV alone, no play-by-play required.
    opp_h = sum(p["h"] for p in pitching_stats)
    opp_e = sum(b["roe"] for b in batting_stats)

    # ── Line score (DS-91) ──────────────────────────────────────────────
    # Comes from the official box score PDF, stored on the game at upload.
    # Play-by-play is NOT a valid source: mercy rules, time limits,
    # drop-dead innings and scorekeeper corrections never appear in it, so
    # derived per-inning runs disagree with the official record. When the
    # PDF grid is missing or internally inconsistent we show nothing rather
    # than something plausible-looking and wrong.
    line_score = game.get("line_score") or None
    if isinstance(line_score, str):
        import json as _json
        try:
            line_score = _json.loads(line_score)
        except ValueError:
            line_score = None
    if line_score and not line_score.get("consistent", True):
        line_score = None

    # A placeholder must never reach the report as a team name (DS-95). The
    # raw "TBD- 07/15/26, 7:00 PM" reads like a bug, and because it carries a
    # date it makes every game look like a different opponent — which is what
    # breaks tournament grouping and opponent scouting.
    from parsers.common import is_placeholder_opponent
    opponent_unnamed = is_placeholder_opponent(game.get("opponent_name"))
    opponent_name = "Opponent not named" if opponent_unnamed else game["opponent_name"]
    opp_short, team_short = _distinct_short_names(opponent_name, team_name, "Opp", "Team")
    if opponent_unnamed:
        opp_short = "Opp"

    # Venue decides row order; it does not decide which team is "ours".
    # is_away is recorded from the box score's own venue marker.
    is_away = game.get("is_away")

    us_cells, opp_cells = [], []
    line_header = []
    if line_score:
        line_header = line_score.get("innings", [])
        visitor_row, home_row = line_score["rows"][0], line_score["rows"][1]
        our_row, their_row = (visitor_row, home_row) if is_away else (home_row, visitor_row)
        us_cells, opp_cells = our_row["cells"], their_row["cells"]
        # H and E come from the PDF too — the derived team_h undercounts
        # whenever a player was dropped for having no jersey number (DS-94a).
        team_h = our_row["h"]   if our_row["h"]   is not None else team_h
        team_e = our_row["e"]   if our_row["e"]   is not None else team_e
        opp_h  = their_row["h"] if their_row["h"] is not None else opp_h
        opp_e  = their_row["e"] if their_row["e"] is not None else opp_e

    us = {
        "label": team_name, "short": team_short, "cells": us_cells,
        "r": game.get("team_runs") or 0, "h": team_h, "e": team_e,
    }
    opponent = {
        "label": opponent_name, "short": opp_short, "cells": opp_cells,
        "r": game.get("opponent_runs") or 0, "h": opp_h, "e": opp_e,
        "unnamed": opponent_unnamed,
    }

    rows = _ordered_rows(us, opponent, is_away)

    header_block = {
        "has_line_score": bool(line_score),
        "line_header": line_header,
        "us": us,
        "opponent": opponent,
        "rows": rows,
    }

    # ── Play-by-play presence + baserunning ─────────────────────────────
    pa_count = (
        sb.table("plate_appearances").select("pa_id", count="exact")
        .eq("game_id", game_id).execute()
    ).count or 0
    has_pbp = pa_count > 0

    # Stolen bases and caught stealing come from the stats CSV, which is what
    # GameChanger actually scored (DS-101). They were previously counted by
    # scanning base_running_events, which is parsed from play-by-play prose —
    # that over-counted, because the prose describes advances the scorer does
    # not credit as steals ("advances to 2nd on defensive indifference").
    # Play-by-play says what happened; the CSV says what was scored, and the
    # CSV is what the coach sees in GameChanger.
    sb_count = sum(b["sb"] for b in batting_stats)
    cs_count = sum(b["cs"] for b in batting_stats)
    # Retained for play-level colour only — who stole which base and how —
    # which the CSV cannot give. Never used as a count.
    stealers = [
        {"name": b["name"], "sb": b["sb"], "cs": b["cs"]}
        for b in batting_stats if b["sb"] or b["cs"]
    ]

    # ── Season context ───────────────────────────────────────────────────
    prior_games_resp = (
        sb.table("games").select("game_id,game_date,result,team_runs,opponent_runs")
        .eq("team_id", team_id).lt("game_date", game["game_date"])
        .order("game_date").execute()
    )
    prior_games = prior_games_resp.data
    game_number_in_season = len(prior_games) + 1

    season_game_ids = [g2["game_id"] for g2 in prior_games] + [game_id]
    last3_game_ids = season_game_ids[-3:]
    season_to_date = _team_batting_line(sb, season_game_ids)
    last3 = _team_batting_line(sb, last3_game_ids)

    # ── Fielding context ────────────────────────────────────────────────
    # The Fielding section previously received only the team error count and
    # the players who made errors, and consequently wrote that "the scorebook
    # does not detail other defensive sequences" — untrue, and it reads as a
    # product limitation rather than the plumbing gap it was.
    #
    # Two sources, deliberately separate:
    #   team_fielding — the official line from the stats CSV. Always present.
    #   fielding_plays — who the ball was actually hit to, from play-by-play.
    #                    None when the coach didn't paste any, which is common
    #                    and must degrade to silence rather than to a claim
    #                    that the data does not exist.
    team_fielding = (game.get("team_totals") or {}).get("fielding") or {}

    fielding_plays = None
    if has_pbp:
        # Only the opponent's plate appearances put a ball in play against our
        # defence; our own at-bats say nothing about our fielding.
        opp_pas = (
            sb.table("plate_appearances")
            .select("hit_location,result")
            .eq("game_id", game_id).eq("batting_team", "opponent")
            .execute()
        ).data or []

        # A ball in play is one struck into fair territory — every one of them,
        # not only the ones whose location we could pin to a player.
        #
        # This used to be sum(by_fielder.values()), i.e. the count of opposing
        # plate appearances whose hit_location happened to resolve to someone on
        # the roster. That is a parsing success rate wearing the name of a
        # baseball statistic. On 2026-08-11 it printed 13, which read as if it
        # were the 13 putouts and was neither.
        balls_in_play = sum(
            1 for pa in opp_pas if pa.get("result") in BALL_IN_PLAY_RESULTS)

        by_fielder = {}
        for pa in opp_pas:
            loc = _resolve_fielder_name(
                (pa.get("hit_location") or "").strip(), pid_to_player.values())
            if loc:
                by_fielder[loc] = by_fielder.get(loc, 0) + 1
        if balls_in_play:
            fielding_plays = {
                "balls_in_play": balls_in_play,
                # Deliberately separate: this is a subset, and the difference
                # between the two is balls we could not place.
                "located": sum(by_fielder.values()),
                "by_fielder": sorted(by_fielder.items(), key=lambda kv: -kv[1]),
            }

    # What happened in each of our half-innings, in order.
    #
    # Until now the recap received only per-player game totals, and was asked
    # to narrate innings. With no way to know WHEN anything happened it placed
    # events wherever it was writing: on 2026-08-11 it had Arnerich driving in
    # the first run with a hit in the 3rd, when he was hit by a pitch in the
    # 3rd, scored on Salisian's single, and got his hit in the 4th.
    #
    # Batter names come from the play-by-play and can carry a trailing verb
    # ("A Arnerich is" — DS-114), so they are trimmed against the roster here
    # rather than passed through.
    inning_events = None
    opponent_innings = None
    if has_pbp:
        our_pas = (
            sb.table("plate_appearances")
            .select("inning,half_inning,pa_sequence,batter_name,result,narrative")
            .eq("game_id", game_id).eq("batting_team", "our_team")
            .order("pa_sequence").execute()
        ).data or []
        surnames = {(p.get("last_name") or "").lower(): p for p in pid_to_player.values()}

        def _clean_batter(name, narrative=""):
            n = (name or "").strip()
            # The parser stores "Unknown" when a result phrasing it does not
            # recognise swallows the name (DS-114) — "R Yamada-Harivandi is out
            # on foul tip". The name is right there at the start of the
            # narrative, so recover it rather than printing "Unknown" into a
            # coach's recap. Two of the eight games contain one.
            if not n or n == "Unknown":
                m = re.match(r"([A-Z][\w.'-]*(?:\s+[\w.'-]+)*?)\s+(?:is|was|hits|"
                             r"singles|doubles|triples|walks|strikes|grounds|flies|"
                             r"pops|lines|reaches|steals|picked|caught|out|"
                             r"advances|scores|homers|bunts)\b", narrative or "")
                n = m.group(1).strip() if m else n
            for tail in (" is", " was"):
                if n.endswith(tail):
                    n = n[: -len(tail)].strip()
            m = _FIELDER_ABBREV.match(f"x {n}")
            if m and m.group("last").lower() in surnames:
                p = surnames[m.group("last").lower()]
                return f"{p['first_name']} {p['last_name']}"
            return n

        by_inning = {}
        for pa in our_pas:
            scored = re.findall(r"([A-Z][\w.'-]*(?:\s+[\w.'-]+)*?)\s+scores",
                                pa.get("narrative") or "")
            by_inning.setdefault(pa["inning"], []).append({
                "batter": _clean_batter(pa.get("batter_name"), pa.get("narrative")),
                "result": pa.get("result"),
                "scored": [_clean_batter(s) for s in scored],
            })
        inning_events = [{"inning": i, "events": by_inning[i]} for i in sorted(by_inning)]

        # The other half of the game.
        #
        # inning_events covers only OUR batting, so the recap had no facts at
        # all about the innings our pitchers threw — and filled the gap: on
        # 2026-08-11 it wrote "DeFlorio retired the side in the 5th" when that
        # half-inning ended on TIME with two outs and four runs in. Nothing in
        # the prompt contradicted it because nothing in the prompt mentioned it.
        #
        # Outs are what makes "retired the side" true or false, so they are the
        # figure to carry. An inning that did not reach three outs was stopped
        # by the clock or the run rule, not by the defence.
        # Outs and batters faced only — deliberately NOT runs.
        #
        # `inning_scores` is derived from the running score in the play-by-play,
        # and GameChanger's scorers correct that score mid-game with a line the
        # parser does not understand: "Score changed to 6-4". For this very game
        # the play-by-play climbs to 9 before being corrected back to 6, so the
        # per-inning runs sum to 12 in a 6-4 game. See DS-125.
        #
        # Nothing read inning_scores before this function did, so feeding those
        # runs into the recap would have made a wrong figure user-facing for the
        # first time — while fixing a different wrong figure. Outs come from
        # outs_recorded on each plate appearance and are unaffected.
        opp_pas_seq = (
            sb.table("plate_appearances")
            .select("inning,outs_recorded")
            .eq("game_id", game_id).eq("batting_team", "opponent")
            .order("pa_sequence").execute()
        ).data or []

        opp_by_inning = {}
        for pa in opp_pas_seq:
            slot = opp_by_inning.setdefault(pa["inning"], {"outs": 0, "batters": 0})
            slot["outs"] += (pa.get("outs_recorded") or 0)
            slot["batters"] += 1
        opponent_innings = [
            {"inning": i,
             "outs": opp_by_inning[i]["outs"],
             "batters": opp_by_inning[i]["batters"],
             "reached_three_outs": opp_by_inning[i]["outs"] >= 3}
            for i in sorted(opp_by_inning)
        ]

    # Where each error actually happened.
    #
    # The stats CSV cannot say — it gives a player one position label for the
    # whole game and an error count, so a pitcher's error got reported at
    # whatever position the label held (on 2026-08-11, "at catcher"). The
    # play-by-play states it outright: "reaches on an error by pitcher
    # A Arnerich". hit_location is null on these rows, which is why the
    # existing by-fielder pass misses them entirely.
    error_plays = None
    if has_pbp:
        err_pas = (
            sb.table("plate_appearances")
            .select("narrative,inning")
            .eq("game_id", game_id).eq("batting_team", "opponent")
            .ilike("narrative", "%error by%")
            .execute()
        ).data or []
        found = []
        for pa in err_pas:
            for pos, who in re.findall(
                    r"error by ([a-z][a-z ]*?) ([A-Z][\w.'-]*(?:\s+[\w.'-]+)*?)"
                    r"(?=,|\.|\s+on the same|\s*$)", pa.get("narrative") or ""):
                # The name class allows a period so initials like "J." survive;
                # strip sentence punctuation so "DeFlorio." doesn't reach the
                # roster lookup and fail to match.
                who = who.strip(" .,;")
                who = _resolve_fielder_name(f"{pos} {who}", pid_to_player.values()) or who
                found.append({"position": pos.strip(), "fielder": who,
                              "inning": pa.get("inning")})
        error_plays = found or None

    # ── Signal cards as season context (DS-102) ─────────────────────────
    # The same cards the dashboard shows, keyed so a section can pick up the
    # one that matches it — `fielding_conversion` for Fielding, `pitching`
    # for Pitching, and so on. Previously every section reasoned from this
    # game alone and filled the gap with generic age-level commentary, while
    # a season's worth of analysis sat computed and unused one module away.
    #
    # A card's `state` says whether its metric could be computed at all; it
    # is NOT a sample-size signal, and DS-67 deliberately has none — team
    # samples behave normally from game one. Whether there is enough season
    # to compare against is game_number_in_season's job, below.
    signal_cards_by_key = {}
    try:
        from signals.load import compute_signals_for_team
        signal_cards_by_key = {
            c["key"]: c for c in compute_signals_for_team(sb, team_id)["cards"]
        }
    except Exception:
        # Season context is an enhancement, never a reason a report fails to
        # generate — same best-effort contract the signal snapshot uses.
        signal_cards_by_key = {}

    return {
        "game": game,
        "team_name": team_name,
        "game_number_in_season": game_number_in_season,
        "header_block": header_block,
        "batting_stats": batting_stats,
        "pitching_stats": pitching_stats,
        "fielding_stats": fielding_stats,
        "catching_stats": catching_stats,
        "catching_summary": catching_summary,
        "age_level": age_level,
        "has_pbp": has_pbp,
        "sb_count": sb_count, "cs_count": cs_count,
        # Computed here so the narrative never derives it. GameChanger reports
        # 90.91 for 10-of-11; same formula, same answer.
        "sb_pct": (round(sb_count / (sb_count + cs_count) * 100, 1)
                   if (sb_count + cs_count) else None),
        "stealers": stealers,
        "team_fielding": team_fielding,
        "fielding_plays": fielding_plays,
        "error_plays": error_plays,
        "inning_events": inning_events,
        "opponent_innings": opponent_innings,
        "team_h": team_h, "team_e": team_e,
        "season_to_date": season_to_date,
        "last3": last3,
        "signal_cards": signal_cards_by_key,
    }


def _fmt_avg(val) -> str:
    """Format .333 style — omit leading zero."""
    try:
        f = float(val)
        if f >= 1.0:
            return f"{f:.3f}"
        return f"{f:.3f}".lstrip("0") or ".000"
    except Exception:
        return str(val)
