"""Rows the retired claim audit wrote, planted as a store from before 2.23.0
holds them.

`suggestions.mark_audited` was the audited analyst run's entry to `validated`
— a config, a `checked` / `untested` label and the recounted claims — and went
with that run (free-hand F-9). Its rows did not: a proposal the household
applied under it is still verified, offered for rollback and rendered with its
label. Tests of that rendering and of the review's withdrawal of the rest need
such a row, and nothing in the code writes one any more, so this writes it the
way `mark_audited` did — the same columns, from `candidate` only.
"""
from core import suggestions


def plant_audited(suggestion_id, config, evidence_level="untested", claims=()):
    """What `mark_audited` stored: `validated`, the label and the claims, no
    replay and no estimate. True when the row was at `candidate`."""
    conn = suggestions._conn()
    suggestions._begin(conn)
    moved = conn.execute(
        "UPDATE suggestions SET state = 'validated', updated_at = ?, config = ?,"
        " evidence_level = ?, claims = ?, replay = NULL, estimate = NULL"
        " WHERE id = ? AND state = 'candidate' AND source = ?",
        (suggestions._now(), suggestions._dump(config), evidence_level,
         suggestions._dump(list(claims)) if claims else None, int(suggestion_id),
         suggestions.SOURCE_ANALYST),
    ).rowcount
    conn.commit()
    return moved == 1
