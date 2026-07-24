"""Parse *_Box-Score.pdf files and update Supabase."""
import io
import re
import pdfplumber

BATTER_RE = re.compile(
    r"(.+?)\s+#(\d+)\s*(?:\((\w+)\))?\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)"
)
POS_COLS = {"P":189,"C":190,"1B":191,"2B":192,"3B":193,"SS":194,"LF":195,"CF":196,"RF":197,"SF":198}


def _words_to_text(word_list):
    if not word_list:
        return ""
    rows = {}
    for w in word_list:
        y = round(w["top"] / 3) * 3
        rows.setdefault(y, []).append(w)
    return "\n".join(
        " ".join(w["text"] for w in sorted(rows[y], key=lambda w: w["x0"]))
        for y in sorted(rows)
    )


def _get_columns(page):
    mid = page.width / 2
    words = page.extract_words(x_tolerance=3, y_tolerance=3)
    left  = [w for w in words if (w["x0"]+w["x1"])/2 < mid]
    right = [w for w in words if (w["x0"]+w["x1"])/2 >= mid]
    return _words_to_text(left), _words_to_text(right)


def _parse_game_info(raw_text):
    is_away = bool(re.search(r"\bAway\b", raw_text[:400]))
    storm_runs = opp_runs = None
    in_score = False
    for line in raw_text.splitlines():
        s = line.strip()
        if re.match(r"^1\s+2\s+3", s):
            in_score = True; continue
        if in_score:
            if not s or s.startswith("BATTING"):
                break
            m = re.match(r"^([A-Z0-9]{2,6})\s+([\dX\s]+)$", s)
            if m:
                nums = [int(n) for n in re.findall(r"\d+", m.group(2))]
                if len(nums) >= 3:
                    if "STRM" in m.group(1):
                        storm_runs = nums[-3]
                    else:
                        opp_runs = nums[-3]

    header = " ".join(raw_text[:500].split())
    opponent_name = None
    if is_away:
        pre  = re.search(r"^(.+?)\s+Storm\s+12u\s+Silver\s+All\s+Star", header, re.I)
        post = re.search(r"Storm\s+12u\s+Silver\s+All\s+Star\s+\d+\s*-\s*\d+\s+(.+?)\s*(?:Away|Home)", header, re.I)
        parts = [g.group(1).strip() for g in [pre, post] if g and g.group(1).strip()]
        opponent_name = " ".join(parts) if parts else None
    else:
        m = re.search(r"^(.+?)\s+\d+\s*-\s*\d+\s+Storm\s+12u\s+Silver\s+All\s+Star\s*(.*?)\s*Home", header, re.I)
        if m:
            parts = [m.group(1).strip(), m.group(2).strip()]
            opponent_name = " ".join(p for p in parts if p)
    if opponent_name:
        opponent_name = re.sub(r"-\s+", "-", opponent_name).strip()
    return opponent_name, storm_runs, opp_runs, is_away


def _parse_batting_column(col_text):
    in_section = False
    players = []
    pos = 0
    for line in col_text.splitlines():
        if re.search(r"AB\s+R\s+H\s+RBI\s+BB\s+SO", line):
            in_section = True; continue
        if not in_section:
            continue
        if re.match(r"Totals", line.strip()):
            break
        if re.search(r"\bTB:|SB:|CS:|LOB:|HBP:|PITCHING\b", line):
            break
        m = BATTER_RE.match(line.strip())
        if m:
            pos += 1
            players.append({
                "name": m.group(1).strip(), "number": int(m.group(2)),
                "position": m.group(3), "batting_position": pos,
            })
    return players


def process(sb, file_bytes, team_id, team_name, game_id=None, filename=None):
    """
    Process a box score PDF.
    If game_id is provided, updates that game record.
    If filename is provided (legacy), looks up game by filename convention.
    """
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        raw = "\n".join(page.extract_text() or "" for page in pdf.pages)
        left0, right0 = _get_columns(pdf.pages[0])

    opponent_name, storm_runs, opp_runs, is_away = _parse_game_info(raw)
    storm_col = left0 if is_away else right0
    opp_col   = right0 if is_away else left0

    # Resolve game_id
    if game_id is None and filename:
        import re as _re
        m = _re.match(r"^(\d{4}-\d{2}-\d{2})-(.+?)_((?:Game)?(\d+))_", filename)
        if not m:
            raise ValueError(f"Cannot identify game from filename: {filename}")
        date    = m.group(1)
        game_num = int(m.group(4))
        game_resp = (sb.table("games").select("game_id")
                     .eq("game_date", date).eq("game_number", game_num)
                     .eq("team_id", team_id).execute())
        if not game_resp.data:
            raise ValueError(f"No game found for {date} game {game_num}.")
        game_id = game_resp.data[0]["game_id"]
    elif game_id is None:
        raise ValueError("Either game_id or filename must be provided")

    update = {}
    if storm_runs is not None:
        update["team_runs"] = storm_runs
        update["opponent_runs"] = opp_runs
    if opponent_name:
        update["opponent_name"] = opponent_name
    if update:
        sb.table("games").update(update).eq("game_id", game_id).execute()

    storm_batters = _parse_batting_column(storm_col)
    opp_batters   = _parse_batting_column(opp_col)
    details = []

    for batter in storm_batters:
        player_resp = (sb.table("players").select("player_id")
                       .eq("number", batter["number"]).eq("team_id", team_id).execute())
        if not player_resp.data:
            continue
        player_id = player_resp.data[0]["player_id"]
        existing = (sb.table("batting_order").select("id")
                    .eq("game_id", game_id).eq("player_id", player_id).execute())
        row = {"game_id": game_id, "player_id": player_id, "team_id": team_id,
               "batting_position": batter["batting_position"],
               "defensive_position": batter["position"]}
        if existing.data:
            sb.table("batting_order").update(row).eq("id", existing.data[0]["id"]).execute()
        else:
            sb.table("batting_order").insert(row).execute()
        details.append(f"#{batter['number']} bat#{batter['batting_position']} ({batter['position']})")

    for batter in opp_batters:
        num_str = str(batter["number"])
        if not sb.table("unmatched_box_score_players").select("id") \
                  .eq("game_id", game_id).eq("number", num_str) \
                  .eq("team_name", opponent_name or "Unknown").execute().data:
            sb.table("unmatched_box_score_players").insert({
                "game_id": game_id, "box_score_name": batter["name"],
                "number": num_str, "team_name": opponent_name or "Unknown",
                "batting_position": batter["batting_position"],
                "defensive_position": batter["position"],
                "resolution_status": "pending",
            }).execute()

    venue = "Away" if is_away else "Home"
    score = f"Storm {storm_runs} – {opponent_name or 'Opponent'} {opp_runs}"
    return {
        "message": f"{venue} | {score} | {len(storm_batters)} batting order + {len(opp_batters)} opponent players stored.",
        "details": details,
    }
