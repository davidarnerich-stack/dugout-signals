"""
Pull all data needed to generate a tournament analysis report.
Returns a structured dict that gets passed to the AI and rendered into tables.
"""

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
    pit_totals = defaultdict(lambda: defaultdict(float))
    for row in pit_rows:
        pid = row["player_id"]
        for f in ["innings_pitched","games_pitched","batters_faced","total_pitches",
                  "hits_allowed","runs_allowed","earned_runs","walks_allowed",
                  "strikeouts","hit_batters","wild_pitches"]:
            pit_totals[pid][f] += (row.get(f) or 0)

    pitching_stats = []
    for pid, sums in pit_totals.items():
        p = pid_to_player.get(pid, {})
        ip   = sums["innings_pitched"]
        er   = sums["earned_runs"]
        bb   = sums["walks_allowed"]
        h    = sums["hits_allowed"]
        k    = sums["strikeouts"]
        hbp  = sums["hit_batters"]
        era  = round(er * 6 / ip, 2) if ip > 0 else 0
        whip = round((bb + h) / ip, 2) if ip > 0 else 0
        pitching_stats.append({
            "number": p.get("number"),
            "name":   f"{p.get('first_name','?')} {p.get('last_name','?')}",
            "player_id": pid,
            "ip":  ip, "gp": int(sums["games_pitched"]),
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
    bre_rows = (
        sb.table("base_running_events")
        .select("event_type,player_id,how")
        .in_("game_id", game_ids)
        .execute()
    ).data

    sb_count  = sum(1 for e in bre_rows if e["event_type"] == "stolen_base")
    cs_count  = sum(1 for e in bre_rows if e["event_type"] == "caught_stealing")
    sb_pct_br = round(sb_count / (sb_count + cs_count) * 100) if (sb_count + cs_count) else 0

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
            "ab": ab, "h": h, "doubles": row.get("doubles") or 0,
            "triples": row.get("triples") or 0, "hr": row.get("home_runs") or 0,
            "rbi": row.get("runs_batted_in") or 0, "r": row.get("runs_scored") or 0,
            "bb": bb, "k": row.get("strikeouts") or 0, "sb": row.get("stolen_bases") or 0,
            "cs": row.get("caught_stealing") or 0, "roe": row.get("reached_on_error") or 0,
            "avg": _fmt_avg(avg), "obp": _fmt_avg(obp), "slg": _fmt_avg(slg),
            "ops": _fmt_avg(round(obp + slg, 3)),
        })
    batting_stats.sort(key=lambda x: x["number"] or 99)

    pitching_stats = []
    for row in pit_rows:
        name, number = name_of(row["player_id"])
        ip, er = row.get("innings_pitched") or 0, row.get("earned_runs") or 0
        bb, h  = row.get("walks_allowed") or 0, row.get("hits_allowed") or 0
        era  = round(er * 6 / ip, 2) if ip else 0
        whip = round((bb + h) / ip, 2) if ip else 0
        pitching_stats.append({
            "player_id": row["player_id"], "number": number, "name": name,
            "ip": ip, "h": h, "r": row.get("runs_allowed") or 0, "er": er,
            "bb": bb, "k": row.get("strikeouts") or 0, "era": era, "whip": whip,
        })
    pitching_stats.sort(key=lambda x: -(x["ip"] or 0))

    fielding_stats = []
    for row in fld_rows:
        name, number = name_of(row["player_id"])
        tc, e = row.get("total_chances") or 0, row.get("errors") or 0
        fpct = round((tc - e) / tc, 3) if tc else 1.0
        fielding_stats.append({
            "player_id": row["player_id"], "number": number, "name": name,
            "position": row.get("position") or "—", "tc": tc, "e": e,
            "fpct": _fmt_avg(fpct), "pb": row.get("passed_balls") or 0,
            "sb_allowed": row.get("stolen_bases_allowed") or 0,
        })
    fielding_stats.sort(key=lambda x: x["number"] or 99)

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

    # Inning-by-inning line score is only ever populated by the play-by-play
    # parser (the box score PDF parser doesn't extract per-inning detail) —
    # so this is the one header_block piece that degrades without PBP.
    inning_rows = (
        sb.table("inning_scores").select("inning,team,runs")
        .eq("game_id", game_id).order("inning").execute()
    ).data
    has_line_score = bool(inning_rows)
    max_inning = max((r["inning"] for r in inning_rows), default=0)

    def cells_for(side):
        by_inning = {}
        for r in inning_rows:
            if r["team"] == side:
                by_inning[r["inning"]] = by_inning.get(r["inning"], 0) + (r["runs"] or 0)
        return [by_inning.get(i, "") for i in range(1, max_inning + 1)]

    opponent_name = game.get("opponent_name") or "Opponent"
    header_block = {
        "has_line_score": has_line_score,
        "line_header": [str(i) for i in range(1, max_inning + 1)],
        "visitor": {
            "label": opponent_name, "short": opponent_name.split()[0] if opponent_name else "Opp",
            "cells": cells_for("opponent"), "r": game.get("opponent_runs") or 0, "h": opp_h, "e": opp_e,
        },
        "home": {
            "label": team_name, "short": team_name.split()[0] if team_name else "Team",
            "cells": cells_for("storm"), "r": game.get("team_runs") or 0, "h": team_h, "e": team_e,
        },
    }

    # ── Play-by-play presence + baserunning ─────────────────────────────
    pa_count = (
        sb.table("plate_appearances").select("pa_id", count="exact")
        .eq("game_id", game_id).execute()
    ).count or 0
    has_pbp = pa_count > 0

    bre_rows = sb.table("base_running_events").select("event_type").eq("game_id", game_id).execute().data
    sb_count = sum(1 for e in bre_rows if e["event_type"] == "stolen_base")
    cs_count = sum(1 for e in bre_rows if e["event_type"] == "caught_stealing")

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

    return {
        "game": game,
        "team_name": team_name,
        "game_number_in_season": game_number_in_season,
        "header_block": header_block,
        "batting_stats": batting_stats,
        "pitching_stats": pitching_stats,
        "fielding_stats": fielding_stats,
        "has_pbp": has_pbp,
        "sb_count": sb_count, "cs_count": cs_count,
        "team_h": team_h, "team_e": team_e,
        "season_to_date": season_to_date,
        "last3": last3,
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
