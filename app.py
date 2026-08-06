"""Dugout Signals — file upload web app."""
import os
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import (Flask, session, redirect, url_for, request,
                   render_template, jsonify)
from supabase import create_client
from supabase_auth.errors import AuthApiError

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.secret_key   = os.environ.get("FLASK_SECRET_KEY", "change-me-in-production")
# Flask session cookies are browser-session-only by default (die when the
# browser closes). DS-54 AC #3 requires staying logged in across browser
# restarts, so sessions marked permanent get a real expiry instead.
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
SUPABASE_URL     = os.environ.get("SUPABASE_URL",     "https://bqjbswbxtyapupufoarv.supabase.co")
SUPABASE_KEY     = os.environ.get("SUPABASE_KEY",     "")
# Publishable/anon key — safe to expose, used only for self-service Auth
# operations (signup, resend, session exchange), never for data queries.
SUPABASE_ANON_KEY = os.environ.get(
    "SUPABASE_ANON_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImJxamJzd2J4dHlhcHVwdWZvYXJ2Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTY0ODk2MzMsImV4cCI6MjA3MjA2NTYzM30.LBHDFQy1Nvr_0yvd2reub6VLsvrxKKvYzVaQCl64LNE",
)
# Base URL used to build Supabase Auth email-confirmation redirect links.
SITE_URL         = os.environ.get("SITE_URL",         "https://app.dugoutsignals.ai")
MAX_FILE_MB      = 20


def get_anon_client():
    """Supabase client using the anon key, for self-service Auth operations."""
    return create_client(SUPABASE_URL, SUPABASE_ANON_KEY)


# DS-53: Team Setup Wizard — field option sets and season default logic.
GOVERNING_BODY_OPTIONS = {
    "Baseball": ["Little League", "USSSA", "Other"],
    "Softball": ["Little League", "USA Softball", "USSSA", "Other"],
}
AGE_LEVEL_OPTIONS   = ["8U", "9U", "10U", "11U", "12U", "14U"]
SEASON_OPTIONS      = ["Spring 2026", "Summer 2026", "Fall 2026", "Spring 2027", "Summer 2027"]
HEARD_ABOUT_OPTIONS = ["Word of mouth", "Google Search", "Reddit", "Social Media", "QR Code", "Other"]

# DS-77: default teams.regulation_innings from governing_body + age_level.
# Sport isn't a dimension here — USSSA/Little League cover both. "Other"
# governing bodies have no table entry (regulation_innings stays null; the
# inferred value from the team's own data is used until a coach sets it).
REGULATION_INNINGS_TABLE = {
    "USA Softball":  {"8U": 6, "9U": 6, "10U": 6, "11U": 6, "12U": 6, "14U": 7},
    "Little League": {"8U": 6, "9U": 6, "10U": 6, "11U": 6, "12U": 6, "14U": 7},
    "USSSA":         {"8U": 5, "9U": 6, "10U": 6, "11U": 6, "12U": 6, "14U": 7},
}

def default_regulation_innings(governing_body, age_level):
    return REGULATION_INNINGS_TABLE.get(governing_body, {}).get(age_level)


def infer_regulation_innings(sb, team_id):
    """DS-77: reverse GameChanger's own ERA calculation from this team's
    stored pitching data, as an independent sanity check on the configured
    regulation_innings. GameChanger computes ERA scaled to the regulation
    game length rather than a fixed 9 innings: ERA = regulation_innings *
    earned_runs / innings_pitched. Solving for regulation_innings per row
    and aggregating as Sum(ERA_i * IP_i) / Sum(ER_i) — rather than averaging
    per-row estimates directly — weights each game by how much earned-run
    evidence it actually contributes, so a one-inning relief outing with 1
    earned run doesn't swing the estimate as much as a full start.

    Returns None if there isn't enough data to infer anything yet (no
    earned runs recorded across any stored outing)."""
    resp = (sb.table("pitching_stats").select("era, earned_runs, innings_pitched")
            .eq("team_id", team_id).execute())
    total_er = sum((r.get("earned_runs") or 0) for r in resp.data)
    if total_er <= 0:
        return None
    weighted = sum(
        r["era"] * r["innings_pitched"]
        for r in resp.data if r.get("era") and r.get("innings_pitched")
    )
    if weighted <= 0:
        return None
    return round(weighted / total_er, 2)


def default_season():
    month, year = datetime.now().month, datetime.now().year
    if month <= 5:
        return f"Spring {year}"
    if month <= 8:
        return f"Summer {year}"
    return f"Fall {year}"


# ── Auth ───────────────────────────────────────────────────────────────────────
def _sync_team_session(coach_id):
    """Populate session team_id/team_name/sport from the coach's team record, if any.

    team_id is the real scoping key every query/parser filters by; team_name
    and sport are carried alongside purely for display (page headers, report
    narrative, sport-appropriate logo/pill on Roster).
    """
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    resp = (
        sb.table("teams").select("id, team_name, sport")
        .eq("coach_id", coach_id).limit(1).execute()
    )
    if resp.data:
        session["team_id"]   = resp.data[0]["id"]
        session["team_name"] = resp.data[0]["team_name"]
        session["sport"]     = resp.data[0]["sport"]
    else:
        # Must explicitly clear, not just skip — otherwise a stale team_id
        # left over from a *different* coach's session (switched accounts in
        # the same browser without logging out) silently carries forward and
        # misattributes this coach to someone else's team.
        session.pop("team_id", None)
        session.pop("team_name", None)
        session.pop("sport", None)


def get_authenticated_client():
    """Validates (and refreshes, if needed) the current session's Supabase
    Auth tokens. Returns (client, user), or (None, None) if there's no valid
    session. Persists refreshed tokens back into the Flask session."""
    access_token  = session.get("access_token")
    refresh_token = session.get("refresh_token")
    if not access_token or not refresh_token:
        return None, None

    client = get_anon_client()
    try:
        auth_resp = client.auth.set_session(access_token, refresh_token)
    except AuthApiError:
        return None, None
    if not auth_resp.user:
        return None, None

    current = client.auth.get_session()
    if current:
        session["access_token"]  = current.access_token
        session["refresh_token"] = current.refresh_token

    return client, auth_resp.user


def auth_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        _, user = get_authenticated_client()
        if not user:
            session.clear()
            return redirect(url_for("login"))
        session["coach_id"]    = user.id
        session["coach_email"] = user.email
        if not session.get("team_name"):
            _sync_team_session(user.id)
        if not session.get("team_name"):
            # Verified and logged in, but never finished team setup.
            return redirect(url_for("onboarding"))
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    email = ""
    if request.method == "POST":
        email    = (request.form.get("email") or "").strip()
        password = request.form.get("password") or ""
        try:
            resp = get_anon_client().auth.sign_in_with_password({
                "email": email, "password": password,
            })
        except AuthApiError:
            error = "Incorrect email or password."
        else:
            session.permanent         = True
            session["access_token"]  = resp.session.access_token
            session["refresh_token"] = resp.session.refresh_token
            session["coach_id"]      = resp.user.id
            session["coach_email"]   = resp.user.email
            _sync_team_session(resp.user.id)
            # A coach who verified but never finished team setup (e.g. closed
            # the browser mid-onboarding) has no team_id yet — send them back
            # to finish it rather than a /dashboard that doesn't apply to them.
            if not session.get("team_id"):
                return redirect(url_for("onboarding"))
            return redirect("/dashboard")
    return render_template("login.html", error=error, email=email)


@app.route("/logout")
def logout():
    access_token  = session.get("access_token")
    refresh_token = session.get("refresh_token")
    if access_token and refresh_token:
        try:
            client = get_anon_client()
            client.auth.set_session(access_token, refresh_token)
            client.auth.sign_out()
        except AuthApiError:
            pass
    session.clear()
    return redirect(url_for("login"))


@app.after_request
def add_no_cache_headers(response):
    # Prevents the browser back button from showing cached authenticated
    # pages after logout (DS-54 AC #6).
    if not request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


# ── DS-52: Supabase Auth signup + email verification ───────────────────────────
@app.route("/signup", methods=["GET", "POST"])
def signup():
    # Already authenticated + verified coaches don't need to sign up again.
    # /dashboard now resolves for every coach (DS-60) — empty state or a
    # redirect to /reports if they already have games.
    if session.get("email_verified"):
        return redirect("/dashboard")

    if request.method == "POST":
        email    = (request.form.get("email") or "").strip()
        password = request.form.get("password") or ""

        if len(password) < 8:
            return render_template(
                "signup.html", email=email,
                error_field="password",
                error_message="Password must be at least 8 characters.",
            )

        redirect_to = f"{SITE_URL}/auth/callback"
        try:
            resp = get_anon_client().auth.sign_up({
                "email": email,
                "password": password,
                "options": {"email_redirect_to": redirect_to},
            })
        except AuthApiError as e:
            if e.code == "user_already_exists":
                return render_template(
                    "signup.html", email=email,
                    error_field="email",
                    error_message="An account with this email already exists — try logging in.",
                )
            return render_template(
                "signup.html", email=email,
                error_field="email",
                error_message="Something went wrong creating your account. Please try again.",
            )

        # Supabase doesn't raise an error for an email that's already registered
        # (anti-enumeration behavior) — it returns a 200 with a synthetic user
        # whose identities list is empty instead. Must check for that explicitly.
        if resp.user is not None and not resp.user.identities:
            return render_template(
                "signup.html", email=email,
                error_field="email",
                error_message="An account with this email already exists — try logging in.",
            )

        session["pending_email"] = email
        return redirect(url_for("verify_email"))

    return render_template("signup.html", email=None, error_field=None, error_message=None)


@app.route("/verify-email")
def verify_email():
    email = session.get("pending_email")
    if not email:
        return redirect(url_for("signup"))
    return render_template("verify_email.html", email=email, resent=False)


@app.route("/verify-email/resend", methods=["POST"])
def verify_email_resend():
    email = session.get("pending_email")
    if not email:
        return redirect(url_for("signup"))
    redirect_to = f"{SITE_URL}/auth/callback"
    try:
        get_anon_client().auth.resend({
            "type": "signup",
            "email": email,
            "options": {"email_redirect_to": redirect_to},
        })
        resent = True
    except AuthApiError:
        resent = False
    return render_template("verify_email.html", email=email, resent=resent)


@app.route("/auth/callback", methods=["GET"])
def auth_callback():
    # Supabase redirects here after email confirmation with the session
    # tokens in the URL fragment (never sent to the server) — this page's
    # inline script reads them and exchanges them via POST below.
    return render_template("auth_callback.html")


@app.route("/auth/callback", methods=["POST"])
def auth_callback_session():
    data          = request.get_json(silent=True) or {}
    access_token  = data.get("access_token")
    refresh_token = data.get("refresh_token")
    if not access_token or not refresh_token:
        return jsonify({"redirect": url_for("signup")}), 400

    try:
        auth_resp = get_anon_client().auth.set_session(access_token, refresh_token)
    except AuthApiError:
        return jsonify({"redirect": url_for("signup")}), 400

    user = auth_resp.user
    if not user:
        return jsonify({"redirect": url_for("signup")}), 400

    if not user.email_confirmed_at:
        session["pending_email"] = user.email
        return jsonify({"redirect": url_for("verify_email")})

    session.pop("pending_email", None)
    session.permanent          = True
    session["access_token"]   = auth_resp.session.access_token
    session["refresh_token"]  = auth_resp.session.refresh_token
    session["coach_id"]       = user.id
    session["coach_email"]    = user.email
    session["email_verified"] = True
    return jsonify({"redirect": url_for("onboarding")})


# ── DS-53: Team Setup Wizard ────────────────────────────────────────────────────
def _render_onboarding(errors, form):
    return render_template(
        "onboarding.html",
        governing_body_options=GOVERNING_BODY_OPTIONS,
        age_level_options=AGE_LEVEL_OPTIONS,
        season_options=SEASON_OPTIONS,
        heard_about_options=HEARD_ABOUT_OPTIONS,
        default_season=default_season(),
        errors=errors,
        form=form,
    )


@app.route("/onboarding", methods=["GET", "POST"])
def onboarding():
    # Only a coach who's been through signup + email verification gets here.
    # Full protected-route enforcement for every page is DS-54's job.
    if not session.get("coach_id"):
        return redirect(url_for("signup"))

    sb = create_client(SUPABASE_URL, SUPABASE_KEY)

    def has_team():
        resp = (
            sb.table("teams").select("id")
            .eq("coach_id", session["coach_id"]).limit(1).execute()
        )
        return bool(resp.data)

    if has_team():
        return redirect("/dashboard")

    if request.method == "POST":
        form           = request.form
        team_name      = (form.get("team_name") or "").strip()
        sport          = form.get("sport") or ""
        age_level      = form.get("age_level") or ""
        governing_body       = form.get("governing_body") or ""
        governing_body_other = (form.get("governing_body_other_text") or "").strip()
        season         = form.get("season") or ""
        league_name    = (form.get("league_name") or "").strip() or None
        source         = form.get("source") or ""
        source_other   = (form.get("source_other_text") or "").strip() or None

        errors = {}
        if not team_name:
            errors["team_name"] = "Team name is required."
        if sport not in GOVERNING_BODY_OPTIONS:
            errors["sport"] = "Please select a sport."
        if age_level not in AGE_LEVEL_OPTIONS:
            errors["age_level"] = "Please select an age level."
        if governing_body not in GOVERNING_BODY_OPTIONS.get(sport, []):
            errors["governing_body"] = "Please select a governing body."
        elif governing_body == "Other" and not governing_body_other:
            errors["governing_body"] = "Please specify your governing body."
        if season not in SEASON_OPTIONS:
            errors["season"] = "Please select a season."
        if not league_name:
            errors["league_name"] = "League name is required."

        if errors:
            return _render_onboarding(errors, form)

        # DS-77: keep governing_body as the literal selection (including
        # "Other") and store the free text separately in governing_body_other
        # — same pattern already used for source/source_other_text below.
        # Previously this overwrote governing_body with the free text, which
        # silently broke the "Other" case for regulation_innings defaulting
        # (nothing downstream could ever match governing_body == "Other"
        # again once a coach had typed something in).

        try:
            # Re-check immediately before insert — guards a double-submit race,
            # same single-team-per-coach limit as the GET guard above.
            if has_team():
                return redirect("/dashboard")

            insert_resp = sb.table("teams").insert({
                "coach_id":                session["coach_id"],
                "team_name":               team_name,
                "sport":                   sport,
                "age_level":               age_level,
                "governing_body":          governing_body,
                "governing_body_other":    governing_body_other if governing_body == "Other" else None,
                "season":                  season,
                "league_name":             league_name,
                "regulation_innings":      default_regulation_innings(governing_body, age_level),
                "continuous_batting_order": True,
            }).execute()

            if source:
                sb.auth.admin.update_user_by_id(session["coach_id"], {
                    "user_metadata": {"source": source, "source_other_text": source_other},
                })
        except Exception:
            errors["_general"] = "Something went wrong creating your team. Please try again."
            return _render_onboarding(errors, form)

        session["team_id"]   = insert_resp.data[0]["id"]
        session["team_name"] = team_name
        return redirect("/dashboard")

    return _render_onboarding({}, {})


# ── Pages ──────────────────────────────────────────────────────────────────────
@app.route("/")
@app.route("/upload", methods=["GET"])
@auth_required
def index():
    return render_template("upload.html", team_name=session["team_name"])


# ── DS-65: Dashboard shell and navigation (empty state carried over from DS-60) ─
DASHBOARD_LOGO_BY_SPORT = {
    "Softball": "Logo_Dugout-Signals__softball_-transparent.svg",
    "Baseball": "Logo_Dugout-Signals__baseball_-transparent.svg",
}


@app.route("/dashboard")
@auth_required
def dashboard():
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    games_resp = (
        sb.table("games")
        .select("game_id, game_date, opponent_name, team_runs, opponent_runs, result, team_totals")
        .eq("team_id", session["team_id"])
        .order("game_date", desc=True)
        .order("game_number", desc=True)
        .execute()
    )
    all_games = games_resp.data or []
    last_game = all_games[0] if all_games else None
    games_played = len(all_games)

    # DS-66: the report row for the last game — DS-62's unique (game_id,
    # report_type) constraint guarantees at most one, so .limit(1) already
    # is "exactly one report surfaced" (AC #5), not a truncation.
    last_game_report = None
    if last_game:
        report_resp = (
            sb.table("reports")
            .select("report_id, report_headline, status")
            .eq("game_id", last_game["game_id"])
            .eq("report_type", "single_game")
            .eq("team_id", session["team_id"])
            .limit(1)
            .execute()
        )
        last_game_report = report_resp.data[0] if report_resp.data else None

    sport = session.get("sport") or "Baseball"

    # ── DS-67: Team Signals ─────────────────────────────────────────────────
    signal_cards, summary_line, show_age_footnote = [], None, False
    if all_games:
        team_resp = (sb.table("teams").select("*").eq("id", session["team_id"]).limit(1).execute())
        team = team_resp.data[0] if team_resp.data else {}
        age_level = team.get("age_level") or "12U"

        team_totals_list = [g["team_totals"] for g in all_games if g.get("team_totals")]
        pitching_resp = (
            sb.table("pitching_stats").select("*")
            .eq("team_id", session["team_id"]).execute()
        )
        pitcher_rows_raw = pitching_resp.data or []
        pit_totals_by_player = {}
        for row in pitcher_rows_raw:
            pid = row["player_id"]
            agg = pit_totals_by_player.setdefault(pid, {
                "batters_faced": 0, "walks_allowed": 0, "strikeouts": 0,
            })
            agg["batters_faced"] += row.get("batters_faced") or 0
            agg["walks_allowed"] += row.get("walks_allowed") or 0
            agg["strikeouts"] += row.get("strikeouts") or 0
        pitcher_rows = list(pit_totals_by_player.values())

        if team_totals_list:
            from signals.team_signals import compute_team_signals, compute_familiar_anchors, CARD_ORDER
            signal_cards = compute_team_signals(
                team_totals_list, pitcher_rows,
                age_level=age_level, regulation_innings=team.get("regulation_innings") or 6,
            )
            # Narrative is NOT generated here — it's computed once per game
            # commit in the upload flow (_generate_and_record_team_signals_safe)
            # and cached in signal_history. Generating it here too used to
            # mean 6 sequential Claude calls (~24s) on every dashboard view;
            # this route just reads back whatever was cached at upload time.
            if signal_cards and last_game:
                t_to_key = {f"T{i+1}": k for i, k in enumerate(CARD_ORDER)}
                history_resp = (
                    sb.table("signal_history")
                    # id + why_text: DS-69 explanation view. id is the "signal
                    # instance" identifier feedback is stored against; a card
                    # with no cached row (narrative generation failed or
                    # hasn't run yet) simply has no explanation to open.
                    .select("id, signal_key, headline, interpretation, why_text, games_in_sample")
                    .eq("team_id", session["team_id"])
                    .eq("game_id", last_game["game_id"])
                    .order("computed_at", desc=True)
                    .execute()
                )
                cached_by_key = {}
                for row in (history_resp.data or []):
                    key = t_to_key.get(row["signal_key"])
                    if key and key not in cached_by_key:  # first hit per key = most recent, since ordered desc
                        cached_by_key[key] = row
                for card in signal_cards:
                    cached = cached_by_key.get(card["key"])
                    card["headline"] = cached["headline"] if cached else None
                    card["interpretation"] = cached["interpretation"] if cached else None
                    card["why_text"] = cached["why_text"] if cached else None
                    card["signal_history_id"] = cached["id"] if cached else None
                    card["games_in_sample"] = cached["games_in_sample"] if cached else len(team_totals_list)

        # Summary line — games since last viewed, per DS-67 §7b. Never "this
        # week"; counts games because teams often play twice in a week and a
        # bye week would leave calendar framing as dead air.
        total_games = len(all_games)
        last_viewed = team.get("dashboard_last_viewed_game_count") or 0
        new_games = max(0, total_games - last_viewed)
        card_count = len(signal_cards)
        if new_games == 0:
            summary_line = f"{card_count} things worth knowing through {total_games} game{'s' if total_games != 1 else ''}."
        elif new_games == 1:
            weekday = None
            raw_date = last_game.get("game_date")
            if raw_date:
                try:
                    weekday = datetime.strptime(str(raw_date)[:10], "%Y-%m-%d").strftime("%A")
                except ValueError:
                    weekday = None
            when = f"after {weekday}" if weekday else "after your last game"
            summary_line = f"{card_count} things worth knowing {when}."
        else:
            summary_line = f"{card_count} things worth knowing after your last {new_games} games."
        sb.table("teams").update({"dashboard_last_viewed_game_count": total_games}).eq("id", session["team_id"]).execute()

        # 8U suppression footnote — sessions 1-3 only, per teams.dashboard_age_footnote_shown_count.
        if age_level == "8U":
            shown_count = team.get("dashboard_age_footnote_shown_count") or 0
            if shown_count < 3:
                show_age_footnote = True
                sb.table("teams").update(
                    {"dashboard_age_footnote_shown_count": shown_count + 1}
                ).eq("id", session["team_id"]).execute()

    from signals.team_signals import card_metric_rows as _card_metric_rows, card_context_chips as _card_context_chips
    for card in signal_cards:
        card["rows"] = _card_metric_rows(card)
        # DS-69: explanation view's context chips. games_in_sample was set
        # above from the cached signal_history row when present; falls back
        # to 0 only if signal_cards exists without a matching history row
        # (narrative generation failed at upload time) — chips still render,
        # just without a precise window count.
        card["context_chips"] = _card_context_chips(card, card.get("games_in_sample") or 0)

    # DS-68: reshapes the same signal_cards facts into the Offence/Defence
    # containment hierarchy — not a second computation pass. See
    # signals/bucket_modules.py's module docstring.
    bucket_hierarchy = {"offence": None, "defence": None}
    metric_explainers = {}
    if signal_cards:
        from signals.bucket_modules import build_bucket_hierarchy
        bucket_hierarchy = build_bucket_hierarchy(signal_cards)

        # DS-73: metric explainer content, one entry per metric that's
        # actually tappable on this render (see explainers.yml's scope
        # note). team_totals_list/team are only reachable here because
        # signal_cards is non-empty, which requires the same all_games /
        # team_totals_list block above to have run.
        #
        # Wrapped defensively (found via a live incident: DS-73 shipped
        # without PyYAML in requirements.txt, and this call — unguarded —
        # took down the entire /dashboard route for every team with games,
        # not just the metric-explainer feature). A failure here now
        # degrades to plain, non-tappable metric names rather than a
        # server error — same "no single point of failure should crash
        # the whole page" lesson as DS-85.
        try:
            from signals.team_signals import compute_familiar_anchors as _compute_familiar_anchors
            from metrics.explainer import build_metric_explainers
            familiar_anchors = _compute_familiar_anchors(
                team_totals_list, regulation_innings=team.get("regulation_innings") or 6,
            )
            metric_explainers = build_metric_explainers(signal_cards, familiar_anchors)
        except Exception:
            app.logger.exception("DS-73 metric explainer build failed — degrading to plain metric names")
            metric_explainers = {}

    return render_template(
        "dashboard.html",
        team_name=session.get("team_name") or "your team",
        sport=sport,
        logo_file=DASHBOARD_LOGO_BY_SPORT.get(sport, DASHBOARD_LOGO_BY_SPORT["Baseball"]),
        # DS-66 (Last Game), DS-67 (Team Signals) and DS-68 (bucket modules)
        # are the real content that replaces DS-65's interim placeholder.
        has_games=last_game is not None,
        games_played=games_played,
        last_game=last_game,
        last_game_report=last_game_report,
        signal_cards=signal_cards,
        summary_line=summary_line,
        show_age_footnote=show_age_footnote,
        offence_module=bucket_hierarchy["offence"],
        defence_module=bucket_hierarchy["defence"],
        metric_explainers=metric_explainers,
    )


@app.route("/api/bucket-tap", methods=["POST"])
@auth_required
def api_bucket_tap():
    """DS-68 AC #15: record which bucket a coach tapped, for DS-32/33/34
    discovery — showing what coaches actually reach for rather than what
    was guessed. Fire-and-forget from the client; a failure here must never
    block navigation to the tapped bucket's signal card."""
    data = request.get_json(silent=True) or {}
    bucket = (data.get("bucket") or "").strip()
    if not bucket:
        return jsonify({"error": "bucket is required"}), 400
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    try:
        sb.table("bucket_taps").insert({"team_id": session["team_id"], "bucket": bucket}).execute()
    except Exception:
        pass
    return jsonify({"ok": True})


@app.route("/api/signals/<int:signal_history_id>/feedback", methods=["POST"])
@auth_required
def api_signal_feedback(signal_history_id):
    """DS-69 req 5/6: Useful / Not-useful rating on one signal explanation
    view instance. signal_history_id is the "signal instance" identifier —
    signal_history is the closest stable one for team signals (there's no
    separate per-view id; each computed-and-narrated card already gets one
    row there per game commit). RLS scopes the insert to the caller's own
    team, same as bucket_taps."""
    data = request.get_json(silent=True) or {}
    rating = (data.get("rating") or "").strip()
    if rating not in ("useful", "not_useful"):
        return jsonify({"error": "rating must be 'useful' or 'not_useful'"}), 400
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    try:
        sb.table("signal_feedback").insert({
            "signal_history_id": signal_history_id,
            "team_id": session["team_id"],
            "coach_id": session["coach_id"],
            "rating": rating,
        }).execute()
    except Exception:
        return jsonify({"error": "Could not save feedback."}), 500
    return jsonify({"ok": True})


# ── DS-56: Roster ────────────────────────────────────────────────────────────
# Design source of truth: Jira DS-56 attachment "design_handoff_roster" (Roster.dc.html),
# approved final 2026-07-22. Icon/color table below matches its meta() function exactly.
POSITION_ORDER = ["P", "C", "1B", "2B", "3B", "SS", "LF", "CF", "RF"]
# 6-state tap-cycle model. "blank" is the absence of a key in position_eligibility.
POSITION_STATE_CYCLE = ["primary", "secondary", "developing", "requested", "no"]
POSITION_STATE_META = {
    "primary":    {"sym": "★", "color": "#38bdf8", "bg": "rgba(56,189,248,.12)",  "border": "rgba(56,189,248,.4)",  "label": "Primary"},
    "secondary":  {"sym": "✓", "color": "#3fb950", "bg": "rgba(63,185,80,.12)",   "border": "rgba(63,185,80,.4)",   "label": "Secondary"},
    "developing": {"sym": "◐", "color": "#d29922", "bg": "rgba(210,153,34,.12)",  "border": "rgba(210,153,34,.4)",  "label": "Developing"},
    "requested":  {"sym": "⚑", "color": "#a371f7", "bg": "rgba(163,113,247,.12)", "border": "rgba(163,113,247,.42)", "label": "Requested"},
    "no":         {"sym": "✗", "color": "#f85149", "bg": "rgba(248,81,73,.1)",    "border": "rgba(248,81,73,.35)",  "label": "No"},
    "blank":      {"sym": "○", "color": "#6e7681", "bg": "#161b22",              "border": "#30363d",              "label": "Blank"},
}
ROSTER_ROW_STATES = {"primary", "secondary", "developing"}  # only these show on the card strip

LEAGUE_AGE_OPTIONS = [6, 7, 8, 9, 10, 11, 12, 13, 14]
BATS_OPTIONS   = ["L", "R", "Switch"]
THROWS_OPTIONS = ["L", "R"]
ARM_OPTIONS    = [("Strong", "Strong"), ("Average", "Average"), ("Developing", "Developing")]
GLOVE_OPTIONS  = ["Sure-handed", "Average", "Developing"]
SPEED_OPTIONS  = ["Wheels", "Average", "Developing"]

LOGO_BY_SPORT = {
    "Softball": "Logo_Dugout-Signals__softball_-transparent.svg",
    "Baseball": "Logo_Dugout-Signals__baseball_-transparent.svg",
}


@app.route("/roster")
@auth_required
def roster():
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    resp = (
        sb.table("players")
        .select("player_id, number, first_name, last_name, league_age, bats, "
                "throws, arm, glove, speed, position_eligibility, signals, status")
        .eq("team_id", session["team_id"])
        .eq("status", "active")
        .order("number")
        .execute()
    )
    players = resp.data
    for p in players:
        pe = p.get("position_eligibility") or {}
        p["position_strip"] = [
            (pos, pe[pos]) for pos in POSITION_ORDER
            if pe.get(pos) in ROSTER_ROW_STATES
        ]
    sport = session.get("sport") or "Baseball"
    return render_template(
        "roster.html",
        players=players,
        sport=sport,
        logo_file=LOGO_BY_SPORT.get(sport, LOGO_BY_SPORT["Baseball"]),
        state_meta=POSITION_STATE_META,
        position_order=POSITION_ORDER,
        state_cycle=POSITION_STATE_CYCLE,
        league_age_options=LEAGUE_AGE_OPTIONS,
        bats_options=BATS_OPTIONS,
        throws_options=THROWS_OPTIONS,
        arm_options=ARM_OPTIONS,
        glove_options=GLOVE_OPTIONS,
        speed_options=SPEED_OPTIONS,
    )


@app.route("/api/players/<player_id>", methods=["PATCH"])
@auth_required
def api_update_player(player_id):
    data = request.get_json() or {}
    allowed = {"first_name", "last_name", "number", "league_age", "bats",
               "throws", "arm", "glove", "speed", "position_eligibility"}
    update = {k: v for k, v in data.items() if k in allowed}
    if not update:
        return jsonify({"error": "No valid fields to update"}), 400

    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    resp = (
        sb.table("players").update(update)
        .eq("player_id", player_id).eq("team_id", session["team_id"])
        .execute()
    )
    if not resp.data:
        return jsonify({"error": "Player not found"}), 404
    return jsonify({"ok": True})


@app.route("/api/players/<player_id>/archive", methods=["POST"])
@auth_required
def api_archive_player(player_id):
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    resp = (
        sb.table("players").update({"status": "archived"})
        .eq("player_id", player_id).eq("team_id", session["team_id"])
        .execute()
    )
    if not resp.data:
        return jsonify({"error": "Player not found"}), 404
    return jsonify({"ok": True})


@app.route("/api/players/<player_id>/notes", methods=["GET"])
@auth_required
def api_list_notes(player_id):
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    resp = (
        sb.table("coach_notes")
        .select("id, note_text, note_type, game_id, created_at, games(opponent_name, game_date)")
        .eq("player_id", player_id).eq("team_id", session["team_id"])
        .order("created_at", desc=True)
        .execute()
    )
    return jsonify(resp.data)


@app.route("/api/players/<player_id>/notes", methods=["POST"])
@auth_required
def api_add_note(player_id):
    data      = request.get_json() or {}
    note_text = (data.get("note_text") or "").strip()
    note_type = data.get("note_type")
    game_id   = data.get("game_id") or None

    if not note_text:
        return jsonify({"error": "Note text is required."}), 400
    if note_type not in ("game", "general"):
        return jsonify({"error": "note_type must be 'game' or 'general'."}), 400
    if note_type == "game" and not game_id:
        return jsonify({"error": "A game must be selected for a game note."}), 400
    if note_type == "general":
        game_id = None

    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    resp = sb.table("coach_notes").insert({
        "player_id": player_id,
        "team_id":   session["team_id"],
        "note_text": note_text,
        "note_type": note_type,
        "game_id":   game_id,
    }).execute()
    return jsonify(resp.data[0])


# ── API: tournaments ───────────────────────────────────────────────────────────
@app.route("/api/tournaments")
@auth_required
def api_tournaments():
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    resp = (
        sb.table("tournaments")
        .select("tournament_id, name, season, year")
        .eq("team_id", session["team_id"])
        .order("year", desc=True)
        .order("name")
        .execute()
    )
    return jsonify(resp.data)


@app.route("/api/tournaments/<tournament_id>/next_game")
@auth_required
def api_next_game(tournament_id):
    from parsers.common import next_game_number
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    return jsonify({"next": next_game_number(sb, tournament_id, session["team_id"])})


# ── API: detect — read files, extract game info, no DB writes ──────────────────
@app.route("/api/detect", methods=["POST"])
@auth_required
def api_detect():
    """
    Step 1: inspect uploaded files, extract game info from the box score, parse
    the stats CSV for a pitcher/batter preview, and check whether the matched
    game already has stats (duplicate detection).  No DB writes.
    """
    from parsers.common import (detect_file_type, extract_game_info_from_pdf,
                                find_game)

    files   = request.files.getlist("files")
    result  = {"files": [], "game_info": None,
               "pitchers": [], "batters_count": 0,
               "already_exists": False, "conflict_detail": None}
    box_bytes = stats_bytes = None
    box_filename = ""

    for f in files:
        fb    = f.read()
        ftype = detect_file_type(f.filename, fb)
        result["files"].append({"name": f.filename, "type": ftype})
        if ftype == "box_score":
            box_bytes    = fb
            box_filename = f.filename
        elif ftype == "stats":
            stats_bytes  = fb

    game_info = None
    if box_bytes:
        try:
            game_info = extract_game_info_from_pdf(box_bytes, filename=box_filename, team_name=session["team_name"])
            result["game_info"] = game_info
        except Exception as e:
            result["game_info_error"] = str(e)

    # Pitcher/batter preview straight from the CSV (no DB)
    if stats_bytes:
        try:
            from parsers.stats import preview as stats_preview
            pv = stats_preview(stats_bytes)
            result["pitchers"]      = pv["pitchers"]
            result["batters_count"] = pv["batters_count"]
        except Exception as e:
            result["stats_error"] = str(e)

    # Duplicate detection — does the matched game already have stats?
    if game_info:
        sb = create_client(SUPABASE_URL, SUPABASE_KEY)
        game_id = find_game(sb, game_info.get("game_date"),
                            game_info.get("opponent_name"), session["team_id"])
        if game_id:
            result["game_id"] = game_id
            pit = (sb.table("pitching_stats").select("stat_id", count="exact")
                   .eq("game_id", game_id).execute())
            bat = (sb.table("batting_stats").select("stat_id", count="exact")
                   .eq("game_id", game_id).execute())
            pit_n, bat_n = pit.count or 0, bat.count or 0
            # already_exists keys on pitching_stats rows (per spec §5c)
            if pit_n > 0:
                result["already_exists"]  = True
                result["conflict_detail"] = (
                    f"This game already has pitching stats for {pit_n} "
                    f"pitcher{'s' if pit_n != 1 else ''} and batting stats for "
                    f"{bat_n} player{'s' if bat_n != 1 else ''}. "
                    f"Uploading will overwrite existing data."
                )

    return jsonify(result)


# ── API: upload — process all files and write to DB ───────────────────────────
@app.route("/upload", methods=["POST"])
@auth_required
def upload():
    """
    Step 2: process files with confirmed metadata and write to Supabase.

    Form fields:
      files[]            — the uploaded files
      tournament_name    — selected or new tournament name
      game_number        — game number within tournament
      pbp_text           — pasted play-by-play text (optional)
    """
    from parsers.common import (detect_file_type, extract_game_info_from_pdf,
                                find_or_create_game, find_or_create_tournament,
                                SEASON)

    team_id            = session["team_id"]
    team_name          = session["team_name"]
    files              = request.files.getlist("files")
    game_type          = request.form.get("game_type", "tournament").strip()
    tournament_name    = request.form.get("tournament_name", "").strip()
    game_number        = int(request.form.get("game_number", 1) or 1)
    pbp_text           = request.form.get("pbp_text", "").strip()
    # Manual overrides — used when auto-detection fails
    manual_date        = request.form.get("manual_date", "").strip()
    manual_opponent    = request.form.get("manual_opponent", "").strip()

    if not files and not pbp_text:
        return jsonify([{"filename": "", "status": "error",
                         "message": "No files or play-by-play text received."}])

    sb      = create_client(SUPABASE_URL, SUPABASE_KEY)
    results = []

    # ── 1. Sort files by type ──────────────────────────────────────────────
    file_data = {}   # type → (filename, bytes)
    for f in files:
        fb    = f.read()
        if len(fb) > MAX_FILE_MB * 1024 * 1024:
            results.append({"filename": f.filename, "status": "error",
                             "message": f"File exceeds {MAX_FILE_MB} MB limit."})
            continue
        ftype = detect_file_type(f.filename, fb)
        if ftype == "unknown":
            results.append({"filename": f.filename, "status": "error",
                             "message": "Unrecognised file. Expected a GameChanger stats CSV or box score PDF."})
            continue
        file_data[ftype] = (f.filename, fb)

    # ── 2. Resolve game identity from box score ────────────────────────────
    game_id = None

    if "box_score" in file_data:
        bs_name, bs_bytes = file_data["box_score"]
        try:
            info = extract_game_info_from_pdf(bs_bytes, filename=bs_name, team_name=team_name)
            # Tournament (only relevant for tournament game type)
            tournament_id = None
            if game_type == "tournament" and tournament_name:
                tournament_id = find_or_create_tournament(sb, tournament_name, team_id)

            game_id = find_or_create_game(
                sb,
                game_date     = info["game_date"]     or manual_date,
                opponent_name = info["opponent_name"] or manual_opponent,
                storm_runs    = info["storm_runs"],
                opponent_runs = info["opponent_runs"],
                tournament_id = tournament_id,
                game_number   = game_number if game_type == "tournament" else 1,
                team_id       = team_id,
                team_name     = team_name,
                game_type     = game_type,
            )
        except Exception as e:
            results.append({"filename": bs_name, "status": "error",
                             "message": f"Box score error: {e}"})

    # ── 3. Process each file type ─────────────────────────────────────────
    # Box score
    if "box_score" in file_data and game_id:
        bs_name, bs_bytes = file_data["box_score"]
        try:
            from parsers.box_scores import process as bs_process
            r = bs_process(sb, bs_bytes, team_id, team_name, game_id=game_id)
            results.append({"filename": bs_name, "status": "success",
                             "message": r["message"], "details": r.get("details", [])})
        except Exception as e:
            results.append({"filename": bs_name, "status": "error", "message": str(e)})

    # Stats CSV
    if "stats" in file_data:
        st_name, st_bytes = file_data["stats"]
        if game_id is None:
            results.append({"filename": st_name, "status": "error",
                             "message": "Stats CSV needs a box score PDF to identify the game. "
                                        "Upload them together."})
        else:
            try:
                from parsers.stats import process as st_process
                r = st_process(sb, st_bytes, team_id, team_name, game_id=game_id)
                results.append({"filename": st_name, "status": "success",
                                 "message": r["message"], "details": r.get("details", [])})
            except Exception as e:
                results.append({"filename": st_name, "status": "error", "message": str(e)})

    # Play-by-play DOCX (legacy)
    if "play_by_play" in file_data:
        pb_name, pb_bytes = file_data["play_by_play"]
        if game_id is None:
            results.append({"filename": pb_name, "status": "error",
                             "message": "Play-by-play needs a box score PDF to identify the game."})
        else:
            try:
                from parsers.play_by_play import process as pb_process
                r = pb_process(sb, pb_bytes, team_id, team_name, game_id=game_id, is_text=False)
                results.append({"filename": pb_name, "status": "success",
                                 "message": r["message"], "details": r.get("details", [])})
            except Exception as e:
                results.append({"filename": pb_name, "status": "error", "message": str(e)})

    # Play-by-play pasted text
    if pbp_text:
        if game_id is None:
            results.append({"filename": "play-by-play (pasted)", "status": "error",
                             "message": "Play-by-play needs a box score PDF to identify the game."})
        else:
            try:
                from parsers.play_by_play import process as pb_process
                r = pb_process(sb, pbp_text, team_id, team_name, game_id=game_id, is_text=True)
                results.append({"filename": "play-by-play (pasted)", "status": "success",
                                 "message": r["message"], "details": r.get("details", [])})
            except Exception as e:
                results.append({"filename": "play-by-play (pasted)", "status": "error",
                                 "message": str(e)})

    if not results:
        results.append({"filename": "", "status": "error",
                         "message": "Nothing was processed. Upload a box score PDF and/or stats CSV."})

    # ── DS-11: auto-generate a single-game report once the commit succeeded ──
    # "Commit succeeded" = the box score landed (game identity + final score),
    # which is the minimum needed for a report (stats CSV enriches it but
    # AC #1 only requires "detect step passes, commit step succeeds").
    box_score_ok = any(r["status"] == "success" and r["filename"] == file_data.get("box_score", (None,))[0]
                        for r in results)
    report_id = None
    if game_id and box_score_ok:
        report_id = _generate_single_game_report_safe(sb, game_id, team_id, team_name)
        # DS-67/DS-76: compute + narrate + snapshot team signals once per
        # game commit here, not on every dashboard view — found live that
        # generating narrative for 6 cards on every /dashboard GET made
        # every page view block for ~24s. Upload is already a
        # 30-60s-expected operation; dashboard views must be instant.
        _generate_and_record_team_signals_safe(sb, game_id, team_id, team_name)

    return jsonify({"results": results, "report_id": report_id})


def _generate_and_record_team_signals_safe(sb, game_id, team_id, team_name):
    """Best-effort team-signals computation + narrative + DS-76 history
    snapshot for one game commit. Never raises — this is instrumentation
    plus a dashboard-read cache, not a critical path, same contract as
    signal_history.record_signal_history's own try/except."""
    try:
        team_resp = sb.table("teams").select("*").eq("id", team_id).limit(1).execute()
        team = team_resp.data[0] if team_resp.data else {}
        age_level = team.get("age_level") or "12U"
        sport = team.get("sport") or "Baseball"

        games_resp = (
            sb.table("games").select("team_totals").eq("team_id", team_id).execute()
        )
        team_totals_list = [g["team_totals"] for g in (games_resp.data or []) if g.get("team_totals")]
        if not team_totals_list:
            return

        pitching_resp = sb.table("pitching_stats").select("*").eq("team_id", team_id).execute()
        pit_totals_by_player = {}
        for row in (pitching_resp.data or []):
            agg = pit_totals_by_player.setdefault(row["player_id"], {
                "batters_faced": 0, "walks_allowed": 0, "strikeouts": 0,
            })
            agg["batters_faced"] += row.get("batters_faced") or 0
            agg["walks_allowed"] += row.get("walks_allowed") or 0
            agg["strikeouts"] += row.get("strikeouts") or 0
        pitcher_rows = list(pit_totals_by_player.values())

        from signals.team_signals import compute_team_signals, CARD_ORDER, card_metric_rows
        signal_cards = compute_team_signals(
            team_totals_list, pitcher_rows,
            age_level=age_level, regulation_innings=team.get("regulation_innings") or 6,
        )
        if not signal_cards:
            return

        ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
        if ANTHROPIC_KEY:
            from signals.narrative import generate_all_narratives
            generate_all_narratives(ANTHROPIC_KEY, sport, team_name, age_level, signal_cards)

        from signals.history import record_signal_history
        key_to_t = {k: f"T{i+1}" for i, k in enumerate(CARD_ORDER)}
        contact_ok = all(c["state"] != "missing_data" for c in signal_cards)
        record_signal_history(
            sb, team_id, game_id,
            [{
                "signal_key": key_to_t[c["key"]],
                "bucket": c["bucket"],
                "headline": c.get("headline"),
                "interpretation": c.get("interpretation"),
                "why_text": c.get("why_text"),
                "metrics": card_metric_rows(c),
                "raw_inputs": c["facts"],
                "games_in_sample": len(team_totals_list),
            } for c in signal_cards],
            age_band=age_level, contact_data_ok=contact_ok,
        )
    except Exception:
        pass


def _generate_single_game_report_safe(sb, game_id, team_id, team_name):
    """Best-effort single-game report generation — never breaks the upload
    response. Silently skips if ANTHROPIC_API_KEY isn't configured (matches
    the existing /reports/generate guard) rather than erroring the upload.
    Returns the new report_id so the upload UI can link straight to it, or
    None if generation was skipped/failed.

    DS-62: replaces in place (upsert on game_id+report_type, matching the
    `reports_game_id_report_type_unique` partial index) instead of inserting
    a new row on every re-upload.

    `generated_at` is set explicitly here rather than left to the column's
    DEFAULT now() — that default only fires on the row's first INSERT, so an
    upsert that regenerates an existing report would otherwise leave the
    original generation timestamp in place forever, silently making it wrong
    (found while verifying DS-79/DS-78 fixes against a real re-import).

    On failure: if no report exists yet for this game, write a minimal
    status='error' row so a dashboard reading reports.status has something
    to point a retry affordance at (DS-17b). If a working report already
    exists, leave it completely untouched — a failed regeneration attempt
    must never make a previously-good report look broken. This is the two
    cases AC2 covers; they need different handling, not one upsert for both.
    """
    ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
    if not ANTHROPIC_KEY:
        return None
    try:
        team_resp = (sb.table("teams").select("sport, age_level, governing_body")
                     .eq("id", team_id).limit(1).execute())
        team = team_resp.data[0] if team_resp.data else {}

        from reports.generate import generate_single_game_report
        result = generate_single_game_report(
            sb, game_id, ANTHROPIC_KEY, team_id, team_name,
            team.get("sport"), team.get("age_level"), team.get("governing_body"),
        )
        d, sections, headline = result["data"], result["sections"], result["headline"]
        g = d["game"]

        import json
        resp = sb.table("reports").upsert({
            "report_type":     "single_game",
            "game_id":         game_id,
            "team_id":         team_id,
            "team_name":       team_name,
            "season":          g.get("season"),
            "title":           f"{team_name} vs {g.get('opponent_name', 'Opponent')} — Game Analysis",
            "report_headline": headline,
            "header_block":    json.dumps(d["header_block"]),
            "sections":        json.dumps(sections),
            "games_played":    1,
            "wins":            1 if g.get("result") == "W" else 0,
            "losses":          1 if g.get("result") == "L" else 0,
            "ties":            1 if g.get("result") == "T" else 0,
            "runs_scored":     g.get("team_runs"),
            "runs_allowed":    g.get("opponent_runs"),
            "status":          "complete",
            "generated_at":    datetime.now(timezone.utc).isoformat(),
        }, on_conflict="game_id,report_type").execute()
        return resp.data[0]["report_id"]
    except Exception:
        # Best-effort: a failed report generation should never surface as an
        # upload failure to the coach. But don't just vanish — if nothing
        # exists yet for this game, leave a record the dashboard can show a
        # retry affordance for. If a good report already exists, touching it
        # here would destroy working content over a regeneration hiccup, so
        # leave it alone entirely (see docstring).
        try:
            existing = (sb.table("reports").select("report_id")
                        .eq("game_id", game_id).eq("report_type", "single_game")
                        .limit(1).execute())
            if not existing.data:
                sb.table("reports").upsert({
                    "report_type": "single_game",
                    "game_id":     game_id,
                    "team_id":     team_id,
                    "team_name":   team_name,
                    "title":       f"{team_name} — Game Analysis",
                    "status":      "error",
                }, on_conflict="game_id,report_type").execute()
        except Exception:
            pass
        return None


@app.route("/api/games")
@auth_required
def api_games():
    """Return all games for the edit dropdown, most recent first."""
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    resp = (
        sb.table("games")
        .select("game_id, game_date, opponent_name, team_runs, opponent_runs, result, game_type")
        .eq("team_id", session["team_id"])
        .order("game_date", desc=True)
        .order("game_number", desc=True)
        .execute()
    )
    return jsonify(resp.data)


@app.route("/api/games/<game_id>", methods=["PATCH"])
@auth_required
def api_update_game(game_id):
    """Update editable fields on an existing game record."""
    data = request.get_json()
    allowed = {"opponent_name", "team_runs", "opponent_runs"}
    update  = {k: v for k, v in data.items() if k in allowed}

    if not update:
        return jsonify({"error": "No valid fields to update"}), 400

    # Recalculate result if scores are present
    sr = update.get("team_runs")
    or_ = update.get("opponent_runs")
    if sr is not None and or_ is not None:
        if   sr > or_:  update["result"] = "W"
        elif sr < or_:  update["result"] = "L"
        else:            update["result"] = "T"

    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    resp = (
        sb.table("games").update(update)
        .eq("game_id", game_id).eq("team_id", session["team_id"])
        .execute()
    )
    if not resp.data:
        return jsonify({"error": "Game not found"}), 404
    return jsonify({"ok": True})


# ── Settings (DS-77) ─────────────────────────────────────────────────────────
@app.route("/settings", methods=["GET"])
@auth_required
def settings_page():
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    team = (sb.table("teams").select("*")
            .eq("id", session["team_id"]).limit(1).execute()).data[0]

    inferred = infer_regulation_innings(sb, session["team_id"])
    mismatch = (inferred is not None and team.get("regulation_innings") is not None
                and round(inferred) != team["regulation_innings"])

    return render_template(
        "settings.html", team=team, season_options=SEASON_OPTIONS,
        inferred_innings=inferred, mismatch=mismatch,
    )


@app.route("/api/settings", methods=["PATCH"])
@auth_required
def api_update_settings():
    """DS-77 AC7: minimum viable settings — regulation_innings,
    continuous_batting_order, season."""
    data = request.get_json() or {}
    allowed = {"regulation_innings", "continuous_batting_order", "season"}
    update = {k: v for k, v in data.items() if k in allowed}
    if not update:
        return jsonify({"error": "No valid fields to update"}), 400
    if "season" in update and update["season"] not in SEASON_OPTIONS:
        return jsonify({"error": "Invalid season"}), 400
    if "regulation_innings" in update:
        try:
            update["regulation_innings"] = int(update["regulation_innings"])
        except (TypeError, ValueError):
            return jsonify({"error": "regulation_innings must be a number"}), 400

    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    resp = (sb.table("teams").update(update)
            .eq("id", session["team_id"]).execute())
    if not resp.data:
        return jsonify({"error": "Team not found"}), 404
    return jsonify({"ok": True, "team": resp.data[0]})


# ── Reports ───────────────────────────────────────────────────────────────────
@app.route("/reports")
@auth_required
def reports_list():
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)

    # All saved reports
    saved = (
        sb.table("reports")
        .select("report_id,title,season,games_played,wins,losses,ties,generated_at")
        .eq("team_id", session["team_id"])
        .order("generated_at", desc=True)
        .execute()
    ).data

    # All tournaments for the generate dropdown
    tournaments = (
        sb.table("tournaments")
        .select("tournament_id,name,season")
        .eq("team_id", session["team_id"])
        .order("season", desc=True)
        .order("name")
        .execute()
    ).data

    return render_template("reports.html", reports=saved, tournaments=tournaments)


@app.route("/reports/generate", methods=["POST"])
@auth_required
def reports_generate():
    ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
    if not ANTHROPIC_KEY:
        return jsonify({"error": "ANTHROPIC_API_KEY is not configured. Add it in Render environment variables."}), 500

    data = request.get_json()
    tournament_id = data.get("tournament_id")
    if not tournament_id:
        return jsonify({"error": "No tournament_id provided."}), 400

    sb = create_client(SUPABASE_URL, SUPABASE_KEY)

    try:
        from reports.generate import generate_full_report
        result = generate_full_report(sb, tournament_id, ANTHROPIC_KEY,
                                       session["team_id"], session["team_name"])
        d = result["data"]
        sections = result["sections"]

        title = f"{d['tournament_name']} — Performance Analysis"

        # Save report to Supabase
        import json
        resp = sb.table("reports").insert({
            "tournament_id":   tournament_id,
            "tournament_name": d["tournament_name"],
            "season":          d["season"],
            "team_id":         session["team_id"],
            "team_name":       d["team_name"],
            "title":           title,
            "sections":        json.dumps(sections),
            "games_played":    len(d["games"]),
            "wins":            d["wins"],
            "losses":          d["losses"],
            "ties":            d["ties"],
            "runs_scored":     d["runs_scored"],
            "runs_allowed":    d["runs_allowed"],
            "status":          "complete",
        }).execute()

        report_id = resp.data[0]["report_id"]
        return jsonify({"report_id": report_id})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/reports/<report_id>")
@auth_required
def report_view(report_id):
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)

    # Load saved report — scoped to the coach's own team
    r = (sb.table("reports").select("*")
         .eq("report_id", report_id)
         .eq("team_id", session["team_id"])
         .execute())
    if not r.data:
        return "Report not found", 404
    report = r.data[0]

    import json
    sections = json.loads(report["sections"]) if isinstance(report["sections"], str) else report["sections"]

    if report.get("report_type") == "single_game":
        header_block = report.get("header_block")
        if isinstance(header_block, str):
            header_block = json.loads(header_block)
        # Reload live data (so edits to game stats after generation show fresh
        # in the box score / player lines, same freshness contract as tournament).
        from reports.data import get_single_game_data
        d = get_single_game_data(sb, report["game_id"], session["team_id"], report["team_name"])
        return render_template("game_report.html", report=report, sections=sections,
                                header_block=header_block, d=d,
                                game_type=d["game"].get("game_type"))

    # Reload live data for tables (so edits to game data show fresh)
    from reports.data import get_tournament_data
    d = get_tournament_data(sb, report["tournament_id"], session["team_id"], report["team_name"])

    return render_template("report.html", report=report, sections=sections, d=d)


@app.route("/reports/<report_id>/delete", methods=["POST"])
@auth_required
def report_delete(report_id):
    """DS-62: minimal delete entry point. Coach-facing placement/affordance
    design belongs to the split-out story (DS-70) — this is the capability
    plus a bare entry point, per that ticket's own scope note."""
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    resp = (
        sb.table("reports").delete()
        .eq("report_id", report_id).eq("team_id", session["team_id"])
        .execute()
    )
    if not resp.data:
        return jsonify({"error": "Report not found"}), 404
    return jsonify({"ok": True})


@app.route("/reports/<report_id>/retry", methods=["POST"])
@auth_required
def report_retry(report_id):
    """DS-66 AC #4: retry path for a single-game report stuck in status='error'.
    Re-runs the same best-effort generator the upload flow uses — DS-62's
    upsert means this replaces the error stub in place rather than duplicating it."""
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    r = (sb.table("reports").select("report_id, game_id, report_type")
         .eq("report_id", report_id).eq("team_id", session["team_id"])
         .limit(1).execute())
    if not r.data:
        return jsonify({"error": "Report not found"}), 404
    report = r.data[0]
    if report.get("report_type") != "single_game" or not report.get("game_id"):
        return jsonify({"error": "Retry is only supported for single-game reports"}), 400

    new_report_id = _generate_single_game_report_safe(
        sb, report["game_id"], session["team_id"], session["team_name"]
    )
    if new_report_id is None:
        return jsonify({"ok": False, "error": "Report generation failed again — try again shortly."}), 502
    return jsonify({"ok": True, "report_id": new_report_id})


# ── Errors ─────────────────────────────────────────────────────────────────────
@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404


if __name__ == "__main__":
    app.run(debug=False)
