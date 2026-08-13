"""Parse *_Play-by-Play.docx files and upload to Supabase."""
import io
import re
from docx import Document
from .common import build_player_map, resolve_player

# ── Constants ──────────────────────────────────────────────────────────────────
RESULT_TYPES = {
    "Single","Double","Triple","Home Run","Walk","Strikeout",
    "Ground Out","Fly Out","Pop Out","Line Out","Fielder's Choice",
    "Double Play","Triple Play","Error","Dropped 3rd Strike",
    "Sacrifice Bunt","Runner Out","Inning Ended","Hit By Pitch",
}

RESULT_META = {
    "Single":(0,True,True),"Double":(0,True,True),"Triple":(0,True,True),
    "Home Run":(0,True,True),"Walk":(0,False,True),"Hit By Pitch":(0,False,True),
    "Dropped 3rd Strike":(0,False,True),"Error":(0,False,True),
    "Fielder's Choice":(1,False,True),"Sacrifice Bunt":(1,False,False),
    "Strikeout":(1,False,False),"Ground Out":(1,False,False),
    "Fly Out":(1,False,False),"Pop Out":(1,False,False),
    "Line Out":(1,False,False),"Double Play":(2,False,False),
    "Triple Play":(3,False,False),"Runner Out":(1,False,False),
    "Inning Ended":(0,False,False),
}

HIT_TYPE_MAP = {
    "hard line drive":"line_drive","line drive":"line_drive",
    "hard ground ball":"ground_ball","ground ball":"ground_ball",
    "fly ball":"fly_ball","pop fly":"pop_fly",
}

BASE_NAMES = {"1st":"1b","2nd":"2b","3rd":"3b","home":"home"}

# A runner's name, as the play-by-play writes it: up to four capitalised tokens.
#
# The old pattern was [\w\sÀ-ÿ]+?, which has no hyphen in it. finditer scans
# left to right, so on "Reuben Yamada-Harivandi steals 2nd" it could not match
# from the start of the name — the hyphen stopped it — and matched from
# "Harivandi" instead. It did not fail; it silently stored half a name. Nineteen
# events across the season belonged to Yamada-Harivandi and Mendez-Palos and
# were attributed to nobody. Requiring capitals also keeps a bare jersey number
# out of a name column, which is what runner_number is now for.
# Tokens are separated by spaces or tabs, never a newline: \s+ would let a
# name run off the end of its line and swallow the first word of the next
# ("M Barragan.\nHalf-inning"). A name does not span lines.
RUNNER = r"[A-ZÀ-Ÿ][A-Za-zÀ-ÿ'’.\-]*(?:[ \t]+[A-ZÀ-Ÿ][A-Za-zÀ-ÿ'’.\-]*){0,3}"

# "Top 3rd - Hilighters batting".
#
# There were once two of these, one per parse path, and the live one was the
# stricter: case-sensitive, plain ASCII hyphen only. An inning header this
# cannot read is not skipped in isolation — the inning counter simply does not
# advance, so every plate appearance after it is filed under the previous
# inning and its runs land in the wrong half, which reaches the coach as a
# wrong inning-by-inning line score. This is the tolerant version, and a header
# that still fails is counted rather than passed over (DS-112). The second
# parse path is gone (DS-120); this pattern outliving it is the only part of it
# that was worth keeping.
INNING_RE = re.compile(
    r"^(TOP|BOTTOM)\s+(\d+)(?:ST|ND|RD|TH)?\s*[-–—]\s*(.+?)(?:\s+batting)?$", re.I)


# The fielder credited on a pickoff or caught stealing.
#
# Both patterns used to accept only "catcher" or "pitcher" and to require a
# name after it. Neither holds. GameChanger writes "third baseman M Barragan",
# "shortstop J Carter", "catcher #5", and — twice in PSW's own games —
# "third baseman" with no name at all. Every one of those was dropped in
# silence, taking the whole event with it, not just the fielder's name
# (DS-112).
POSITION = (r"(?:pitcher|catcher|shortstop"
            r"|(?:first|second|third)\s+baseman"
            r"|(?:left|right|cent(?:er|re)|short)\s+fielder)")

# The base-running patterns live at module level so the coverage ratchet in
# tests/test_parser_coverage.py can import the *same objects* the parser uses.
# Rebuilding equivalent patterns in the test was the obvious shortcut and the
# wrong one: it guards a replica, so the parser can change and the test stays
# green describing code that no longer exists.
_WHO = rf"(#\d+|{RUNNER})"
# The fielder is optional in both plays below. An event that happened is worth
# recording even when we cannot say who made the play (DS-112).
_FIELDER = rf"(?:,?\s*(?:by\s+)?({POSITION}(?:\s+(?:#\d+|{RUNNER}))?))?"

STEAL_RE   = re.compile(rf"{_WHO}\s+steals\s+(2nd|3rd|home)")
CS_RE      = re.compile(rf"{_WHO}\s+caught stealing\s+(\w+){_FIELDER}")
PICKOFF_RE = re.compile(rf"{_WHO}\s+picked off at\s+(\w+){_FIELDER}")
PICKOFF_ATTEMPT_RE = re.compile(r"Pickoff attempt at\s+(\w+)", re.I)
ADVANCE_RE = re.compile(
    rf"{_WHO}\s+advances? to\s+(\w+)\s+on\s+"
    r"(wild pitch|passed ball|error by[^,\.]*|"
    r"the (?:same )?(?:error|throw)|throw)")
# A run is the event that matters most, so this is the pattern that can least
# afford to be fussy about punctuation. It used to end at "," "." or a line
# break only, which dropped "M Y scores]" inside a bracketed play and
# "Peyton L scores; Adelaide J advances to 3rd" — a semicolon separates clauses
# in exactly the multi-runner plays where a run is easiest to lose. "after"
# joins "on" as a cause: "scores after tagging up" is a sacrifice fly, and it
# was dropped entirely (DS-112 pass 2).
SCORED_RE  = re.compile(
    rf"{_WHO}\s+scores?(?:\s+(?:on|after)\s+(.+?))?(?:[,;.\]]|\n|$)")


def _resolve_runner(raw, player_map):
    """
    Resolve one captured runner to (name, number, player_id).

    RUNNER can over-capture: "on error by pitcher A Arnerich R Yamada-Harivandi
    scores" has no comma to stop it. So rather than trust the capture, try it
    whole and then drop leading tokens one at a time, keeping the longest
    suffix that resolves. A name that never resolves is stored as parsed —
    unattributed, but not silently truncated to something wrong.
    """
    raw = (raw or "").strip().lstrip("#").strip(",.;:").strip()
    if not raw:
        return None, None, None
    if raw.isdigit():
        return None, int(raw), None
    tokens = raw.split()
    for start in range(len(tokens)):
        candidate = " ".join(tokens[start:])
        pid = resolve_player(candidate, player_map)
        if pid:
            return candidate, None, pid
    return raw, None, None

SCORE_RE = re.compile(r"([A-Z0-9]{2,6})\s+(\d+)\s*-\s*([A-Z0-9]{2,6})\s+(\d+)(?:\s*\|\s*(\d+)\s*Out)?",re.I)
OUTS_RE  = re.compile(r"^(\d)\s+Outs?$",re.I)

# Two defects lived here (DS-114), and they compound.
#
# re.IGNORECASE silently voided the capitalisation guard: with it, [A-ZÀ-Ÿ]
# matches lowercase, so "is" could be absorbed into a name and "W Salisian is
# hit by pitch" stored the batter as "W Salisian is". Names in GameChanger's
# output are always capitalised, so the flag bought nothing and cost that.
#
# Removing it alone is not enough — it turns the same line into no match at
# all, because "is hit by pitch", "is out on…", "picked off" and "caught
# stealing" were never listed. An unmatched line stored the batter as
# "Unknown", which then reached a coach's recap. Longer phrasings come first
# so "is hit by pitch" wins over a bare "hits".
# Periods belong in the name class: "N Smith Jr. triples…" is a real batter,
# and without them the suffix broke the match and the plate appearance lost
# its batter entirely.
BATTER_RE = re.compile(
    r"^([A-ZÀ-Ÿ][a-zA-ZÀ-ÿ\-'.]*(?:\s+[A-ZÀ-Ÿ][a-zA-ZÀ-ÿ\-'.]*){0,3})"
    r"\s+(?:is\s+hit\s+by\s+pitch|is\s+out|picked\s+off|caught\s+stealing|"
    r"strikes?\s+out|grounds?\s+out|flies?\s+out|pops?\s+out|lines?\s+out|"
    r"pops?\s+into|lines?\s+into|flies?\s+into|grounds?\s+into|"
    r"singles?|doubles?|triples?|homers?|walks?|steals?|scores?|advances?|"
    r"hits?|reaches?|sacrifices?|at\s+bat)\b")

# ── Helpers ────────────────────────────────────────────────────────────────────
def _get_paras_from_docx(docx_bytes):
    doc = Document(io.BytesIO(docx_bytes))
    return [p.text.replace("\xa0"," ").strip() for p in doc.paragraphs if p.text.strip()]

def _get_paras_from_text(raw_text: str):
    """Split raw pasted text into paragraphs, normalising whitespace."""
    lines = []
    for line in raw_text.replace("\xa0", " ").splitlines():
        line = line.strip()
        if line:
            lines.append(line)
    return lines

def _parse_score(text, we_are_away: bool):
    """
    Score lines carry GameChanger's auto-generated per-team abbreviation
    ("STRM", "YANK", "PSWS" …) which is unpredictable and must never be
    matched by name. The visiting team is printed first — the same
    left-is-visitor convention verified for the box score line score — so
    `we_are_away` alone says which column is ours (DS-92).
    """
    m = SCORE_RE.search(text)
    if not m: return None,None,None
    s1,s2 = int(m.group(2)), int(m.group(4))
    outs = int(m.group(5)) if m.group(5) else None
    return (s1,s2,outs) if we_are_away else (s2,s1,outs)

def _parse_outs(text):
    m = OUTS_RE.match(text.strip())
    return int(m.group(1)) if m else None

def _hit_type(text):
    low = text.lower()
    for phrase,ht in HIT_TYPE_MAP.items():
        if phrase in low: return ht
    return None

def _hit_location(text):
    m = re.search(r"to\s+((?:first|second|third|shortstop|pitcher|catcher|"
                  r"left fielder|center fielder|right fielder|first baseman|"
                  r"second baseman|third baseman)[^,\.]*)",text,re.I)
    return m.group(1).strip() if m else None

def _count_rbi(text):
    return len(re.findall(r"\w[\w\s]+?\s+scores",text,re.I))

def _update_inning(inning_scores, inning, half, score_storm, score_opp, closed):
    """
    Commit the runs scored in the current half-inning as (cumulative score
    now) minus (cumulative score at the half-inning's start). GameChanger
    play-by-play can signal the end of a half-inning two ways for the same
    half-inning — the 3rd out landing mid-PA, and a separate "Inning Ended"
    marker block right after it — so callers must only apply this once per
    half-inning; a second call would subtract the just-committed delta
    (already small) from the game's cumulative score instead of the
    half-inning's starting score, producing a wildly inflated inning total.
    `closed` is a set of (inning,half) keys already committed this game.
    """
    if not inning or not half: return
    key = (inning,half)
    if key in closed: return
    closed.add(key)
    ks,ko = (inning,half,"our_team"),(inning,half,"opponent")
    inning_scores[ks] = score_storm - inning_scores.get(ks,0)
    inning_scores[ko] = score_opp  - inning_scores.get(ko,0)


def _batting_team_for(half: str, is_away: bool) -> str:
    """
    Which team is batting in this half-inning (DS-92).

    The visiting team bats the top of every inning and the home team the
    bottom — universal, so knowing which side we were is enough. `is_away`
    comes from the venue marker printed on the box score.

    This replaces matching the coach's team name against the inning header.
    That never worked reliably: GameChanger's team name routinely differs
    from what the coach entered at onboarding ("PSW Summer 26 Hilighters"
    vs "PSW Hilighters"), and a miss silently attributed *every* plate
    appearance to the opponent rather than failing loudly.
    """
    return "our_team" if (half == "top") == bool(is_away) else "opponent"


# ── GameChanger parser ─────────────────────────────────────────────────────────
def _parse_gc(paras, is_away: bool):
    pas,inning_scores = [],{}
    bad_innings=[]
    inning=half=batting_team=pitcher=None
    pitcher_number=None
    outs=score_storm=score_opp=0; pa_sequence=0
    closed_innings=set()
    we_are_away = bool(is_away)

    blocks=[]
    cur_result=None; cur_block=[]
    for p in paras:
        if re.match(r"^(Top|Bottom)\s+\d",p):
            if cur_result: blocks.append((cur_result,cur_block))
            blocks.append(("__INNING__",[p])); cur_result=None; cur_block=[]
        elif p in RESULT_TYPES:
            if cur_result: blocks.append((cur_result,cur_block))
            cur_result=p; cur_block=[]
        else:
            cur_block.append(p)
    if cur_result: blocks.append((cur_result,cur_block))

    for result_type,block_paras in blocks:
        if result_type=="__INNING__":
            m=INNING_RE.match(block_paras[0].strip())
            if m:
                half=m.group(1).lower(); inning=int(m.group(2))
                batting_team=_batting_team_for(half, we_are_away)
                outs=0
                inning_scores.setdefault((inning,half,"our_team"),score_storm)
                inning_scores.setdefault((inning,half,"opponent"),score_opp)
            else:
                # Do not pass over this. The inning has not advanced, so
                # everything below is about to be misfiled.
                bad_innings.append(block_paras[0].strip()[:80])
            continue
        if result_type=="Inning Ended":
            # Read this block's own score line BEFORE closing the half-inning.
            # It carries the half's final scoring play — a run that crosses as
            # the inning ends, including the run that trips a mercy-rule cap —
            # and skipping it dropped that run and leaked it into the next
            # inning (DS-92).
            for line in block_paras:
                s,o,_ = _parse_score(line, we_are_away)
                if s is not None:
                    score_storm,score_opp = s,o
            _update_inning(inning_scores,inning,half,score_storm,score_opp,closed_innings)
            outs=0; continue

        all_text=" ".join(block_paras)
        pa_sequence+=1

        for line in block_paras:
            if "Lineup changed" in line and "pitcher" in line.lower():
                m=re.search(r"Lineup changed:\s+([\w\sÀ-ÿ]+?)\s+in at pitcher",line,re.I)
                if m: pitcher=m.group(1).strip()
        # The opposing pitcher is identified only by number ("#11 pitching"),
        # and \w matched the digits — so pitcher_name held "11", a value of a
        # different kind from the column's name. Keep the number, but in a
        # field that says number.
        pt_num=re.search(r"#(\d+)\s+pitching",all_text,re.I)
        if pt_num:
            pitcher_number=pt_num.group(1); pitcher=None
        else:
            pt=re.search(r"([A-ZÀ-Ÿ][\w.'\-]*(?:\s+[A-ZÀ-Ÿ][\w.'\-]*)*)\s+pitching",all_text)
            if pt: pitcher=pt.group(1).strip(); pitcher_number=None

        new_storm=new_opp=None; outs_after=None
        for line in block_paras:
            s,o,oa=_parse_score(line, we_are_away)
            if s is not None: new_storm,new_opp=s,o
            if oa is not None: outs_after=oa
            oa2=_parse_outs(line)
            if oa2 is not None: outs_after=oa2

        narrative_lines=[]; pitch_parts=[]
        for line in block_paras:
            if "Lineup changed" in line: continue
            if _parse_score(line, we_are_away)[0] is not None or _parse_outs(line) is not None: continue
            if re.search(r"\bBall\s+\d|Strike\s+\d|Foul\b|In play\b",line):
                pitch_parts.append(line)
            else:
                narrative_lines.append(line)

        # Opposing batters appear as "#13 hits a ground ball…" — a number, not
        # a name. Storing "Unknown" satisfied the old NOT NULL while asserting
        # something false, and the same fallback let a failed parse of OUR
        # batter reach a report looking like a player. Keep the number in a
        # column that says number; leave the name genuinely absent.
        batter=None; batter_number=None
        for line in narrative_lines:
            line=line.strip()
            m=BATTER_RE.match(line)
            if m: batter=m.group(1).strip(); break
            mn=re.match(r"#(\d+)\s", line)
            if mn: batter_number=mn.group(1); break

        outs_rec=RESULT_META.get(result_type,(0,False,False))[0]
        if outs_rec==1 and "double play" in all_text.lower(): outs_rec=2

        pas.append({
            "inning":inning,"half_inning":half,"pa_sequence":pa_sequence,
            "batting_team":batting_team,"batter_name":batter,
            "batter_number":batter_number,
            "pitcher_name":pitcher,"pitcher_number":pitcher_number,
            "outs_before":min(outs,2),
            "score_our_before":score_storm,"score_opp_before":score_opp,
            "runner_on_1b":None,"runner_on_2b":None,"runner_on_3b":None,
            "result":result_type,"hit_type":_hit_type(all_text),
            "hit_location":_hit_location(all_text),"rbi":_count_rbi(all_text),
            "outs_recorded":outs_rec,
            "pitch_sequence":" ".join(pitch_parts) or None,
            "narrative":" ".join(narrative_lines) or all_text,
        })

        if new_storm is not None: score_storm,score_opp=new_storm,new_opp
        if outs_after is not None:
            if outs_after>=3:
                _update_inning(inning_scores,inning,half,score_storm,score_opp,closed_innings)
                outs=0
            else: outs=outs_after
        else:
            outs=min(outs+outs_rec,3)
            if outs>=3:
                _update_inning(inning_scores,inning,half,score_storm,score_opp,closed_innings)
                outs=0

    return pas,inning_scores,bad_innings


# ── Base running ───────────────────────────────────────────────────────────────
def _base_running_events(block_text, pa_id, game_id, player_map):
    """
    Pull base-running events out of one plate appearance's text.

    Note what this is and is not. DS-101 moved the season's stolen-base and
    caught-stealing *counts* onto the official CSV, so nothing here feeds a
    total the coach sees as a number. What it feeds is play-level detail — who
    took second, who scored on the passed ball — so a name that resolves to
    nobody costs a sentence in the report, not a wrong statistic.
    """
    events=[]
    def bn(raw): return BASE_NAMES.get(raw.lower().strip())

    def ev(raw, **fields):
        name, number, player_id = _resolve_runner(raw, player_map)
        if not (name or number):
            return
        events.append({"pa_id":pa_id,"game_id":game_id,"runner_name":name,
                       "runner_number":number,"player_id":player_id, **fields})

    for m in STEAL_RE.finditer(block_text):
        base=m.group(2).lower()
        ev(m.group(1), event_type="stolen_base",
           from_base={"2nd":"1b","3rd":"2b","home":"3b"}.get(base), to_base=bn(m.group(2)),
           scored=base=="home", how="stolen_base", fielder=None)

    for m in CS_RE.finditer(block_text):
        ev(m.group(1), event_type="caught_stealing", from_base=None, to_base="out",
           scored=False, how="caught_stealing",
           fielder=(m.group(3) or "").strip().rstrip(".,") or None)

    for m in PICKOFF_RE.finditer(block_text):
        ev(m.group(1), event_type="pickoff_out", from_base=bn(m.group(2)), to_base="out",
           scored=False, how="pickoff",
           fielder=(m.group(3) or "").strip().rstrip(".,") or None)

    # A pickoff attempt names no runner at all. That is a fact about the play,
    # not a parse failure — so the name is NULL rather than the word "unknown".
    for m in PICKOFF_ATTEMPT_RE.finditer(block_text):
        events.append({"pa_id":pa_id,"game_id":game_id,"runner_name":None,
            "runner_number":None,"player_id":None,
            "event_type":"pickoff_attempt","from_base":bn(m.group(1)),"to_base":None,
            "scored":False,"how":"pickoff_attempt","fielder":None})

    for m in ADVANCE_RE.finditer(block_text):
        how=m.group(3).strip().lower()
        if "wild pitch" in how: et="advance_wild_pitch"
        elif "passed ball" in how: et="advance_passed_ball"
        elif "error" in how: et="advance_error"
        else: et="advance_throw"
        ev(m.group(1), event_type=et, from_base=None, to_base=bn(m.group(2)),
           scored=False, how=how, fielder=None)

    for m in SCORED_RE.finditer(block_text):
        ev(m.group(1), event_type="scored", from_base="3b", to_base="home",
           scored=True, how=(m.group(2) or "").strip() or None, fielder=None)

    return events


# ── Main entry point ──────────────────────────────────────────────────────────
def process(sb, source, team_id, team_name, game_id=None, filename=None, is_text=False):
    """
    Process play-by-play data.

    source    : bytes (DOCX) or str (raw pasted text) depending on is_text
    game_id   : pre-resolved game UUID (preferred)
    filename  : legacy DOCX filename for game identity fallback
    is_text   : True if source is a raw text string, False if DOCX bytes
    """
    player_map = build_player_map(sb, team_id)

    if is_text:
        paras = _get_paras_from_text(source)
    else:
        paras = _get_paras_from_docx(source)

    # Resolve game_id (legacy filename path) BEFORE parsing — the parse needs
    # the game's venue to know which half-inning is ours (DS-92).
    if game_id is None and filename:
        import re as _re
        m = _re.match(r"^(\d{4}-\d{2}-\d{2})-(.+?)_((?:Game)?(\d+))_", filename)
        if not m:
            raise ValueError(f"Cannot identify game from filename: {filename}")
        date    = m.group(1)
        game_num = int(m.group(4))
        game_resp = (sb.table("games").select("game_id")
                     .eq("game_date",date).eq("game_number",game_num)
                     .eq("team_id",team_id).execute())
        if not game_resp.data:
            raise ValueError(f"No game found for {date} game {game_num}. Upload Stats CSV first.")
        game_id = game_resp.data[0]["game_id"]
    elif game_id is None:
        raise ValueError("Either game_id or filename must be provided")

    # Venue decides which half-inning belongs to which team. It is recorded
    # from the box score's venue marker when the game is created or
    # re-uploaded (DS-91), so it is present whenever a box score PDF has been
    # processed for this game — which the upload flow already requires before
    # accepting play-by-play.
    game_row = (sb.table("games").select("is_away")
                .eq("game_id", game_id).limit(1).execute())
    is_away = game_row.data[0].get("is_away") if game_row.data else None
    if is_away is None:
        raise ValueError(
            "This game has no home/away record yet, so play-by-play can't be "
            "attributed to the right team. Upload the box score PDF for this "
            "game first, then add the play-by-play."
        )

    pa_dicts, inning_scores, bad_innings = _parse_gc(paras, is_away)

    # Clear existing
    sb.table("inning_scores").delete().eq("game_id",game_id).execute()
    sb.table("base_running_events").delete().eq("game_id",game_id).execute()
    sb.table("plate_appearances").delete().eq("game_id",game_id).execute()

    # A batter we cannot place is reported, never swallowed. Storing "Unknown"
    # meant a name the parser failed on reached a coach's recap looking like a
    # player (DS-114); a miss is a parsing problem and should read as one.
    unresolved = []
    for pa in pa_dicts:
        pa["game_id"] = game_id
        pa["team_id"] = team_id
        if pa["batting_team"] == "our_team":
            pa["batter_player_id"] = resolve_player(pa["batter_name"], player_map)
            if not pa["batter_player_id"]:
                unresolved.append(
                    f"inning {pa.get('inning')} {pa.get('half_inning')}: "
                    f"{pa.get('batter_name') or '(no name parsed)'} — {pa.get('result')}")
        else:
            pa["batter_player_id"] = None
        pa["pitcher_player_id"] = resolve_player(pa.get("pitcher_name"),player_map)

    BATCH=50
    inserted=[]
    for i in range(0,len(pa_dicts),BATCH):
        resp=sb.table("plate_appearances").insert(pa_dicts[i:i+BATCH]).execute()
        inserted.extend(resp.data)

    pa_id_map={r["pa_sequence"]:r["pa_id"] for r in inserted}
    all_bre=[]
    for pa in pa_dicts:
        pa_id=pa_id_map.get(pa["pa_sequence"])
        if not pa_id: continue
        txt=" ".join(filter(None,[pa.get("pitch_sequence"),pa.get("narrative")]))
        all_bre.extend(_base_running_events(txt,pa_id,game_id,player_map))

    for bre in all_bre:
        bre["team_id"] = team_id

    for i in range(0,len(all_bre),BATCH):
        sb.table("base_running_events").insert(all_bre[i:i+BATCH]).execute()

    is_rows=[]
    for (inn,half,team),runs in inning_scores.items():
        if inn and inn>0:
            is_rows.append({"game_id":game_id,"team_id":team_id,"inning":inn,"half_inning":half,
                            "team":team,"runs":max(0,runs)})
    if is_rows:
        sb.table("inning_scores").insert(is_rows).execute()

    storm_pas=sum(1 for p in pa_dicts if p["batting_team"]=="our_team")
    message = (f"{len(inserted)} plate appearances, {len(all_bre)} base running events, "
               f"{len(is_rows)} inning score rows. {team_name} PAs: {storm_pas}.")
    if unresolved:
        message += (f" {len(unresolved)} plate appearance"
                    f"{'' if len(unresolved) == 1 else 's'} could not be matched to a "
                    f"player ({'; '.join(unresolved[:5])}"
                    f"{f' and {len(unresolved) - 5} more' if len(unresolved) > 5 else ''}).")
    if bad_innings:
        # Worth saying loudly: this one does not lose a single row, it misfiles
        # every row after it, and the wrong inning shows up in the line score.
        message += (f" {len(bad_innings)} inning header"
                    f"{'' if len(bad_innings) == 1 else 's'} could not be read "
                    f"({'; '.join(bad_innings[:3])}) — plays after "
                    f"{'it' if len(bad_innings) == 1 else 'them'} may be "
                    f"recorded in the wrong inning.")
    return {
        "message": message,
        "details": [f"PA #{p['pa_sequence']}: {p['batter_name']} — {p['result']}"
                    for p in pa_dicts if p["batting_team"]=="our_team"][:20],
        "unresolved_batters": unresolved,
        "bad_innings": bad_innings,
    }
