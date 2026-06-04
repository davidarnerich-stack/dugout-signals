"""Parse *_Play-by-Play.docx files and upload to Supabase."""
import io
import re
from docx import Document
from .common import TEAM_NAME, build_player_map, resolve_player

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

NARR_RESULT_PATTERNS = [
    (r"\bhit\s+by\s+pitch\b","Hit By Pitch"),
    (r"\bwalks?\b","Walk"),
    (r"\bsingles?\b","Single"),
    (r"\bdoubles?\b","Double"),
    (r"\btriples?\b","Triple"),
    (r"\bhome\s+run\b|\bhomers?\b","Home Run"),
    (r"\bstrikes?\s+out\b|\bout\s+at\s+first\s+on\s+dropped","Strikeout"),
    (r"\bpops?\s+out\b","Pop Out"),
    (r"\bflies?\s+out\b","Fly Out"),
    (r"\bgrounds?\s+out\b","Ground Out"),
    (r"\blines?\s+out\b","Line Out"),
    (r"\breaches?\s+on\s+(?:dropped\s+3rd|dropped\s+third)","Dropped 3rd Strike"),
    (r"\bhits?.*reaches?\s+on\s+error|\breaches?\s+on\s+error","Error"),
    (r"\bdouble\s+play\b","Double Play"),
    (r"\bhits?\s+into\s+(?:a\s+)?fielder.s\s+choice","Fielder's Choice"),
    (r"\bsacrifices?\b|\bsacrifice\s+bunt\b","Sacrifice Bunt"),
]

SCORE_RE = re.compile(r"([A-Z0-9]{2,6})\s+(\d+)\s*-\s*([A-Z0-9]{2,6})\s+(\d+)(?:\s*\|\s*(\d+)\s*Out)?",re.I)
OUTS_RE  = re.compile(r"^(\d)\s+Outs?$",re.I)
NARR_SCORE_RE = re.compile(r"STRM\s+(\d+)\s*[-–]\s*\w+\s+(\d+)|(\w+)\s+(\d+)\s*[-–]\s*STRM\s+(\d+)",re.I)
NARR_VERB_RE  = re.compile(
    r"\b(singles?|doubles?|triples?|home\s+run|walks?|hit\s+by\s+pitch|"
    r"strikes?\s+out|pops?\s+out|flies?\s+out|grounds?\s+out|lines?\s+out|"
    r"reaches?\s+on|hits?\s+(?:a\s+)?(?:ground|fly|line|into)|hits?\s+by|"
    r"sacrifices?|out\s+at\s+first|double\s+play|fielder.s\s+choice)\b",re.I)

BATTER_RE = re.compile(
    r"^([A-ZÀ-Ÿ][a-zA-ZÀ-ÿ\-]*(?:\s+[A-ZÀ-Ÿ][a-zA-ZÀ-ÿ\-]*){0,3})"
    r"\s+(singles?|doubles?|triples?|homers?|walks?|strikes? out|grounds? out|"
    r"flies? out|pops? out|lines? out|hits?|reaches?|pops? into|lines? into|"
    r"flies? into|grounds? into|sacrifices?|at bat)",re.I)

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

def _is_narrative(paras):
    return any("PLAY-BY-PLAY" in p.upper() for p in paras[:3])

def _parse_score(text):
    m = SCORE_RE.search(text)
    if not m: return None,None,None
    t1,s1,t2,s2 = m.group(1),int(m.group(2)),m.group(3),int(m.group(4))
    outs = int(m.group(5)) if m.group(5) else None
    if "STRM" in t1.upper(): return s1,s2,outs
    return s2,s1,outs

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

def _extract_batter_narr(para):
    m = NARR_VERB_RE.search(para)
    if not m or m.start()==0: return None
    raw = para[:m.start()].strip().rstrip(".,; ")
    raw = re.sub(r"\s*\(#?\d+\)","",raw).strip()
    raw = re.sub(r"(?<=[A-Z])\.","",raw).strip()
    words = raw.split()
    if 1<=len(words)<=4 and words[0][0].isupper(): return raw
    return None

def _update_inning(inning_scores, inning, half, score_storm, score_opp):
    if not inning or not half: return
    ks,ko = (inning,half,"storm"),(inning,half,"opponent")
    inning_scores[ks] = score_storm - inning_scores.get(ks,0)
    inning_scores[ko] = score_opp  - inning_scores.get(ko,0)


# ── GameChanger parser ─────────────────────────────────────────────────────────
def _parse_gc(paras):
    pas,inning_scores = [],{}
    inning=half=batting_team=pitcher=None
    outs=score_storm=score_opp=0; pa_sequence=0

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
            m=re.match(r"^(Top|Bottom)\s+(\d+)\w*\s+-\s+(.+)$",block_paras[0])
            if m:
                half=m.group(1).lower(); inning=int(m.group(2))
                batting_team="storm" if "storm" in m.group(3).lower() else "opponent"
                outs=0
                inning_scores.setdefault((inning,half,"storm"),score_storm)
                inning_scores.setdefault((inning,half,"opponent"),score_opp)
            continue
        if result_type=="Inning Ended":
            _update_inning(inning_scores,inning,half,score_storm,score_opp)
            outs=0; continue

        all_text=" ".join(block_paras)
        pa_sequence+=1

        for line in block_paras:
            if "Lineup changed" in line and "pitcher" in line.lower():
                m=re.search(r"Lineup changed:\s+([\w\sÀ-ÿ]+?)\s+in at pitcher",line,re.I)
                if m: pitcher=m.group(1).strip()
        pt=re.search(r"([\w\s]+?)\s+pitching",all_text,re.I)
        if pt: pitcher=pt.group(1).strip()

        new_storm=new_opp=None; outs_after=None
        for line in block_paras:
            s,o,oa=_parse_score(line)
            if s is not None: new_storm,new_opp=s,o
            if oa is not None: outs_after=oa
            oa2=_parse_outs(line)
            if oa2 is not None: outs_after=oa2

        narrative_lines=[]; pitch_parts=[]
        for line in block_paras:
            if "Lineup changed" in line: continue
            if _parse_score(line)[0] is not None or _parse_outs(line) is not None: continue
            if re.search(r"\bBall\s+\d|Strike\s+\d|Foul\b|In play\b",line):
                pitch_parts.append(line)
            else:
                narrative_lines.append(line)

        batter=None
        for line in narrative_lines:
            m=BATTER_RE.match(line.strip())
            if m: batter=m.group(1).strip(); break

        outs_rec=RESULT_META.get(result_type,(0,False,False))[0]
        if outs_rec==1 and "double play" in all_text.lower(): outs_rec=2

        pas.append({
            "inning":inning,"half_inning":half,"pa_sequence":pa_sequence,
            "batting_team":batting_team,"batter_name":batter or "Unknown",
            "pitcher_name":pitcher,"outs_before":min(outs,2),
            "score_storm_before":score_storm,"score_opp_before":score_opp,
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
                _update_inning(inning_scores,inning,half,score_storm,score_opp)
                outs=0
            else: outs=outs_after
        else:
            outs=min(outs+outs_rec,3)
            if outs>=3:
                _update_inning(inning_scores,inning,half,score_storm,score_opp)
                outs=0

    return pas,inning_scores


# ── Narrative parser ───────────────────────────────────────────────────────────
def _parse_narrative(paras):
    pas,inning_scores=[],{}
    inning=half=batting_team=None
    outs=score_storm=score_opp=0; pa_sequence=0; pitcher=None

    for para in paras:
        if any(k in para.upper() for k in ("PLAY-BY-PLAY","FINAL:","STRM:")):
            if not re.match(r"^(TOP|BOTTOM)",para,re.I): continue
        if re.match(r"^FINAL\s+(TOP|BOTTOM)",para,re.I):
            _update_inning(inning_scores,inning,half,score_storm,score_opp)
            outs=0; continue

        m=re.match(r"^(TOP|BOTTOM)\s+(\d+)(?:ST|ND|RD|TH)\s*[-–—]\s*(.+?)(?:\s+batting)?$",
                   para.strip(),re.I)
        if m:
            half=m.group(1).lower(); inning=int(m.group(2))
            batting_team="storm" if "storm" in m.group(3).lower() else "opponent"
            outs=0
            inning_scores.setdefault((inning,half,"storm"),score_storm)
            inning_scores.setdefault((inning,half,"opponent"),score_opp)
            continue

        if inning is None: continue

        batter=_extract_batter_narr(para)
        if not batter: continue

        result=None
        for pattern,res in NARR_RESULT_PATTERNS:
            if re.search(pattern,para,re.I): result=res; break
        if not result: continue

        sm=NARR_SCORE_RE.search(para)
        new_storm=new_opp=None
        if sm:
            if sm.group(1) is not None: new_storm,new_opp=int(sm.group(1)),int(sm.group(2))
            else: new_opp,new_storm=int(sm.group(4)),int(sm.group(5))

        outs_m=re.search(r"\((\d)\s+Outs?\)",para,re.I)
        outs_after=int(outs_m.group(1)) if outs_m else None

        pit_m=re.search(r"\(([A-Z]\.?\s*[A-Za-z]+)\s+pitching\)",para)
        if pit_m: pitcher=re.sub(r"\.\s*"," ",pit_m.group(1)).strip()

        outs_rec=RESULT_META.get(result,(0,False,False))[0]
        pa_sequence+=1
        pas.append({
            "inning":inning,"half_inning":half,"pa_sequence":pa_sequence,
            "batting_team":batting_team,"batter_name":batter,"pitcher_name":pitcher,
            "outs_before":min(outs,2),"score_storm_before":score_storm,
            "score_opp_before":score_opp,"runner_on_1b":None,"runner_on_2b":None,
            "runner_on_3b":None,"result":result,"hit_type":_hit_type(para),
            "hit_location":_hit_location(para),"rbi":_count_rbi(para),
            "outs_recorded":outs_rec,"pitch_sequence":None,"narrative":para,
        })

        if new_storm is not None: score_storm,score_opp=new_storm,new_opp
        if outs_after is not None:
            if outs_after>=3:
                _update_inning(inning_scores,inning,half,score_storm,score_opp)
                outs=0
            else: outs=outs_after
        else:
            outs=min(outs+outs_rec,3)
            if outs>=3:
                _update_inning(inning_scores,inning,half,score_storm,score_opp)
                outs=0

    return pas,inning_scores


# ── Base running events ────────────────────────────────────────────────────────
def _base_running_events(block_text, pa_id, game_id, player_map):
    events=[]
    def pid(n): return resolve_player(n.strip(),player_map)
    def bn(raw): return BASE_NAMES.get(raw.lower().strip())

    for m in re.finditer(r"([\w\sÀ-ÿ]+?)\s+steals\s+(2nd|3rd|home)",block_text,re.I):
        r=m.group(1).strip().split(",")[-1].strip()
        to=bn(m.group(2)); frm={"2nd":"1b","3rd":"2b","home":"3b"}.get(m.group(2).lower())
        events.append({"pa_id":pa_id,"game_id":game_id,"runner_name":r,"player_id":pid(r),
            "event_type":"stolen_base","from_base":frm,"to_base":to,
            "scored":m.group(2).lower()=="home","how":"stolen_base","fielder":None})

    for m in re.finditer(r"([\w\sÀ-ÿ]+?)\s+caught stealing\s+(\w+),?\s*catcher\s+([\w\sÀ-ÿ]+?)(?:,|\.|\n|$)",block_text,re.I):
        r=m.group(1).strip()
        events.append({"pa_id":pa_id,"game_id":game_id,"runner_name":r,"player_id":pid(r),
            "event_type":"caught_stealing","from_base":None,"to_base":"out",
            "scored":False,"how":"caught_stealing","fielder":m.group(3).strip()})

    for m in re.finditer(r"([\w\sÀ-ÿ]+?)\s+picked off at\s+(\w+),?\s*(?:catcher|pitcher)\s+([\w\sÀ-ÿ]+?)(?:,|\.|\n|$)",block_text,re.I):
        r=m.group(1).strip()
        events.append({"pa_id":pa_id,"game_id":game_id,"runner_name":r,"player_id":pid(r),
            "event_type":"pickoff_out","from_base":bn(m.group(2)),"to_base":"out",
            "scored":False,"how":"pickoff","fielder":m.group(3).strip()})

    for m in re.finditer(r"Pickoff attempt at\s+(\w+)",block_text,re.I):
        events.append({"pa_id":pa_id,"game_id":game_id,"runner_name":"unknown","player_id":None,
            "event_type":"pickoff_attempt","from_base":bn(m.group(1)),"to_base":None,
            "scored":False,"how":"pickoff_attempt","fielder":None})

    for m in re.finditer(r"([\w\sÀ-ÿ]+?)\s+advances? to\s+(\w+)\s+on\s+(wild pitch|passed ball|error by[^,\.]*|the (?:same )?(?:error|throw)|throw)",block_text,re.I):
        r=m.group(1).strip(); how=m.group(3).strip().lower()
        if "wild pitch" in how: et="advance_wild_pitch"
        elif "passed ball" in how: et="advance_passed_ball"
        elif "error" in how: et="advance_error"
        else: et="advance_throw"
        events.append({"pa_id":pa_id,"game_id":game_id,"runner_name":r,"player_id":pid(r),
            "event_type":et,"from_base":None,"to_base":bn(m.group(2)),
            "scored":False,"how":how,"fielder":None})

    for m in re.finditer(r"([\w\sÀ-ÿ]+?)\s+scores?(?: on\s+(.+?))?(?:,|\.|\n|$)",block_text,re.I):
        r=m.group(1).strip()
        if not r or r.lower() in ("half-inning","inning"): continue
        events.append({"pa_id":pa_id,"game_id":game_id,"runner_name":r,"player_id":pid(r),
            "event_type":"scored","from_base":"3b","to_base":"home",
            "scored":True,"how":(m.group(2) or "").strip() or "unknown","fielder":None})

    return events


# ── Main entry point ──────────────────────────────────────────────────────────
def process(sb, source, game_id=None, filename=None, is_text=False):
    """
    Process play-by-play data.

    source    : bytes (DOCX) or str (raw pasted text) depending on is_text
    game_id   : pre-resolved game UUID (preferred)
    filename  : legacy DOCX filename for game identity fallback
    is_text   : True if source is a raw text string, False if DOCX bytes
    """
    player_map = build_player_map(sb)

    if is_text:
        paras = _get_paras_from_text(source)
    else:
        paras = _get_paras_from_docx(source)

    pa_dicts, inning_scores = (_parse_narrative(paras) if _is_narrative(paras)
                               else _parse_gc(paras))

    # Resolve game_id (legacy filename path)
    if game_id is None and filename:
        import re as _re
        m = _re.match(r"^(\d{4}-\d{2}-\d{2})-(.+?)_((?:Game)?(\d+))_", filename)
        if not m:
            raise ValueError(f"Cannot identify game from filename: {filename}")
        date    = m.group(1)
        game_num = int(m.group(4))
        game_resp = (sb.table("games").select("game_id")
                     .eq("game_date",date).eq("game_number",game_num)
                     .eq("team_name",TEAM_NAME).execute())
        if not game_resp.data:
            raise ValueError(f"No game found for {date} game {game_num}. Upload Stats CSV first.")
        game_id = game_resp.data[0]["game_id"]
    elif game_id is None:
        raise ValueError("Either game_id or filename must be provided")

    # Clear existing
    sb.table("inning_scores").delete().eq("game_id",game_id).execute()
    sb.table("base_running_events").delete().eq("game_id",game_id).execute()
    sb.table("plate_appearances").delete().eq("game_id",game_id).execute()

    for pa in pa_dicts:
        pa["game_id"] = game_id
        pa["batter_player_id"]  = (resolve_player(pa["batter_name"],player_map)
                                    if pa["batting_team"]=="storm" else None)
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

    for i in range(0,len(all_bre),BATCH):
        sb.table("base_running_events").insert(all_bre[i:i+BATCH]).execute()

    is_rows=[]
    for (inn,half,team),runs in inning_scores.items():
        if inn and inn>0:
            is_rows.append({"game_id":game_id,"inning":inn,"half_inning":half,
                            "team":team,"runs":max(0,runs)})
    if is_rows:
        sb.table("inning_scores").insert(is_rows).execute()

    storm_pas=sum(1 for p in pa_dicts if p["batting_team"]=="storm")
    return {
        "message": (f"{len(inserted)} plate appearances, {len(all_bre)} base running events, "
                    f"{len(is_rows)} inning score rows. Storm PAs: {storm_pas}."),
        "details": [f"PA #{p['pa_sequence']}: {p['batter_name']} — {p['result']}"
                    for p in pa_dicts if p["batting_team"]=="storm"][:20],
    }
