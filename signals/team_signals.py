"""
DS-67: Team Signals module — the six fixed-order signal cards.

Phase 1: all six (five at 8U) always render, in fixed order, no ranking, no
confidence meter (team sample size, ~20 PA/game, behaves normally from game
one — confidence is a player-signal concept, paused with DS-57). This module
computes only the DATA each card needs: numbers, states (genuine zero /
missing data / insufficient attempts), and the raw facts a narrative layer
grounds its copy in. Card narrative text is model-generated elsewhere
(team_signals_narrative.py) — "the six cards themselves are code-defined ...
the model writes copy, it does not decide what appears" (DS-67 requirements).

Card 6 (Command vs velocity) does not exist at 8U — not computed, not
suppressed-with-a-note, absent. Every other card renders at every age band,
leaning on whatever data survives the 8U no-walk rule.
"""

from metrics.compute import (
    compute_team_context, compute_batting_metrics, compute_pitching_metrics,
    compute_team_metrics, walks_enabled, safe_div,
    contact_data_missing_batting, contact_data_missing_pitching,
)

CARD_ORDER = [
    "defensive_split", "run_gap", "offence_funnel",
    "fielding_conversion", "catching_load", "command_vs_velocity",
]


def _games_played(team_totals_list):
    return sum((t["pitching"].get("games_pitched") or 0) for t in team_totals_list)


def compute_team_signals(team_totals_list, pitcher_rows, *, age_level, regulation_innings):
    """team_totals_list: one games.team_totals dict per game in the window
    (see metrics.compute.compute_team_context for the shape).
    pitcher_rows: individual pitching_stats-shaped dicts, one per pitcher,
    aggregated across the same window — needed for card 6's staff spread,
    which is undefined at the team-totals level (a spread is a property of
    individuals, not an aggregate).

    Returns a list of card dicts in CARD_ORDER (five entries at 8U — card 6
    is simply absent from the list, not present with a suppressed flag).
    Each card dict carries `facts` (the grounding data a narrative layer
    needs) and `state` (complete / genuine_zero / missing_data /
    insufficient_attempts) — never prose. No card here decides whether it
    "deserves" to show; Phase 1 shows all of them unconditionally.
    """
    walks_ok = walks_enabled(age_level)
    games = _games_played(team_totals_list)
    if games <= 0:
        return []

    team_context = compute_team_context(team_totals_list, regulation_innings=regulation_innings)

    bat_totals = _sum_team_batting(team_totals_list)
    pit_totals = _sum_team_pitching(team_totals_list)
    fld_totals = _sum_team_fielding(team_totals_list)

    contact_ok_batting = not contact_data_missing_batting(bat_totals)
    contact_ok_pitching = not contact_data_missing_pitching(pit_totals)

    cards = []
    cards.append(_defensive_split(pit_totals, fld_totals, team_context, games, walks_ok=walks_ok))
    cards.append(_run_gap(bat_totals, pit_totals, games))
    cards.append(_offence_funnel(bat_totals, contact_ok_batting))
    cards.append(_fielding_conversion(fld_totals, pit_totals, team_context, games))
    cards.append(_catching_load(fld_totals, age_level))
    if walks_ok:
        cards.append(_command_vs_velocity(pitcher_rows))

    return cards


# ── Team-level aggregation across the window ────────────────────────────────
# Raw counts sum exactly across games (each games.team_totals row is
# GameChanger's own per-game aggregate, itself already correct). Rate stats
# (BABIP, FB%, batting_average, etc.) reuse compute_team_context's
# innings-weighted approach for pitching; batting-side rates aren't needed
# raw here (compute_batting_metrics recomputes them from the summed counts).

def _sum_team_batting(team_totals_list):
    fields = [
        "plate_appearances", "at_bats", "hits", "doubles", "triples",
        "home_runs", "walks", "hit_by_pitch", "sacrifice_flies",
        "reached_on_error", "fielders_choice", "catcher_interference",
        "strikeouts", "hard_hit_balls", "extra_base_hits", "runs_scored",
        "runs_batted_in", "ground_ball_pct", "line_drive_pct", "fly_ball_pct",
    ]
    out = {f: 0 for f in fields}
    for t in team_totals_list:
        b = t.get("batting") or {}
        for f in fields:
            out[f] += b.get(f) or 0
    ab, h = out["at_bats"], out["hits"]
    tb = h + out["doubles"] + 2 * out["triples"] + 3 * out["home_runs"]
    out["batting_average"] = safe_div(h, ab) or 0.0
    out["slugging_percentage"] = safe_div(tb, ab) or 0.0
    return out


def _sum_team_pitching(team_totals_list):
    fields = [
        "batters_faced", "walks_allowed", "hit_batters", "strikeouts",
        "hits_allowed", "home_runs_allowed", "runs_allowed", "earned_runs",
        "games_pitched",
    ]
    out = {f: 0 for f in fields}
    outs_total = 0
    for t in team_totals_list:
        p = t.get("pitching") or {}
        for f in fields:
            out[f] += p.get(f) or 0
        from metrics.compute import parse_innings_to_outs
        outs_total += parse_innings_to_outs(p.get("innings_pitched")) or 0
    out["innings_pitched"] = str(outs_total // 3) + "." + str(outs_total % 3)
    # BABIP/fly_ball_pct: innings-weighted, same method as compute_team_context.
    babip_num, fb_num, weight = 0.0, 0.0, 0
    for t in team_totals_list:
        p = t.get("pitching") or {}
        from metrics.compute import parse_innings_to_outs as _pito
        w = _pito(p.get("innings_pitched")) or 0
        if p.get("babip") is not None:
            babip_num += p["babip"] * w
        if p.get("fly_ball_pct") is not None:
            fb_num += p["fly_ball_pct"] * w
        weight += w
    out["babip"] = safe_div(babip_num, weight)
    out["fly_ball_pct"] = safe_div(fb_num, weight)
    return out


def _sum_team_fielding(team_totals_list):
    fields = ["errors", "passed_balls", "stolen_bases_allowed", "runners_caught_stealing",
              "total_chances"]
    out = {f: 0 for f in fields}
    innings_as_catcher = 0.0
    for t in team_totals_list:
        f_ = t.get("fielding") or {}
        for f in fields:
            out[f] += f_.get(f) or 0
        innings_as_catcher += f_.get("innings_as_catcher") or 0.0
    out["innings_as_catcher"] = innings_as_catcher
    return out


# ── Card 1: Defensive split ─────────────────────────────────────────────────

def _defensive_split(pit_totals, fld_totals, team_context, games, *, walks_ok):
    runs_allowed = pit_totals["runs_allowed"]
    runs_allowed_per_game = safe_div(runs_allowed, games)

    team_pit_metrics = compute_pitching_metrics(
        pit_totals, team_context, walks_ok=walks_ok, contact_ok=True,
        regulation_innings=None, typical_game_innings=team_context.get("L"),
    )
    fra = team_pit_metrics.get("fra")

    fielding_remainder = None
    if runs_allowed_per_game is not None and fra is not None:
        fielding_remainder = runs_allowed_per_game - fra

    state = "complete" if runs_allowed_per_game is not None else "insufficient_attempts"
    return {
        "key": "defensive_split", "bucket": "Team Defence", "state": state,
        "facts": {
            "runs_allowed_per_game": runs_allowed_per_game,
            "fra_pitching_share": fra,
            "fielding_remainder": fielding_remainder,
            "games": games,
        },
    }


# ── Card 2: Run gap ──────────────────────────────────────────────────────────

def _run_gap(bat_totals, pit_totals, games):
    runs_scored_per_game = safe_div(bat_totals["runs_scored"], games)
    runs_allowed_per_game = safe_div(pit_totals["runs_allowed"], games)
    gap = None
    if runs_scored_per_game is not None and runs_allowed_per_game is not None:
        gap = runs_allowed_per_game - runs_scored_per_game
    return {
        "key": "run_gap", "bucket": "Offence / Defence", "state": "complete",
        "facts": {
            "runs_scored_per_game": runs_scored_per_game,
            "runs_allowed_per_game": runs_allowed_per_game,
            "gap": gap,  # positive: allowing more than scoring
            "games": games,
        },
    }


# ── Card 3: Offence funnel ───────────────────────────────────────────────────

def _offence_funnel(bat_totals, contact_ok):
    team_bat_metrics = compute_batting_metrics(
        bat_totals, None, league_age_at_game=None, walks_ok=False, contact_ok=contact_ok,
    )
    obpe = team_bat_metrics.get("obpe")
    opse = team_bat_metrics.get("opse")
    score_pct = team_bat_metrics.get("score_pct")
    state = "complete" if (opse is not None and score_pct is not None) else "missing_data"
    return {
        "key": "offence_funnel", "bucket": "Offence", "state": state,
        # team_obpe: DS-68's funnel bar 1 ("get on base") fill — not
        # re-derived there, read from here, single source of truth for both
        # DS-67's ledger and DS-68's bar geometry.
        "facts": {"team_obpe": obpe, "team_opse": opse, "team_score_pct": score_pct},
    }


# ── Card 4: Fielding conversion ─────────────────────────────────────────────

def _fielding_conversion(fld_totals, pit_totals, team_context, games):
    def_eff = compute_team_metrics(team_context).get("def_eff")
    errors = fld_totals["errors"]
    runs_allowed_per_game = safe_div(pit_totals["runs_allowed"], games)
    state = "complete" if def_eff is not None else "missing_data"
    return {
        "key": "fielding_conversion", "bucket": "Fielding", "state": state,
        "facts": {
            "def_eff": def_eff, "errors": errors,
            "runs_allowed_per_game": runs_allowed_per_game, "games": games,
        },
    }


# ── Card 5: Catching load ────────────────────────────────────────────────────

def _catching_load(fld_totals, age_level):
    """PB/inning below 12U (catching is genuinely the headline at 8U-10U —
    the reference Yankees data shows 70 passed balls in 74 innings against
    only 7 steal attempts all season, which is exactly why PB replaces CS%
    below 12U: there isn't enough attempt volume for CS% to mean anything)."""
    use_pb = (age_level or "").strip().upper() in ("8U", "10U")
    pb = fld_totals["passed_balls"]
    innings = fld_totals["innings_as_catcher"]
    cs = fld_totals["runners_caught_stealing"]
    sb = fld_totals["stolen_bases_allowed"]
    attempts = cs + sb

    if use_pb:
        pb_per_inning = safe_div(pb, innings)
        state = "genuine_zero" if pb == 0 else ("complete" if pb_per_inning is not None else "missing_data")
        return {
            "key": "catching_load", "bucket": "Catching", "state": state,
            "facts": {"metric": "pb_per_inning", "pb_per_inning": pb_per_inning,
                      "passed_balls": pb, "innings": innings},
        }
    else:
        cs_pct = safe_div(cs, attempts)
        state = "insufficient_attempts" if attempts == 0 else "complete"
        return {
            "key": "catching_load", "bucket": "Catching", "state": state,
            "facts": {"metric": "cs_pct", "cs_pct": cs_pct, "attempts": attempts, "cs": cs},
        }


# ── Card 6: Command vs velocity (not at 8U) ─────────────────────────────────

def _command_vs_velocity(pitcher_rows):
    """K% / BB% spread across the staff — a property of individual pitchers,
    not the team aggregate, so this is the one card that works from
    per-pitcher rows rather than team_totals."""
    eligible = [p for p in pitcher_rows if (p.get("batters_faced") or 0) > 0]
    if len(eligible) < 2:
        return {
            "key": "command_vs_velocity", "bucket": "Pitching", "state": "insufficient_attempts",
            "facts": {"staff_size": len(eligible)},
        }

    k_pcts = [(p["strikeouts"] / p["batters_faced"]) * 100 for p in eligible]
    bb_pcts = [(p.get("walks_allowed") or 0) / p["batters_faced"] * 100 for p in eligible]

    return {
        "key": "command_vs_velocity", "bucket": "Pitching", "state": "complete",
        "facts": {
            "k_pct_spread": max(k_pcts) - min(k_pcts),
            "bb_pct_spread": max(bb_pcts) - min(bb_pcts),
            "staff_size": len(eligible),
        },
    }


# ── Metric ledger rows (design spec §2: name / value / comparator) ─────────
# Comparators are code-generated, not model-generated — deliberately: the
# design spec's own examples ("7 attempts all season", "above team average")
# are simple, mechanical, sample-size-driven framings, and generating them
# from the same facts the headline is grounded in keeps them exactly as
# trustworthy as the numbers themselves, with nothing left for a model to
# invent. `is_zero` marks the design's Inter-600-not-Orbitron typography
# exception (§7) — a standalone zero, not any number containing one.

def _row(name, value, decimals, comparator, *, is_zero=None):
    if value is None:
        return {"name": name, "value": None, "value_display": "—",
                "comparator": comparator, "is_zero": False}
    display = f"{value:.{decimals}f}"
    zero = is_zero if is_zero is not None else (round(value, decimals) == 0)
    return {"name": name, "value": value, "value_display": display,
            "comparator": comparator, "is_zero": zero}


def card_metric_rows(card):
    f = card["facts"]
    key = card["key"]

    if key == "defensive_split":
        games_note = f"{f['games']} games this season"
        return [
            _row("Runs allowed/game", f["runs_allowed_per_game"], 1, games_note),
            _row("FRA (pitching)", f["fra_pitching_share"], 2, "fair runs the pitching owns"),
            _row("Fielding remainder", f["fielding_remainder"], 2, "the rest"),
        ]
    if key == "run_gap":
        games_note = f"{f['games']} games this season"
        return [
            _row("Runs/game scored", f["runs_scored_per_game"], 1, games_note),
            _row("Runs/game allowed", f["runs_allowed_per_game"], 1, games_note),
        ]
    if key == "offence_funnel":
        return [
            _row("Team OPSE", f["team_opse"], 3, "on-base + slugging, errors included"),
            _row("Team SCORE%", f["team_score_pct"], 1, "of times on base"),
        ]
    if key == "fielding_conversion":
        return [
            _row("DefEff", f["def_eff"], 3, "balls in play converted to outs"),
            _row("Errors", f["errors"], 0, f"across {f['games']} games", is_zero=(f["errors"] == 0)),
            _row("Runs allowed/game", f["runs_allowed_per_game"], 1, ""),
        ]
    if key == "catching_load":
        if f["metric"] == "pb_per_inning":
            return [
                _row("Passed balls/inning", f["pb_per_inning"], 2, ""),
                _row("Passed balls", f["passed_balls"], 0, f"in {f['innings']:.0f} innings caught",
                     is_zero=(f["passed_balls"] == 0)),
            ]
        return [
            _row("Caught stealing %", (f["cs_pct"] * 100) if f["cs_pct"] is not None else None, 1,
                 f"{f['attempts']} attempts all season"),
            _row("Runners caught", f["cs"], 0, f"of {f['attempts']} attempts", is_zero=(f["cs"] == 0)),
        ]
    if key == "command_vs_velocity":
        return [
            _row("K% spread", f["k_pct_spread"], 1, f"across {f['staff_size']} pitchers"),
            _row("BB% spread", f["bb_pct_spread"], 1, f"across {f['staff_size']} pitchers"),
        ]
    return []


# ── DS-69: explanation-view context chips (bucket / window / players) ──────
# Code-generated, same discipline as card_metric_rows — no model call, no
# new computation, just reshaping facts already on the card. Team signals
# rarely name individual players (that's a player-signal/DS-57 concept);
# command_vs_velocity is the one card with a staff-size fact worth surfacing
# as a chip here.

def card_context_chips(card, games_in_sample):
    f = card["facts"]
    chips = [
        {"label": "Bucket", "value": card["bucket"]},
        {"label": "Window", "value": f"{games_in_sample} game{'s' if games_in_sample != 1 else ''} this season"},
    ]
    if card["key"] == "command_vs_velocity" and f.get("staff_size"):
        chips.append({"label": "Staff", "value": f"{f['staff_size']} pitchers compared"})
    return chips
