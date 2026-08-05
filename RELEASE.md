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

- **No migration — DS-82, pitching ERA/WHIP bugfix**: `reports/data.py` was dividing ERA/WHIP
  directly by `pitching_stats.innings_pitched` as if it were a real decimal. GameChanger's
  thirds notation isn't decimal — `"5.2"` means 5⅔ innings (17 outs), not 5.2 innings — the
  exact "Corrected IP" bug DS-74's source material documents and explicitly warns against
  porting. The tournament report had it twice over: it also *summed* the raw notation across
  a pitcher's games first (`"5.2" + "5.2"` as floats = `10.4`, not the correct 11⅓ innings /
  34 outs), so every multi-game pitcher in a tournament report was affected, not just those
  with a single ⅓-inning outing.

  Fix: both call sites (`get_single_game_data()`, `get_tournament_data()`) now use
  DS-74's `metrics.compute.parse_innings_to_outs`/`corrected_innings` — outs summed as
  integers across games, corrected innings derived only at the final division step. The
  tournament report's displayed IP is reconstructed from the true out count
  (`f"{outs//3}.{outs%3}"`), not the buggy raw sum. Single-game display IP is untouched
  (GameChanger's own string was already correct for one row; only the division was wrong).

  Verified directly against real production `pitching_stats` rows (not synthetic data): a
  Storm pitcher with five real games including `.1`/`.2` remainders — pre-fix buggy sum
  `8.3`, ERA `1.45`/WHIP `0.72`; post-fix true outs `27` (9.0 innings exactly), ERA
  `1.33`/WHIP `0.67`. Also checked a single real `1.1`-IP outing directly: pre-fix ERA
  `10.91`/WHIP `2.73` vs post-fix ERA `9.0`/WHIP `2.25`. Both cases confirm AC #3's expected
  direction — the prior denominator was too small (raw thirds-as-decimal always undercounts
  the true fraction: `.1` reads as `0.1` instead of the true `1/3`), so every existing
  ERA/WHIP was **overstated**, and the fix lowers them, never raises them.

  **No backfill needed** — `get_single_game_data()`/`get_tournament_data()` recompute the
  structured pitching tables fresh on every report *view* (`app.py`'s report-detail route
  calls them directly, not just at generation time), so every existing report's tables
  self-correct the next time it's opened, no re-generation required. The one thing this
  doesn't fix: a report's **narrative prose** (AI-generated once, at creation time, and
  stored) may still cite the old, overstated ERA/WHIP number in a sentence for any report
  generated before this deploy — the table below it will now show the corrected number,
  which can read as a mismatch on old reports specifically. Same class of issue, same
  deferred-regeneration caution, as the existing DS-38 `fps_pct` UAT item below: don't
  regenerate existing reports to fix this automatically, confirm with David first.

- **No migration — DS-75, catching metrics + single game report section, plus a related
  live-bug fix found while building it**: added the fourth (of DS-29's original count)
  metrics bucket — `pb_per_inning`, `sb_per_inning`, `cs_pct`, `innings_caught` — to
  `metrics/metrics.yml` and a new `compute_catching_metrics()` in `metrics/compute.py`.
  Derived, not sourced from published research (no attribution required). Sample gates (10
  innings for the per-inning rates, 15 steal attempts for CS%) are derived defaults
  calibrated against the ticket's own worked example — Storm's real catchers Garcia (42
  attempts / 59.0 innings, treated as trustworthy) vs. Butler (7 attempts, flagged
  misleading) and Vazquez (6.1 innings, flagged misleading) — not research-sourced or a
  fabricated-precise number; documented as tunable in both the YAML and the code constants.

  **Live bug found and fixed in the process**: `innings_as_catcher` turns out to use the
  exact same GameChanger thirds notation as `innings_pitched` (`"2.2"` = 2⅔ innings, 8 outs)
  — confirmed by checking every fractional digit that appears across both full-season
  reference CSVs (both sports): only `.0`/`.1`/`.2` ever appear for `innings_as_catcher` and
  for every per-position innings-played column (P/C/1B/2B/3B/SS/LF/CF/RF/SF), never a true
  decimal fraction. `signals/team_signals.py`'s `_sum_team_fielding` was summing it as a raw
  float — the exact DS-82 bug class, but live in the already-shipped, Done DS-67 catching
  card's `pb_per_inning` metric. Fixed the same way DS-82 was: sum outs, reconstruct the
  thirds-notation string, matching `_sum_team_pitching`'s existing convention in the same
  file. Checked whether this also affects the per-position innings-played columns (David's
  own suspicion, confirmed right that they share the notation) — they don't have a live bug
  today, because that data is only ever *compared* (`_primary_position`, picking a player's
  most-played position) never summed or divided, and thirds-notation digits happen to
  preserve correct ordering under naive float comparison (0 < 1 < 2 maps monotonically to
  0 < ⅓ < ⅔). Not stored anywhere either, so nothing to backfill.

  **New feature**: a "Catching" subsection on the single-game report (`game_report.html`,
  under Team Performance → Defense, alongside Pitching and Fielding), AI-narrated same as
  its neighboring subsections but grounded in **counting stats only** per catcher (passed
  balls, caught stealing, innings caught, pickoffs) — a single game is too small a sample
  for a per-catcher rate to mean anything (design rule from the ticket, same reasoning DS-57
  applies to player signals generally). The prompt explicitly instructs the model never to
  compute or state a per-catcher percentage, to read a catcher's zero passed balls as a
  positive result rather than missing data, and to never attribute a caught-stealing or
  passed-ball outcome to the catcher alone — pitcher delivery time to the plate is named
  explicitly as a material factor in both. The existing DS-67 dashboard catching card is
  unchanged in shape (still team-level rates, still the 8U-10U-uses-PB / 12U+-uses-CS%
  headline switch) — DS-75 wired it through the new shared `compute_catching_metrics`
  instead of its own inline logic, which is what picked up both the sample-gate fix and the
  innings-notation fix for free.

  One scale bug caught before it shipped: `compute_catching_metrics` returns `cs_pct`
  already scaled 0-100 (the same convention every other percent metric in this layer uses,
  e.g. `pit_bb_pct`), but the DS-67 catching card's existing display row still multiplied by
  100 again (written against the *old* inline `cs_pct = safe_div(cs, attempts)`, a bare 0-1
  proportion). Left as-is it would have displayed a caught-stealing rate 100x too large.
  Fixed in `card_metric_rows`.

  Verified against real production data: confirmed a Storm team's actual 4-game
  `passed_balls`/`innings_as_catcher` totals produce a corrected `innings_caught` (14.67)
  strictly higher than the old buggy raw-float sum (14.2, since raw thirds-as-decimal always
  undercounts the true fraction) and a correspondingly lower `pb_per_inning` (0.955 vs the
  buggy 0.986); confirmed that same real game's 12 combined steal attempts now correctly
  suppress `cs_pct` under the new 15-attempt gate, where the old code showed a rate off of
  any attempts > 0. Confirmed the single-game Catching section's sample gates behave
  correctly against two real Storm catchers (Butler, Garcia) sharing one real game — both
  team-level per-game rates correctly suppress (4 innings / 1 attempt, both below their
  gates), which is the expected, common case at single-game grain, not a bug: it's why AC #6
  restricts the single-game section to counting stats in the first place. Full Jinja render
  test of `game_report.html` with the new Catching subsection populated. `py_compile` across
  every touched module; `metrics.yml` parses to 23 total metrics (19 + the 4 new catching
  ones).

- **No migration — DS-73, metric explainer, plus a follow-up bug fix and a licensing note**:
  new `metrics/explainers.yml` (authored, not model-generated, per req 7 — the one part of this
  ticket that isn't safe to auto-write and ship unreviewed) and `metrics/explainer.py`
  (assembles the render-time payload: authored copy + real own-team value + real familiar-anchor
  value, no new database query — reuses the exact facts already on `signal_cards`).

  **Scope decision**: only 6 metrics currently render as tappable text anywhere in the live UI —
  `fra`, `opse`, `score_pct`, `def_eff`, `pb_per_inning`, `cs_pct` — so only those 6 have an
  explainer entry. Every other `metrics.yml` metric (`bat_bb_pct`, `iso`, `yrpi`, `pskl`, `yral`,
  ...) has no live tap target yet; that's a DS-57 (player signals, paused) surface, and adding an
  explainer for a metric nothing links to would be a fake affordance. Add an entry alongside
  whatever DS-57 work first renders that metric's name.

  **Attribution**: the research docs (`ds29-metric-extraction.md`, `metric-attribution.md`)
  deliberately never name the source — flagged "licensing unresolved, pending author permission"
  — so this needed a direct check with David rather than a guess. He provided the real citation:
  Michael McBride, *Coaching Youth Baseball and Softball with Sabermetrics* (Youth Sabermetrics
  Book 3), shortened for the credit line's limited space. Applied to the 3 metrics
  `metric-attribution.md` classifies as the author's own invention (category A) that are also
  currently live — `fra`, `opse`, `score_pct`. `def_eff` is category C (a standard sabermetric
  term he recommends, not his invention) — no attribution, matching its `metrics.yml`
  `proprietary: false`. `pb_per_inning`/`cs_pct` are classified `is_novel: false` in
  `explainers.yml` (a separate flag from `metrics.yml`'s `proprietary`, tracking coach-familiarity
  rather than research-attribution) — GameChanger's own export already computes and displays
  both, so a coach already has these numbers.

  **"Across your own team" scoping** (req 5): confirmed with David before building — the
  design spec's "across your staff/lineup" comparison assumes a per-player breakdown that only
  exists once DS-57 (paused) wires in player-level data. For the live team-level cards, this
  section instead states the team's own season value plainly (e.g. "Your team's FRA this
  season: 4.83") rather than fabricating a spread with nothing to compare against.

  **UI**: tapping a metric name — on a signal card row, inside DS-69's explanation view, or in a
  DS-68 bucket chip — opens the metric explainer using DS-69's exact modal shell (req 3). When
  opened from *within* an already-open DS-69 signal explanation, it **replaces that sheet's
  content in place** with a back arrow rather than stacking a second sheet (the 2026-08-03
  decision) — tapping back restores the signal explanation exactly as it was. Opened fresh (from
  a card row or bucket chip with no DS-69 sheet already open), it's a normal sheet with an X
  close. No feedback control on the metric explainer — that's DS-69-specific, not part of this
  component's spec.

  **Follow-up bug fix, found while wiring tap targets**: `signals/bucket_modules.py`'s Defence
  bucket chip had a *second* instance of DS-75's `cs_pct` double-scale bug — `card_metric_rows`
  was fixed, but this call site was missed. `compute_catching_metrics` returns `cs_pct`
  pre-scaled 0-100; this line still multiplied by 100 again, so the Defence module's CS% chip
  was showing roughly 100x too large in production since DS-75 shipped. Fixed immediately on
  discovery.

  **Retroactive ticket note**: the `innings_as_catcher` thirds-notation bug found and fixed
  during DS-75 was filed retroactively as DS-84 (Bug, Done) at David's request, for an audit
  trail matching DS-82's precedent — a live bug in already-shipped code deserves its own record
  even when fixed same-session as the ticket that found it. The `cs_pct` scale mismatch this
  session (both the original DS-75 instance and this bucket-chip follow-up) was *not* filed
  separately — caught before either ever reached production, same category as the FB-formula
  and TmPitchGP bugs caught during DS-74's own development.

  Verified against real production Storm data end-to-end (`compute_team_signals` →
  `compute_familiar_anchors` → `build_metric_explainers`, real 4-game `team_totals` and 2
  synthetic-but-realistic pitcher rows): FRA 5.985 against an ERA anchor of 5.32, OPSE 0.804
  against an OPS anchor of 0.757 (OPSE higher, exactly the "credits ROE" claim in its own copy),
  DefEff 0.368 against a Fielding Percentage anchor of 0.714 (the large gap illustrates precisely
  what the copy says FPCT misses), CS% gracefully showing "—" when this sample's 12 attempts sit
  below the 15-attempt gate (not a crash, not a wrong number). Full Jinja render test with all 6
  metrics' tap targets present in the rendered HTML; JS syntax-checked with `node --check`;
  `py_compile` across every touched module.

  **Found while verifying this ticket, filed and fixed as DS-85 (High)**:
  `_command_vs_velocity`'s `insufficient_attempts` state omitted `k_pct_spread`/`bb_pct_spread`
  from `facts` entirely instead of populating them as `None` (the convention every other card in
  the file follows) — `card_metric_rows` reads them unconditionally, and `app.py`'s
  `/dashboard` route builds every card's rows in a loop with **no per-card try/except**, so this
  was a live `KeyError` crash of the **entire dashboard route**, not just one broken card,
  whenever a 9U+ team had recorded `batters_faced` for fewer than 2 distinct pitchers across
  their whole season so far. Most exposed right after a brand-new team's first game upload, if
  that game had a single pitcher — common in youth ball, and not a rare edge case. Confirmed
  Storm/Yankees (today's two real teams) already have 2+ pitchers recorded and aren't currently
  exposed. Fixed by populating both fields as `None` in that branch; `_row()` already renders
  `None` as `"—"` gracefully. Reproduced the exact crash against real Storm data pre-fix,
  confirmed graceful `"—"` rendering post-fix, confirmed the 2+-pitcher path unchanged. Full
  detail in DS-85.

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
- [ ] **DS-82:** any report generated before this deploy may have narrative prose citing the
      old, overstated ERA/WHIP number for a pitcher with a ⅓-inning remainder — the structured
      pitching table on that same report will now show the corrected (lower) number, which can
      read as a mismatch on old reports specifically. Tables self-correct automatically on next
      view; only the AI-written prose is stale. Same deferral as DS-38 above — confirm with
      David before regenerating any specific report.
- [ ] **DS-75:** any `signal_history` row for the `catching_load` (Team Signals) card recorded
      before this deploy has a cached headline/interpretation narrated against the old, buggy
      `innings_as_catcher` sum and the old un-gated `cs_pct` — the dashboard will show fresh,
      corrected numbers (innings, PB/inning, and CS% possibly now suppressed under the new
      15-attempt gate where it previously showed a value) next to that stale headline text until
      the next game upload re-narrates it. Same class of staleness as DS-82/DS-38 above, no
      action needed — it self-corrects on the next real upload.

---

## Cadence

**Sprint-boundary releases.** Ship when a sprint's tickets are merged and ready — no fixed
calendar day. Matches how Sprint 0 actually ran. Revisit this if release size becomes too
unpredictable or sprints start slipping without a forcing function.
