"""DS-76: append-only history of every computed team signal.

Phase 1 (DS-67) renders all six team signals on every dashboard load, with
no trigger logic — deliberately, because the display/don't-display rule for
a first-of-its-kind feature can't be specified from a desk, only observed.
This module is what makes that observation durable: every computed signal,
on every game commit, gets snapshotted here regardless of whether a future
trigger rule would end up suppressing it. DS-64 Phase 2 sets real thresholds
from this history once 8-10 games exist, instead of guessing a second time.

No consumer yet — DS-67 (which computes the six signals) and DS-74 (the
metrics layer the signals are built from) don't exist yet at the time this
module was written. This is the storage half only: a table and a reusable
write function, ready for DS-67 to call once "signals recompute" is a real
step in the upload flow rather than a hook with nothing to attach to.
"""
import logging

log = logging.getLogger(__name__)


def record_signal_history(sb, team_id, game_id, signals, *, age_band, contact_data_ok):
    """
    Snapshot every computed team signal for one game commit.

    signals: list of dicts, one per signal — 6 for a 12U+ team, 5 at 8U.
        DS-67's finalized spec (later than this module's original req 2
        assumption) is explicit that card 6 "must not be computed or
        rendered" at 8U, not just hidden after computing — the no-walk rule
        means there's no K%/BB% spread across the staff to compute at all,
        so there's nothing for DS-64 Phase 2 to threshold later either.
        Every OTHER card is still recorded regardless of what a future
        trigger would suppress. Each dict:
            {
                "signal_key":      str,   # "T1".."T6"
                "bucket":          str,   # e.g. "Team Defence"
                "headline":        str | None,   # generated copy, as rendered
                "interpretation":  str | None,
                "why_text":        str | None,   # DS-69: explanation view's "Why it might be happening"
                "metrics":         list,  # [{"label":.., "value":.., "comparator":..}, ...]
                "raw_inputs":      dict,  # underlying metric values, UNFORMATTED
                "games_in_sample": int,
            }
    age_band: "8U" / "10U" / "12U" / "14U" — the team's band at computation time.
    contact_data_ok: whether contact tagging was present for this game
        (roughly 1 game in 5 is missing it — see DS-74 req 10). A single flag
        here, at the commit level; DS-74 itself tracks batting/pitching
        separately, but signal-level analysis only needs "was this game's
        contact-dependent data trustworthy" as one bit.

    Append-only (req 3) — this is always an INSERT, never an UPDATE/DELETE;
    also enforced at the database level (signal_history has no update/delete
    RLS policy at all, only select + insert).

    Never fails the upload over this (req 5) — this is instrumentation, not
    a critical path. A write failure is logged and swallowed, same
    contract as _generate_single_game_report_safe's best-effort pattern
    elsewhere in this codebase.
    """
    if not signals:
        return

    rows = [{
        "team_id":          team_id,
        "game_id":          game_id,
        "signal_key":       s["signal_key"],
        "bucket":           s["bucket"],
        "headline":         s.get("headline"),
        "interpretation":   s.get("interpretation"),
        "why_text":         s.get("why_text"),
        "metrics":          s["metrics"],
        "raw_inputs":       s["raw_inputs"],
        "games_in_sample":  s["games_in_sample"],
        "age_band":         age_band,
        "contact_data_ok":  contact_data_ok,
    } for s in signals]

    try:
        sb.table("signal_history").insert(rows).execute()
    except Exception:
        log.exception(
            "signal_history write failed for game_id=%s team_id=%s (%d signals) — "
            "continuing, this must never fail the upload",
            game_id, team_id, len(rows),
        )
