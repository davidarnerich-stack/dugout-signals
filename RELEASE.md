# Dugout Signals — Release Process

A lightweight process sized for a single-maintainer app with real coaching data behind it.
No CI, no staging environment today — this is what replaces that until the team/scale justifies more.

---

## When to release

Batch by sprint, not by ticket. Release once a sprint's tickets are merged to `main` and ready,
not incrementally per-commit. Exceptions: a live data-correctness bug (like DS-38) can jump the
queue if it's actively misleading a coaching decision — use judgment.

## Pre-release checklist

- [ ] `git status` on `dugout-signals-web` is clean or every untracked file is accounted for
      (either committed, or deliberately left out — don't let stray files linger silently)
- [ ] `git log origin/main..HEAD` reviewed — know what's about to ship, in plain language
- [ ] Any Supabase migration applied *during* the sprint is listed below under **Migrations this release**,
      even though it's already live (see note on Supabase timing)
- [ ] Any deferred "regenerate the report" / destructive-and-costs-money action is confirmed
      with David before this release, not assumed — check open items in memory /
      [BACKLOG.md](BACKLOG.md) first
- [ ] New environment variables introduced this sprint (e.g. `TEAM_NAME` from DS-36) are set in
      Render's dashboard *before* or immediately after deploy — check [DEPLOY.md](DEPLOY.md) for the
      current list

## Release steps

1. `git push origin main` — Render is connected to GitHub and auto-deploys on push to `main`
   (see [render.yaml](render.yaml)). There is no manual deploy step.
2. Watch the Render dashboard for the build to go healthy (~3 min). If it fails, the previous
   deploy stays live — nothing goes down mid-deploy.
3. Run the **UAT checklist** below against the live site.
4. If UAT passes, update [BACKLOG.md](BACKLOG.md)'s Resolved section for anything closed this release
   (matches the pattern already in use there).

## A note on Supabase migrations

Migrations applied via the Supabase MCP tool (or the dashboard) take effect **immediately** —
there's no "deploy" step separate from the application code, and no staging database today. This
means schema changes can land in production before the application code that depends on them is
deployed. That's been safe so far because the changes have been additive or touched
views/columns nothing live was reading yet — but it's worth checking deliberately, not assuming:

- [ ] Before applying a migration, confirm whether any *currently deployed* code reads the
      column/view being changed. If yes, sequence the deploy first (or accept a short window
      where old code ignores the new shape, never the other way around).
- [ ] List every migration applied this sprint here, so a release always has a record of what
      changed underneath it, not just what changed in git:

**Migrations this release (Sprint 0 → Sprint 1):**
- `add_pitcher_fps_pct_to_v_pitching` — added `fps_pct` to `v_pitching` (additive, no risk)
- `disambiguate_fps_pct_in_v_player_stats` — dropped/recreated `v_player_stats` with
  `batter_fps_pct` / `pitcher_fps_pct` (breaking rename, but `v_player_stats` is not queried by
  any deployed code — confirmed via grep before applying)
- `drop_reports_team_name_default` — dropped the `'Storm 12U All-Stars'` default on
  `reports.team_name` (safe — the insert path already always passes an explicit value)
- `ds56_add_player_attributes` — added `league_age`, `bats`, `throws`, `arm`, `glove`, `speed`,
  `position_eligibility` (jsonb), `signals` (jsonb, reserved for DS-15b), `status` (default
  `'active'`) to `players`. All additive/nullable except `status`/`position_eligibility`/`signals`
  which default safely — no risk to currently-deployed code, which doesn't read these columns yet.
- `ds56_create_coach_notes` — new `coach_notes` table (RLS + grants matching the `players` pattern).
  Not yet read/written by any deployed code.
- `ds11_add_single_game_report_columns` — added `game_id`, `report_headline`, `header_block` (jsonb)
  to `reports`. Additive/nullable — no risk to currently-deployed code.
- `ds11_allow_null_tournament_name` — dropped the `NOT NULL` constraint on `reports.tournament_name`.
  Found during DS-11 testing: single-game reports have no tournament, and the insert would have
  failed in production on the very first single-game report generated. Safe — no deployed code
  relies on `tournament_name` being non-null (tournament reports already always pass a value).
- `ds63_ds72_our_team_and_plate_appearances_team_id` — **breaking, unlike everything above.**
  Renamed `'storm'` → `'our_team'` in the `plate_appearances.batting_team` /
  `inning_scores.team` CHECK constraints, renamed `score_storm_before` →
  `score_our_before`, added `plate_appearances.team_id` (FK to `teams.id`, backfilled,
  RLS switched from deny-all to the standard own-team policy). Currently-deployed code at
  the time of migration still wrote/read the old names — this violated the "sequence the
  deploy first" rule below, not deliberately, and left a real (if brief) window where a
  play-by-play upload would have failed outright. Treated as the RELEASE.md exception
  ("a live data-correctness bug can jump the queue") and pushed same-session rather than
  batched to sprint-end. Also backfilled `games.team_id` for 22 Storm games uploaded under
  a legacy team name (`"Storm 12U All-Stars"` vs. the current `"Storm 12U Silver All
  Stars"`) that had never gotten `team_id` set — found while preparing this migration, not
  previously known.
- `ds43_batting_fields_and_pitch_type_detail` — additive, safe. Added 13 nullable `numeric`
  columns to `batting_stats` (QAB%, PA/BB, BB/K, C%, LD%, FB%, GB%, BABIP, BA/RISP, PS/PA,
  2S+3%, 6+%, AB/HR — GameChanger already computes all 13, column mapping only) and one
  nullable `pitching_stats.pitch_type_detail jsonb` column. Not read by any deployed code
  yet — parser writes it going forward, nothing displays it this sprint.
- `ds62_reports_replace_in_place` — **mild edge case, worth knowing before pushing.**
  Deleted the one existing duplicate report (game `4884b464...`, kept the newer row) and
  added a `UNIQUE (game_id, report_type)` constraint on `reports`. First attempt used a
  partial unique index (`WHERE game_id IS NOT NULL`, to leave tournament reports
  unconstrained) but Postgres can't infer a partial index from a bare `ON CONFLICT (cols)`
  — which is all PostgREST's `upsert()` ever sends — so it was replaced with a **plain**
  unique constraint instead; Postgres treats `NULL` as distinct by default, so tournament
  reports (`game_id IS NULL`) stay unconstrained without needing the partial predicate at
  all. Verified the fix directly against a real row before trusting it (see commit).

  **The edge case:** currently-deployed code still does a plain `.insert()` for reports,
  not the new `.upsert(on_conflict=...)`. Until the matching code deploys, re-uploading a
  game that already has a report will hit the new constraint and the insert will fail —
  caught by the existing best-effort try/except in `_generate_single_game_report_safe`
  (never surfaces as an upload failure), so the practical effect is just "the report
  silently doesn't refresh" rather than a duplicate or a crash. Low severity given current
  usage (two low-volume accounts), but real — batch this one sooner rather than later.

If a future migration *would* break currently-deployed code, consider a Supabase branch
(`create_branch` / `merge_branch` are available via the MCP tools) to test the migration against
a copy of the schema before applying it to production. Not needed yet at this scale, but the
option exists once a change is genuinely risky.

---

## UAT Checklist

Run this against the live site after every deploy. Copy this list fresh each release rather than
checking the same boxes — that's what catches regressions.

### Auth & session (DS-36 onward)
- [ ] Log in with the current password at `app.dugoutsignals.ai` (or `upload.` — same backend)
- [ ] `/api/games` returns only Storm 12U All-Stars games (no cross-team leakage — will matter
      more once a second team exists)
- [ ] Log out, confirm session clears and re-login is required

### Upload flow
- [ ] Upload a Stats CSV + Box Score PDF together for a **new** game — confirm game created,
      players matched/created correctly
- [ ] Re-upload the same files — confirm the overwrite-confirmation warning appears and works
- [ ] Paste play-by-play text for an existing game — confirm it attaches to the right game
- [ ] "Edit existing game" panel — change an opponent name / score, confirm it saves

### Reports
- [ ] Generate a new report for a tournament with data — confirm it completes and saves
- [ ] Open an existing report — confirm all 9 tables render, no server error
- [ ] **Pitching section specifically (DS-38 regression check):** spot-check a pitcher's `fps_pct`
      in a *newly generated* report against `pitching_stats.fps_pct` in Supabase directly — they
      should match. (The 2 pre-existing reports will still show the old, pre-fix values until they
      are explicitly regenerated — see the deferred item below. That's expected, not a bug.)
- [ ] Reports list — confirm badges (W/L/T) and truncated titles render correctly

### Data integrity spot-check
- [ ] Pick one player, one game. Compare `batting_stats` / `pitching_stats` / `fielding_stats` in
      Supabase against the original GameChanger CSV for that game — numbers should match exactly
- [ ] Confirm no unexpected rows in `unmatched_box_score_players` beyond genuine opponent-roster
      entries

### Known deferred item — do not treat as new
- [ ] **DS-38 AC#5:** the 2 existing Storm tournament reports still display pre-fix `fps_pct`
      values (showing ERA instead, on the old reports only). This is intentionally deferred —
      regenerating costs Anthropic API tokens and overwrites the existing report rows. Confirm
      with David before regenerating; do not do this automatically as part of routine UAT.

---

## Cadence

**Sprint-boundary releases.** Ship when a sprint's tickets are merged and ready — no fixed
calendar day. Matches how Sprint 0 actually ran. Revisit this if release size becomes too
unpredictable or sprints start slipping without a forcing function.
