"""Citation-report baselines: registration, idempotency, conflict, restart
persistence, and the review that splits drift into regrouping vs. backfill.

Run with:  python -m pytest -q
"""

from __future__ import annotations

from app import create_app
from app.db import db_session


def _seed(client):
    """Works 1={j,p}, 2={b}, 3={c}, 4={d} with six raw citations (one internal).

        10.1/b -> 10.1/j    2 -> 1
        10.1/c -> 10.1/p    3 -> 1
        10.1/d -> 10.1/b    4 -> 2
        10.1/j -> 10.1/b    1 -> 2
        10.1/p -> 10.1/d    1 -> 4
        10.1/j -> 10.1/p    internal to work 1
    """
    response = client.post(
        "/import",
        json={
            "works": [
                {"work_id": 1, "dois": ["10.1/j", "10.1/p"]},
                {"work_id": 2, "dois": ["10.1/b"]},
                {"work_id": 3, "dois": ["10.1/c"]},
                {"work_id": 4, "dois": ["10.1/d"]},
            ],
            "citations": [
                {"citing": "10.1/b", "cited": "10.1/j"},
                {"citing": "10.1/c", "cited": "10.1/p"},
                {"citing": "10.1/d", "cited": "10.1/b"},
                {"citing": "10.1/j", "cited": "10.1/b"},
                {"citing": "10.1/p", "cited": "10.1/d"},
                {"citing": "10.1/j", "cited": "10.1/p"},
            ],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["version"]


def _register(client, report_no="RPT-1", doi="10.1/j"):
    return client.post("/report-baselines", json={"report_no": report_no, "doi": doi})


def _review(client, report_no="RPT-1"):
    return client.get(f"/report-baselines/{report_no}/review")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_register_captures_version_citing_works_and_range(client):
    _seed(client)  # version 1, six raw citations
    response = _register(client)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["created"] is True
    assert body["report_no"] == "RPT-1"
    assert body["representative_doi"] == "10.1/j"
    assert body["work_id"] == 1
    assert body["baseline_version"] == 1
    assert body["max_citation_id"] == 6  # the whole raw range existed
    assert body["citing_works"] == [2, 3]  # deduplicated, internal excluded
    assert body["citation_count"] == 2


def test_register_dedupes_per_citing_work_and_excludes_internal(client):
    client.post(
        "/import",
        json={
            "works": [
                {"work_id": 1, "dois": ["10.2/j", "10.2/p"]},
                {"work_id": 2, "dois": ["10.2/b1", "10.2/b2"]},
            ],
            "citations": [
                {"citing": "10.2/b1", "cited": "10.2/j"},
                {"citing": "10.2/b2", "cited": "10.2/p"},  # same citing work
                {"citing": "10.2/j", "cited": "10.2/p"},  # internal to work 1
            ],
        },
    )
    body = _register(client, doi="10.2/j").json()
    assert body["citing_works"] == [2]
    assert body["citation_count"] == 1
    assert body["max_citation_id"] == 3


def test_register_is_idempotent_for_same_report_and_doi(client):
    _seed(client)
    first = _register(client)
    assert first.status_code == 201

    # Dataset moves on; the stored baseline must not.
    client.post(
        "/import",
        json={
            "works": [{"work_id": 5, "dois": ["10.1/e"]}],
            "citations": [{"citing": "10.1/e", "cited": "10.1/j"}],
        },
    )
    again = _register(client)
    assert again.status_code == 200, again.text
    body = again.json()
    assert body["created"] is False
    assert body["baseline_version"] == 1
    assert body["citing_works"] == [2, 3]
    assert body["max_citation_id"] == 6

    # Whitespace around the DOI is still the same DOI.
    padded = client.post(
        "/report-baselines", json={"report_no": "RPT-1", "doi": "  10.1/j  "}
    )
    assert padded.status_code == 200
    assert padded.json()["created"] is False


def test_same_report_with_different_doi_conflicts(client):
    _seed(client)
    assert _register(client, doi="10.1/j").status_code == 201

    conflict = _register(client, doi="10.1/p")
    assert conflict.status_code == 409
    body = conflict.json()
    assert body["representative_doi"] == "10.1/j"
    assert "RPT-1" in body["detail"]

    # The original baseline is untouched and still reviewable.
    review = _review(client)
    assert review.status_code == 200
    assert review.json()["representative_doi"] == "10.1/j"
    assert review.json()["counts"]["baseline"] == 2


def test_failed_registration_leaves_no_baseline(client, db_path):
    _seed(client)
    ghost = _register(client, report_no="RPT-X", doi="10.9/ghost")
    assert ghost.status_code == 422
    assert "10.9/ghost" in ghost.json()["detail"]

    # No row, and the report number is still free.
    with db_session(db_path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) AS n FROM report_baselines").fetchone()["n"]
            == 0
        )
    assert _review(client, "RPT-X").status_code == 404

    ok = _register(client, report_no="RPT-X", doi="10.1/j")
    assert ok.status_code == 201
    assert ok.json()["created"] is True


def test_register_rejects_bad_report_numbers(client):
    _seed(client)
    blank = client.post("/report-baselines", json={"report_no": "   ", "doi": "10.1/j"})
    assert blank.status_code == 422
    slash = client.post(
        "/report-baselines", json={"report_no": "2026/Q1", "doi": "10.1/j"}
    )
    assert slash.status_code == 422
    assert "/" in slash.json()["detail"]


def test_registration_and_review_do_not_bump_version(client):
    _seed(client)
    assert _register(client).status_code == 201
    assert client.get("/health").json()["version"] == 1
    assert _review(client).status_code == 200
    assert client.get("/health").json()["version"] == 1


def test_baseline_persists_across_restart(db_path):
    from fastapi.testclient import TestClient

    with TestClient(create_app(db_path)) as client1:
        _seed(client1)
        assert _register(client1).status_code == 201

    # A fresh app process on the same database file: the baseline survives.
    with TestClient(create_app(db_path)) as client2:
        review = _review(client2)
        assert review.status_code == 200
        body = review.json()
        assert body["baseline_version"] == 1
        assert body["counts"] == {
            "baseline": 2,
            "registration_recount": 2,
            "full_recount": 2,
        }


def test_concurrent_registration_same_report_one_wins(db_path):
    """Two registrations race on one report number with different DOIs."""
    import threading

    from fastapi.testclient import TestClient

    app = create_app(db_path)
    with TestClient(app) as client:
        _seed(client)
        barrier = threading.Barrier(2)
        outcomes: list[int] = []
        lock = threading.Lock()

        def attempt(doi: str) -> None:
            with TestClient(app) as thread_client:
                barrier.wait()
                r = thread_client.post(
                    "/report-baselines", json={"report_no": "RPT-R", "doi": doi}
                )
                with lock:
                    outcomes.append(r.status_code)

        threads = [
            threading.Thread(target=attempt, args=("10.1/j",)),
            threading.Thread(target=attempt, args=("10.1/p",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sorted(outcomes) == [201, 409], outcomes
        review = client.get("/report-baselines/RPT-R/review")
        assert review.status_code == 200


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------


def test_review_unknown_report_is_404(client):
    assert _review(client, "RPT-NONE").status_code == 404


def test_review_counts_only_citations_received_by_the_work(client):
    _seed(client)
    # Work 2 receives from works 1 and 4; its own outgoing edge (2 -> 1) and
    # citations between other works must not leak in.
    assert _register(client, report_no="RPT-2", doi="10.1/b").json()[
        "citing_works"
    ] == [1, 4]
    review = _review(client, "RPT-2").json()
    assert review["work_id"] == 2
    assert review["work_dois"] == ["10.1/b"]
    assert review["counts"] == {
        "baseline": 2,
        "registration_recount": 2,
        "full_recount": 2,
    }
    assert review["regrouping"] == {"added": [], "removed": []}
    assert review["backfill"] == {"added": [], "removed": []}


def test_review_with_only_backfill(client):
    _seed(client)
    _register(client)
    client.post(
        "/import",
        json={
            "works": [{"work_id": 5, "dois": ["10.1/e"]}],
            "citations": [{"citing": "10.1/e", "cited": "10.1/j"}],
        },
    )
    review = _review(client).json()
    assert review["baseline_version"] == 1
    assert review["current_version"] == 2
    assert review["counts"] == {
        "baseline": 2,
        "registration_recount": 2,
        "full_recount": 3,
    }
    assert review["regrouping"] == {"added": [], "removed": []}
    assert review["backfill"] == {"added": [5], "removed": []}


def test_review_with_only_regrouping(client):
    """The representative DOI itself is split out into a new work."""
    _seed(client)
    _register(client, doi="10.1/p")  # work 1 = {j, p}, citing works {2, 3}
    client.post("/revisions", json={"version": 1, "assignments": {"10.1/p": 5}})

    review = _review(client).json()
    assert review["work_id"] == 5  # the DOI's current work
    assert review["work_dois"] == ["10.1/p"]
    assert review["baseline_version"] == 1
    assert review["current_version"] == 2
    # Under the new grouping, work 5 receives c -> p (work 3) and j -> p
    # (work 1, no longer internal).  Work 2's b -> j no longer reaches it.
    assert review["counts"] == {
        "baseline": 2,
        "registration_recount": 2,
        "full_recount": 2,
    }
    # Totals are equal; the added/removed lists still show the churn.
    assert review["regrouping"] == {"added": [1], "removed": [2]}
    assert review["backfill"] == {"added": [], "removed": []}


def test_review_where_regrouping_and_backfill_cancel_out(client):
    """Regrouping removes one citing work, backfill adds another: the headline
    count returns to the baseline number, and only the added/removed lists
    reveal what happened."""
    client.post(
        "/import",
        json={
            "works": [
                {"work_id": 1, "dois": ["10.3/j", "10.3/p"]},
                {"work_id": 2, "dois": ["10.3/b"]},
                {"work_id": 3, "dois": ["10.3/c"]},
            ],
            "citations": [
                {"citing": "10.3/b", "cited": "10.3/j"},  # 2 -> 1
                {"citing": "10.3/c", "cited": "10.3/p"},  # 3 -> 1
            ],
        },
    )
    baseline = _register(client, doi="10.3/j").json()
    assert baseline["citing_works"] == [2, 3]
    assert baseline["baseline_version"] == 1

    # Regrouping: 10.3/c joins work 1, so c -> p becomes internal (-1 work).
    r = client.post("/revisions", json={"version": 1, "assignments": {"10.3/c": 1}})
    assert r.status_code == 201, r.text
    # Backfill: a brand-new citation from work 5 (+1 work).
    client.post(
        "/import",
        json={
            "works": [{"work_id": 5, "dois": ["10.3/e"]}],
            "citations": [{"citing": "10.3/e", "cited": "10.3/j"}],
        },
    )

    review = _review(client).json()
    assert review["baseline_version"] == 1
    assert review["current_version"] == 3
    assert review["counts"] == {
        "baseline": 2,
        "registration_recount": 1,  # work 3's citation turned internal
        "full_recount": 2,  # backfilled work 5 restores the headline number
    }
    assert review["counts"]["baseline"] == review["counts"]["full_recount"]
    assert review["regrouping"] == {"added": [], "removed": [3]}
    assert review["backfill"] == {"added": [5], "removed": []}


def test_review_reads_one_snapshot_while_writes_continue(client, db_path):
    """Imports and corrections keep working around a review; the review sees
    one consistent snapshot of the current version."""
    _seed(client)
    _register(client)
    client.post(
        "/import",
        json={
            "works": [{"work_id": 5, "dois": ["10.1/e"]}],
            "citations": [{"citing": "10.1/e", "cited": "10.1/j"}],
        },
    )
    client.post("/revisions", json={"version": 2, "assignments": {"10.1/p": 7}})

    review = _review(client).json()
    assert review["current_version"] == 3
    # Work 1 = {j} now; registration-range citations into it: b -> j only.
    assert review["counts"] == {
        "baseline": 2,
        "registration_recount": 1,
        "full_recount": 2,
    }
    assert review["regrouping"] == {"added": [], "removed": [3]}
    assert review["backfill"] == {"added": [5], "removed": []}
    # The preprint's citations moved to work 7 along with the DOI.
    assert client.get("/works/7/cited-by").json()["total_works"] == 2
