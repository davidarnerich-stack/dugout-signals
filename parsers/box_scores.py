"""Parse *_Box-Score.pdf files and update Supabase."""
import io
import re
import pdfplumber

# The jersey number is OPTIONAL. GameChanger prints a batter with no number
# as "C McClung (CF) 1 0 0 0 1 1", and requiring "#N" dropped that line before
# anything else ran. Worse than losing the row: batting position is counted by
# matched lines, so every batter below a numberless one was recorded a slot too
# high. Four of the seven PSW games were affected, one of them from the 5-hole
# down. The lineup was not merely incomplete, it was wrong.
BATTER_RE = re.compile(
    r"(.+?)\s+(?:#(\d+)\s*)?(?:\((\w+)\))?\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*$"
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


def _parse_game_info(raw_text, title_names=None):
    from parsers.common import parse_score_and_opponent
    info = parse_score_and_opponent(raw_text, title_names)
    return info["opponent_name"], info["our_runs"], info["opponent_runs"], info["is_away"]


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
        # Stop at the pitching table too. Now that the jersey number is
        # optional, a line like "Eloise W 2.0 1 1 1 2 3 0" would otherwise
        # match as a batter named "Eloise W 2.0". The Totals row happens to
        # stop us first on every file seen so far, which is luck, not a rule.
        if re.search(r"IP\s+H\s+R\s+ER\s+BB\s+SO", line):
            break
        m = BATTER_RE.match(line.strip())
        if m:
            pos += 1
            players.append({
                "name": m.group(1).strip(),
                "number": int(m.group(2)) if m.group(2) else None,
                "position": m.group(3), "batting_position": pos,
            })
    return players


def _match_lineup_player(batter, roster):
    """
    Resolve one box score lineup entry to a player.

    By jersey number when the line has one — that is unambiguous. Otherwise by
    name, which the box score abbreviates and sometimes truncates: "C McClung",
    "W Salisi…". So match on the last name as a prefix, and on the first
    initial when one is given.

    Returns the player_id, or None when nothing matches or more than one thing
    does. A None is reported by the caller rather than skipped in silence —
    that silence is what hid this for four games.
    """
    if batter["number"] is not None:
        for p in roster:
            if p.get("number") == batter["number"]:
                return p["player_id"]
        return None

    raw = batter["name"].replace("…", "").replace("...", "").strip().lower()
    if not raw:
        return None
    parts = raw.split()
    surname = parts[-1]
    initial = parts[0][0] if len(parts) > 1 else None

    hits = []
    for p in roster:
        last  = (p.get("last_name")  or "").strip().lower()
        first = (p.get("first_name") or "").strip().lower()
        # Either side may be the truncated one.
        if not last or not (last.startswith(surname) or surname.startswith(last)):
            continue
        if initial and first and not first.startswith(initial):
            continue
        hits.append(p["player_id"])
    return hits[0] if len(hits) == 1 else None


def process(sb, file_bytes, team_id, team_name, game_id=None, filename=None):
    """
    Process a box score PDF.
    If game_id is provided, updates that game record.
    If filename is provided (legacy), looks up game by filename convention.
    """
    from parsers.common import parse_title_names
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        raw = "\n".join(page.extract_text() or "" for page in pdf.pages)
        left0, right0 = _get_columns(pdf.pages[0])
        title_names = parse_title_names(pdf.pages[0]) if pdf.pages else None

    opponent_name, storm_runs, opp_runs, is_away = _parse_game_info(raw, title_names)
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
    # Never let the parsed name overwrite one a coach supplied. This step runs
    # after the upload has already applied the coach's answer, so writing
    # unconditionally put GameChanger's "TBD- <date>" placeholder straight back
    # over the name they had just typed.
    if opponent_name:
        from parsers.common import should_write_opponent_name
        stored = (sb.table("games").select("opponent_name")
                  .eq("game_id", game_id).execute().data or [{}])
        if should_write_opponent_name(opponent_name, stored[0].get("opponent_name")):
            update["opponent_name"] = opponent_name
    if update:
        sb.table("games").update(update).eq("game_id", game_id).execute()

    storm_batters = _parse_batting_column(storm_col)
    opp_batters   = _parse_batting_column(opp_col)
    details = []

    # One roster read, then match in memory — the old code issued a query per
    # batter and had no way to fall back to a name.
    roster = (sb.table("players")
              .select("player_id, number, first_name, last_name")
              .eq("team_id", team_id).execute().data or [])
    unmatched_lineup = []

    for batter in storm_batters:
        player_id = _match_lineup_player(batter, roster)
        if player_id is None:
            # Say so. A silently dropped lineup entry also shifts every
            # batting position below it, so this must never pass unremarked.
            unmatched_lineup.append(
                f"{batter['name']}"
                + (f" #{batter['number']}" if batter["number"] is not None else "")
                + f" (bat #{batter['batting_position']})"
            )
            continue
        existing = (sb.table("batting_order").select("id")
                    .eq("game_id", game_id).eq("player_id", player_id).execute())
        row = {"game_id": game_id, "player_id": player_id, "team_id": team_id,
               "batting_position": batter["batting_position"],
               "defensive_position": batter["position"]}
        if existing.data:
            sb.table("batting_order").update(row).eq("id", existing.data[0]["id"]).execute()
        else:
            sb.table("batting_order").insert(row).execute()
        label = f"#{batter['number']}" if batter["number"] is not None else batter["name"]
        details.append(f"{label} bat#{batter['batting_position']} ({batter['position']})")

    for batter in opp_batters:
        num_str = str(batter["number"]) if batter["number"] is not None else ""
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
    score = f"{team_name} {storm_runs} – {opponent_name or 'Opponent'} {opp_runs}"
    message = (f"{venue} | {score} | {len(details)} batting order "
               f"+ {len(opp_batters)} opponent players stored.")
    if unmatched_lineup:
        message += (f" {len(unmatched_lineup)} lineup "
                    f"{'entry' if len(unmatched_lineup) == 1 else 'entries'} "
                    f"could not be matched to a player ({', '.join(unmatched_lineup)}) "
                    f"— batting positions for this game may be incomplete.")
    return {
        "message": message,
        "details": details,
        "unmatched_lineup": unmatched_lineup,
    }
