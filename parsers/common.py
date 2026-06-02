"""Shared utilities used by all parsers."""
import re

TEAM_NAME = "Storm 12U All-Stars"
SEASON    = "Summer 2026"


def parse_filename(name):
    """Return (date_str, game_number) from a Stats/Box-Score/Play-by-Play filename."""
    m = re.match(r"^(\d{4}-\d{2}-\d{2})-(.+?)_((?:Game)?(\d+))_", name)
    if not m:
        raise ValueError(f"Unrecognised filename format: {name}")
    return m.group(1), int(m.group(4))


def build_player_map(sb):
    """Return {name_variant: player_id} for all Storm players."""
    resp = sb.table("players").select("player_id,first_name,last_name,number") \
             .eq("team_name", TEAM_NAME).execute()
    pmap = {}
    for p in resp.data:
        pid = p["player_id"]
        fn, ln = p.get("first_name") or "", p.get("last_name") or ""
        for key in [f"{fn} {ln}", f"{fn[0]} {ln}" if fn else None, ln]:
            if key and key.strip():
                pmap[key.strip()] = pid
    return pmap


def resolve_player(name, player_map):
    if not name:
        return None
    if name in player_map:
        return player_map[name]
    for part in reversed(name.strip().split()):
        if part in player_map:
            return player_map[part]
    return None


def parse_num(val, as_float=False):
    s = str(val).strip()
    if s in ("-", "", "N/A", "None"):
        return None
    try:
        f = float(s.replace("%", ""))
        return f if as_float else int(f)
    except (ValueError, TypeError):
        return None
