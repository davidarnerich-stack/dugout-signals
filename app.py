"""Dugout Signals — file upload web app."""
import os
from datetime import datetime
from functools import wraps

from flask import (Flask, session, redirect, url_for, request,
                   render_template, flash, jsonify)
from supabase import create_client
from supabase_auth.errors import AuthApiError

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.secret_key   = os.environ.get("FLASK_SECRET_KEY", "change-me-in-production")
APP_PASSWORD     = os.environ.get("APP_PASSWORD",     "storm2026")
# Stub until DS-14 (Auth and Account Management) establishes team_name per
# coach account. Every query/insert below reads team_name from the session,
# not this constant — this is just what login currently seeds it with.
DEFAULT_TEAM_NAME = os.environ.get("TEAM_NAME",       "Storm 12U All-Stars")
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
    "Baseball": ["Little League Baseball", "USSSA", "Other"],
    "Softball": ["Little League Softball", "USA Softball", "USSSA", "Other"],
}
AGE_LEVEL_OPTIONS   = ["8U", "9U", "10U", "11U", "12U", "14U"]
SEASON_OPTIONS      = ["Spring 2026", "Summer 2026", "Fall 2026", "Spring 2027", "Summer 2027"]
HEARD_ABOUT_OPTIONS = ["Word of mouth", "Google Search", "Reddit", "Social Media", "QR Code", "Other"]


def default_season():
    month, year = datetime.now().month, datetime.now().year
    if month <= 5:
        return f"Spring {year}"
    if month <= 8:
        return f"Summer {year}"
    return f"Fall {year}"


# ── Auth ───────────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        # Self-heal sessions issued before team_name existed in the session
        # (e.g. a browser still logged in from before this was added).
        session.setdefault("team_name", DEFAULT_TEAM_NAME)
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("password") == APP_PASSWORD:
            session["authenticated"] = True
            session["team_name"]     = DEFAULT_TEAM_NAME
            return redirect(url_for("index"))
        flash("Wrong password.")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── DS-52: Supabase Auth signup + email verification ───────────────────────────
@app.route("/signup", methods=["GET", "POST"])
def signup():
    # Already authenticated + verified coaches don't need to sign up again.
    # /dashboard doesn't exist yet (DS-17) — this will resolve once it ships.
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
        governing_body = form.get("governing_body") or ""
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
        if season not in SEASON_OPTIONS:
            errors["season"] = "Please select a season."

        if errors:
            return _render_onboarding(errors, form)

        try:
            # Re-check immediately before insert — guards a double-submit race,
            # same single-team-per-coach limit as the GET guard above.
            if has_team():
                return redirect("/dashboard")

            sb.table("teams").insert({
                "coach_id":       session["coach_id"],
                "team_name":      team_name,
                "sport":          sport,
                "age_level":      age_level,
                "governing_body": governing_body,
                "season":         season,
                "league_name":    league_name,
            }).execute()

            if source:
                sb.auth.admin.update_user_by_id(session["coach_id"], {
                    "user_metadata": {"source": source, "source_other_text": source_other},
                })
        except Exception:
            errors["_general"] = "Something went wrong creating your team. Please try again."
            return _render_onboarding(errors, form)

        session["team_name"] = team_name  # keeps the DS-36 stub consistent until DS-54's cutover
        return redirect("/dashboard")

    return _render_onboarding({}, {})


# ── Pages ──────────────────────────────────────────────────────────────────────
@app.route("/")
@login_required
def index():
    return render_template("upload.html")


# ── API: tournaments ───────────────────────────────────────────────────────────
@app.route("/api/tournaments")
@login_required
def api_tournaments():
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    resp = (
        sb.table("tournaments")
        .select("tournament_id, name, season, year")
        .order("year", desc=True)
        .order("name")
        .execute()
    )
    return jsonify(resp.data)


@app.route("/api/tournaments/<tournament_id>/next_game")
@login_required
def api_next_game(tournament_id):
    from parsers.common import next_game_number
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    return jsonify({"next": next_game_number(sb, tournament_id, session["team_name"])})


# ── API: detect — read files, extract game info, no DB writes ──────────────────
@app.route("/api/detect", methods=["POST"])
@login_required
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
            game_info = extract_game_info_from_pdf(box_bytes, filename=box_filename)
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
                            game_info.get("opponent_name"), session["team_name"])
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
@login_required
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
            info = extract_game_info_from_pdf(bs_bytes, filename=bs_name)
            # Tournament (only relevant for tournament game type)
            tournament_id = None
            if game_type == "tournament" and tournament_name:
                tournament_id = find_or_create_tournament(sb, tournament_name)

            game_id = find_or_create_game(
                sb,
                game_date     = info["game_date"]     or manual_date,
                opponent_name = info["opponent_name"] or manual_opponent,
                storm_runs    = info["storm_runs"],
                opponent_runs = info["opponent_runs"],
                tournament_id = tournament_id,
                game_number   = game_number if game_type == "tournament" else 1,
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
            r = bs_process(sb, bs_bytes, team_name, game_id=game_id)
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
                r = st_process(sb, st_bytes, team_name, game_id=game_id)
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
                r = pb_process(sb, pb_bytes, team_name, game_id=game_id, is_text=False)
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
                r = pb_process(sb, pbp_text, team_name, game_id=game_id, is_text=True)
                results.append({"filename": "play-by-play (pasted)", "status": "success",
                                 "message": r["message"], "details": r.get("details", [])})
            except Exception as e:
                results.append({"filename": "play-by-play (pasted)", "status": "error",
                                 "message": str(e)})

    if not results:
        results.append({"filename": "", "status": "error",
                         "message": "Nothing was processed. Upload a box score PDF and/or stats CSV."})

    return jsonify(results)


@app.route("/api/games")
@login_required
def api_games():
    """Return all games for the edit dropdown, most recent first."""
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    resp = (
        sb.table("games")
        .select("game_id, game_date, opponent_name, team_runs, opponent_runs, result, game_type")
        .eq("team_name", session["team_name"])
        .order("game_date", desc=True)
        .order("game_number", desc=True)
        .execute()
    )
    return jsonify(resp.data)


@app.route("/api/games/<game_id>", methods=["PATCH"])
@login_required
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
    sb.table("games").update(update).eq("game_id", game_id).execute()
    return jsonify({"ok": True})


# ── Reports ───────────────────────────────────────────────────────────────────
@app.route("/reports")
@login_required
def reports_list():
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)

    # All saved reports
    saved = (
        sb.table("reports")
        .select("report_id,title,season,games_played,wins,losses,ties,generated_at")
        .eq("team_name", session["team_name"])
        .order("generated_at", desc=True)
        .execute()
    ).data

    # All tournaments for the generate dropdown
    tournaments = (
        sb.table("tournaments")
        .select("tournament_id,name,season")
        .order("season", desc=True)
        .order("name")
        .execute()
    ).data

    return render_template("reports.html", reports=saved, tournaments=tournaments)


@app.route("/reports/generate", methods=["POST"])
@login_required
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
        result = generate_full_report(sb, tournament_id, ANTHROPIC_KEY, session["team_name"])
        d = result["data"]
        sections = result["sections"]

        title = f"{d['tournament_name']} — Performance Analysis"

        # Save report to Supabase
        import json
        resp = sb.table("reports").insert({
            "tournament_id":   tournament_id,
            "tournament_name": d["tournament_name"],
            "season":          d["season"],
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
@login_required
def report_view(report_id):
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)

    # Load saved report — scoped to the coach's own team
    r = (sb.table("reports").select("*")
         .eq("report_id", report_id)
         .eq("team_name", session["team_name"])
         .execute())
    if not r.data:
        return "Report not found", 404
    report = r.data[0]

    import json
    sections = json.loads(report["sections"]) if isinstance(report["sections"], str) else report["sections"]

    # Reload live data for tables (so edits to game data show fresh)
    from reports.data import get_tournament_data
    d = get_tournament_data(sb, report["tournament_id"], report["team_name"])

    return render_template("report.html", report=report, sections=sections, d=d)


if __name__ == "__main__":
    app.run(debug=False)
