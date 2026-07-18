# Dugout Signals — Claude Code Session Briefing
**Purpose:** Update the CSV upload pipeline at `https://upload.dugoutsignals.ai/` to capture newly discovered pitching stat columns, add duplicate detection, and implement a two-step upload confirmation flow.

**Prepared from:** Claude.ai conversation session, June 2026  
**Hand this document to Claude Code as the first message of your session.**

---

## 1. Context & Background

Dugout Signals is a custom softball analytics application for the Storm 12U Silver All-Stars. It uses a **Supabase** backend (project ID: `bqjbswbxtyapupufoarv`) and an upload pipeline at `https://upload.dugoutsignals.ai/` that accepts **GameChanger CSV exports** and ingests them into the database.

During a data audit in June 2026, we discovered that the CSV ingestion pipeline was **not capturing several important pitching statistics** that GameChanger exports in every game CSV. These columns exist in the CSV but were never written to Supabase. We have now:

1. Added the missing columns to the `pitching_stats` table in Supabase (migration already applied — see Section 3)
2. Backfilled historical data for all 22 All-Star season games manually
3. Need the upload pipeline updated so **all future CSV uploads capture these columns automatically**

We also identified two UX gaps in the upload flow that need to be addressed: **duplicate upload detection** and **late/missed game upload handling**.

---

## 2. Supabase Schema

**Project ID:** `bqjbswbxtyapupufoarv`

### Key tables relevant to this work

**`games`**
- `game_id` (UUID, PK)
- `game_date` (DATE)
- `opponent_name` (TEXT)
- `tournament_name` (TEXT, nullable — some games use `tournament_id` FK instead)
- `tournament_id` (UUID, FK → `tournaments.tournament_id`)
- `game_type` (TEXT: `'tournament'` or `'scrimmage'`)
- `team_runs` (INTEGER)
- `opponent_runs` (INTEGER)
- `team_name` (TEXT)
- `season` (TEXT, e.g. `'Summer 2026'`)

**`players`**
- `player_id` (UUID, PK)
- `first_name`, `last_name` (TEXT)
- `number` (TEXT — jersey number as string)
- `team_name` (TEXT)

**`pitching_stats`** — full column list after migration:
```
stat_id, game_id, player_id,
innings_pitched, games_pitched, games_started, batters_faced, total_pitches,
wins, losses, saves, save_opportunities, blown_saves,
hits_allowed, runs_allowed, earned_runs, walks_allowed, strikeouts,
strikeouts_looking, hit_batters, home_runs_allowed, wild_pitches,
era, whip, batting_average_against, strikeouts_per_9, walks_per_9,
balks, pickoffs, stolen_bases_against, cs_against, leadoff_outs,
one_two_three_innings, created_at,
-- NEW COLUMNS (added June 2026, backfill complete):
strikes_thrown, first_pitch_strikes, strike_pct, fps_pct,
fpso_pct, fpsw_pct, fpsh_pct,
k_per_bf, k_per_bb, bb_per_inn, zero_bb_inn,
p_per_ip, fip, sm_pct
```

**`tournaments`**
- `tournament_id` (UUID, PK)
- `name` (TEXT)
- `season` (TEXT)
- `year` (INTEGER)

**`unmatched_box_score_players`** — holds opponent roster data (not relevant to this task)

**`batting_stats`** — batting stats per player per game (not the focus of this task but same pattern)

---

## 3. Migration Already Applied

The following migration was already executed in Supabase. **Do not run it again.** It is included here for reference so Claude Code understands the current schema state.

```sql
ALTER TABLE pitching_stats
  ADD COLUMN IF NOT EXISTS strikes_thrown       INTEGER,
  ADD COLUMN IF NOT EXISTS first_pitch_strikes  INTEGER,
  ADD COLUMN IF NOT EXISTS strike_pct           NUMERIC(5,2),
  ADD COLUMN IF NOT EXISTS fps_pct              NUMERIC(5,2),
  ADD COLUMN IF NOT EXISTS fpso_pct             NUMERIC(5,2),
  ADD COLUMN IF NOT EXISTS fpsw_pct             NUMERIC(5,2),
  ADD COLUMN IF NOT EXISTS fpsh_pct             NUMERIC(5,2),
  ADD COLUMN IF NOT EXISTS k_per_bf             NUMERIC(5,3),
  ADD COLUMN IF NOT EXISTS k_per_bb             NUMERIC(5,2),
  ADD COLUMN IF NOT EXISTS bb_per_inn           NUMERIC(5,3),
  ADD COLUMN IF NOT EXISTS zero_bb_inn          INTEGER,
  ADD COLUMN IF NOT EXISTS p_per_ip             NUMERIC(5,1),
  ADD COLUMN IF NOT EXISTS fip                  NUMERIC(5,2),
  ADD COLUMN IF NOT EXISTS sm_pct               NUMERIC(5,2);
```

---

## 4. GameChanger CSV Structure

Every GameChanger game export CSV follows this structure:

- **Row 0:** Column headers (stat names as strings)
- **Rows 1+:** One row per player (both teams)
- **Key identifier columns:** `[0]` = jersey number, `[1]` = last name, `[2]` = first name

The CSV contains **both batting and pitching sections** for each player. Pitching stats only appear for players who actually pitched (IP > 0).

### Pitching columns and their CSV header names

The following are the **exact string values** that appear in row 0 of the CSV for the columns we need to capture. Column positions are not fixed — always look up by header name, not index.

| CSV Header | Supabase Column | Type | Notes |
|---|---|---|---|
| `IP` | `innings_pitched` | NUMERIC | Already captured |
| `GP` | `games_pitched` | INTEGER | Already captured |
| `GS` | `games_started` | INTEGER | Already captured |
| `BF` | `batters_faced` | INTEGER | Already captured |
| `#P` | `total_pitches` | INTEGER | Already captured |
| `W` | `wins` | INTEGER | Already captured |
| `L` | `losses` | INTEGER | Already captured |
| `H` | `hits_allowed` | INTEGER | Already captured |
| `R` | `runs_allowed` | INTEGER | Already captured |
| `ER` | `earned_runs` | INTEGER | Already captured |
| `BB` | `walks_allowed` | INTEGER | Already captured |
| `SO` | `strikeouts` | INTEGER | Already captured |
| `K-L` | `strikeouts_looking` | INTEGER | Already captured |
| `HBP` | `hit_batters` | INTEGER | Already captured |
| `ERA` | `era` | NUMERIC | Already captured |
| `WHIP` | `whip` | NUMERIC | Already captured |
| `WP` | `wild_pitches` | INTEGER | Already captured |
| `BAA` | `batting_average_against` | NUMERIC | Already captured |
| `123INN` | `one_two_three_innings` | INTEGER | Already captured |
| `LOO` | `leadoff_outs` | INTEGER | Already captured |
| `S%` | `strike_pct` | NUMERIC(5,2) | **NEW — add to pipeline** |
| `FPS%` | `fps_pct` | NUMERIC(5,2) | **NEW — add to pipeline** |
| `FPSO%` | `fpso_pct` | NUMERIC(5,2) | **NEW — add to pipeline** |
| `FPSW%` | `fpsw_pct` | NUMERIC(5,2) | **NEW — add to pipeline** |
| `FPSH%` | `fpsh_pct` | NUMERIC(5,2) | **NEW — add to pipeline** |
| `K/BF` | `k_per_bf` | NUMERIC(5,3) | **NEW — add to pipeline** |
| `K/BB` | `k_per_bb` | NUMERIC(5,2) | **NEW — add to pipeline** |
| `BB/INN` | `bb_per_inn` | NUMERIC(5,3) | **NEW — add to pipeline** |
| `0BBINN` | `zero_bb_inn` | INTEGER | **NEW — add to pipeline** |
| `P/IP` | `p_per_ip` | NUMERIC(5,1) | **NEW — add to pipeline** |
| `FIP` | `fip` | NUMERIC(5,2) | **NEW — add to pipeline** |
| `SM%` | `sm_pct` | NUMERIC(5,2) | **NEW — add to pipeline** |

### Derived columns to compute during ingestion

Two columns should be **computed from the CSV percentages × raw totals** rather than stored directly, because the CSV stores percentages but not the raw integer counts:

```javascript
// strikes_thrown = derived from S% × total_pitches
strikes_thrown = Math.round(total_pitches * (strike_pct / 100))

// first_pitch_strikes = derived from FPS% × batters_faced
first_pitch_strikes = Math.round(batters_faced * (fps_pct / 100))
```

### NULL handling rules

- If a stat value in the CSV is `'0.00'`, `'0.0'`, `'0'`, `''`, `'-'`, or `NaN`: treat as **NULL** for percentage columns (`S%`, `FPS%`, `FPSO%`, `FPSW%`, `FPSH%`, `SM%`, `K/BB`, `BB/INN`, `P/IP`, `FIP`)
- Exception: `0BBINN` (zero-walk innings) — `0` is a valid real value, store it as `0` not NULL
- Exception: `K/BB` — if walks = 0, K/BB is undefined/infinity in GC; store as NULL

### Player matching

Players in the CSV are identified by jersey number + name. Match to `players` table using:
1. Jersey number (`players.number`) — primary key for matching within a team
2. Team context — always scope to Storm players only (`team_name ILIKE '%storm 12u%'`)
3. If a jersey number appears in the CSV but is not found in `players`, log it as unmatched and skip (do not error out the whole upload)

---

## 5. Upload Pipeline Changes Required

### 5a. Add new pitching stat columns to the ingestion write

Find the section of the ingestion code that builds the `pitching_stats` INSERT or UPSERT payload and add all 14 new columns listed in Section 4.

The pattern should follow whatever the existing code uses for columns like `fps_pct`, `k_per_bf`, etc. If the existing code uses a column mapping object or array, extend it. If it builds a SQL string directly, add the new columns to that string.

**Critical:** Use `UPSERT` (INSERT ... ON CONFLICT DO UPDATE) keyed on `(game_id, player_id)` — not a plain INSERT. This ensures re-uploading a CSV updates existing records rather than creating duplicates.

### 5b. Implement two-step upload confirmation flow

The current pipeline likely does a single-step write: receive CSV → parse → write to DB. This needs to become a two-step flow:

**Step 1 — Preview (no DB writes)**
When a CSV is uploaded, parse it and return a preview payload to the frontend before writing anything. The preview should include:

```json
{
  "game": {
    "date": "2026-05-24",
    "opponent": "SCGS Strelitz",
    "score": "8-3",
    "tournament": "San Clemente Tournament",
    "game_id": "fb73af28-..."
  },
  "pitchers": [
    { "number": "2", "name": "Addison Arnerich", "ip": "2.2", "bf": 13, "pitches": 58, "s_pct": 58.62, "fps_pct": 53.85 },
    { "number": "4", "name": "Isabella Villaseñor", "ip": "1.1", "bf": 6, "pitches": 26, "s_pct": 50.0, "fps_pct": 50.0 }
  ],
  "batters_count": 12,
  "already_exists": true,
  "conflict_detail": "This game already has pitching stats for 2 pitchers and batting stats for 12 players. Uploading will overwrite existing data."
}
```

The `already_exists` flag should be `true` if `pitching_stats` already has rows for this `game_id`. The frontend displays this preview and asks the coach to confirm before writing.

**Step 2 — Confirmed write**
Only after the coach clicks Confirm does the pipeline execute the DB writes. Pass a `confirmed: true` flag or use a second endpoint (e.g. `/upload/confirm` with a session token from step 1).

### 5c. Duplicate detection logic

Before writing, check if `pitching_stats` already contains rows for the matched `game_id`:

```sql
SELECT COUNT(*) FROM pitching_stats WHERE game_id = $1;
```

If count > 0, set `already_exists = true` in the preview payload. The frontend must surface this clearly — not as an error, but as an informational warning that requires explicit confirmation.

### 5d. Game matching logic

The pipeline needs to match a CSV to an existing `games` record. Use this matching strategy in order of preference:

1. **Filename date + score match** — extract date from filename (format: `YYYY-MM-DD`), compute Storm runs from batting totals and opponent runs from pitching totals in the CSV, query `games` for `game_date = date AND team_runs = storm_runs AND opponent_runs = opp_runs`
2. **Date + opponent name** — if score match is ambiguous, additionally filter by `opponent_name ILIKE '%<opponent_fragment>%'`
3. **No match found** — if no `games` record can be matched, return an error in the preview payload: `"game_not_found": true`. Do not create a game record automatically. Instruct the coach to create the game first in the app, then re-upload the CSV.

### 5e. Orphaned CSV handling

If a coach uploads a CSV for a game that doesn't exist yet in the `games` table:
- Return a clear preview error: *"We couldn't match this file to an existing game. Please create the game record in Dugout Signals first, then re-upload this file."*
- Do not write any data
- Do not error silently

---

## 6. Frontend UX — Confirmation Screen

When `already_exists: true` is returned in the preview, the UI should display something like:

> **Game found:** May 24 vs SCGS Strelitz (W 8–3) · San Clemente Tournament  
> **Pitchers found in file:** Addison Arnerich (#2), Isabella Villaseñor (#4)  
> **⚠ This game already has data.** Uploading will overwrite existing pitching and batting stats for this game.  
> [Cancel] [Confirm & Upload]

When `already_exists: false`:

> **Game found:** May 24 vs SCGS Strelitz (W 8–3) · San Clemente Tournament  
> **Pitchers found in file:** Addison Arnerich (#2), Isabella Villaseñor (#4)  
> **12 batters** will also be imported.  
> [Cancel] [Confirm & Upload]

When `game_not_found: true`:

> **⚠ Game not found.** We couldn't match this file to a game in Dugout Signals. Please create the game record first, then re-upload.  
> [OK]

---

## 7. Testing Checklist

After making changes, verify the following:

- [ ] Upload a fresh CSV for a game that has no existing data → stats appear correctly in Supabase including all 14 new columns
- [ ] Confirm `strikes_thrown` = `round(total_pitches × strike_pct / 100)` matches expected value
- [ ] Confirm `first_pitch_strikes` = `round(batters_faced × fps_pct / 100)` matches expected value
- [ ] Upload the same CSV a second time → preview shows `already_exists: true` with conflict warning
- [ ] Confirm overwrites work correctly — values in Supabase match the re-uploaded CSV
- [ ] Upload a CSV with no matching game → returns `game_not_found: true`, no data written
- [ ] Upload a CSV where one pitcher is not in the `players` table → that player is skipped, others are written correctly, unmatched player is logged
- [ ] `0BBINN = 0` is stored as integer `0`, not NULL
- [ ] `K/BB` where walks = 0 is stored as NULL, not infinity or error
- [ ] `S%` and `FPS%` values in Supabase match what GameChanger shows in its UI for the same game

---

## 8. Known Issues & Future Enhancements (Out of Scope for This Session)

The following were identified during the June 2026 audit but are explicitly **not** in scope for this Claude Code session. Log them as backlog items:

- **Opponent pitcher S% benchmarking:** Box score PDFs contain P-S lines for opponent pitchers (e.g., "Rhyan R 59-36") which could be used to derive S% for pitchers Addison faced. Not currently captured anywhere. Future enhancement: parse box score PDFs or opponent CSVs and store in a separate `opponent_pitching_stats` table.
- **Batting stat gaps:** The same audit approach should be applied to `batting_stats` — verify which GameChanger batting columns are currently captured vs. available in the CSV. A future session should backfill and pipeline-update batting stats the same way this session handled pitching stats.
- **`fps_pct` in `v_player_stats` view:** This view currently computes FPS% as a *batter* (pitches seen starting with a strike), not as a pitcher. The column name is misleading. Future fix: rename the batting-side FPS% to `batter_fps_pct` and add a pitcher-side `pitcher_fps_pct` that reads from `pitching_stats.fps_pct`.
- **Season-over-season report:** Addison's pitching coach wants a view comparing All-Stars 2025 vs 2026 performance. Requires 2025 data to be imported first.

---

## 9. Player ID Reference (Storm 12U Silver All-Stars)

For convenience, here are the player UUIDs for all pitchers on the roster:

| Jersey | Name | player_id |
|---|---|---|
| #2 | Addison Arnerich | `f3e41b5c-0c57-4111-a7c2-7009c60d7a71` |
| #4 | Isabella Villaseñor | `591ee529-bfd9-4e68-beab-1c997ac4eb07` |
| #12 | Ally Morales | `9c948b76-2fa9-4e32-add6-ed7f4f746522` |
| #23 | Cecilia Gonzalez | `0eb047cb-73d3-4fd5-99ab-26c8630d7ea1` |

---

## 10. How to Start the Claude Code Session

1. Open a new Claude Code session
2. Paste or attach this document as your first message
3. Add: *"Please read the codebase at the upload pipeline, understand the current ingestion flow, then implement the changes described in Sections 5 and 6. Start by showing me the current CSV parsing and DB write logic before making any changes."*
4. Let Claude Code read the existing code first before writing anything new
5. Review the preview payload structure (Section 5b) and confirm it matches what the frontend needs before Claude Code implements it

---

*Document prepared June 2026 · Dugout Signals · Storm 12U Silver All-Stars*
