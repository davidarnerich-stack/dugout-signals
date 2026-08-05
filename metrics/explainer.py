"""
DS-73: Metric explainer — assembles the render-time payload for the metric
explanation view from explainers.yml's authored content plus the real
computed values (own-team value, familiar-anchor value). Content itself is
never model-generated (req 7); this module only substitutes real numbers
into already-written copy — same "code decides what appears, the model (or
here, the author) only supplies the words" discipline as team_signals.py.

Scope note: only builds an entry for a metric that's actually live on the
current signal_cards render (see explainers.yml's header comment) — no
fabricated entry for a metric the team has no card/value for.
"""

import os
import yaml

_EXPLAINERS_PATH = os.path.join(os.path.dirname(__file__), "explainers.yml")
_METRICS_PATH = os.path.join(os.path.dirname(__file__), "metrics.yml")


def _load_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)


def _fmt(value, decimals):
    if value is None:
        return "—"
    return f"{value:.{decimals}f}"


def build_metric_explainers(signal_cards, familiar_anchors):
    """signal_cards: DS-67's computed cards (compute_team_signals output).
    familiar_anchors: compute_familiar_anchors() output (team ERA/OPS/
    Runs/FieldingPct).

    Returns {metric_key: explainer_dict}, one entry per metric with a real
    value on this render. Each dict carries everything the client needs to
    render either structure from req 4 without any further lookup."""
    explainers_cfg = _load_yaml(_EXPLAINERS_PATH)
    metrics_cfg = _load_yaml(_METRICS_PATH)
    metrics_by_key = {m["key"]: m for m in metrics_cfg["metrics"]}
    attribution_text = explainers_cfg["attribution_text"]

    # Pull each target metric's current team value straight from the
    # already-computed signal_cards facts — single source of truth, no
    # recomputation, same values already rendered on the cards themselves.
    by_card_key = {c["key"]: c for c in signal_cards}
    own_values = {}

    defensive_split = by_card_key.get("defensive_split")
    if defensive_split:
        own_values["fra"] = defensive_split["facts"].get("fra_pitching_share")

    offence_funnel = by_card_key.get("offence_funnel")
    if offence_funnel:
        own_values["opse"] = offence_funnel["facts"].get("team_opse")
        own_values["score_pct"] = offence_funnel["facts"].get("team_score_pct")

    fielding_conversion = by_card_key.get("fielding_conversion")
    if fielding_conversion:
        own_values["def_eff"] = fielding_conversion["facts"].get("def_eff")

    catching_load = by_card_key.get("catching_load")
    if catching_load:
        f = catching_load["facts"]
        if f.get("metric") == "pb_per_inning":
            own_values["pb_per_inning"] = f.get("pb_per_inning")
        else:
            own_values["cs_pct"] = f.get("cs_pct")

    out = {}
    for entry in explainers_cfg["explainers"]:
        key = entry["metric_key"]
        if key not in own_values:
            continue  # not live on this render — no fake entry (req: no fabricated affordances)
        m = metrics_by_key[key]
        decimals = m["scale"]["precision"]
        own_value = own_values[key]
        own_display = _fmt(own_value, decimals)
        if m["scale"]["type"] == "percent" and own_value is not None:
            own_display += "%"

        payload = {
            "metric_key": key,
            "display_name": m["display_name"],
            "is_novel": entry["is_novel"],
            "own_team_value_display": own_display,
            "what_it_measures": entry["what_it_measures"].strip(),
            "caveat": (entry.get("caveat") or "").strip() or None,
            "attribution": attribution_text if entry.get("attribution") else None,
        }
        if entry["is_novel"]:
            anchor = entry["familiar_anchor"]
            anchor_value = familiar_anchors.get(anchor["anchor_key"])
            payload["familiar_anchor_label"] = anchor["label"]
            payload["familiar_anchor_value_display"] = _fmt(anchor_value, anchor["decimals"])
            payload["why_more_useful"] = entry["why_more_useful"].strip()
        else:
            payload["how_to_read_at_age"] = entry["how_to_read_at_age"].strip()

        out[key] = payload

    return out
