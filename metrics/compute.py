"""
DS-74: Youth metric computation layer.

Compute-on-read, deliberate not a shortcut — every value here is fully
derived from stored stats (batting_stats / pitching_stats / fielding_stats /
teams), so nothing is persisted and a re-import never leaves anything stale.

Formula source: Requirements/Youth_Sabermetrics/ds29-metric-extraction.md
(this session's local copy of the DS-29 research). Three known errors in the
author's companion spreadsheet are corrected here, not reproduced:
  1. Corrected IP's float-modulo threshold typo (understates IP by 0.23
     whenever there's a 1/3-inning remainder).
  2. HH%'s sign on SF (spreadsheet subtracts, should add).
  3. YFIP's printed FB denominator (100, should be BABIP).

Field names below match the real batting_stats / pitching_stats /
fielding_stats / teams columns, not GameChanger's own header abbreviations —
short-name locals inside each function follow the extraction doc's notation
so the formula is recognizable next to its source.
"""

import math

# ── Innings arithmetic ──────────────────────────────────────────────────────
# GameChanger's thirds notation is NOT decimal: "5.2" means 5 and two-thirds
# innings (17 outs), not 5.2 innings, and it is not additive — 5.2 + 5.2 must
# yield 11 1/3 innings (34 outs), never the float sum 10.4. Store and sum
# outs as integers; derive innings only at the point of final division.

def parse_innings_to_outs(ip_value):
    """Parse GameChanger's thirds notation into an integer out count.
    Returns None if ip_value is missing. Raises on a fractional part outside
    {0,1,2} — that's export format drift, not a value to silently coerce."""
    if ip_value is None:
        return None
    ip_str = str(ip_value)
    if "." in ip_str:
        whole, frac = ip_str.split(".", 1)
    else:
        whole, frac = ip_str, "0"
    frac = frac[0]
    if frac not in ("0", "1", "2"):
        raise ValueError(
            f"innings_pitched {ip_value!r} has an unexpected fractional part "
            f"{frac!r} — GameChanger's thirds notation should only ever be "
            f".0/.1/.2. Export format may have changed."
        )
    whole_i = int(whole) if whole not in ("", "-") else 0
    sign = -1 if whole.startswith("-") else 1
    return sign * (abs(whole_i) * 3 + int(frac))


def sum_outs(ip_values):
    """Sum innings pitched across multiple games/rows correctly. Never sum
    raw innings_pitched values directly — that reproduces the same bug this
    function exists to avoid."""
    total = 0
    for ip in ip_values:
        outs = parse_innings_to_outs(ip)
        if outs is not None:
            total += outs
    return total


def corrected_innings(outs):
    """outs / 3, as a float. None in, None out."""
    if outs is None:
        return None
    return outs / 3


# ── Guards ───────────────────────────────────────────────────────────────────

def safe_div(numerator, denominator):
    """Requirement #11: every denominator guard returns None, never raises
    and never silently divides by zero. Callers treat None as 'not
    computable for this sample', not as a real zero."""
    if not denominator:
        return None
    return numerator / denominator


# ── Contact-data-missing detection (requirement #10) ────────────────────────
# GameChanger emits 0.00 / .000 rather than blanks when a scorekeeper skips
# contact tagging, so missing data is indistinguishable from a real zero by
# type alone. The corrupt state is internally inconsistent, so it's
# detectable: a pitcher can't allow hits with BABIP still reading zero, and a
# batter can't have balls in play with every batted-ball-type percentage
# reading zero. Observed rate: roughly 1 game in 5.

def contact_data_missing_batting(row):
    ab = row.get("at_bats") or 0
    so = row.get("strikeouts") or 0
    sf = row.get("sacrifice_flies") or 0
    balls_in_play = ab - so - sf
    gb = row.get("ground_ball_pct") or 0
    ld = row.get("line_drive_pct") or 0
    fb = row.get("fly_ball_pct") or 0
    return balls_in_play > 0 and (gb + ld + fb) == 0


def contact_data_missing_pitching(row):
    h = row.get("hits_allowed") or 0
    babip = row.get("babip")
    return h > 0 and (babip == 0 or babip is None)


# ── Age / league-rule config ─────────────────────────────────────────────────

def walks_enabled(age_level: str) -> bool:
    """8U plays under a no-walk rule (four balls brings a coach in to pitch
    rather than awarding a walk) — confirmed in the source data: zero walks
    across 22 batting PAs and zero allowed pitching for an 8U sample. Walks
    return at 9U. `age_level` is the team's configured band (e.g. "8U",
    "10U", "12U")."""
    return (age_level or "").strip().upper() != "8U"


SUPPRESSED_AT_8U = {"bat_bb_pct", "pit_bb_pct", "k_minus_bb_pct", "pit_fip", "yfip"}


# ── Batting ──────────────────────────────────────────────────────────────────

def _bskl(row, league_age_at_game):
    """Single metric, age-variant formula — not two metrics. Selected from
    league_age_at_game (see module docstring in metrics.yml for the caveat:
    this product has no per-game age history, so the team's current
    players.league_age is used as a stable proxy — league age is fixed by a
    season cutoff in real youth leagues, not continuously drifting, so this
    keeps recomputation stable even though it isn't a true historical
    snapshot)."""
    bb = row.get("walks") or 0
    hhb = row.get("hard_hit_balls") or 0
    xbh = row.get("extra_base_hits") or 0
    hbp = row.get("hit_by_pitch") or 0
    pa = row.get("plate_appearances") or 0
    if league_age_at_game is not None and league_age_at_game >= 11:
        numerator = bb + hbp + 0.5 * hhb + 0.5 * xbh
    else:
        numerator = bb + 0.5 * hhb + 0.5 * xbh
    return safe_div(numerator, pa)


def compute_batting_metrics(row, team_context, *, league_age_at_game, walks_ok, contact_ok):
    """row: a batting_stats-shaped dict for one player (one game, or a
    season-aggregate row — formulas are grain-agnostic; only YRPI/YRPIPA
    carry an explicit min_games gate against single-game use).
    team_context: dict from compute_team_context(); required for YRPI/YRPIPA.
    Returns a dict of {metric_key: value}. A suppressed metric is simply
    absent from the dict — never present as 0 or null-with-a-caveat."""
    out = {}

    pa = row.get("plate_appearances") or 0
    ab = row.get("at_bats") or 0
    bb = row.get("walks") or 0
    h = row.get("hits") or 0
    roe = row.get("reached_on_error") or 0
    hbp = row.get("hit_by_pitch") or 0
    sf = row.get("sacrifice_flies") or 0
    k = row.get("strikeouts") or 0
    hhb = row.get("hard_hit_balls") or 0
    r = row.get("runs_scored") or 0
    ci = row.get("catcher_interference") or 0
    fc = row.get("fielders_choice") or 0
    avg = row.get("batting_average") or 0.0
    slg = row.get("slugging_percentage") or 0.0

    if walks_ok:
        out["bat_bb_pct"] = safe_div(bb, pa) * 100 if safe_div(bb, pa) is not None else None

    out["iso"] = slg - avg

    obpe_den = ab + bb + hbp + sf
    obpe = safe_div(h + bb + roe, obpe_den)
    out["obpe"] = obpe
    out["opse"] = (obpe + slg) if obpe is not None else None

    if contact_ok:
        hh_den = ab - k + sf
        hh = safe_div(hhb, hh_den)
        out["hh_pct"] = hh * 100 if hh is not None else None

        out["bskl"] = _bskl(row, league_age_at_game)

    score_den = h + bb + hbp + ci + fc + roe
    score = safe_div(r, score_den)
    out["score_pct"] = score * 100 if score is not None else None

    if team_context is not None:
        y = team_context.get("y")
        rbi = row.get("runs_batted_in") or 0
        if y is not None:
            yrpi = y * r + y * rbi
            out["yrpi"] = yrpi
            out["yrpipa"] = safe_div(yrpi, pa)

    return out


# ── Pitching ─────────────────────────────────────────────────────────────────

def compute_pitching_metrics(row, team_context, *, walks_ok, contact_ok, regulation_innings, typical_game_innings):
    """row: a pitching_stats-shaped dict for one player.
    team_context: dict from compute_team_context() — required for YFIP.
    regulation_innings / typical_game_innings: from teams config (req #8) —
    regulation_innings feeds nothing here directly (it's the GameChangerL
    cross-check, computed at the team level), typical_game_innings is `L`,
    the config value FRA and YRAL multiply by."""
    out = {}

    bf = row.get("batters_faced") or 0
    if bf <= 0:
        return out  # requirement: all pitching metrics gate on BF > 0

    bb = row.get("walks_allowed") or 0
    hbp = row.get("hit_batters") or 0
    k = row.get("strikeouts") or 0
    h = row.get("hits_allowed") or 0
    hr = row.get("home_runs_allowed") or 0
    r = row.get("runs_allowed") or 0
    babip = row.get("babip")
    weak_pct = row.get("weak_pct")

    outs = parse_innings_to_outs(row.get("innings_pitched"))
    corr_ip = corrected_innings(outs)

    if walks_ok:
        out["pit_bb_pct"] = (bb / bf) * 100
        k_pct = (k / bf) * 100
        out["k_minus_bb_pct"] = k_pct - out["pit_bb_pct"]
        if row.get("fip") is not None:
            out["pit_fip"] = row["fip"]

    weak = None
    if contact_ok and babip is not None:
        weak = safe_div(h - hr, babip)
        if weak is not None:
            weak = (weak_pct or 0) / 100 * (weak + hr)
            out["weak"] = weak

        if weak is not None:
            out["pskl"] = safe_div(k - (bb + hbp) + weak, bf)

    # FPR — full blame for over-the-fence HR (approximated by total HR, per
    # the author's own spreadsheet — GameChanger has no inside-the-park
    # distinction) and for BB/HBP runners; half blame, at the rate those
    # runners actually scored, for everyone else who reached.
    if outs is not None:
        over_hr = hr
        non_hr_reached_denom = bf - outs - over_hr
        non_hr_runners = bb + hbp + 0.5 * (non_hr_reached_denom - bb - hbp)
        non_hr_score_pct = safe_div(r - over_hr, non_hr_reached_denom)
        # DS-74 requirement #11 states the general rule as "return null,
        # never a divide error" for exactly this zero-denominator case (a
        # perfect outing, BF - outs - OverHR == 0). The extraction doc's own
        # FPR section instead says "return 0 ... rather than erroring" for
        # the same case — a real conflict between the two source documents.
        # Following the ticket's explicit, story-level requirement (null)
        # rather than silently picking a side.
        fpr = None
        if non_hr_score_pct is not None:
            fpr = over_hr + non_hr_runners * non_hr_score_pct
        out["fpr"] = fpr

        if fpr is not None and typical_game_innings is not None and corr_ip:
            out["fra"] = (fpr / corr_ip) * typical_game_innings

        if typical_game_innings is not None and corr_ip:
            out["yral"] = (r / corr_ip) * typical_game_innings

    # YFIP — team-dependent, recompute at read time, never cache. Stored but
    # not surfaced (DS-29 decision: FIP ranks pitchers identically on real
    # data and needs no contact-quality fields).
    if (
        walks_ok
        and contact_ok
        and team_context is not None
        and corr_ip
        and babip is not None
        and row.get("fly_ball_pct") is not None
    ):
        fb_pct = row["fly_ball_pct"]
        fb = (fb_pct / 100) * ((safe_div(h - hr, babip) or 0) + hr) if babip else None
        tm_hr = team_context.get("TmHR")
        tm_fb = team_context.get("TmFB")
        tm_era = team_context.get("TmERA")
        l = typical_game_innings
        gcl = team_context.get("GameChangerL")
        if fb is not None and tm_fb:
            yfip = (
                (13 * fb * (tm_hr / tm_fb) if tm_fb else 0)
                + 3 * (bb + hbp)
                - 2 * k
            ) / corr_ip
            if tm_era is not None and l is not None and gcl:
                yfip += tm_era * (l / gcl)
            out["yfip"] = yfip

    return out


# ── Team context (feeds YRPI/YRPIPA, YFIP, FRA, YRAL) ───────────────────────

def compute_team_context(team_totals_list, *, regulation_innings):
    """Team-season aggregates used by the team-dependent metrics.

    `team_totals_list`: one `games.team_totals` dict per game in the window
    — GameChanger's own computed "Totals" row for that game, captured
    verbatim by the parser (see stats.py's Totals-row handling), shaped
    `{"batting": {...}, "pitching": {...}, "fielding": {...}}`. Deliberately
    NOT derived by summing/weighting individual player rows: verified against
    the reference workbooks that this drifts from GameChanger's own
    computation for the rate-based fields (team BABIP, FB%, games pitched)
    — team games-pitched in particular is not summable from individual
    pitchers' own games-pitched counts at all, and was the largest source of
    error before this fix.

    Raw counts (runs, hits, errors, etc.) sum exactly across any number of
    games. Team-level rate stats (BABIP, FB%) don't have a GameChanger-exact
    multi-game recomputation available, so they're innings-weighted across
    each game's own (GameChanger-exact-for-that-game) rate — which collapses
    to an exact match for the single-game/single-upload case this ticket's
    AC #2 validates against, and is a principled approximation for a true
    multi-game window.

    Must be recomputed over the SAME window as whatever player values it's
    paired with — mixing a season-scoped team_context with a last-3-games
    player row silently breaks YRPI's conservation property (player YRPI
    values summing exactly to team runs)."""
    bat_rows = [t["batting"] for t in team_totals_list if t.get("batting")]
    pit_rows = [t["pitching"] for t in team_totals_list if t.get("pitching")]
    fld_rows = [t["fielding"] for t in team_totals_list if t.get("fielding")]

    team_r = sum((b.get("runs_scored") or 0) for b in bat_rows)
    team_rbi = sum((b.get("runs_batted_in") or 0) for b in bat_rows)
    team_pa = sum((b.get("plate_appearances") or 0) for b in bat_rows)

    tm_h = sum((p.get("hits_allowed") or 0) for p in pit_rows)
    tm_hr = sum((p.get("home_runs_allowed") or 0) for p in pit_rows)
    tm_er = sum((p.get("earned_runs") or 0) for p in pit_rows)
    team_e = sum((f.get("errors") or 0) for f in fld_rows)

    # Team games pitched: sum each Totals row's own games_pitched field
    # (each row already represents however many games that upload covers —
    # 1 for a genuine single-game upload, N for a season-cumulative
    # re-upload). NOT a sum of individual PLAYERS' games-pitched counts
    # (that double/triple counts every game a team used >1 pitcher) — these
    # rows are team-level Totals rows, not per-player rows, so the same
    # field name means something different here.
    tm_pitch_gp = sum((p.get("games_pitched") or 0) for p in pit_rows)

    tm_outs = sum_outs(p.get("innings_pitched") for p in pit_rows)
    tm_corr_ip = corrected_innings(tm_outs)

    def _innings_weighted(field):
        weighted_sum, weight_total = 0.0, 0
        for p in pit_rows:
            val = p.get(field)
            if val is None:
                continue
            outs = parse_innings_to_outs(p.get("innings_pitched")) or 0
            weighted_sum += val * outs
            weight_total += outs
        return safe_div(weighted_sum, weight_total)

    tm_babip = _innings_weighted("babip")
    tm_fb_pct = _innings_weighted("fly_ball_pct")

    tm_era = safe_div(tm_er * 6, tm_corr_ip)  # GameChanger's own ERA convention (regulation-length multiplier baked in)

    typical_game_innings = safe_div(tm_corr_ip, tm_pitch_gp)  # L — requirement #8, derived, never configured

    # B37 — reverse-engineers GC's assumed regulation length from the team
    # aggregate directly. Validation-only cross-check (feeds DS-77's settings
    # mismatch banner via infer_regulation_innings in app.py, not one of this
    # layer's surfaced metrics) — the source material itself describes it as
    # fragile and rounding, not a value AC #2 needs to match precisely.
    game_changer_l = safe_div((tm_era or 0) * (tm_corr_ip or 0), tm_er) if tm_er else None

    y = safe_div(team_r, team_r + team_rbi)

    tm_fb = None
    if tm_fb_pct is not None and tm_babip:
        tm_fb = (tm_fb_pct / 100) * ((safe_div(tm_h - tm_hr, tm_babip) or 0) + tm_hr)

    tm_bip = safe_div(tm_h, tm_babip)

    def_eff = None
    if tm_bip:
        def_eff = 1 - (tm_h + team_e) / tm_bip

    return {
        "TeamR": team_r, "TeamRBI": team_rbi, "TeamPA": team_pa,
        "TmH": tm_h, "TmHR": tm_hr, "TmBABIP": tm_babip, "TmFB%": tm_fb_pct,
        "TmERA": tm_era, "TmER": tm_er, "TmCorrIP": tm_corr_ip, "TmPitchGP": tm_pitch_gp,
        "TeamE": team_e, "L": typical_game_innings, "GameChangerL": game_changer_l,
        "y": y, "TmFB": tm_fb, "TmBIP": tm_bip, "DefEff": def_eff,
    }


def compute_team_metrics(team_context):
    """DefEff — team-only; player cells render as a dash, never a value."""
    return {"def_eff": team_context.get("DefEff")}
