"""Dugout Signals — file upload web app."""
import os
from functools import wraps

from flask import (Flask, session, redirect, url_for, request,
                   render_template, flash, jsonify)
from supabase import create_client

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.secret_key   = os.environ.get("FLASK_SECRET_KEY", "change-me-in-production")
APP_PASSWORD     = os.environ.get("APP_PASSWORD",     "storm2026")
SUPABASE_URL     = os.environ.get("SUPABASE_URL",     "https://bqjbswbxtyapupufoarv.supabase.co")
SUPABASE_KEY     = os.environ.get("SUPABASE_KEY",     "")
MAX_FILE_MB      = 20


# ── Auth ───────────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("password") == APP_PASSWORD:
            session["authenticated"] = True
            return redirect(url_for("index"))
        flash("Wrong password.")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


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
    return jsonify({"next": next_game_number(sb, tournament_id)})


# ── API: detect — read files, extract game info, no DB writes ──────────────────
@app.route("/api/detect", methods=["POST"])
@login_required
def api_detect():
    """
    Step 1: inspect uploaded files and extract game info from the box score.
    Returns detected file types + extracted game metadata.  No DB writes.
    """
    from parsers.common import detect_file_type, extract_game_info_from_pdf

    files   = request.files.getlist("files")
    result  = {"files": [], "game_info": None}
    box_bytes = None

    for f in files:
        fb   = f.read()
        ftype = detect_file_type(f.filename, fb)
        result["files"].append({"name": f.filename, "type": ftype})
        if ftype == "box_score":
            box_bytes = fb

    if box_bytes:
        try:
            result["game_info"] = extract_game_info_from_pdf(box_bytes)
        except Exception as e:
            result["game_info_error"] = str(e)

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
                                TEAM_NAME, SEASON)

    files           = request.files.getlist("files")
    tournament_name = request.form.get("tournament_name", "").strip()
    game_number     = int(request.form.get("game_number", 1) or 1)
    pbp_text        = request.form.get("pbp_text", "").strip()

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
            info = extract_game_info_from_pdf(bs_bytes)
            # Tournament
            tournament_id = None
            if tournament_name:
                tournament_id = find_or_create_tournament(sb, tournament_name)

            game_id = find_or_create_game(
                sb,
                game_date     = info["game_date"],
                opponent_name = info["opponent_name"],
                storm_runs    = info["storm_runs"],
                opponent_runs = info["opponent_runs"],
                tournament_id = tournament_id,
                game_number   = game_number,
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
            r = bs_process(sb, bs_bytes, game_id=game_id)
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
                r = st_process(sb, st_bytes, game_id=game_id)
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
                r = pb_process(sb, pb_bytes, game_id=game_id, is_text=False)
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
                r = pb_process(sb, pbp_text, game_id=game_id, is_text=True)
                results.append({"filename": "play-by-play (pasted)", "status": "success",
                                 "message": r["message"], "details": r.get("details", [])})
            except Exception as e:
                results.append({"filename": "play-by-play (pasted)", "status": "error",
                                 "message": str(e)})

    if not results:
        results.append({"filename": "", "status": "error",
                         "message": "Nothing was processed. Upload a box score PDF and/or stats CSV."})

    return jsonify(results)


if __name__ == "__main__":
    app.run(debug=False)
