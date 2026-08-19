#!/usr/bin/env python3
"""
Regenerate a single-game report in place, without re-uploading the game.

    python3 tools/regenerate_report.py <report-id | game-id | report URL>

Why this exists: report *generation* changes far more often than parsing
does, and re-uploading three files to see a copy change is a slow way to
review writing. Nothing about the game's data needs to move — the report is
built from rows that are already correct.

This calls the same generator the upload flow and the /reports/<id>/retry
route call, so what you read is what a coach would get. DS-62's upsert means
it replaces the existing report in place: the URL does not change.

Two things worth knowing before running it:

  * It uses ANTHROPIC_API_KEY from .env and makes roughly a dozen model
    calls. That is real money and 30-60 seconds, per run.
  * It runs YOUR WORKING COPY against the PRODUCTION database. That is the
    point — you can see a prompt change without waiting for a deploy — but
    it does mean an uncommitted edit becomes a real coach's report. Check
    `git status` if that matters.
"""
import os
import re
import sys


def _load_env(repo_root):
    path = os.path.join(repo_root, ".env")
    if not os.path.exists(path):
        sys.exit(f"no .env at {path} — see DS-106 for setup")
    for line in open(path):
        if line.strip().startswith("#"):
            continue
        m = re.match(r"\s*([A-Z_]+)\s*=\s*(.*)", line)
        if m:
            os.environ.setdefault(m.group(1), m.group(2).strip().strip('"').strip("'"))


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, repo)
    _load_env(repo)

    # A URL, a report id, or a game id — all resolve to the same thing.
    ident = sys.argv[1].rstrip("/").split("/")[-1]

    from supabase import create_client
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

    game_id = None
    r = (sb.table("reports").select("report_id,game_id,report_type")
         .eq("report_id", ident).limit(1).execute().data)
    if r:
        if r[0].get("report_type") != "single_game":
            sys.exit("that is not a single-game report")
        game_id = r[0]["game_id"]
    else:
        g = sb.table("games").select("game_id").eq("game_id", ident).limit(1).execute().data
        if g:
            game_id = g[0]["game_id"]
    if not game_id:
        sys.exit(f"no report or game found for {ident}")

    game = (sb.table("games").select("game_id,team_id,game_date,opponent_name,"
                                     "team_runs,opponent_runs,result")
            .eq("game_id", game_id).limit(1).execute().data)[0]
    team = (sb.table("teams").select("team_name")
            .eq("id", game["team_id"]).limit(1).execute().data)[0]

    print(f"{team['team_name']} vs {game['opponent_name']} on {game['game_date']} "
          f"— {game['result']} {game['team_runs']}-{game['opponent_runs']}")
    print("generating (30-60s, ~a dozen model calls)…")

    from app import _generate_single_game_report_safe
    report_id = _generate_single_game_report_safe(
        sb, game_id, game["team_id"], team["team_name"])

    if not report_id:
        sys.exit("generation failed — the report row will show status='error'")
    print(f"done: https://app.dugoutsignals.ai/reports/{report_id}")


if __name__ == "__main__":
    main()
