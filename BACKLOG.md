# Dugout Signals — Backlog

Future features identified but intentionally out of scope so far. Newest first.

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
- Season-over-season report (All-Stars 2025 vs 2026) — needs 2025 data imported first.

### Resolved
- **[DS-38](https://dugoutsignals.atlassian.net/browse/DS-38)** — `fps_pct` in the pitching game log was
  reading `era` instead of the real column; `v_player_stats.fps_pct` was also
  ambiguous (batter stat, misleadingly named). Fixed: `data.py` now reads
  `pitching_stats.fps_pct` directly, and `v_player_stats` exposes
  `batter_fps_pct` / `pitcher_fps_pct` separately.

### Superseded
- ~~Batting-stat column audit + backfill~~ → folded into
  **[DS-43](https://dugoutsignals.atlassian.net/browse/DS-43)** — Capture all
  columns from GameChanger's per-game Stats CSV export (covers batting,
  the full pitch-type breakdown, and fielding, not just batting).
