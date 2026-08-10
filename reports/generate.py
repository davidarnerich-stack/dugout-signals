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
    dropped in as literal text — DS-50.

    Claude also doesn't reliably separate intended paragraphs with a full
    blank line — a single \\n between them is standard-markdown-legal but
    gets silently collapsed into one dense block by the parser. Normalize
    every lone newline to a full paragraph break first so any intended
    break renders, regardless of which convention Claude used."""
    text = re.sub(r"\n(?!\n)", "\n\n", text)
    return _markdown_lib.markdown(text)


def _strip_leading_label(text: str, label: str) -> str:
    """
    Backstop for duplicated subsection labels ("Pitching" / "Pitching...").
    The system prompt already tells Claude not to restate a section's own
    title, but that instruction isn't reliably followed — a real generated
    report still showed it happening (with the repeated title rendered bold,
    i.e. Claude wrapped it in markdown `**Pitching**` despite being told not
    to use markdown at all). Since the template already renders `label` as
    its own heading, strip a leading duplicate here instead of trusting the
    prompt alone. Matches an optional markdown-bold wrapper and/or trailing
    colon on the label, at the very start of the text only — but only when
    the label stands alone (followed by a colon, or its own line break), not
    when it's genuinely the first word of a real sentence like "Pitching was
    excellent this game...".
    """
    pattern = rf"^\s*\*{{0,2}}{re.escape(label)}\*{{0,2}}(?:\s*:\s*|\s*\n+)"
    return re.sub(pattern, "", text, count=1, flags=re.I)


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
Refer to the team as "{team_name}" throughout, or as "the team". Do not shorten it, expand it, or
alternate between forms within a report — a reader seeing two different names assumes they are two
different teams.
Never state or imply a player's age. The team plays at {age_level}, which describes an age bracket,
not any individual on the roster — a player's actual age is not something you are given.
Every figure you cite must appear verbatim in the data supplied with the request. Do not compute,
sum, average or estimate anything, and do not describe a category of evidence you were not given —
if swing-and-miss, command or contact quality is not listed, you do not know it.
When you cite a rate stat, name the window it covers in the same sentence ("over the last 3 games",
not "recently" or "in the recent stretch"). When you describe something changing, give both the
starting and ending values, so the reader can judge the size of the change for themselves.
Never begin a section by restating its own title (e.g. don't start the "Hitting" subsection with
the word "Hitting") — the title is already displayed as a heading directly above your text. Start
straight into the analysis.
When referencing innings, follow sportswriting convention: use ordinals like "1st inning" or "3rd
inning", not "the first inning". Say "inning" more often than "frame" — "frame" is fine as an
occasional variation, not the default word. When describing a stretch of the game, name the actual
inning numbers involved (e.g. "innings 3 and 4") rather than vague phase language like "the middle
innings".
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


def _ordinal(label: str) -> str:
    """'3' -> '3rd'. Innings read as ordinals in sportswriting, and the
    grounding block should model the convention the prose is asked to use
    rather than contradict it. Non-numeric labels pass through untouched."""
    s = str(label).strip()
    if not s.isdigit():
        return s
    n = int(s)
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _line_score_note(data, opp, us) -> str:
    """
    Inning-by-inning runs, plus the running score after every inning (DS-98).

    The cumulative figures are the point. Handing over a bare list of cells
    left the model to add them up, and it got it wrong: on the 2026-08-05
    report it announced the game "knotted up at 7-7" after the 3rd when the
    line score said 7-8, then invented "TBD's additional run somewhere in the
    sequence" to reconcile its own total against the real final. Arithmetic
    is not the model's job when we can hand it the answer.
    """
    hb = data["header_block"]
    if not hb["has_line_score"]:
        return ""

    innings = hb["line_header"]
    us_run = opp_run = 0
    running = []
    for i, label in enumerate(innings):
        u = us["cells"][i]   if i < len(us["cells"])   else ""
        o = opp["cells"][i]  if i < len(opp["cells"])  else ""
        us_run  += u if isinstance(u, int) else 0
        opp_run += o if isinstance(o, int) else 0
        running.append(f"after the {_ordinal(label)}: "
                       f"{us['short']} {us_run}, {opp['short']} {opp_run}")

    return (f"Inning-by-inning ({', '.join(innings)}): "
            f"{us['short']} {us['cells']}, {opp['short']} {opp['cells']}.\n"
            f"Running score — {'; '.join(running)}.\n"
            "These running totals are already computed. Use them exactly as given for any "
            "statement about the score at a point in the game; do not add up innings yourself.")


def _game_facts_note(data, opp, us) -> str:
    """Compact, factual grounding block for the prompts that don't otherwise
    receive per-play detail (headline, snapshot, focus areas). DS-79: these
    three prompts used to ask for "specific" narrative with nothing specific
    to draw from, which reliably produced confident, fabricated detail (a
    walk and wild pitch that never happened, an inning that scored the
    wrong team). This is the only lever those prompts have to stay grounded."""
    lines = [f"Final: {us['label']} {us['r']} – {opp['r']} {opp['label']} ({data['game']['result']})."]
    line_score = _line_score_note(data, opp, us)
    if line_score:
        lines.append(line_score)
    t = _team_totals_note(data, us, opp)
    if t:
        lines.append(t)
    return "\n".join(lines)


def _team_totals_note(data, us, opp) -> str:
    """
    Team totals, summed here rather than by the model (DS-98).

    Per-batter lines were already in the prompts, and the model still
    reported "8 strikeouts across 11 batters" when the correct figure was 9 —
    twice, in two separate reviews. Any total the narrative might cite is
    computed here so there is nothing left to get wrong.
    """
    bats = data.get("batting_stats") or []
    pits = data.get("pitching_stats") or []
    if not bats and not pits:
        return ""
    team_bb = sum(b["bb"] for b in bats)
    team_k  = sum(b["k"]  for b in bats)
    opp_bb  = sum(p["bb"] for p in pits)
    opp_k   = sum(p["k"]  for p in pits)
    out = [
        f"{us['short']} totals: {us['h']} H, {us['e']} E, {team_bb} BB drawn, "
        f"{team_k} strikeouts by our hitters across {len(bats)} batters.",
        f"{opp['short']} totals: {opp['h']} H, {opp['e']} E, {opp_bb} BB issued "
        f"and {opp_k} strikeouts recorded by our pitching.",
    ]
    sb_ct, cs_ct = data.get("sb_count"), data.get("cs_count")
    if sb_ct is not None:
        pct = data.get("sb_pct")
        rate = f", a {pct}% success rate" if pct is not None else ""
        line = f"Baserunning: {sb_ct} stolen bases, {cs_ct} caught stealing{rate}."
        # Name the runner when an event has exactly one owner — a bare "1
        # caught stealing" left the coach asking who it was.
        singles = [s["name"] for s in (data.get("stealers") or []) if s.get("cs") == 1]
        if cs_ct == 1 and len(singles) == 1:
            line += f" The caught stealing was {singles[0]}."
        out.append(line)
    out.append("These totals are already summed. Cite them as given — do not add up "
               "the per-player lines yourself.")
    return "\n".join(out)


_NO_INVENTION_NOTE = ("Ground every specific claim — which inning, which team, what happened — "
    "strictly in the facts above. Do not invent a scoring mechanism (a walk, a wild pitch, a "
    "hit-by-pitch, an error, etc.) unless the numbers above actually support it. If you don't have "
    "enough detail to name a specific play, describe the outcome at the level the data supports "
    "instead (e.g. \"pulled ahead in the 5th\") rather than inventing how it happened.")


def generate_game_headline(client, system, data, opp, us) -> str:
    return _single_game_call(client, system, f"""Write ONE punchy, specific sentence summarizing the
defining story of this game — this is a headline, not a summary. It will be displayed on its own,
directly under the box score.

{_game_facts_note(data, opp, us)}

{_NO_INVENTION_NOTE}
""", max_tokens=100)


def generate_game_snapshot(client, system, data, opp, us) -> str:
    return _single_game_call(client, system, f"""Write a 3-4 sentence "Game Snapshot" — the story of
the game and final score. This is the reader's first narrative context, right after the headline.

{_game_facts_note(data, opp, us)}
{_season_context_note(data['game_number_in_season'])}

{_NO_INVENTION_NOTE}
""")


def generate_how_it_happened(client, system, data, opp, us) -> list:
    line_note = _line_score_note(data, opp, us)
    pbp_note = "" if data["has_pbp"] else ("\nPlay-by-play was not uploaded for this game — you only "
        "have final box-score numbers, not sequence detail. Write the recap from the score/stats you "
        "have; do not invent specific in-game sequences you don't have data for.")

    text = _single_game_call(client, system, f"""Write "How It Happened" — a condensed recap
structured in exactly 3 paragraphs: Early, Middle, Late. These labels are section headings only —
each paragraph should reference the specific inning numbers it covers (e.g. "In the 4th, ...") using
the inning-by-inning data below, not the words "early/middle/late" themselves. Each paragraph calls
out what happened and why it mattered — conversational prose, like a beat reporter recap, not a
bullet list.

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
If there's enough to cover (multiple contributors, contrasting performances), split it into 2-3
short paragraphs separated by a blank line rather than one dense block — a single short paragraph
is fine when there isn't much to say.

Batting lines:
{bats}
""")


def generate_baserunning(client, system, data, us) -> str:
    return _single_game_call(client, system, f"""Write the "Baserunning" subsection — stolen bases,
smart reads, aggressive advancement, or missed opportunities.

Stolen bases: {data['sb_count']}, caught stealing: {data['cs_count']}.
""")


def _season_context_block(data: dict, card_key: str, labels: dict) -> str:
    """
    Season context for one report section, drawn from the signal card that
    matches it (DS-102).

    Returns "" — meaning the section says nothing about the season — in three
    cases, each for a different reason:

      * fewer than three games, so there is no baseline worth comparing to;
      * no card for this section;
      * the card's state says the metric could not be computed.

    Silence is the right output for all three. A section with no baseline
    previously filled the gap with generic age-level commentary ("at 9U, X is
    hard"), which reads as insight while asserting nothing.
    """
    if data.get("game_number_in_season", 0) < 3:
        return ""
    card = (data.get("signal_cards") or {}).get(card_key)
    if not card or card.get("state") not in ("complete", "genuine_zero"):
        return ""

    facts = card.get("facts") or {}
    parts = [labels[k].format(v) for k, v in facts.items()
             if k in labels and v is not None]
    if not parts:
        return ""

    games = data["game_number_in_season"]
    return (f"\nSeason to date across {games} games: " + ", ".join(parts) + ".\n"
            "Use this only to say whether this game was typical for this team. "
            "State both the game figure and the season figure when you compare "
            "them, and name the window. If the two are close, say so plainly "
            "rather than manufacturing a trend.")


def _pitcher_line(p: dict) -> str:
    """One pitcher's line for the prompt (DS-103).

    Command and contact-quality figures are appended only where GameChanger
    actually recorded them. Omitting a missing metric outright — rather than
    printing "n/a" — is deliberate: an absent figure cannot be misread as a
    measurement, and the prompt forbids claims about anything not listed.
    """
    parts = [f"{p['ip']} IP", f"{p['k']} K", f"{p['bb']} BB",
             f"{p['er']} ER", f"{p['era']} ERA"]
    optional = [
        ("strike_pct", "{} % strikes"),
        ("fps_pct",    "{} % first-pitch strikes"),
        ("sm_pct",     "{} % swing-and-miss"),
        ("weak_pct",   "{} % of batted balls weakly hit"),
        ("hhb_pct",    "{} % of batted balls hit hard"),
        ("p_per_ip",   "{} pitches per inning"),
    ]
    for key, fmt in optional:
        val = p.get(key)
        if val is not None:
            parts.append(fmt.format(val))
    return f"{p['name']} #{p['number']}: " + ", ".join(parts)


def generate_pitching(client, system, data) -> str:
    pitchers = ("; ".join(_pitcher_line(p) for p in data["pitching_stats"])
                or "No pitching data recorded.")
    return _single_game_call(client, system, f"""Write the "Pitching" subsection — pitching
performance for this game, with individual pitcher contributions woven into the prose.
If there's enough to cover (multiple pitchers, contrasting outings), split it into 2-3 short
paragraphs separated by a blank line rather than one dense block — a single short paragraph is fine
when there isn't much to say.

Pitching lines:
{pitchers}

Beyond the raw line, each pitcher may carry command and contact-quality figures. Lead with whichever
one or two actually characterise the outing — a high swing-and-miss rate, a low first-pitch-strike
rate, a batted-ball profile that is nearly all weak contact — rather than reciting every figure
listed. Two well-chosen numbers say more than six.

Cite only figures that appear above. If a metric is not listed for a pitcher it was not recorded,
so make no claim about it — in particular, do not infer swing-and-miss, command or contact quality
from the strikeout and walk counts.
{_season_context_block(data, "pitching", {
    "k_minus_bb_pct": "K-BB% {}",
    "s_pct":          "{} % strikes",
    "bb_pct":         "BB% {}",
})}
""")


def _fielding_facts(data, us) -> str:
    """
    Everything the Fielding section can legitimately draw on.

    It used to receive the team error count and the players who made errors,
    and nothing else — so it wrote that "the scorebook does not detail other
    defensive sequences", which is false. The official fielding line was
    already stored, and play-by-play records who each ball was hit to.

    Play-by-play is optional by design: plenty of coaches never paste it.
    When it is absent this returns the CSV half alone and says nothing about
    plays, rather than asserting the detail does not exist.
    """
    lines = []

    tf = data.get("team_fielding") or {}
    tc, po, a = tf.get("total_chances"), tf.get("putouts"), tf.get("assists")
    fpct = tf.get("fielding_percentage")
    def _plural(n, word):
        return f"{n} {word}" if n == 1 else f"{n} {word}s"

    if tc is not None:
        # Fielding percentage reads as .875 in a box score, not 0.875.
        fpct_str = str(fpct).lstrip("0") if fpct is not None else "—"
        lines.append(
            f"Team fielding: {_plural(tc, 'total chance')}, {po} putouts, "
            f"{a} assists, {_plural(us['e'], 'error')}, "
            f"fielding percentage {fpct_str}."
        )
    else:
        lines.append(f"Team {_plural(us['e'], 'error')}.")

    # Everyone who handled a chance, not only those charged with an error —
    # a clean game is still a game someone fielded.
    handled = [f for f in data["fielding_stats"] if (f.get("tc") or 0) > 0]
    if handled:
        lines.append("By player: " + "; ".join(
            f"{f['name']} ({f['position']}) {f['tc']} TC, {f['e']} E" for f in handled))

    plays = data.get("fielding_plays")
    if plays:
        where = ", ".join(f"{loc} ({n})" for loc, n in plays["by_fielder"])
        lines.append(f"Balls put in play against this defence: {plays['balls_in_play']}. "
                     f"Where they went: {where}.")
    else:
        lines.append("No play-by-play was provided for this game, so there is no "
                     "ball-by-ball detail. Do not remark on its absence — write "
                     "what the fielding numbers above support and stop there.")
    return "\n".join(lines)


def generate_fielding(client, system, data, us) -> str:
    return _single_game_call(client, system, f"""Write the "Fielding" subsection — errors, key plays
made or missed, defensive impact on the game.
If there's enough to cover, split it into 2-3 short paragraphs separated by a blank line rather
than one dense block — a single short paragraph is fine when there isn't much to say.

{_fielding_facts(data, us)}
{_season_context_block(data, "fielding_conversion", {
    "def_eff":               "defensive efficiency {}",
    "errors":                "{} errors",
    "runs_allowed_per_game": "{} runs allowed per game",
})}
An error is the scorekeeper's judgement that the play should have been made — that is what
distinguishes it from a hit. Do not speculate about whether a charged error was a hard chance.
""")


def generate_catching(client, system, data) -> str:
    """DS-75: counting stats only, per catcher — AC #6 (single-game grain is
    too small a sample for a per-catcher rate to mean anything). Team-level
    rates in catching_summary are fine to reference for context but must
    not be attributed to catchers by number of PB or SB allowed alone; the
    prompt carries AC #8's caveat explicitly, same discipline as narrative.py
    elsewhere in this codebase — grounded facts plus an explicit instruction
    not to overreach beyond them."""
    cs = data["catching_summary"]
    catchers = "; ".join(
        f"{c['name']} #{c['number']}: {c['innings']} innings caught, {c['pb']} PB, "
        f"{c['sb_allowed']} SB allowed, {c['cs']} CS" + (f", {c['pik']} PIK" if c["pik"] else "")
        for c in data["catching_stats"]
    ) or "No catcher recorded innings this game."

    headline_line = ""
    if cs["headline_metric"] == "cs_pct" and cs["cs_pct"] is not None:
        headline_line = f"Team caught-stealing rate this game: {cs['cs_pct']:.1f}% ({cs['cs']} of {cs['attempts']} attempts)."
    elif cs["pb_per_inning"] is not None:
        headline_line = f"Team passed balls per inning this game: {cs['pb_per_inning']:.2f} ({cs['passed_balls']} PB in {cs['innings_caught']:.1f} innings)."

    return _single_game_call(client, system, f"""Write the "Catching" subsection — catcher
performance for this game, using ONLY the counting stats below (passed balls, caught stealing,
innings caught) — never compute or state a per-catcher rate or percentage, the sample within one
game is too small for that to mean anything (e.g. never say a catcher "threw out X%").
A catcher with zero passed balls had a clean game behind the plate — state that as a positive
result, not as an absence of data.
If a runner was caught stealing or advanced on a passed ball, remember that pitcher delivery time
to the plate is a material factor in both outcomes — do not credit or fault the catcher alone for
either.
If there's enough to cover, split it into 2-3 short paragraphs separated by a blank line rather
than one dense block — a single short paragraph is fine when there isn't much to say, and a game
with no innings caught should say so plainly rather than being padded out.

Catching lines: {catchers}
{headline_line}
""")


def generate_signals(client, system, data) -> list:
    # Both windows, explicitly labelled and paired (DS-98). Previously the two
    # figures went in unlabelled and came out as "climbing nearly 80 points
    # above the season mark" and "the jump to .618 OBP in recent games" —
    # numbers with no stated baseline, which a reader cannot judge. Each line
    # below carries its own comparison so a signal can quote one and be
    # complete.
    std, l3 = data["season_to_date"], data["last3"]
    has_l3 = data["game_number_in_season"] >= 3
    if has_l3:
        windows = "\n".join([
            f"AVG — last 3 games {l3['avg']}, season to date {std['avg']} ({std['games']} games).",
            f"OBP — last 3 games {l3['obp']}, season to date {std['obp']}.",
            f"OPS — last 3 games {l3['ops']}, season to date {std['ops']}.",
        ])
        window_rule = (
            "Each line above pairs a recent window with the season baseline. When you cite one, "
            "give both figures and name the window — 'over the last 3 games (.317) against .253 "
            "for the season', never '80 points above the season mark' or 'in the recent stretch'. "
            "Say whether the gap is large enough to mean anything; a reader cannot judge a number "
            "with nothing to measure it against. Do not spend two separate signals half-describing "
            "the same comparison."
        )
    else:
        windows = (f"Season to date: {std['avg']} AVG / {std['obp']} OBP / {std['ops']} OPS "
                   f"across {std['games']} games.")
        window_rule = ("There are fewer than 3 games, so there is no recent window to compare "
                       "against. State figures plainly and make no claim about a trend.")

    text = _single_game_call(client, system, f"""Write 3-5 "Team Signals" — pattern observations
phrased as developmental observations, not verdicts. Age/sport calibrated per the system prompt.

{windows}

{window_rule}

Return each signal as its own line, no numbering, no bullets — just one sentence-or-two observation
per line, 3 to 5 lines total.
""", max_tokens=600)
    return [_to_html(line.strip()) for line in text.splitlines() if line.strip()]


def generate_focus_areas(client, system, data) -> list:
    bats = "; ".join(f"{b['name']}: {b['h']}-for-{b['ab']}, {b['bb']} BB, {b['k']} K"
                      for b in data["batting_stats"]) or "No batting data recorded."
    pitchers = "; ".join(f"{p['name']}: {p['ip']} IP, {p['k']} K, {p['bb']} BB, {p['er']} ER"
                          for p in data["pitching_stats"]) or "No pitching data recorded."
    errs = "; ".join(f"{f['name']} ({f['position']}): {f['e']} E / {f['tc']} TC"
                      for f in data["fielding_stats"] if f["e"] > 0) or "no charged errors"
    g = data["game"]
    text = _single_game_call(client, system, f"""Generate exactly 3 ranked "Before Next Practice"
focus areas based on this game's data. Each needs a short title, a 1-2 sentence rationale grounded in
what happened this game, and one concrete drill cue a coach could run at the next practice.

Final: {g.get('result')} ({g.get('team_runs')}-{g.get('opponent_runs')}).
Batting lines: {bats}
Pitching lines: {pitchers}
Fielding errors: {errs}

Every rationale must cite only numbers or events shown above — do not invent a stat, a run total, or
an in-game sequence (e.g. a "rally") that isn't directly supported by this data.

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
    us, opp = hb["us"], hb["opponent"]

    # Sections are generated concurrently rather than one after another.
    #
    # Eleven sequential Claude calls sat inside the upload request, under
    # gunicorn's 120s timeout. That was already close to the limit, and the
    # richer prompts from DS-98/DS-102/DS-103 pushed it over — the worker was
    # killed mid-request and the browser received an HTML error page instead
    # of JSON. Nothing in the application raised: every section here is
    # already wrapped, so an ordinary failure returns a report with an error
    # note rather than killing the request. Only the process dying produces
    # HTML, which is what pointed at the timeout.
    #
    # Every section takes the same `data` and none reads another's output, so
    # concurrency needs no ordering. Wall time becomes roughly the slowest
    # single call. signals/narrative.py already does exactly this, for the
    # same reason and with the same independent-failure contract.
    import concurrent.futures

    def _section(fn, *args):
        return fn(client, system, data, *args)

    jobs = {
        "headline":        (generate_game_headline,  (opp, us)),
        "snapshot":        (generate_game_snapshot,  (opp, us)),
        "how_it_happened": (generate_how_it_happened,(opp, us)),
        "hitting":         (generate_hitting,        (us,)),
        "pitching":        (generate_pitching,       ()),
        "fielding":        (generate_fielding,       (us,)),
        "catching":        (generate_catching,       ()),
        "signals":         (generate_signals,        ()),
        "focus_areas":     (generate_focus_areas,    ()),
    }
    # Baserunning is only generated when play-by-play exists; without it the
    # template renders fixed fallback copy and no model call is made.
    if data["has_pbp"]:
        jobs["baserunning"] = (generate_baserunning, (us,))

    raw, errors = {}, {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {pool.submit(_section, fn, *args): key for key, (fn, args) in jobs.items()}
        for fut in concurrent.futures.as_completed(futures):
            key = futures[fut]
            try:
                raw[key] = fut.result()
            except Exception as e:
                errors[key] = e

    # Post-processing stays here rather than inside the workers so the
    # threads do nothing but wait on the API.
    sections = {}
    headline = raw.get("headline") or f"[Generation error: {errors.get('headline')}]"

    for key, label in (("hitting", "Hitting"), ("baserunning", "Baserunning"),
                       ("pitching", "Pitching"), ("fielding", "Fielding"),
                       ("catching", "Catching")):
        if key == "baserunning" and not data["has_pbp"]:
            sections[key] = None      # fixed fallback copy in the template
        elif key in errors:
            sections[key] = f"<p>[Generation error: {errors[key]}]</p>"
        else:
            sections[key] = _to_html(_strip_leading_label(raw[key], label))

    sections["snapshot"] = (f"<p>[Generation error: {errors['snapshot']}]</p>"
                            if "snapshot" in errors else _to_html(raw["snapshot"]))
    # None here is meaningful — the template shows the section-failed card.
    sections["how_it_happened"] = None if "how_it_happened" in errors else raw["how_it_happened"]
    sections["signals"] = ([f"[Generation error: {errors['signals']}]"]
                           if "signals" in errors else raw["signals"])
    sections["focus_areas"] = [] if "focus_areas" in errors else raw["focus_areas"]

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
