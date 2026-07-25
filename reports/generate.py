"""
Generate the narrative sections of a tournament analysis report using Claude.
Each section is generated separately so partial failures don't break the report.
"""

import os
import re
import anthropic
import markdown as _markdown_lib

MODEL = "claude-opus-4-5"


def _to_html(text: str) -> str:
    """Render Claude's output through a real markdown parser. Claude isn't
    reliably forbidden from using markdown syntax (headers, bold, numbered
    lists) in its prose, so section text is rendered as HTML instead of
    dropped in as literal text — DS-50."""
    return _markdown_lib.markdown(text)


def _priority_items(text: str) -> list:
    """Split the priority-areas numbered list into individual items, each
    markdown-rendered (so a '**Label**' prefix becomes real <strong>, not a
    literal '**' — this is what report.html's priority-item cards render)."""
    items = []
    for line in text.strip().splitlines():
        line = line.strip()
        m = re.match(r"^\d+\.\s*(.+)$", line)
        if m:
            items.append(_markdown_lib.markdown(m.group(1)))
    return items

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


# ── DS-11: Single Game Analysis Report ──────────────────────────────────────
SINGLE_GAME_SYSTEM_TEMPLATE = """You are an analytical assistant for a youth {sport} coaching staff.
Team: {team_name} ({age_level}, {governing_body}).
You write direct, data-driven game analysis in the style of a professional analytics department,
but the audience is a VOLUNTEER coach working with YOUTH players — observations must be
developmental and encouraging, never evaluative or harsh. Calibrate language to age-appropriate
baselines: a strikeout rate that would be alarming at a higher level (e.g. 50% at 8U) is normal
at this age and should not be treated as a concern.
Tone: factual and specific, not generic praise. Write in present tense for trends, past tense for
specific game events. Write in flowing paragraphs — like a beat reporter recap, not a bullet list —
unless a section explicitly asks for a different format. Do not use markdown syntax (no #, **, etc.)
— output will be inserted as plain text.
Your audience is coaches only — never parents or players.
"""


def _single_game_call(client, system: str, prompt: str, max_tokens: int = 700) -> str:
    msg = client.messages.create(
        model=MODEL, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def _season_context_note(game_number: int) -> str:
    if game_number == 1:
        return ("This is the team's FIRST game in Dugout Signals. Set an explicit baseline — "
                "say this is the first game and results here will be what future games are compared "
                "against. Do not make any trend comparisons; there is nothing yet to compare to.")
    if game_number == 2:
        return ("This is the team's SECOND game. You may make LIGHT comparisons to the first game, "
                "but explicitly acknowledge the sample size is still small (two games is not a trend "
                "yet) — keep any comparison tentative.")
    return ("This is game {n} of the season. Use full intra-season trending — compare the last 3 "
            "games' aggregate performance against the season-to-date aggregate, and call out "
            "whether recent performance is ahead of, behind, or in line with the season average."
            ).format(n=game_number)


def generate_game_headline(client, system, data, opp, us) -> str:
    return _single_game_call(client, system, f"""Write ONE punchy, specific sentence summarizing the
defining story of this game — this is a headline, not a summary. It will be displayed on its own,
directly under the box score.

Final: {us['label']} {us['r']} – {opp['r']} {opp['label']} ({data['game']['result']}).
""", max_tokens=100)


def generate_game_snapshot(client, system, data, opp, us) -> str:
    return _single_game_call(client, system, f"""Write a 3-4 sentence "Game Snapshot" — the story of
the game and final score. This is the reader's first narrative context, right after the headline.

Final: {us['label']} {us['r']} – {opp['r']} {opp['label']} ({data['game']['result']}).
{_season_context_note(data['game_number_in_season'])}
""")


def generate_how_it_happened(client, system, data, opp, us) -> list:
    hb = data["header_block"]
    line_note = ""
    if hb["has_line_score"]:
        line_note = (f"Inning-by-inning ({', '.join(hb['line_header'])}): "
                     f"{us['short']} {hb['home']['cells']}, {opp['short']} {hb['visitor']['cells']}.")
    pbp_note = "" if data["has_pbp"] else ("\nPlay-by-play was not uploaded for this game — you only "
        "have final box-score numbers, not sequence detail. Write the recap from the score/stats you "
        "have; do not invent specific in-game sequences you don't have data for.")

    text = _single_game_call(client, system, f"""Write "How It Happened" — a condensed recap
structured in exactly 3 paragraphs: Early, Middle, Late. Each paragraph calls out what happened and
why it mattered — conversational prose, like a beat reporter recap, not a bullet list.

Final: {us['label']} {us['r']} – {opp['r']} {opp['label']} ({data['game']['result']}).
{line_note}
{_season_context_note(data['game_number_in_season'])}{pbp_note}

Return exactly 3 paragraphs separated by a blank line, in order: Early, Middle, Late. No headers,
no labels — just the 3 paragraphs.
""", max_tokens=900)

    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    labels = ["Early", "Middle", "Late"]
    recap = [{"label": labels[i] if i < 3 else f"Part {i+1}", "text": _to_html(p)}
             for i, p in enumerate(paras[:3])]
    if not data["has_pbp"] and recap:
        recap[0]["text"] += "<p><em>Add play-by-play for richer game flow detail.</em></p>"
    return recap


def generate_hitting(client, system, data, us) -> str:
    bats = "; ".join(f"{b['name']} #{b['number']}: {b['h']}-for-{b['ab']}, {b['rbi']} RBI, {b['r']} R, "
                      f"{b['bb']} BB, {b['k']} K" for b in data["batting_stats"]) or "No batting data recorded."
    return _single_game_call(client, system, f"""Write the "Hitting" subsection — team hitting
performance for this game, with individual contributions woven naturally into the prose (not listed
separately). Team totals: {us['h']} hits, {us['r']} runs.

Batting lines:
{bats}
""")


def generate_baserunning(client, system, data, us) -> str:
    return _single_game_call(client, system, f"""Write the "Baserunning" subsection — stolen bases,
smart reads, aggressive advancement, or missed opportunities.

Stolen bases: {data['sb_count']}, caught stealing: {data['cs_count']}.
""")


def generate_pitching(client, system, data) -> str:
    pitchers = "; ".join(f"{p['name']} #{p['number']}: {p['ip']} IP, {p['k']} K, {p['bb']} BB, "
                          f"{p['er']} ER, {p['era']} ERA" for p in data["pitching_stats"]) or "No pitching data recorded."
    return _single_game_call(client, system, f"""Write the "Pitching" subsection — pitching
performance for this game, with individual pitcher contributions (IP, K, BB, ERA) woven into the
prose.

Pitching lines:
{pitchers}
""")


def generate_fielding(client, system, data, us) -> str:
    errs = "; ".join(f"{f['name']} #{f['number']} ({f['position']}): {f['e']} E / {f['tc']} TC"
                      for f in data["fielding_stats"] if f["e"] > 0) or "no charged errors"
    return _single_game_call(client, system, f"""Write the "Fielding" subsection — errors, key plays
made or missed, defensive impact on the game.

Team errors: {us['e']}. Errors by player: {errs}.
""")


def generate_signals(client, system, data) -> list:
    text = _single_game_call(client, system, f"""Write 3-5 "Team Signals" — pattern observations
phrased as developmental observations, not verdicts. Age/sport calibrated per the system prompt.

Season-to-date team line: {data['season_to_date']['avg']} AVG / {data['season_to_date']['obp']} OBP
/ {data['season_to_date']['ops']} OPS across {data['season_to_date']['games']} games.
{"Last 3 games: " + data['last3']['avg'] + " AVG / " + data['last3']['obp'] + " OBP / " + data['last3']['ops'] + " OPS." if data['game_number_in_season'] >= 3 else ""}

Return each signal as its own line, no numbering, no bullets — just one sentence-or-two observation
per line, 3 to 5 lines total.
""", max_tokens=600)
    return [_to_html(line.strip()) for line in text.splitlines() if line.strip()]


def generate_focus_areas(client, system, data) -> list:
    text = _single_game_call(client, system, f"""Generate exactly 3 ranked "Before Next Practice"
focus areas based on this game's data. Each needs a short title, a 1-2 sentence rationale grounded in
what happened this game, and one concrete drill cue a coach could run at the next practice.

Format each item on its own line EXACTLY as: TITLE | RATIONALE | DRILL CUE
Do not add numbering, headers, or any other text — exactly 3 lines, pipe-delimited as shown.
""", max_tokens=500)
    focus = []
    for i, line in enumerate(text.splitlines()):
        parts = [p.strip() for p in line.split("|")]
        if len(parts) == 3:
            focus.append({"rank": str(i + 1), "title": parts[0], "rationale": parts[1], "cue": parts[2]})
    return focus[:3]


def generate_single_game_report(sb, game_id: str, anthropic_key: str, team_id: str,
                                 team_name: str, sport: str, age_level: str, governing_body: str) -> dict:
    """
    Generate a single-game report: structured header block (no AI) + AI narrative
    sections, mirroring generate_full_report's independent-section-failure pattern.
    """
    from .data import get_single_game_data
    data = get_single_game_data(sb, game_id, team_id, team_name)

    system = SINGLE_GAME_SYSTEM_TEMPLATE.format(
        sport=sport or "baseball", team_name=team_name,
        age_level=age_level or "youth", governing_body=governing_body or "youth league",
    )
    client = anthropic.Anthropic(api_key=anthropic_key)

    hb = data["header_block"]
    us, opp = hb["home"], hb["visitor"]

    sections = {}
    try:
        headline = generate_game_headline(client, system, data, opp, us)
    except Exception as e:
        headline = f"[Generation error: {e}]"

    try:
        sections["snapshot"] = _to_html(generate_game_snapshot(client, system, data, opp, us))
    except Exception as e:
        sections["snapshot"] = f"<p>[Generation error: {e}]</p>"

    try:
        sections["how_it_happened"] = generate_how_it_happened(client, system, data, opp, us)
    except Exception as e:
        sections["how_it_happened"] = None  # template shows the section-failed fallback

    try:
        sections["hitting"] = _to_html(generate_hitting(client, system, data, us))
    except Exception as e:
        sections["hitting"] = f"<p>[Generation error: {e}]</p>"

    if data["has_pbp"]:
        try:
            sections["baserunning"] = _to_html(generate_baserunning(client, system, data, us))
        except Exception as e:
            sections["baserunning"] = f"<p>[Generation error: {e}]</p>"
    else:
        sections["baserunning"] = None  # template renders the fixed fallback copy — not an AI call

    try:
        sections["pitching"] = _to_html(generate_pitching(client, system, data))
    except Exception as e:
        sections["pitching"] = f"<p>[Generation error: {e}]</p>"

    try:
        sections["fielding"] = _to_html(generate_fielding(client, system, data, us))
    except Exception as e:
        sections["fielding"] = f"<p>[Generation error: {e}]</p>"

    try:
        sections["signals"] = generate_signals(client, system, data)
    except Exception as e:
        sections["signals"] = [f"[Generation error: {e}]"]

    try:
        sections["focus_areas"] = generate_focus_areas(client, system, data)
    except Exception as e:
        sections["focus_areas"] = []

    return {"data": data, "sections": sections, "headline": headline}


def generate_full_report(sb, tournament_id: str, anthropic_key: str, team_id: str, team_name: str) -> dict:
    """
    Generate all narrative sections and return a dict of section text + data.
    Raises if ANTHROPIC_API_KEY is not set.
    """
    from .data import get_tournament_data
    data = get_tournament_data(sb, tournament_id, team_id, team_name)

    client = anthropic.Anthropic(api_key=anthropic_key)

    sections = {}

    # Generate each narrative section (each is independent — partial failures are caught).
    # priority_areas is rendered as a list of items (not one HTML blob) so
    # report.html's numbered priority-item cards keep working.
    for section_name, fn, args in [
        ("summary",              generate_summary,              (client, data)),
        ("hitting_highlights",   generate_hitting_highlights,   (client, data)),
        ("hitting_areas",        generate_hitting_areas,        (client, data)),
        ("fielding_narrative",   generate_fielding_narrative,   (client, data)),
        ("base_running",         generate_base_running_narrative, (client, data)),
    ]:
        try:
            sections[section_name] = _to_html(fn(*args))
        except Exception as e:
            sections[section_name] = f"<p>[Generation error: {e}]</p>"

    try:
        sections["priority_areas"] = _priority_items(generate_priority_areas(client, data))
    except Exception as e:
        sections["priority_areas"] = [f"[Generation error: {e}]"]

    # Pitcher narratives
    sections["pitcher_narratives"] = {}
    for pitcher in data["pitching_stats"]:
        try:
            sections["pitcher_narratives"][pitcher["player_id"]] = _to_html(
                generate_pitching_narrative(client, pitcher, data))
        except Exception as e:
            sections["pitcher_narratives"][pitcher["player_id"]] = f"<p>[Generation error: {e}]</p>"

    return {"data": data, "sections": sections}
