"""
DS-102: shared loading of the team-signal inputs.

`team_signals.py` is deliberately database-free — it computes cards from
data handed to it. This module is its counterpart: it assembles that data
from Supabase once, so every surface that shows signals feeds the same
inputs to the same computation.

Three call sites built this independently before this existed — the
dashboard, the per-commit signal snapshot, and the single-game report was
about to become a fourth. Divergence between them would surface as the same
team showing different signals depending on which page the coach opened,
which is exactly the class of inconsistency this codebase has already been
bitten by elsewhere.
"""


def load_signal_inputs(sb, team_id, *, games=None, team=None) -> dict:
    """
    Gather everything compute_team_signals needs for one team.

    `games` and `team` may be passed in when the caller has already fetched
    them, to avoid a second round trip; both are otherwise loaded here.

    Returns a dict rather than a tuple because callers need different
    subsets — the dashboard wants team_totals_list for its sample count,
    the report wants only the cards.
    """
    if team is None:
        resp = sb.table("teams").select("*").eq("id", team_id).limit(1).execute()
        team = resp.data[0] if resp.data else {}

    if games is None:
        resp = sb.table("games").select("team_totals").eq("team_id", team_id).execute()
        games = resp.data or []

    team_totals_list = [g["team_totals"] for g in games if g.get("team_totals")]

    # Per-pitcher season totals. compute_team_signals wants one row per
    # pitcher across the season, not per game.
    pitching = sb.table("pitching_stats").select("*").eq("team_id", team_id).execute()
    by_player = {}
    for row in (pitching.data or []):
        agg = by_player.setdefault(row["player_id"], {
            "batters_faced": 0, "walks_allowed": 0, "strikeouts": 0,
        })
        agg["batters_faced"] += row.get("batters_faced") or 0
        agg["walks_allowed"] += row.get("walks_allowed") or 0
        agg["strikeouts"]    += row.get("strikeouts") or 0

    return {
        "team":              team,
        "age_level":         team.get("age_level") or "12U",
        "team_totals_list":  team_totals_list,
        "pitcher_rows":      list(by_player.values()),
    }


def compute_signals_for_team(sb, team_id, *, games=None, team=None) -> dict:
    """
    Load the inputs and compute the signal cards.

    Returns the same dict as load_signal_inputs plus `cards`, which is empty
    when the team has no game totals yet — callers should treat an empty
    list as "nothing to say", not as an error.
    """
    from signals.team_signals import compute_team_signals

    inputs = load_signal_inputs(sb, team_id, games=games, team=team)
    if not inputs["team_totals_list"]:
        return {**inputs, "cards": []}

    inputs["cards"] = compute_team_signals(
        inputs["team_totals_list"],
        inputs["pitcher_rows"],
        age_level=inputs["age_level"],
        regulation_innings=inputs["team"].get("regulation_innings") or 6,
    )
    return inputs
