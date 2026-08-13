"""
Shared fixtures for the end-to-end upload tests (DS-106).

These drive the real Flask app through its own HTTP layer, against the real
database, using the real GameChanger export files. That combination is the
point: every bug this suite exists to catch — DS-105, DS-107, DS-108, DS-114 —
lived between the HTTP request and the database, and was invisible to reading
the code or querying the data separately.

Two things are deliberately faked, and only two:

  auth      `@auth_required` validates Supabase tokens on every request. That
            is its own subject (DS-116) and not what these tests are about, so
            the token check is patched and the session set directly.

  the model  report generation makes several Anthropic calls per upload —
            real money, 30-60s per run. Stubbed. The report's *content* is
            covered by David reading real reports; what these tests protect is
            the write path underneath it.

Everything else is real: real parsing, real inserts, real constraints.
"""
import os
import re
import uuid
import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = "/Users/davidarnerich/Desktop/PSW Hilighters Summer 2026"

TEST_TEAM_NAME = "ZZ Test Harness"


def _load_env():
    """Read .env into the environment. Never printed, never committed."""
    path = os.path.join(REPO, ".env")
    if not os.path.exists(path):
        pytest.skip(".env not found — see DS-106 for setup", allow_module_level=True)
    for line in open(path):
        if line.strip().startswith("#"):
            continue
        m = re.match(r"\s*([A-Z_]+)\s*=\s*(.*)", line)
        if m:
            os.environ.setdefault(m.group(1), m.group(2).strip().strip('"').strip("'"))


_load_env()


@pytest.fixture(scope="session")
def sb():
    from supabase import create_client
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


@pytest.fixture(scope="session")
def test_team(sb):
    """
    A team no real coach uses. Every write these tests make is scoped to it,
    which is the same team_id scoping that keeps the three real teams apart.

    Reused across runs rather than recreated, so a crashed run leaves nothing
    orphaned; `clean_team` empties it before each test instead.
    """
    found = (sb.table("teams").select("*")
             .eq("team_name", TEST_TEAM_NAME).limit(1).execute().data)
    if found:
        return found[0]
    # Created once by hand, not here: teams.coach_id is a foreign key to a real
    # auth user, so the team cannot be conjured. Failing loudly beats silently
    # inventing one — and beats attaching it to a real coach, since
    # _sync_team_session picks a coach's team with an unordered limit(1) and
    # would then sometimes log that coach into this team.
    pytest.skip(
        f"test team {TEST_TEAM_NAME!r} not found — see DS-106 for one-time setup",
        allow_module_level=True)


def _wipe(sb, team_id):
    """
    Empty the test team. Order matters: rows referencing a game must go before
    the game, and players are referenced by stats rows.
    """
    games = (sb.table("games").select("game_id")
             .eq("team_id", team_id).execute().data or [])
    for g in games:
        gid = g["game_id"]
        for table in ("batting_stats", "pitching_stats", "fielding_stats",
                      "batting_order", "plate_appearances", "base_running_events",
                      "inning_scores", "unmatched_box_score_players", "reports"):
            sb.table(table).delete().eq("game_id", gid).execute()
    sb.table("reports").delete().eq("team_id", team_id).execute()
    sb.table("games").delete().eq("team_id", team_id).execute()
    sb.table("players").delete().eq("team_id", team_id).execute()


@pytest.fixture
def clean_team(sb, test_team):
    """A team with nothing in it, before and after. Each test starts empty."""
    _wipe(sb, test_team["id"])
    yield test_team
    _wipe(sb, test_team["id"])


@pytest.fixture
def client(sb, clean_team, monkeypatch):
    """
    A logged-in client for the test team, with the model stubbed.

    The auth patch replaces only the token check — every route, parser and
    write below it is the real thing.
    """
    import app as A

    class _User:
        id = "test-harness"
        email = "harness@example.test"

    monkeypatch.setattr(A, "get_authenticated_client", lambda: (sb, _User()))

    # No Anthropic calls: report generation is several model round-trips per
    # upload. Returning None makes _generate_single_game_report_safe skip,
    # exactly as it does when the key is absent.
    monkeypatch.setattr(A, "_generate_single_game_report_safe",
                        lambda *a, **k: None)
    monkeypatch.setattr(A, "_generate_and_record_team_signals_safe",
                        lambda *a, **k: None)

    c = A.app.test_client()
    with c.session_transaction() as s:
        s["team_id"]    = clean_team["id"]
        s["team_name"]  = clean_team["team_name"]
        s["coach_id"]   = _User.id
        s["coach_email"] = _User.email
        s["sport"]      = clean_team["sport"]
    return c


def game_files(prefix):
    """
    The real GameChanger exports for one PSW game, by filename prefix.
    Returns (box_score_bytes, box_name, stats_bytes, stats_name, pbp_text).

    Using the coach's own files is deliberate: every parser bug found so far
    came from a real quirk — a blank surname, a missing jersey number, a name
    with a suffix — that invented fixtures would not have contained.
    """
    import glob
    box = stats = pbp = None
    box_name = stats_name = None
    for path in glob.glob(os.path.join(FIXTURES, prefix + "*")):
        base = os.path.basename(path)
        if base.endswith(".pdf"):
            box, box_name = open(path, "rb").read(), base
        elif base.endswith(".csv"):
            stats, stats_name = open(path, "rb").read(), base
        else:
            pbp = open(path, "rb").read().decode("utf-8", "ignore")
    return box, box_name, stats, stats_name, pbp


def detect(client, prefix):
    """
    POST one real game's files to /api/detect exactly as the browser does
    before the coach sees the review block. Writes nothing.

    This is the step that decides which players the coach is asked about, and
    it is a different code path from /upload — a player can be written
    correctly and still be asked about forever.
    """
    import io
    box, box_name, stats, stats_name, _ = game_files(prefix)
    files = []
    if box:
        files.append((io.BytesIO(box), box_name))
    if stats:
        files.append((io.BytesIO(stats), stats_name))
    return client.post("/api/detect", data={"files": files},
                       content_type="multipart/form-data")


def upload(client, prefix, *, pbp=False, **form):
    """POST one real game through /upload exactly as the browser does."""
    import io
    box, box_name, stats, stats_name, pbp_text = game_files(prefix)
    data = {"game_type": "regular", "tournament_name": "", "game_number": "1",
            "manual_date": "", "manual_opponent": "",
            "pbp_text": pbp_text if pbp and pbp_text else ""}
    data.update(form)
    files = []
    if box:
        files.append(("files", (io.BytesIO(box), box_name)))
    if stats:
        files.append(("files", (io.BytesIO(stats), stats_name)))
    for key, val in files:
        data.setdefault(key, [])
    payload = dict(data)
    payload["files"] = [f[1] for f in files]
    return client.post("/upload", data=payload,
                       content_type="multipart/form-data")
