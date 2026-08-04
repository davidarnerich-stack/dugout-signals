"""
DS-68: Bucket context modules — the Offence and Defence hierarchy cards.

Deliberately NOT a second computation pass. Every number here already exists
in DS-67's `signal_cards` (built from the same DS-74 metrics layer) — this
module only reshapes those same facts into the two-level containment
hierarchy the design spec calls for (outcome card holding its buckets as
chips), plus the two bar geometries (defensive split's two-segment
proportional bar, the offence funnel's nested bars). Single source of truth:
if DS-67's numbers are right, DS-68's are right by construction.

Each chip anchors to its own DS-67 signal card (`#signal-<key>`) rather than
to a generic section — "Pitching → FRA" means the defensive_split card,
which has FRA, not the command_vs_velocity card, which is also bucketed
Pitching but a different metric entirely.
"""

from metrics.compute import safe_div


def _cards_by_key(signal_cards):
    return {c["key"]: c for c in signal_cards}


def _chip(bucket, metric_name, value, value_display, anchor_key, *, state="complete"):
    return {
        "bucket": bucket, "metric_name": metric_name, "value": value,
        "value_display": value_display, "anchor": f"signal-{anchor_key}",
        "state": state,
    }


def build_offence_module(signal_cards):
    by_key = _cards_by_key(signal_cards)
    run_gap = by_key.get("run_gap")
    funnel = by_key.get("offence_funnel")
    if not run_gap or not funnel:
        return None

    runs_per_game = run_gap["facts"].get("runs_scored_per_game")
    obpe = funnel["facts"].get("team_obpe")
    score_pct = funnel["facts"].get("team_score_pct")
    # AC #8: omit rather than show a module with genuinely nothing to say.
    if runs_per_game is None:
        return None

    # Bar 1 ("get on base") fills by OBPE directly (0-1 already). Bar 2
    # ("get home") is drawn at bar 1's fill WIDTH and filled by SCORE% of
    # THAT — a share of the runners, not of everything (design spec §4).
    bar1_pct = min(max(obpe or 0.0, 0.0), 1.0)
    bar2_pct = min(max((score_pct or 0.0) / 100, 0.0), 1.0)

    chips = [
        _chip("Hitting", "OPSE", funnel["facts"].get("team_opse"),
              f"{funnel['facts']['team_opse']:.3f}" if funnel["facts"].get("team_opse") is not None else "—",
              "offence_funnel", state=funnel["state"]),
        _chip("Baserunning", "SCORE%", score_pct,
              f"{score_pct:.1f}%" if score_pct is not None else "—",
              "offence_funnel", state=funnel["state"]),
    ]
    return {
        "runs_per_game": runs_per_game, "bar1_pct": bar1_pct, "bar2_pct": bar2_pct,
        "chips": chips, "anchor": "signal-offence_funnel",
    }


def build_defence_module(signal_cards):
    by_key = _cards_by_key(signal_cards)
    split = by_key.get("defensive_split")
    fielding = by_key.get("fielding_conversion")
    catching = by_key.get("catching_load")
    if not split:
        return None

    runs_allowed = split["facts"].get("runs_allowed_per_game")
    pitching_share = split["facts"].get("fra_pitching_share")
    fielding_share = split["facts"].get("fielding_remainder")
    if runs_allowed is None:
        return None

    pitching_pct = safe_div(pitching_share, runs_allowed) if pitching_share is not None else None
    fielding_pct = safe_div(fielding_share, runs_allowed) if fielding_share is not None else None

    chips = [
        _chip("Pitching", "FRA", pitching_share,
              f"{pitching_share:.2f}" if pitching_share is not None else "—",
              "defensive_split", state=split["state"]),
    ]
    if fielding:
        def_eff = fielding["facts"].get("def_eff")
        chips.append(_chip("Fielding", "DefEff", def_eff,
                            f"{def_eff:.3f}" if def_eff is not None else "—",
                            "fielding_conversion", state=fielding["state"]))
    if catching:
        f = catching["facts"]
        if f.get("metric") == "pb_per_inning":
            val = f.get("pb_per_inning")
            display = f"{val:.2f}" if val is not None else "—"
            name = "PB/inning"
        else:
            val = f.get("cs_pct")
            display = f"{val * 100:.1f}%" if val is not None else "—"
            name = "CS%"
        chips.append(_chip("Catching", name, val, display, "catching_load", state=catching["state"]))

    return {
        "runs_allowed_per_game": runs_allowed,
        "pitching_share": pitching_share, "fielding_share": fielding_share,
        "pitching_pct": pitching_pct, "fielding_pct": fielding_pct,
        "chips": chips, "anchor": "signal-defensive_split",
    }


def build_bucket_hierarchy(signal_cards):
    """Returns {"offence": {...} | None, "defence": {...} | None}."""
    return {
        "offence": build_offence_module(signal_cards),
        "defence": build_defence_module(signal_cards),
    }
