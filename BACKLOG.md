# Dugout Signals — Backlog

Future features identified but intentionally out of scope so far. Newest first.

## Batting-stat column audit + backfill
**Status:** Not started · **Origin:** June 2026 pitching audit

The June 2026 audit found GameChanger was exporting **14 pitching columns** the
upload pipeline never saved (strike %, first-pitch-strike %, FIP, etc.). Those
are now captured (see `parsers/stats.py` `PIT_COLS` / `_pitching_dict`).

The **same gap almost certainly exists on the hitting side.** GameChanger's CSV
exports batting columns that `batting_stats` may not be capturing. Do for batting
what we did for pitching:
1. Diff the batting headers in a real Storm CSV against the columns written in
   `parsers/stats.py` `BAT_COLS`.
2. Add any missing columns to the `batting_stats` table + `BAT_COLS` map.
3. Backfill historical games.

## Opponent strike-% benchmarking
**Status:** Not started · **Origin:** June 2026 pitching audit

Today we only store **our own** pitchers' strike% (`pitching_stats.strike_pct`).
Opponent box-score PDFs include the opposing pitcher's pitch/strike counts —
e.g. a line like `Rhyan R 59-36` means 59 pitches, 36 strikes (~61% strikes).
We don't capture that anywhere.

Capturing it would let us benchmark our pitchers' strike% against the pitchers
they actually face. Likely shape:
1. New `opponent_pitching_stats` table.
2. Parse the opponent pitching lines out of the box-score PDF (`parsers/box_scores.py`).
3. Surface a comparison in reports.

---

### Related notes (from the June 2026 briefing, §8)
- `v_player_stats` view computes `fps_pct` as a *batter* stat (pitches seen
  starting with a strike), not a pitcher stat — name is misleading. Consider
  renaming to `batter_fps_pct` and adding a pitcher-side `pitcher_fps_pct` that
  reads from `pitching_stats.fps_pct`.
- Season-over-season report (All-Stars 2025 vs 2026) — needs 2025 data imported first.
