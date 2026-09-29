"""Deterministic detectors: the half of design §7 the model does not get to do.

Stage 3 of the lifecycle is "reason", and §7.1 draws a hard line through it —
"the model proposes candidates; it does not get to grade them". A detector is
the grading half. It reads the behavioural record, applies rules a person can
argue with, and writes a candidate with the observation behind every number it
states. Nothing here calls an LLM, and nothing here writes to the home: a
candidate is an entry in core/suggestions.py, and the road from there to a
change in someone's house runs through the approval gate like every other write.

One module per suggestion type in §8, each split the way core/observer.py and
core/observation.py are split: pure rules that can be exercised against a list
literal, and a thin layer that fetches the series and stores the result.
"""
from core.detectors import schedule
from core.detectors import standby


# Every detector there is, and the one place the list is written down.
#
# It lived in core/review.py until the §9 rendering needed it, and moving it
# here changed nothing about decision 7 except who can ask: the pass still
# holds no per-kind table, and adding a detector is still adding it to one
# tuple. What it buys is that a module which has to READ a blob can find out
# which detector wrote it without importing the pass — core/artefacts.py
# cannot, since the pass imports core/verify.py and that imports the renderer.
#
# A tuple of MODULES rather than a table of settings, for review.py's reason:
# everything needed to run a detector and to read what it wrote is a fact
# about the detector and is already stated in the detector's own module —
# `KIND`, `LOOKBACK_DAYS`, `scan` and `EVIDENCE_FIELDS`. The order is the
# order the pass scans in and nothing else.
DETECTORS = (standby, schedule)

_BY_KIND = {detector.KIND: detector for detector in DETECTORS}


def for_kind(kind):
    """The detector that raises this kind of candidate, or None.

    None is a refusal and every caller treats it as one rather than falling
    back to the night. A kind with no detector here is a row written by a
    version of this agent that had one this build does not, and nothing may
    read a field off its blob by guessing the vocabulary — core/replay.py,
    core/verify.py and core/builder.py each refuse such a kind from their own
    end, and core/artefacts.py omits the clause it cannot name.
    """
    return _BY_KIND.get(str(kind or ""))
