"""
DS-67: Team Signals module — the fixed-order signal cards.

Phase 1: all cards always render, in fixed order, no ranking, no confidence
meter (team sample size, ~20 PA/game, behaves normally from game one —
confidence is a player-signal concept, paused with DS-57). This module
computes only the DATA each card needs: numbers, states (genuine zero /
missing data / insufficient attempts), and the raw facts a narrative layer
grounds its copy in. Card narrative text is model-generated elsewhere
(narrative.py) — "the cards themselves are code-defined ... the model
writes copy, it does not decide what appears" (DS-67 requirements).

DS-90 (Aug 6 2026): command_vs_velocity retired — no place in the new fixed
TEAM OFFENCE/TEAM DEFENCE page IA — replaced by _pitching (K-BB%/S%/BB%),
which does still vary by age band: K-BB% collapses to K% and BB% is absent
under the 8U no-walk rule, same convention as everything else gated on
walks_enabled().
"""

from metrics.compute import (
    compute_team_context, compute_batting_metrics, compute_pitching_metrics,
    compute_team_metrics, compute_catching_metrics, walks_enabled, safe_div,
    contact_data_missing_batting, contact_data_missing_pitching,
)

CARD_ORDER = [
    "defensive_split", "run_gap", "offence_funnel",
    "fielding_conversion", "catching_load",
    # DS-89: appended, not inserted — app.py's cached-narrative lookup keys
    # signal_history rows by CARD_ORDER *position* (T1..Tn), so inserting
    # these in the middle would shift every card after them onto the wrong
    # cached headline until the next game commit regenerates everything.
    # TEAM OFFENCE/TEAM DEFENCE display grouping (DS-90) is driven by each
    # card's own key, not by this list's order.
    "hitting", "baserunning",
    # DS-90: command_vs_velocity retired (see _pitching below) — its old T6
    # slot is unavoidably reused by "hitting" now that it's gone, so any
    # signal_history row cached under T6 before this deploy will show the
    # wrong card's stale headline until that team's next game commit. Known
    # and accepted (flagged to David 2026-08-06 before this change).
    "pitching",
]

# DS-90: the new fixed page IA's two section groupings — explicit display
# order within each section (spec §9), independent of CARD_ORDER above
# (which exists only for the cached-narrative T-index, not for display).
# defensive_split and run_gap are in neither list: both are still computed
# (their facts feed the Defence module and the Offence/Defence bridge card
# respectively) but neither renders as a standalone ledger card any more —
# defensive_split's content was "on the page twice" per the design doc,
# run_gap has its own dedicated bridge-card rendering (DS-88).
SECTION_TEAM_OFFENCE_KEYS = ["offence_funnel", "hitting", "baserunning"]
SECTION_TEAM_DEFENCE_KEYS = ["pitching", "catching_load", "fielding_conversion"]


def cards_by_keys(signal_cards, keys):
    """Reorders/filters signal_cards to exactly `keys`, in that order —
    silently drops any key not present (e.g. command_vs_velocity's old
    Pitching slot at 8U had no card; a section with nothing to show should
    collapse, not error)."""
    by_key = {c["key"]: c for c in signal_cards}
    return [by_key[k] for k in keys if k in by_key]


def _games_played(team_totals_list):
    return sum((t["pitching"].get("games_pitched") or 0) for t in team_totals_list)


def compute_team_signals(team_totals_list, pitcher_rows, *, age_level, regulation_innings):
    """team_totals_list: one games.team_totals dict per game in the window
    (see metrics.compute.compute_team_context for the shape).
    pitcher_rows: unused since DS-90 retired command_vs_velocity (the one
    card that needed per-pitcher rows for its staff spread) — kept as a
    parameter rather than changed to avoid rippling a signature change into
    app.py's call site for no functional benefit.

    Returns a list of card dicts in CARD_ORDER. Each card dict carries
    `facts` (the grounding data a narrative layer needs) and `state`
    (complete / genuine_zero / missing_data / insufficient_attempts) — never
    prose. No card here decides whether it "deserves" to show; Phase 1
    shows all of them unconditionally.
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
    cards.append(_hitting(bat_totals, contact_ok_batting))
    cards.append(_baserunning(bat_totals))
    cards.append(_pitching(pit_totals, team_context, walks_ok=walks_ok))

    return cards


def compute_familiar_anchors(team_totals_list, *, regulation_innings):
    """DS-73: the "start with what you know" familiar-stat values a novel
    metric's explainer opens with (team ERA for FRA, team OPS for OPSE, team
    Runs for SCORE%, team Fielding Percentage for DefEff). Reuses the exact
    same aggregation compute_team_signals already does — no new database
    query, single source of truth. Kept independent of compute_team_signals
    itself (rather than folded into its return value) so this doesn't touch
    that function's existing, tested contract or its three call sites."""
    team_context = compute_team_context(team_totals_list, regulation_innings=regulation_innings)
    bat_totals = _sum_team_batting(team_totals_list)
    fld_totals = _sum_team_fielding(team_totals_list)

    obp_den = bat_totals["at_bats"] + bat_totals["walks"] + bat_totals["hit_by_pitch"]
    obp = safe_div(bat_totals["hits"] + bat_totals["walks"] + bat_totals["hit_by_pitch"], obp_den)
    ops = (obp + bat_totals["slugging_percentage"]) if obp is not None else None

    tc = fld_totals["total_chances"]
    fielding_pct = safe_div(tc - fld_totals["errors"], tc)

    return {
        "era": team_context.get("TmERA"),
        "ops": ops,
        "runs": team_context.get("TeamR"),
        "fielding_pct": fielding_pct,
    }


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
        # DS-89: Baserunning card's raw counts — plain sums like every
        # other counting field above, no weighting needed.
        "stolen_bases", "caught_stealing", "picked_off",
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

    # DS-89: babip is a directly-parsed per-game GameChanger field (per the
    # CSV stats glossary), same convention as pitching's babip below in
    # _sum_team_pitching — not a formula to derive here, only an aggregate.
    # Weight-averaged by at-bats, the batting-side analogue of how pitching
    # weights its babip by innings-pitched outs.
    babip_num, babip_weight = 0.0, 0
    for t in team_totals_list:
        b = t.get("batting") or {}
        game_ab = b.get("at_bats") or 0
        if b.get("babip") is not None:
            babip_num += b["babip"] * game_ab
            babip_weight += game_ab
    out["babip"] = safe_div(babip_num, babip_weight)

    return out


def _sum_team_pitching(team_totals_list):
    fields = [
        "batters_faced", "walks_allowed", "hit_batters", "strikeouts",
        "hits_allowed", "home_runs_allowed", "runs_allowed", "earned_runs",
        "games_pitched",
        # DS-90: raw pitch count — summed like every other count above, and
        # also the weight for this function's strike_pct aggregation below.
        "total_pitches",
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

    # DS-90: team S% (strike rate) for the new Pitching card — pitch-weighted
    # rather than outs-weighted like babip/fly_ball_pct above, since its
    # natural sample unit is pitches thrown, not innings recorded.
    strike_num, strike_weight = 0.0, 0
    for t in team_totals_list:
        p = t.get("pitching") or {}
        tp = p.get("total_pitches") or 0
        if p.get("strike_pct") is not None:
            strike_num += p["strike_pct"] * tp
            strike_weight += tp
    out["strike_pct"] = safe_div(strike_num, strike_weight)

    return out


def _sum_team_fielding(team_totals_list):
    # DS-75: innings_as_catcher is GameChanger thirds notation ("2.2" = 2 2/3
    # innings, 8 outs), same as innings_pitched — confirmed across both
    # full-season reference CSVs, every P/C/1B/2B/3B/SS/LF/CF/RF/SF/Total
    # innings-played column only ever carries a .0/.1/.2 fractional digit.
    # Summing it as a raw float here was the exact DS-82 bug, just for
    # catching instead of pitching: sum outs, reconstruct the thirds-notation
    # string, same convention _sum_team_pitching already uses for
    # innings_pitched so compute_catching_metrics can parse it the same way.
    from metrics.compute import parse_innings_to_outs

    fields = ["errors", "passed_balls", "stolen_bases_allowed", "runners_caught_stealing",
              "total_chances"]
    out = {f: 0 for f in fields}
    outs_total = 0
    for t in team_totals_list:
        f_ = t.get("fielding") or {}
        for f in fields:
            out[f] += f_.get(f) or 0
        outs_total += parse_innings_to_outs(f_.get("innings_as_catcher")) or 0
    out["innings_as_catcher"] = f"{outs_total // 3}.{outs_total % 3}"
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


# ── Card: Hitting (DS-89) ───────────────────────────────────────────────────
# Placeholder metric set per the Aug 6 2026 design handoff (README.md
# "Signal content" table) — card anatomy (3 ledger rows) is final, the
# metrics themselves are flagged pending final definition. C% formula per
# David 2026-08-06: (AB-K)/AB, from the CSV stats glossary. HH% reuses the
# existing metrics.yml entry (already tappable elsewhere); BABIP is a raw
# parsed field, not computed here (see _sum_team_batting).

def _hitting(bat_totals, contact_ok):
    team_bat_metrics = compute_batting_metrics(
        bat_totals, None, league_age_at_game=None, walks_ok=False, contact_ok=contact_ok,
    )
    c_pct = team_bat_metrics.get("c_pct")
    hh_pct = team_bat_metrics.get("hh_pct")
    babip = bat_totals.get("babip")
    state = "complete" if c_pct is not None else "missing_data"
    return {
        "key": "hitting", "bucket": "Hitting", "state": state,
        "facts": {"c_pct": c_pct, "hh_pct": hh_pct, "babip": babip},
    }


# ── Card: Baserunning (DS-89) ────────────────────────────────────────────────
# Also placeholder metrics per the design handoff. Unlike Hitting, none of
# SB%/CS/PIK need a new formula — stolen_bases/caught_stealing/picked_off
# are plain parsed counts (_sum_team_batting), same treatment as every other
# raw count elsewhere in this file.

def _baserunning(bat_totals):
    sb = bat_totals["stolen_bases"]
    cs = bat_totals["caught_stealing"]
    pik = bat_totals["picked_off"]
    attempts = sb + cs
    sb_pct = safe_div(sb, attempts) * 100 if safe_div(sb, attempts) is not None else None
    state = "insufficient_attempts" if attempts == 0 else "complete"
    return {
        "key": "baserunning", "bucket": "Baserunning", "state": state,
        "facts": {"sb_pct": sb_pct, "cs": cs, "pik": pik, "attempts": attempts},
    }


# ── Card 4: Fielding conversion ─────────────────────────────────────────────

def _fielding_conversion(fld_totals, pit_totals, team_context, games):
    def_eff = compute_team_metrics(team_context).get("def_eff")
    errors = fld_totals["errors"]
    # A season TOTAL cannot be compared to a single game's total, and offering
    # one next to the other invites exactly that. On 2026-08-11 a 4-error game
    # alongside "9 errors" across 8 games produced "that error total matches the
    # team's 9 errors across the full 8-game season in a single contest" — a
    # sentence with no true reading. The per-game rate is the commensurable
    # figure, so supply it and let the total stay context.
    errors_per_game = safe_div(errors, games)
    runs_allowed_per_game = safe_div(pit_totals["runs_allowed"], games)
    state = "complete" if def_eff is not None else "missing_data"
    return {
        "key": "fielding_conversion", "bucket": "Fielding", "state": state,
        "facts": {
            "def_eff": def_eff, "errors": errors,
            "errors_per_game": errors_per_game,
            "runs_allowed_per_game": runs_allowed_per_game, "games": games,
        },
    }


# ── Card 5: Catching load ────────────────────────────────────────────────────

def _catching_load(fld_totals, age_level):
    """PB/inning below 12U (catching is genuinely the headline at 8U-10U —
    the reference Yankees data shows 70 passed balls in 74 innings against
    only 7 steal attempts all season, which is exactly why PB replaces CS%
    below 12U: there isn't enough attempt volume for CS% to mean anything).

    DS-75: delegates the actual formulas and sample gates (10 innings for
    the per-inning rates, 15 attempts for CS%) to compute_catching_metrics,
    which also fixed a live bug here — innings_as_catcher is GameChanger
    thirds notation, and _sum_team_fielding was previously summing it as a
    raw float (same bug class as DS-82, just for catching)."""
    use_pb = (age_level or "").strip().upper() in ("8U", "10U")
    metrics = compute_catching_metrics(fld_totals)
    pb = fld_totals["passed_balls"]
    innings = metrics.get("innings_caught") or 0.0
    cs = fld_totals["runners_caught_stealing"]
    sb = fld_totals["stolen_bases_allowed"]
    attempts = cs + sb

    if use_pb:
        pb_per_inning = metrics.get("pb_per_inning")
        if pb_per_inning is None:
            state = "missing_data" if innings == 0 else "insufficient_attempts"
        else:
            state = "genuine_zero" if pb == 0 else "complete"
        return {
            "key": "catching_load", "bucket": "Catching", "state": state,
            "facts": {"metric": "pb_per_inning", "pb_per_inning": pb_per_inning,
                      "passed_balls": pb, "innings": innings},
        }
    else:
        cs_pct = metrics.get("cs_pct")
        state = "insufficient_attempts" if cs_pct is None else "complete"
        return {
            "key": "catching_load", "bucket": "Catching", "state": state,
            "facts": {"metric": "cs_pct", "cs_pct": cs_pct, "attempts": attempts, "cs": cs},
        }


# ── Card: Pitching (DS-90) ───────────────────────────────────────────────────
# Replaces command_vs_velocity — no place in the new fixed TEAM OFFENCE/TEAM
# DEFENCE page IA for a staff-spread card. K-BB%/BB% reuse
# compute_pitching_metrics's existing formulas (already correct, already
# walks_ok-gated); S% is new — team-level strike rate, pitch-weighted (see
# _sum_team_pitching's strike_pct aggregation below).

def _pitching(pit_totals, team_context, *, walks_ok):
    team_pit_metrics = compute_pitching_metrics(
        pit_totals, team_context, walks_ok=walks_ok, contact_ok=True,
        regulation_innings=None, typical_game_innings=team_context.get("L"),
    )
    s_pct = pit_totals.get("strike_pct")

    if walks_ok:
        k_minus_bb_pct = team_pit_metrics.get("k_minus_bb_pct")
        bb_pct = team_pit_metrics.get("pit_bb_pct")
    else:
        # §6 no-walk rule: K-BB% collapses to K% (same row, different
        # content — compute_pitching_metrics only derives k_pct internally
        # to build k_minus_bb_pct, gated behind walks_ok, so it's recomputed
        # here directly from raw counts since K% itself doesn't depend on
        # walks at all). BB% is genuinely absent, not zero — same
        # convention SUPPRESSED_AT_8U already applies to pit_bb_pct.
        bf = pit_totals.get("batters_faced") or 0
        k = pit_totals.get("strikeouts") or 0
        k_minus_bb_pct = safe_div(k, bf) * 100 if safe_div(k, bf) is not None else None
        bb_pct = None

    state = "complete" if s_pct is not None else "missing_data"
    return {
        "key": "pitching", "bucket": "Pitching", "state": state,
        "facts": {
            "k_minus_bb_pct": k_minus_bb_pct, "s_pct": s_pct, "bb_pct": bb_pct,
            "walks_ok": walks_ok,
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

def _row(name, value, decimals, comparator, *, is_zero=None, metric_key=None):
    # DS-73: metric_key (nullable) marks which rows correspond to a real
    # metrics.yml entry with an explainer — only those render as tappable.
    # A row like "Fielding remainder" or "Errors" is a raw/derived display
    # quantity, not a metrics.yml metric, so it stays inert — no fake
    # affordance for a tap target with nothing to open.
    if value is None:
        return {"name": name, "value": None, "value_display": "—",
                "comparator": comparator, "is_zero": False, "metric_key": metric_key}
    display = f"{value:.{decimals}f}"
    zero = is_zero if is_zero is not None else (round(value, decimals) == 0)
    return {"name": name, "value": value, "value_display": display,
            "comparator": comparator, "is_zero": zero, "metric_key": metric_key}


def card_metric_rows(card):
    f = card["facts"]
    key = card["key"]

    if key == "defensive_split":
        games_note = f"{f['games']} games this season"
        return [
            _row("Runs allowed/game", f["runs_allowed_per_game"], 1, games_note),
            _row("FRA (pitching)", f["fra_pitching_share"], 2, "fair runs the pitching owns", metric_key="fra"),
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
            _row("Team OPSE", f["team_opse"], 3, "on-base + slugging, errors included", metric_key="opse"),
            _row("Team SCORE%", f["team_score_pct"], 1, "of times on base", metric_key="score_pct"),
        ]
    if key == "fielding_conversion":
        # DS-90: trimmed to two rows per the new metric-sets table — runs
        # allowed/game now lives on the Defence module above, not repeated
        # here (the funnel's `f["runs_allowed_per_game"]` fact stays
        # populated in _fielding_conversion below; just not rendered here).
        return [
            _row("DefEff", f["def_eff"], 3, "balls in play converted to outs", metric_key="def_eff"),
            _row("Errors", f["errors"], 0, f"across {f['games']} games", is_zero=(f["errors"] == 0)),
        ]
    if key == "catching_load":
        if f["metric"] == "pb_per_inning":
            return [
                _row("Passed balls/inning", f["pb_per_inning"], 2, "", metric_key="pb_per_inning"),
                _row("Passed balls", f["passed_balls"], 0, f"in {f['innings']:.0f} innings caught",
                     is_zero=(f["passed_balls"] == 0)),
            ]
        return [
            # DS-75: cs_pct comes from compute_catching_metrics already scaled
            # 0-100 (same convention as every other percent metric in this
            # layer, e.g. pit_bb_pct) — do not re-multiply by 100 here.
            _row("Caught stealing %", f["cs_pct"], 1, f"{f['attempts']} attempts all season", metric_key="cs_pct"),
            _row("Runners caught", f["cs"], 0, f"of {f['attempts']} attempts", is_zero=(f["cs"] == 0)),
        ]
    if key == "hitting":
        return [
            _row("Contact %", f["c_pct"], 1, "of at-bats that avoid a strikeout"),
            _row("Hard-hit %", f["hh_pct"], 1, "of balls in play hit hard", metric_key="hh_pct"),
            _row("BABIP", f["babip"], 3, "hits per ball in play"),
        ]
    if key == "baserunning":
        return [
            _row("Stolen base %", f["sb_pct"], 1, f"of {f['attempts']} attempts"),
            _row("Caught stealing", f["cs"], 0, f"of {f['attempts']} attempts", is_zero=(f["cs"] == 0)),
            _row("Picked off", f["pik"], 0, "picked off base", is_zero=(f["pik"] == 0)),
        ]
    if key == "pitching":
        k_bb_name = "K-BB%" if f["walks_ok"] else "K%"
        k_bb_comparator = (
            "strikeouts minus walks, per batter faced" if f["walks_ok"]
            else "of batters faced strike out"
        )
        bb_comparator = "" if not f["walks_ok"] else "of batters faced, not a per-game count"
        return [
            _row(k_bb_name, f["k_minus_bb_pct"], 1, k_bb_comparator),
            _row("S%", f["s_pct"], 1, "of pitches are strikes"),
            _row("BB%", f["bb_pct"], 1, bb_comparator),
        ]
    return []


# ── DS-69: explanation-view context chips (bucket / window / players) ──────
# Code-generated, same discipline as card_metric_rows — no model call, no
# new computation, just reshaping facts already on the card. Team signals
# rarely name individual players (that's a player-signal/DS-57 concept) —
# no card currently has a chip-worthy extra fact beyond bucket/window.

def card_context_chips(card, games_in_sample):
    return [
        {"label": "Bucket", "value": card["bucket"]},
        {"label": "Window", "value": f"{games_in_sample} game{'s' if games_in_sample != 1 else ''} this season"},
    ]
