"""
DS-67: narrative generation for the team signal cards.

"Card narrative text is model-generated; the six cards themselves are
code-defined. The model writes copy, it does not decide what appears"
(DS-67 requirements). team_signals.py computes what to show and with what
numbers; this module writes the headline and interpretation sentence for
each, strictly grounded in those numbers.

Same grounding discipline DS-79 exists to enforce for game reports: every
prompt here gets ONLY the real computed facts, plus an explicit instruction
not to invent anything beyond them. A signal card inventing a cause the way
DS-79's original headline invented a walk and a wild pitch would be worse
here, not better — these run every week, unattended, with no human review
before a coach sees them.

DS-69 (signal explanation view) adds a third line, WHY, generated in the
same call as HEADLINE/INTERPRETATION — no extra Claude round trip. DS-69's
"One thing to try" section reuses INTERPRETATION as-is: the existing
system prompt already requires interpretation lines to suggest rather than
instruct ("worth a look higher in the order", never "move her up"), which
is exactly what that section needs — writing a second, separate
action-suggestion field would be redundant copy grounded in the same facts.
WHY is genuinely new content: the dashboard card's headline+interpretation
never explained a mechanism, and the explanation view's "why it might be
happening" section needs one. DS-69's "What we saw" section needs no model
call at all — it reads team_signals.card_metric_rows(card), the same
code-generated rows already rendered on the card.
"""

import anthropic

MODEL = "claude-opus-4-5"

SIGNAL_SYSTEM = """You are writing one-line narrative copy for a youth {sport} coaching dashboard.
Team: {team_name} ({age_level}).
Audience is a volunteer coach, not a player or parent. Tone is developmental, never evaluative —
a strikeout rate or run gap that would read as alarming at a higher level is often normal at this
age. Positive and problem signals get the exact same flat, factual treatment: no triumphant framing
for good numbers, no alarmed framing for bad ones, no colour-coded judgment in the words themselves.
Interpretation lines SUGGEST, they never INSTRUCT — "worth a look higher in the order", never
"move her up". Ground every specific number or claim strictly in the facts given in the prompt.
Never invent a cause, a mechanism, or a number not explicitly provided — if you don't have enough
detail to explain WHY something happened, describe WHAT the data shows instead of guessing why.
You will also write a WHY line — one sentence of age-appropriate interpretation, normalising where
normal (e.g. "at this age that pattern usually tracks arm fatigue more than mechanics"). It may
name a plausible, commonly-understood explanation for the pattern, but only in hedged language
("often", "usually", "one common reason is") — never state a specific cause as fact when the facts
given don't establish it.
Do not use markdown syntax. Output will be inserted as plain text.
"""

_NO_INVENTION_NOTE = ("Ground every word in the facts above. Do not invent a cause, an event, or "
    "a number that isn't explicitly given. If the facts don't explain why something is happening, "
    "describe what the numbers show rather than guessing at a reason.")


def _call(client, system, prompt, max_tokens=220):
    msg = client.messages.create(
        model=MODEL, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def _parse_narrative_lines(text):
    """Expects three lines: HEADLINE: ... / INTERPRETATION: ... / WHY: ..."""
    headline, interpretation, why = None, None, None
    for line in text.splitlines():
        line = line.strip()
        if line.upper().startswith("HEADLINE:"):
            headline = line.split(":", 1)[1].strip()
        elif line.upper().startswith("INTERPRETATION:"):
            interpretation = line.split(":", 1)[1].strip()
        elif line.upper().startswith("WHY:"):
            why = line.split(":", 1)[1].strip()
    return headline, interpretation, why


_RESPONSE_FORMAT = """Return exactly three lines, in this format, no other text:
HEADLINE: <one sentence, plain-language claim, never a bare stat>
INTERPRETATION: <one sentence, suggests rather than instructs>
WHY: <one sentence, age-appropriate interpretation of why this might be happening>"""


def _genuine_zero_prompt(claim_hint):
    return (f"This is a genuine-zero state — the team truly had none, not missing data. "
            f"Write the headline as a plain factual claim (e.g. \"{claim_hint}\"), not a status "
            f"report. Interpretation can be brief context or omitted-sounding (still one sentence).")


def _missing_data_prompt(games_affected, games_total):
    return (f"Contact-quality tagging was missing for {games_affected} of {games_total} games in "
            f"this window. Write the headline as a STATUS, not a claim — name the affected game "
            f"count (e.g. \"Hard-hit rate unavailable for {games_affected} of {games_total} games\"). "
            f"Do not speculate about what the missing data might have shown.")


def _insufficient_attempts_prompt(context_hint):
    return (f"There isn't enough sample to compute this reliably yet: {context_hint}. Write the "
            f"headline explaining that the situation simply hasn't come up enough yet, not as a "
            f"failure or a gap.")


def generate_defensive_split(client, sport, team_name, age_level, card):
    f = card["facts"]
    system = SIGNAL_SYSTEM.format(sport=sport, team_name=team_name, age_level=age_level)
    if card["state"] != "complete":
        state_note = _insufficient_attempts_prompt("not enough pitching data yet to split the runs allowed")
    else:
        state_note = ""
    prompt = f"""Write the "Defensive split" card — Team Defence bucket.

Runs allowed per game: {f['runs_allowed_per_game']}
Of that, the pitching's fair share (FRA): {f['fra_pitching_share']}
The fielding remainder: {f['fielding_remainder']}
Games in sample: {f['games']}

{state_note}
{_NO_INVENTION_NOTE}

{_RESPONSE_FORMAT}"""
    return _parse_narrative_lines(_call(client, system, prompt))


def generate_run_gap(client, sport, team_name, age_level, card):
    f = card["facts"]
    system = SIGNAL_SYSTEM.format(sport=sport, team_name=team_name, age_level=age_level)
    prompt = f"""Write the "Run gap" card — Offence / Defence bucket.

Runs scored per game: {f['runs_scored_per_game']}
Runs allowed per game: {f['runs_allowed_per_game']}
Gap (positive = allowing more than scoring, negative = scoring more than allowing): {f['gap']}
Games in sample: {f['games']}

{_NO_INVENTION_NOTE}

{_RESPONSE_FORMAT}"""
    return _parse_narrative_lines(_call(client, system, prompt))


def generate_offence_funnel(client, sport, team_name, age_level, card):
    f = card["facts"]
    system = SIGNAL_SYSTEM.format(sport=sport, team_name=team_name, age_level=age_level)
    state_note = _missing_data_prompt("some", "recent") if card["state"] != "complete" else ""
    prompt = f"""Write the "Offence funnel" card — Offence bucket. This describes the two-step
process of getting on base (hitting) and then getting home (baserunning).

Team OPSE (on-base + slugging with errors, scale 0-1+): {f['team_opse']}
Team SCORE% (share of times on base that became runs): {f['team_score_pct']}

{state_note}
{_NO_INVENTION_NOTE}

{_RESPONSE_FORMAT}"""
    return _parse_narrative_lines(_call(client, system, prompt))


def generate_fielding_conversion(client, sport, team_name, age_level, card):
    f = card["facts"]
    system = SIGNAL_SYSTEM.format(sport=sport, team_name=team_name, age_level=age_level)
    prompt = f"""Write the "Fielding conversion" card — Fielding bucket.

DefEff (share of balls in play converted to outs, scale 0-1): {f['def_eff']}
Team errors: {f['errors']}
Runs allowed per game: {f['runs_allowed_per_game']}
Games in sample: {f['games']}

{_NO_INVENTION_NOTE}

{_RESPONSE_FORMAT}"""
    return _parse_narrative_lines(_call(client, system, prompt))


def generate_catching_load(client, sport, team_name, age_level, card):
    f = card["facts"]
    system = SIGNAL_SYSTEM.format(sport=sport, team_name=team_name, age_level=age_level)
    if f["metric"] == "pb_per_inning":
        if card["state"] == "genuine_zero":
            state_note = _genuine_zero_prompt("No passed balls allowed this season")
        else:
            state_note = ""
        prompt = f"""Write the "Catching load" card — Catching bucket.

Passed balls: {f['passed_balls']}
Innings caught: {f['innings']}
Passed balls per inning: {f['pb_per_inning']}

{state_note}
{_NO_INVENTION_NOTE}

{_RESPONSE_FORMAT}"""
    else:
        if card["state"] == "insufficient_attempts":
            state_note = _insufficient_attempts_prompt(
                f"only {f['attempts']} stolen base attempts against this team all season")
        else:
            state_note = ""
        prompt = f"""Write the "Catching load" card — Catching bucket.

Runners caught stealing: {f['cs']}
Total steal attempts against this team: {f['attempts']}
Caught-stealing rate: {f['cs_pct']}

{state_note}
{_NO_INVENTION_NOTE}

{_RESPONSE_FORMAT}"""
    return _parse_narrative_lines(_call(client, system, prompt))


def generate_pitching(client, sport, team_name, age_level, card):
    f = card["facts"]
    system = SIGNAL_SYSTEM.format(sport=sport, team_name=team_name, age_level=age_level)
    if f["walks_ok"]:
        k_line = f"K-BB% (strikeouts minus walks, per batter faced): {f['k_minus_bb_pct']}"
        bb_line = f"BB% (share of batters faced who walk): {f['bb_pct']}"
    else:
        k_line = f"K% (share of batters faced who strike out — this team is at an age with no walks, so K-BB% collapses to plain K%): {f['k_minus_bb_pct']}"
        bb_line = "BB% does not apply at this age (no-walk rule) — do not mention walks."
    state_note = _missing_data_prompt("some", "recent") if card["state"] != "complete" else ""
    prompt = f"""Write the "Pitching" card — Pitching bucket. This describes strike-throwing:
how often pitches are strikes, and how that trades off against walks (where walks apply).

{k_line}
S% (share of pitches that are strikes): {f['s_pct']}
{bb_line}

{state_note}
{_NO_INVENTION_NOTE}

{_RESPONSE_FORMAT}"""
    return _parse_narrative_lines(_call(client, system, prompt))


def generate_hitting(client, sport, team_name, age_level, card):
    f = card["facts"]
    system = SIGNAL_SYSTEM.format(sport=sport, team_name=team_name, age_level=age_level)
    state_note = _missing_data_prompt("some", "recent") if card["state"] != "complete" else ""
    prompt = f"""Write the "Hitting" card — Hitting bucket. This describes contact quality: how
often the lineup puts the ball in play, and how hard it hits when it does.

Contact % (share of at-bats that avoid a strikeout): {f['c_pct']}
Hard-hit % (share of balls in play hit hard): {f['hh_pct']}
BABIP (hits per ball in play): {f['babip']}

{state_note}
{_NO_INVENTION_NOTE}

{_RESPONSE_FORMAT}"""
    return _parse_narrative_lines(_call(client, system, prompt))


def generate_baserunning(client, sport, team_name, age_level, card):
    f = card["facts"]
    system = SIGNAL_SYSTEM.format(sport=sport, team_name=team_name, age_level=age_level)
    if card["state"] == "insufficient_attempts":
        state_note = _insufficient_attempts_prompt("no stolen base attempts yet this season")
    else:
        state_note = ""
    prompt = f"""Write the "Baserunning" card — Baserunning bucket.

Stolen base % (of attempts): {f['sb_pct']}
Caught stealing: {f['cs']}
Picked off: {f['pik']}
Total steal attempts: {f['attempts']}

{state_note}
{_NO_INVENTION_NOTE}

{_RESPONSE_FORMAT}"""
    return _parse_narrative_lines(_call(client, system, prompt))


GENERATORS = {
    "defensive_split": generate_defensive_split,
    "run_gap": generate_run_gap,
    "offence_funnel": generate_offence_funnel,
    "fielding_conversion": generate_fielding_conversion,
    "catching_load": generate_catching_load,
    "hitting": generate_hitting,
    "baserunning": generate_baserunning,
    "pitching": generate_pitching,
}


def generate_all_narratives(anthropic_key, sport, team_name, age_level, cards):
    """Fills in headline/interpretation/why on each card dict in place. Best-
    effort per card — one card's generation failure never blocks the others
    (same independent-section-failure pattern as reports/generate.py).

    Runs the (up to 6) card generations concurrently rather than
    sequentially — found live that 6 sequential Claude calls took ~24s,
    which is fine as a one-time upload-commit cost but was originally
    (wrongly) wired into the dashboard's read path, where it made every
    single page view block for 24 seconds. Concurrency here cuts the wall
    time to roughly the slowest single call regardless of where this ends
    up being invoked from."""
    import concurrent.futures

    client = anthropic.Anthropic(api_key=anthropic_key)

    def _run(card):
        generator = GENERATORS[card["key"]]
        try:
            return generator(client, sport, team_name, age_level, card)
        except Exception:
            return None, None, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(cards) or 1) as pool:
        results = list(pool.map(_run, cards))

    for card, (headline, interpretation, why) in zip(cards, results):
        card["headline"] = headline
        card["interpretation"] = interpretation
        card["why_text"] = why
    return cards
