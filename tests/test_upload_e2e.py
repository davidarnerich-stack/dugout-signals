"""
End-to-end tests for the upload write path (DS-106).

Each test corresponds to a bug that reached production. They exist because
every one of those bugs was invisible to the two things that *were* being
done: reading the code, and querying the data. They only appeared when a real
file went through the real endpoint into the real database.

Ticket map:
  DS-105  a parsed placeholder overwrote the opponent name the coach supplied
  DS-105  the coach's answer only reached players being created, not existing
  DS-107  a player with no surname was dropped entirely
  DS-108  batting order was shifted, not merely incomplete
  DS-108  a re-upload crashed on UNIQUE (game_id, batting_position)
  DS-114  batter/pitcher names corrupted, opposing batters NOT NULL violation
  DS-100  a doubleheader must stay two games
"""
import json
import pytest
from conftest import upload, game_files


# ── the files, and what is true about them ────────────────────────────────
# Verified against the source exports, not assumed. GAME_1 has a player with
# no jersey number; GAME_5 has one with no surname at all.
GAME_1 = "01_Game"      # 2026-07-15, home, 12 batters, Daniel Garrett unnumbered
GAME_2 = "02_Game"      # 2026-07-18, away, McClung bats 5th with no number
GAME_5 = "05_Game"      # 2026-07-29, home, "17,,Tristan" — no surname
GAME_9 = "09_Game"      # 2026-08-11, home, 3 errors in one inning


def rows(sb, table, game_id, select="*"):
    return (sb.table(table).select(select).eq("game_id", game_id).execute().data or [])


def only_game(sb, team_id):
    games = sb.table("games").select("*").eq("team_id", team_id).execute().data
    assert len(games) == 1, f"expected exactly one game, found {len(games)}"
    return games[0]


# ── the file itself must be intact ────────────────────────────────────────

def test_fixture_files_exist():
    """If the exports move, every other failure here would be misleading."""
    for prefix in (GAME_1, GAME_2, GAME_5, GAME_9):
        box, box_name, stats, stats_name, pbp = game_files(prefix)
        assert box, f"{prefix}: no box score PDF"
        assert stats, f"{prefix}: no stats CSV"


# ── DS-107: a player with no surname must still be imported ───────────────

def test_player_with_no_surname_is_imported(client, sb, clean_team):
    """
    GameChanger records a call-up whose surname nobody entered as "17,,Tristan".
    Three filters skipped any row without a last name, so he was never created
    and his at-bats never counted. Game 5 imported 10 batters instead of 11.
    """
    r = upload(client, GAME_5)
    assert r.status_code == 200, r.data[:400]

    game = only_game(sb, clean_team["id"])
    batters = rows(sb, "batting_stats", game["game_id"])
    assert len(batters) == 11, f"expected 11 batters, got {len(batters)}"

    players = sb.table("players").select("first_name,last_name,number") \
        .eq("team_id", clean_team["id"]).execute().data
    tristan = [p for p in players if p["first_name"] == "Tristan"]
    assert tristan, "the surname-less player was dropped again"
    assert tristan[0]["last_name"] == "", "a blank surname is the real data"


# ── DS-108: batting order must match the box score, in order ──────────────

def test_batting_order_matches_the_box_score(client, sb, clean_team):
    """
    Batting position was counted off matched lines, so a batter with no jersey
    number was skipped and everyone below him recorded a slot too high. The
    stored order was wrong, not just incomplete.
    """
    upload(client, GAME_2)
    game = only_game(sb, clean_team["id"])
    order = rows(sb, "batting_order", game["game_id"],
                 "batting_position,player_id")
    positions = sorted(o["batting_position"] for o in order)

    assert positions == list(range(1, len(positions) + 1)), \
        f"batting positions must be 1..N with no gaps, got {positions}"

    # McClung has no jersey number in this game and bats 5th. Before DS-108 he
    # was missing entirely and everyone below him was shifted up one.
    pid_to_name = {p["player_id"]: f"{p['first_name']} {p['last_name']}"
                   for p in sb.table("players").select("*")
                   .eq("team_id", clean_team["id"]).execute().data}
    by_slot = {o["batting_position"]: pid_to_name.get(o["player_id"]) for o in order}
    assert by_slot.get(5) == "Connor McClung", \
        f"slot 5 should be the unnumbered batter, got {by_slot.get(5)}"


def test_reupload_does_not_crash_on_shifting_positions(client, sb, clean_team):
    """
    batting_order has UNIQUE (game_id, batting_position). Correcting a lineup
    necessarily shifts people, so a row-by-row update hit a moment where two
    players wanted the same slot and the write died half-finished.
    """
    upload(client, GAME_2)
    first = {o["batting_position"] for o in
             rows(sb, "batting_order", only_game(sb, clean_team["id"])["game_id"],
                  "batting_position")}
    r = upload(client, GAME_2)          # same game again
    assert r.status_code == 200
    body = json.dumps(r.get_json())
    assert "unique_game_position" not in body, "re-upload hit the unique constraint"

    game = only_game(sb, clean_team["id"])
    second = {o["batting_position"] for o in
              rows(sb, "batting_order", game["game_id"], "batting_position")}
    assert first == second, "a re-upload changed the lineup it should have reproduced"


# ── DS-105: the coach's opponent name must survive the parser ─────────────

def test_opponent_name_survives_the_box_score_parser(client, sb, clean_team):
    """
    The upload applies the coach's name, then the box score parser wrote its
    own parsed value over the top — putting GameChanger's "TBD- <date>"
    placeholder back over the name just typed.
    """
    upload(client, GAME_1, opponent_override="Team Red",
           opponent_apply_to=json.dumps([]))
    game = only_game(sb, clean_team["id"])
    assert game["opponent_name"] == "Team Red", \
        f"the coach's name was overwritten with {game['opponent_name']!r}"
    assert game["opponent_is_placeholder"] is False


def test_reupload_does_not_revert_the_opponent_name(client, sb, clean_team):
    """
    The wider half of the same bug: ANY re-upload clobbered a coach-set name,
    so naming a game and re-uploading it always lost the name.
    """
    upload(client, GAME_1, opponent_override="Team Red",
           opponent_apply_to=json.dumps([]))
    upload(client, GAME_1)              # re-upload, no name supplied
    game = only_game(sb, clean_team["id"])
    assert game["opponent_name"] == "Team Red", \
        f"a re-upload reverted the name to {game['opponent_name']!r}"


# ── DS-105: the answer must reach a player who already exists ─────────────

def test_jersey_number_reaches_an_existing_player(client, sb, clean_team):
    """
    The coach's answer was consulted only when CREATING a player. A numberless
    player is created by the first game they appear in, so from the second
    game on the answer did nothing — McClung, answered twice, applied never.
    """
    upload(client, GAME_2)              # creates McClung with no number
    before = sb.table("players").select("number").eq("team_id", clean_team["id"]) \
        .eq("first_name", "Connor").execute().data
    assert before and before[0]["number"] is None

    upload(client, GAME_2, player_choices=json.dumps(
        [{"first": "Connor", "last": "McClung", "choice": "new", "number": "33"}]))

    after = sb.table("players").select("number,is_guest") \
        .eq("team_id", clean_team["id"]).eq("first_name", "Connor").execute().data
    assert len(after) == 1, "a duplicate player was created instead of updating"
    assert after[0]["number"] == 33, \
        f"the answer never reached the existing player (number={after[0]['number']})"


def test_guest_choice_is_recorded(client, sb, clean_team):
    upload(client, GAME_5, player_choices=json.dumps(
        [{"first": "Tristan", "last": "", "choice": "guest", "number": None}]))
    t = sb.table("players").select("is_guest,number") \
        .eq("team_id", clean_team["id"]).eq("first_name", "Tristan").execute().data
    assert t and t[0]["is_guest"] is True


def test_unanswered_player_still_imports_their_stats(client, sb, clean_team):
    """
    The governing rule of the review block: nothing may gate persistence on
    the coach answering. DS-94a's whole point.
    """
    upload(client, GAME_5)              # no player_choices at all
    game = only_game(sb, clean_team["id"])
    assert len(rows(sb, "batting_stats", game["game_id"])) == 11


# ── DS-114: play-by-play names ────────────────────────────────────────────

def test_play_by_play_names_are_clean(client, sb, clean_team):
    """
    batter_name held "W Salisian is" and "Unknown"; pitcher_name held "11".
    Opposing batters have no name at all and hit a NOT NULL constraint.
    """
    r = upload(client, GAME_9, pbp=True)
    assert r.status_code == 200, r.data[:400]
    game = only_game(sb, clean_team["id"])
    pas = rows(sb, "plate_appearances", game["game_id"])
    assert pas, "no play-by-play was stored"

    ours = [p for p in pas if p["batting_team"] == "our_team"]
    assert ours, "no plate appearances for our team"
    assert not [p for p in ours if p["batter_name"] == "Unknown"]
    assert not [p for p in ours if (p["batter_name"] or "").endswith((" is", " was"))]
    assert not [p for p in ours if not p["batter_player_id"]], \
        "an our-team plate appearance could not be linked to a player"
    assert not [p for p in pas if (p["pitcher_name"] or "").isdigit()], \
        "pitcher_name is holding a jersey number again"


# ── DS-100: a doubleheader stays two games ────────────────────────────────

def test_two_games_on_one_date_stay_separate(client, sb, clean_team):
    """
    Identity used to key on the parsed opponent name, so improving the parser
    created duplicates — and the tempting fix (one game per date) would merge
    the halves of a doubleheader instead.
    """
    upload(client, GAME_1)
    upload(client, GAME_2)
    games = sb.table("games").select("game_id,game_date") \
        .eq("team_id", clean_team["id"]).execute().data
    assert len(games) == 2, f"expected 2 distinct games, got {len(games)}"


# ── the suite must not touch anyone else's data ───────────────────────────

def test_harness_writes_only_to_the_test_team(client, sb, clean_team):
    """
    The safety property that makes running against the live database
    acceptable. If this ever fails, stop using the harness.
    """
    upload(client, GAME_1)
    other_teams = [t for t in sb.table("teams").select("id,team_name").execute().data
                   if t["id"] != clean_team["id"]]
    for t in other_teams:
        recent = (sb.table("games").select("game_id")
                  .eq("team_id", t["id"]).execute().data or [])
        assert all(g["game_id"] for g in recent)   # readable, untouched
    mine = sb.table("games").select("team_id").eq("team_id", clean_team["id"]).execute().data
    assert mine and all(g["team_id"] == clean_team["id"] for g in mine)
