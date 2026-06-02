"""Dugout Signals — file upload web app."""
import os
from functools import wraps

from flask import (Flask, session, redirect, url_for, request,
                   render_template, flash, jsonify)
from supabase import create_client

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.secret_key        = os.environ.get("FLASK_SECRET_KEY", "change-me-in-production")
APP_PASSWORD          = os.environ.get("APP_PASSWORD",     "storm2026")
SUPABASE_URL          = os.environ.get("SUPABASE_URL",     "https://bqjbswbxtyapupufoarv.supabase.co")
SUPABASE_KEY          = os.environ.get("SUPABASE_KEY",     "")
MAX_FILE_SIZE_MB      = 20

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


# ── File routing ───────────────────────────────────────────────────────────────
def detect_file_type(filename):
    n = filename.lower()
    if n.endswith("_stats.csv"):         return "stats"
    if n.endswith("_box-score.pdf"):     return "box_score"
    if n.endswith("_play-by-play.docx"): return "play_by_play"
    return None


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/")
@login_required
def index():
    return render_template("upload.html")


@app.route("/upload", methods=["POST"])
@login_required
def upload():
    files = request.files.getlist("files")
    if not files:
        return jsonify([{"filename": "", "status": "error",
                         "message": "No files received."}])

    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    results = []

    for file in files:
        filename = file.filename
        file_type = detect_file_type(filename)

        if not file_type:
            results.append({
                "filename": filename, "status": "error",
                "message": "Unrecognised file type. Expected *_Stats.csv, *_Box-Score.pdf, or *_Play-by-Play.docx",
            })
            continue

        file_bytes = file.read()
        if len(file_bytes) > MAX_FILE_SIZE_MB * 1024 * 1024:
            results.append({"filename": filename, "status": "error",
                             "message": f"File exceeds {MAX_FILE_SIZE_MB} MB limit."})
            continue

        try:
            if file_type == "stats":
                from parsers.stats import process
            elif file_type == "box_score":
                from parsers.box_scores import process
            else:
                from parsers.play_by_play import process

            result = process(sb, filename, file_bytes)
            results.append({
                "filename": filename, "status": "success",
                "message": result["message"],
                "details": result.get("details", []),
            })
        except Exception as e:
            results.append({
                "filename": filename, "status": "error",
                "message": str(e),
            })

    return jsonify(results)


if __name__ == "__main__":
    app.run(debug=False)
