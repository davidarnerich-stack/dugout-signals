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

- `ds77_team_configuration_fields` — additive, safe. Added `teams.regulation_innings`,
  `.continuous_batting_order`, `.governing_body_other`, `.dashboard_age_footnote_shown_count`
  (default 0), and `games.contact_data_batting` / `.contact_data_pitching`. Backfilled both
  existing teams' `regulation_innings` (6 each, per the governing-body/age-level table) and
  `continuous_batting_order` (true). Not read by any deployed report/metrics code yet.

  Also fixed a real bug found while implementing this: the onboarding wizard's "Other"
  governing-body handling **overwrote** `governing_body` with the coach's free-text answer
  instead of keeping the literal `"Other"` and storing the free text separately — the
  opposite of the pattern the same route already uses correctly for `source`/
  `source_other_text`. Silently meant `governing_body == "Other"` could never match again
  once a coach had submitted the form, which would have broken `regulation_innings`
  defaulting for exactly the teams that need it most. Fixed to match the existing
  `source` pattern; the free-text input itself was already built and working on the
  frontend, only the backend storage was wrong.

- `ds74_pitching_contact_quality_columns` — additive, safe. Added
  `pitching_stats.weak_pct`, `.fly_ball_pct`, `.babip`. Found while implementing DS-74:
  DS-43 ("capture all 197 columns") captured these fields on the batting side but missed
  their pitching-section counterparts entirely — WEAK, PSKL, and YFIP all need pitching
  BABIP, and requirement #10's pitching contact-data-missing detection needs it too.
  Confirmed the raw export columns exist at the same absolute offsets for both sports
  (upstream of the sport-dependent pitch-type-block divergence at col 118), so no
  offset logic needed, unlike DS-78's fielding fix. Not backfilled on existing games —
  requires a re-import (parser change, not a data migration) to populate.

- `ds74_games_team_totals` — additive, safe. Added `games.team_totals jsonb`. GameChanger's
  stats CSV includes its own team-aggregate "Totals" row, which the parser has always
  discarded (`if fc in ("", "Totals", "Glossary")`). DS-74's team-dependent metrics (FRA,
  YRAL, YFIP, DefEff) need GameChanger's own computed team BABIP/FB%/games-played —
  verified against the reference workbooks that re-deriving these by summing/weighting
  individual player rows drifts from GameChanger's own computation (team games-pitched in
  particular isn't summable from individual pitchers' own games-pitched counts at all).
  Parser now captures the Totals row verbatim via the same column-mapping functions used
  for player rows. Not backfilled on existing games — requires a re-import.

  **Both DS-74 migrations need a fourth re-import pass** (Storm's 4 games, Yankees' 7)
  before FRA/YRAL/YFIP/DefEff and the pitching contact-quality metrics will compute
  correctly against production data — the columns exist but are null on every game
  uploaded before this release. Flag to David before this ships; the metrics layer itself
  handles nulls safely (falls back to no team context / no contact-dependent metrics
  rather than erroring), so this is a completeness gap, not a crash risk.

- `ds76_signal_history` — additive, safe. New `signal_history` table (append-only: RLS has
  only `select_own_team`/`insert_own_team`, no update/delete policies at all) with
  `team_id`/`game_id` FKs, `signal_key`, `bucket`, `headline`, `interpretation`,
  `metrics`/`raw_inputs` (jsonb), `games_in_sample`, `age_band`, `contact_data_ok`,
  `computed_at`. Not read or written by any deployed code yet — this is storage
  infrastructure for DS-67 (which computes the six signals) and doesn't exist as a live
  write path until that ticket wires `signals.history.record_signal_history()` into the
  upload flow. Verified directly against the real table before committing: inserted a
  synthetic 6-row batch (shape matched exactly), inserted a second batch for the same
  `game_id` and confirmed 12 rows resulted (not an overwrite — proves append-only end to
  end), confirmed an invalid `team_id` raises `23503` (FK violation) which
  `record_signal_history`'s try/except is built to catch and log without failing the
  upload, then deleted all test rows so the table starts genuinely empty.

- `ds67_dashboard_last_viewed_game_count` — additive, safe. Added
  `teams.dashboard_last_viewed_game_count`, default 0. DS-67's summary line needs to know
  how many games are new since the coach last opened the dashboard ("after your last 2
  games" vs "through 9 games") — nothing tracked this before. Updated on every dashboard
  render; both existing teams start at 0, so their first post-release dashboard view reads
  as "through N games" (the no-new-games framing), which is correct for a first view.

  **DS-67 (Team Signals) now wired in**, first real consumer of `signal_history` and both
  DS-74 migrations above. `record_signal_history()` is called from `/dashboard` on every
  render that has games — not from the upload flow directly, since the dashboard route is
  where team_totals across the whole season window gets assembled; this means a signal
  snapshot happens on the coach's next dashboard *view* after a game commits, not at
  upload time itself. Six cards at 12U+, five at 8U (Command vs velocity requires walk
  rate, which is structurally zero under the no-walk rule — DS-67's finalized spec says
  it "must not be computed", overriding DS-76's original assumption that all six always
  get recorded; `signals/history.py`'s docstring updated to match).

  Verified the card computation layer (not the narrative generation, which needs
  `ANTHROPIC_API_KEY` unavailable locally) against the real reference data for both teams:
  the defensive-split decomposition for Storm (7.55 runs allowed/game = 4.83 FRA + 2.72
  fielding remainder) matches the design spec's own illustrative example (7.6 = 4.8 + 2.7)
  almost exactly, strong evidence the spec was written against this same dataset. Catching
  totals from the Totals row (70 passed balls / 74.1 innings for the Yankees) likewise
  nearly match the spec's own cited example (69 PB / 72 innings). Confirmed via Jinja
  render tests: correct card count per age band (5 at 8U, 6 at 12U+), card 6 content
  genuinely absent (not hidden) at 8U, genuine-zero vs missing-data border/typography
  states render correctly and independently, the Inter-600 standalone-zero exception
  applies only to a true zero value, no-headline graceful fallback (simulating a failed or
  skipped narrative generation) doesn't break the card, and the existing DS-65/66 empty-
  and populated-dashboard states are unaffected by an empty `signal_cards` list — exactly
  today's real production state, since neither new DS-74 migration is backfilled yet.

- `ds68_bucket_taps` — additive, safe. New `bucket_taps` table (append-only, same
  select/insert-only RLS pattern as `signal_history`) recording which Offence/Defence
  bucket a coach taps — discovery input for DS-32/33/34. No new metrics computed: DS-68's
  Offence/Defence hierarchy modules reshape the exact same `signal_cards` facts DS-67
  already computes (team OBPE/OPSE/SCORE% for the offence funnel, FRA/DefEff/PB-or-CS%
  for the defensive split and its chips) rather than re-deriving anything, so DS-68 has no
  new re-import dependency beyond what DS-74/DS-67 already need.

  Verified against the real reference data for both teams: the defensive-split proportional
  bar's two segments (pitching share / fielding remainder, computed as fractions of runs
  allowed per game) sum to exactly 100% for both Storm and Yankees, as they must by
  construction. Confirmed via Jinja render tests: every bucket chip's anchor link resolves
  to a real signal-card element id, AC #8 (omit rather than show a broken module) verified
  with a synthetic partial-data case — the Offence module correctly omits itself when its
  underlying runs-scored figure is missing while the Defence module still renders normally
  on its own independent data — and today's real production state (empty `signal_cards`)
  renders with no bucket-modules section at all, not a broken one.

- **No migration, but worth recording**: found live immediately after the re-import that
  `/dashboard` was taking ~24s on every single view. DS-67's `generate_all_narratives()`
  (6 sequential Claude calls) and DS-76's `record_signal_history()` were both wired into
  the `/dashboard` GET route instead of the upload flow — every page view re-ran narrative
  generation and wrote a fresh `signal_history` row, not just the view right after a game
  commit. Confirmed in production: 48 duplicate rows (6× for Yankees, 3× for Storm) from
  repeated dashboard views of the same game. **Deleted all 48** — `signal_history` starts
  clean again.

  Fix: moved both calls into the upload flow (`_generate_and_record_team_signals_safe()`,
  alongside the existing single-game report generation — already a 30-60s-expected
  operation, unlike a dashboard view which must be instant) and parallelized the 6
  narrative calls with a thread pool so the added upload-time cost is roughly the slowest
  single call (~4-6s) rather than their sum. `/dashboard` now only computes signal-card
  *numbers* fresh (cheap, matches DS-74's compute-on-read philosophy) and reads cached
  headline/interpretation back from the latest `signal_history` row for the current game.

  **Practical effect for David**: since `signal_history` is now empty, both teams' signal
  cards will show numbers with no headline text until their next real game upload (which is
  when narrative generation now actually happens) — or a manual re-upload of just the most
  recent game's box score + stats CSV, which would populate it immediately. Not broken —
  this is the same graceful "no headline" fallback already built and tested for exactly
  this case.

- `ds69_signal_explanation_and_feedback` — additive, safe. Added `signal_history.why_text`
  (nullable) and a new `signal_feedback` table (append-only, same select/insert-only RLS
  pattern as `signal_history`/`bucket_taps`) storing per-coach Useful/Not-useful ratings
  against a `signal_history` row. A follow-up migration in the same session,
  `ds69_drop_unused_try_text`, dropped a `try_text` column added in the first pass once the
  design settled on reusing `signal_history.interpretation` for the explanation view's "One
  thing to try" section instead — the existing interpretation copy already suggests rather
  than instructs (system prompt requirement from DS-67), so a second model-generated field
  grounded in the same facts would have been redundant. Both migrations landed before any
  deploy, so no production data ever had the dead column.

  **DS-69 (signal explanation view), scoped this sprint to its DS-67 team-signals consumer
  only** — DS-57 (player signals) is paused, see the DS-69 Jira scope comment for full
  rationale. Tapping any of the six signal cards on the dashboard opens a bottom
  sheet/modal (mobile/md+, matches `templates/roster.html`'s existing edit-sheet pattern,
  Esc-to-close) with Headline / What we saw / Why it might be happening / One thing to try /
  Context chips, plus a Useful/Not-useful control. No mute/hide/suppress control anywhere
  (req 6) — Useful/Not-useful is the only feedback mechanism, feeding DS-64's ranking spike
  once picked up.

  "What we saw" and "Context chips" are code-generated (`card_metric_rows` and the new
  `card_context_chips` in `signals/team_signals.py`) — no new computation, reuses exactly
  what's already on the dashboard card. "Why it might be happening" is one new
  model-generated line per card, added to the *same* Claude call `generate_all_narratives()`
  already makes at upload time (HEADLINE/INTERPRETATION/WHY in one response) — no added
  latency, no second narrative pass. "One thing to try" reuses the existing INTERPRETATION
  text verbatim.

  Verified via `py_compile` and manual review (no live server/API key available locally,
  same constraint as DS-67's original verification): confirmed the 3-line
  HEADLINE/INTERPRETATION/WHY parse handles a missing WHY line the same way the original
  2-line parser handled a missing INTERPRETATION (field stays `None`, section hidden rather
  than rendering empty); confirmed a card with no cached `signal_history` row (narrative
  generation failed or hasn't run since this deploy) still opens with real "What we saw"
  rows and a hidden feedback control rather than dead buttons; confirmed the feedback route
  rejects any rating other than `useful`/`not_useful` with a 400.

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
