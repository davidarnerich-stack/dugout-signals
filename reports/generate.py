"""
Generate the narrative sections of a tournament analysis report using Claude.
Each section is generated separately so partial failures don't break the report.
"""

import os
import anthropic

MODEL = "claude-opus-4-5"

SYSTEM = """You are an analytical assistant for a competitive youth softball coaching staff.
You write direct, data-driven scouting reports in the style of a professional analytics department.
Tone: factual, unsentimental, coaching-focused. No fluff. No "great job" language.
Your audience is coaches only — never parents or players.
Write in present tense when describing trends, past tense for specific game events.
Do not use bullet points unless explicitly asked. Write in paragraphs.
Keep each section concise — 3–5 sentences per player, 4–6 sentences for team-level summaries.
"""

def _call(client, prompt: str) -> str:
    msg = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def generate_summary(client, data: dict) -> str:
    d = data
    games_str = "\n".join(
        f"  G{g['game_number']}: {g['opponent_name']} — {d['team_name']} {g['team_runs']} – {g['opponent_runs']} ({g['result']})"
        for g in d["games"]
    )
    tb = d["team_batting"]
    top_hitters = sorted(d["batting_stats"], key=lambda x: float(x["ops"].replace(".","0.",1) if x["ops"].startswith(".") else x["ops"]), reverse=True)[:3]
    top_str = ", ".join(f"{h['name']} (.{h['ops']} OPS)" for h in top_hitters)
    pit_str = "; ".join(
        f"{p['name']}: {p['ip']} IP, {p['er']} ER, {p['era']} ERA"
        for p in d["pitching_stats"]
    )
    return _call(client, f"""Write a 4-paragraph Summary section for this tournament analysis report.
Paragraph 1: Overall tournament arc — record ({d['record']}), RS/RA ({d['runs_scored']}/{d['runs_allowed']}), one-sentence characterization of each game.
Paragraph 2: Offensive highlights — team AVG ({tb['avg']}), OBP ({tb['obp']}), OPS ({tb['ops']}), BB/K ({tb['bb_k']}), SB ({tb['sb']} for {tb['sb_pct']}%). Call out top performers: {top_str}.
Paragraph 3: Pitching summary — {pit_str}. Note any within-tournament arc or patterns.
Paragraph 4: Fielding and overall assessment — team FPCT ({d['team_fpct']}), {d['team_errors']} errors ({d['errors_per_game']}/game). Close with a forward-looking coaching priority sentence.

Games:
{games_str}
""")


def generate_hitting_highlights(client, data: dict) -> str:
    above = [b for b in data["batting_stats"] if b.get("trend") in ("↑ Above", "→ Flat") and float(b["avg"].lstrip(".") or "0") > 0]
    if not above:
        return "No players significantly outperformed their spring baseline this tournament."
    player_lines = "\n".join(
        f"#{b['number']} {b['name']}: {b['avg']} AVG / {b['obp']} OBP / {b['ops']} OPS, "
        f"{b['h']} H, {b['rbi']} RBI, {b['r']} R, {b['bb']} BB, {b['k']} K, {b['sb']} SB "
        f"(spring: {b['spring_avg']} AVG / {b['spring_obp']} OBP) — trend: {b['trend']}"
        for b in above
    )
    return _call(client, f"""Write the Hitting Highlights section. One paragraph per player listed below.
Each paragraph: jersey number, name, key stats, what made this performance notable, comparison to spring baseline where relevant.
Do not use headers — just flowing paragraphs. Coaching staff audience only.

Players:
{player_lines}
""")


def generate_hitting_areas(client, data: dict) -> str:
    below = [b for b in data["batting_stats"] if b.get("trend") == "↓ Below" or
             (float(b["avg"].lstrip(".") or "0") < 0.200 and float(b["spring_avg"].lstrip(".") or "0") > 0.300)]
    if not below:
        return "No players showed significant decline from their spring baseline this tournament."
    player_lines = "\n".join(
        f"#{b['number']} {b['name']}: {b['avg']} AVG / {b['obp']} OBP / {b['ops']} OPS, "
        f"{b['h']} H, {b['k']} K in {b['ab']} AB "
        f"(spring: {b['spring_avg']} AVG / {b['spring_obp']} OBP) — trend: {b['trend']}"
        for b in below
    )
    return _call(client, f"""Write the Areas to Develop section. One paragraph per player listed below.
Each paragraph: what the data shows, specific pattern if visible (strikeouts, weak contact, etc.), coaching recommendation.
Be direct — this is a coaching document. Do not soften the analysis.

Players:
{player_lines}
""")


def generate_pitching_narrative(client, pitcher: dict, data: dict) -> str:
    log_str = "\n".join(
        f"  G{g.get('game_number','?')} vs {g['opponent']}: {g['ip']} IP, {g['h']} H, "
        f"{g['r']} R, {g['er']} ER, {g['bb']} BB, {g['k']} K, {g.get('pitches','?')} pitches"
        for g in pitcher["game_log"]
    )
    return _call(client, f"""Write a 2-paragraph pitching analysis for {pitcher['name']} (#{pitcher['number']}).
Paragraph 1: Within-tournament arc — how did performance evolve game to game? Note any patterns in walk rate, pitch count, earned runs.
Paragraph 2: Coaching implications — what does this tournament tell us about how to deploy this pitcher, what to monitor, what to work on?

Tournament totals: {pitcher['ip']} IP, {pitcher['er']} ER, {pitcher['era']} ERA, {pitcher['whip']} WHIP, {pitcher['bb']} BB, {pitcher['k']} K in {pitcher['pitches']} pitches.

Game log:
{log_str}
""")


def generate_fielding_narrative(client, data: dict) -> str:
    top_errors = sorted(data["fielding_stats"], key=lambda x: x["e"], reverse=True)[:4]
    err_str = ", ".join(f"{f['name']} #{f['number']} ({f['position']}, {f['e']} E / {f['tc']} TC, .{f['fpct']} FPCT)" for f in top_errors)
    return _call(client, f"""Write a 2-paragraph fielding analysis.
Paragraph 1: Overall team fielding — {data['team_errors']} errors ({data['errors_per_game']}/game), team FPCT {data['team_fpct']}. Call out where errors concentrated: {err_str}.
Paragraph 2: Catching — Jay Garcia's passed ball rate and stolen bases allowed (reference data below). Note any improvement trend vs. prior tournaments if visible.

Catching data: {[f for f in data['fielding_stats'] if f['position'] == 'C']}
""")


def generate_base_running_narrative(client, data: dict) -> str:
    top_sb = sorted(data["batting_stats"], key=lambda x: x["sb"], reverse=True)[:4]
    sb_str = ", ".join(f"{b['name']} ({b['sb']} SB)" for b in top_sb if b['sb'] > 0)
    return _call(client, f"""Write one paragraph on base running performance.
Team went {data['sb_total']}-for-{data['sb_total'] + data['cs_total']} on stolen bases ({data['sb_pct']}% success rate).
Top contributors: {sb_str}.
Note what this says about team speed/aggressiveness, and any coaching comment on reads or timing.
""")


def generate_priority_areas(client, data: dict) -> str:
    errors_by_pos = {}
    for f in data["fielding_stats"]:
        pos = f["position"]
        errors_by_pos[pos] = errors_by_pos.get(pos, 0) + f["e"]
    top_pos_errors = sorted(errors_by_pos.items(), key=lambda x: x[1], reverse=True)[:3]

    below_hitters = [b["name"] for b in data["batting_stats"] if b.get("trend") == "↓ Below"]
    pit_summary = "; ".join(f"{p['name']}: {p['era']} ERA, {p['whip']} WHIP" for p in data["pitching_stats"])

    return _call(client, f"""Generate a numbered list of exactly 5 coaching priority areas based on this tournament data.
Format: each item is a short bold label followed by a 2-3 sentence explanation.
Example format:
1. [Area label]: explanation...
2. [Area label]: explanation...

Base your priorities on:
- Fielding: top error positions were {top_pos_errors}
- Batting concerns: {below_hitters}
- Pitching: {pit_summary}
- Stolen bases allowed (catching)
- Any positive trend to sustain

Be specific to the data. No generic coaching platitudes.
""")


def generate_full_report(sb, tournament_id: str, anthropic_key: str) -> dict:
    """
    Generate all narrative sections and return a dict of section text + data.
    Raises if ANTHROPIC_API_KEY is not set.
    """
    from .data import get_tournament_data
    data = get_tournament_data(sb, tournament_id)

    client = anthropic.Anthropic(api_key=anthropic_key)

    sections = {}

    # Generate each narrative section (each is independent — partial failures are caught)
    for section_name, fn, args in [
        ("summary",              generate_summary,              (client, data)),
        ("hitting_highlights",   generate_hitting_highlights,   (client, data)),
        ("hitting_areas",        generate_hitting_areas,        (client, data)),
        ("fielding_narrative",   generate_fielding_narrative,   (client, data)),
        ("base_running",         generate_base_running_narrative, (client, data)),
        ("priority_areas",       generate_priority_areas,       (client, data)),
    ]:
        try:
            sections[section_name] = fn(*args)
        except Exception as e:
            sections[section_name] = f"[Generation error: {e}]"

    # Pitcher narratives
    sections["pitcher_narratives"] = {}
    for pitcher in data["pitching_stats"]:
        try:
            sections["pitcher_narratives"][pitcher["player_id"]] = generate_pitching_narrative(client, pitcher, data)
        except Exception as e:
            sections["pitcher_narratives"][pitcher["player_id"]] = f"[Generation error: {e}]"

    return {"data": data, "sections": sections}
