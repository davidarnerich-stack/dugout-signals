"""
Near-miss detection over the real play-by-play corpus (DS-112, pass 2).

Pass 1 of the DS-112 audit swept *rejection points* — `continue`,
`return None`, `except: pass` — and missed a live bug: the pickoff pattern
accepted only "catcher" or "pitcher" as the fielder, so every "third baseman
M Barragan" was dropped along with its entire event. That miss was invisible
to pass 1 because **a regex that fails to match is not a rejection point.**
It is an absence, and an absence does not appear in a grep for `continue`.

So look for the absences directly. For each pattern the parser depends on,
define a *trigger*: a loose keyword meaning "this line is about that event".
Any line where the trigger fires but the full pattern does not is a candidate
silent miss — the parser saw something it was meant to understand, did not,
and said nothing.

The ceilings below are a ratchet, not a target. They record what the parser
misses today so that a change which makes it miss *more* fails here. Lowering
a ceiling is always welcome; raising one needs a reason in the commit message.

They are **rates, not counts**, and that matters. The corpus is a live folder
on a coach's machine — it grew from 55 files to 65 during the session that
wrote this test, because more games get played and files sync down. Absolute
miss counts move with corpus size and would fail for reasons that have nothing
to do with the parser, which is a test that cries wolf until someone stops
reading it.

Runs against every play-by-play on this machine, across four teams and three
seasons — not just the 8 PSW games the other tests use. Skips when that corpus
is absent, so this does not fail on another machine.
"""
import os
import re
import glob
import collections

import pytest

# Import the patterns the parser actually runs — never rebuild equivalents
# here. A replica drifts: the parser changes, the copy does not, and the test
# stays green describing code that no longer exists. That is how the first
# version of this file passed while the pickoff fix was mutated away.
from parsers.play_by_play import (INNING_RE, BATTER_RE, SCORE_RE, OUTS_RE,
                                  RESULT_TYPES, STEAL_RE, CS_RE, PICKOFF_RE,
                                  PICKOFF_ATTEMPT_RE, ADVANCE_RE, SCORED_RE)

CORPUS_ROOTS = [os.path.expanduser("~/Desktop"),
                os.path.expanduser("~/Documents/Sports")]
CORPUS_GLOBS = ["*lay-by-*lay*", "*Hilighters", "*Hilighers"]

# Lines that are a bare result label or a pitch sequence are not batter lines.
PITCH_ONLY = re.compile(r"^(?:Ball|Strike|Foul|In play|Pickoff|Lineup|\d+ Out)", re.I)
# An opposing batter written "#13 singles…" is handled by the batter_number
# path, not BATTER_RE (DS-114). Not a miss.
NUMBERED_BATTER = re.compile(r"^#\d+\s")
BATTER_VERBS = re.compile(
    r"\b(singles?|doubles?|triples?|homers?|walks?|strikes? out|grounds? out|"
    r"flies? out|pops? out|lines? out|hit by pitch|reaches?|sacrifices?|"
    r"picked off|caught stealing|steals?|advances?|scores?)\b", re.I)


# name -> (trigger, full pattern, anchored?, max miss RATE in percent)
CHECKS = {
    "stolen_base": (
        re.compile(r"\bsteals\b", re.I), STEAL_RE, False, 0.6),
    "caught_stealing": (
        re.compile(r"\bcaught stealing\b", re.I), CS_RE, False, 0.0),
    "pickoff_out": (
        re.compile(r"\bpicked off\b", re.I), PICKOFF_RE, False, 0.0),
    "pickoff_attempt": (
        re.compile(r"\bpickoff attempt\b", re.I), PICKOFF_ATTEMPT_RE, False, 0.0),
    # Was 42.5% when the pattern demanded an explicit cause. DS-122 made the
    # cause optional and derives it from the plate appearance, taking this to
    # ~1%. The remainder are advances whose subject is elided across clauses
    # ("[R M advances on passed ball, steals 3rd, scores...]"), which needs
    # clause tracking rather than a better pattern.
    "advance": (
        re.compile(r"\badvances? to\b", re.I), ADVANCE_RE, False, 1.5),
    "scored": (
        re.compile(r"\bscores\b", re.I), SCORED_RE, False, 0.8),
    "inning_header": (
        re.compile(r"^\s*(top|bottom)\s+\d", re.I), INNING_RE, True, 0.0),
    "batter": (
        re.compile(r"^[A-Z#]"), BATTER_RE, False, 4.0),
    "score_line": (
        re.compile(r"^[A-Z0-9]{2,6}\s+\d+\s*-\s*[A-Z0-9]{2,6}\s+\d+", re.I),
        SCORE_RE, False, 0.0),
    "outs_line": (
        re.compile(r"^\d\s+Outs?\s*$", re.I), OUTS_RE, True, 0.0),
}


def _corpus():
    paths = []
    for root in CORPUS_ROOTS:
        if not os.path.isdir(root):
            continue
        for dirpath, _, files in os.walk(root):
            for f in files:
                if f.startswith("~$"):
                    continue
                if any(glob.fnmatch.fnmatch(f, p) for p in CORPUS_GLOBS):
                    paths.append(os.path.join(dirpath, f))

    seen, out = set(), []
    for p in sorted(set(paths)):
        try:
            if p.lower().endswith(".docx"):
                from docx import Document
                lines = [x.text.replace("\xa0", " ").strip()
                         for x in Document(p).paragraphs if x.text.strip()]
            elif p.lower().endswith((".pdf", ".csv", ".xlsx", ".png", ".jpg")):
                continue
            else:
                lines = [l.strip() for l in open(
                    p, encoding="utf-8", errors="ignore").read().splitlines()
                    if l.strip()]
        except Exception:
            continue
        if not lines:
            continue
        key = (len(lines), lines[0][:60])       # the same export saved twice
        if key in seen:
            continue
        seen.add(key)
        out.append((os.path.basename(p), lines))
    return out


def _skip_line(name, line):
    if name == "scored" and line.upper().startswith("FINAL"):
        return True                              # "FINAL SCORE: …"
    if name == "batter":
        return (not BATTER_VERBS.search(line)
                or line.strip() in RESULT_TYPES
                or bool(PITCH_ONLY.match(line))
                or bool(NUMBERED_BATTER.match(line)))
    return False


@pytest.fixture(scope="module")
def corpus():
    c = _corpus()
    if len(c) < 10:
        pytest.skip("play-by-play corpus not present on this machine")
    return c


@pytest.mark.parametrize("name", sorted(CHECKS))
def test_pattern_does_not_silently_miss_more_than_baseline(name, corpus):
    trigger, full, anchored, max_rate = CHECKS[name]
    misses = []
    triggered = 0
    for label, lines in corpus:
        for line in lines:
            if not trigger.search(line) or _skip_line(name, line):
                continue
            triggered += 1
            hit = full.match(line) if anchored else full.search(line)
            if not hit:
                misses.append((label, line))

    assert triggered, f"{name}: trigger never fired — the corpus or the " \
                      f"trigger is wrong, so this check proves nothing"

    rate = 100.0 * len(misses) / triggered
    if rate > max_rate:
        by_file = collections.Counter(f for f, _ in misses)
        sample = "\n".join(f"    {l[:120]}" for _, l in misses[:5])
        pytest.fail(
            f"{name}: {len(misses)} silent misses out of {triggered} "
            f"triggers = {rate:.2f}% (ceiling {max_rate}%).\n"
            f"  These lines are about a {name} and the parser did not "
            f"understand them, without recording that it failed.\n"
            f"  worst files: {by_file.most_common(3)}\n{sample}")
