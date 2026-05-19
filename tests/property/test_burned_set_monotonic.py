"""Property: burned-set only grows and never overlaps cover_domain_pool (spec A §13.3)."""
from hypothesis import given, settings
from hypothesis import strategies as st

from mthydra.controller.state.burned import is_burned, mark_burned
from mthydra.controller.state.cover_pool import add_candidate, list_by_state, attest_verified, assign_to_box
from mthydra.controller.state.db import connect
from mthydra.controller.state.ru_boxes import insert_box
from mthydra.controller.state.schema import apply_schema


_LABEL = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Nd")),
    min_size=3,
    max_size=12,
)
_DOMAINS = st.builds(lambda label: f"{label}.example", _LABEL).filter(
    lambda d: len(d) <= 32
)


@settings(max_examples=50, deadline=None)
@given(domains=st.lists(_DOMAINS, min_size=1, max_size=10, unique=True))
def test_burned_set_only_grows(tmp_path_factory, domains):
    db = tmp_path_factory.mktemp("prop") / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    burned_seen: set[str] = set()

    for i, d in enumerate(domains):
        add_candidate(conn, d, added_at="2026-05-18T00:00:00Z")
        attest_verified(conn, d, from_vantage="v", at="2026-05-18T00:00:01Z")
        box_id = f"box-{i}"
        insert_box(conn, box_id, "h", "fsn1", None, d, "img", "2026-05-18T00:00:02Z")
        assign_to_box(conn, d, box_id=box_id, at="2026-05-18T00:00:02Z")
        mark_burned(conn, d, "job2_kill", box_id, f"2026-05-18T00:00:{i:02d}Z", None)
        burned_seen.add(d)

        # Invariant 1: every previously-burned domain is still burned
        for prior in burned_seen:
            assert is_burned(conn, prior), f"domain {prior!r} disappeared from burned_domains"

        # Invariant 2: no burned domain appears in cover_domain_pool
        all_pool = (
            [r.domain for r in list_by_state(conn, "candidate_unverified")]
            + [r.domain for r in list_by_state(conn, "candidate_verified")]
            + [r.domain for r in list_by_state(conn, "in_use")]
        )
        assert burned_seen.isdisjoint(set(all_pool)), (
            f"burned domain(s) {burned_seen & set(all_pool)} found in cover_domain_pool"
        )

    conn.close()
