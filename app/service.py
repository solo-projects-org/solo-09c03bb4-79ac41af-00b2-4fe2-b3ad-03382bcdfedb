"""Core domain logic: import, correction (regrouping) and citation lookup.

Data model
----------
* ``works``          -- one row per research output (integer id).
* ``work_dois``      -- DOI -> work mapping.  A DOI belongs to exactly one work;
                        a work may own several DOIs (preprint + journal version).
* ``citations``      -- raw DOI-level directed citation edges, unique on
                        (citing_doi, cited_doi).  Corrections never rewrite these.
* ``citation_index`` -- materialised work->work citation edges with ``doi_count``
                        = number of distinct raw DOI edges that collapse onto
                        that work-level edge.  Internal citations (both ends in
                        the same work) never appear here.

Both citation directions read the same index table in opposite directions.

Versioning
----------
``meta.current_version`` is a monotonically increasing integer.  Each accepted
mutation (import or correction) bumps it and commits together with its writes.
A correction carries the version the librarian was looking at; if a concurrent
correction (or import) committed first, the stale submission is rejected with
HTTP 409 and leaves both the grouping and the index untouched.

Report baselines
----------------
``report_baselines`` pins one report number to a representative DOI: the
dataset version, the deduplicated citing-work set and the raw-citation range
boundary at registration, all read from one serialised transaction.  Raw
citations are never copied.  A review resolves the DOI's work in the *current*
version and recounts from the raw citations twice -- once limited to the
registration-time range, once over everything -- so the drift since
registration splits into a regrouping part and a backfill part.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Iterable

from .db import get_version, set_version, write_transaction
from .models import (
    CitationEdge,
    CitationResponse,
    CitingWorkDiff,
    ImportRequest,
    ImportResult,
    ReportBaseline,
    ReportBaselineIn,
    ReportReview,
    ReviewCounts,
    RevisionIn,
    RevisionResult,
    WorkDetail,
    WorkRef,
)

# SQLite's default limit is 999 bound parameters; stay well below it.
CHUNK = 500


class NotFound(Exception):
    """Requested work or report baseline does not exist."""


class StaleVersion(Exception):
    """The submitted version is no longer current."""

    def __init__(self, current: int):
        super().__init__(f"current version is {current}")
        self.current = current


class InvalidRequest(Exception):
    """The request cannot be applied as submitted."""


class BaselineConflict(Exception):
    """The report number is already registered with a different DOI."""

    def __init__(self, report_no: str, existing_doi: str):
        super().__init__(
            f"report {report_no!r} is already registered with "
            f"representative DOI {existing_doi!r}"
        )
        self.report_no = report_no
        self.existing_doi = existing_doi


@dataclass(frozen=True)
class _Move:
    doi: str
    old_work: str | None
    new_work: str


def _norm(doi: str) -> str:
    return doi.strip()


def _chunks(items: list, size: int = CHUNK) -> Iterable[list]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def import_batch(conn: sqlite3.Connection, payload: ImportRequest) -> ImportResult:
    """Import works and directed DOI citations atomically.

    A DOI may be assigned to only one work in a batch; re-importing a DOI
    updates its work.  Citations may reference DOIs from this batch or ones
    already imported.  Citations whose two DOIs fall in the same work are
    internal: stored but never indexed.  Repeating an existing citation is a
    no-op.
    """
    doi_to_work: dict[str, int] = {}
    for work in payload.works:
        for raw_doi in work.dois:
            doi = _norm(raw_doi)
            if not doi:
                raise InvalidRequest("DOI must not be blank")
            previous = doi_to_work.get(doi)
            if previous is not None and previous != work.work_id:
                raise InvalidRequest(
                    f"DOI {doi!r} is assigned to both work {previous} "
                    f"and work {work.work_id} in this import"
                )
            doi_to_work[doi] = work.work_id

    edges: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    raw_duplicates = 0
    for citation in payload.citations:
        citing, cited = _norm(citation.citing), _norm(citation.cited)
        if not citing or not cited:
            raise InvalidRequest("citation DOIs must not be blank")
        key = (citing, cited)
        if key in seen:
            raw_duplicates += 1
            continue
        seen.add(key)
        edges.append(key)

    with write_transaction(conn):
        work_keys = {str(w.work_id) for w in payload.works}
        conn.executemany(
            "INSERT OR IGNORE INTO works(work_key) VALUES (?)",
            [(key,) for key in work_keys],
        )
        conn.executemany(
            "INSERT INTO work_dois(doi, work_key) VALUES (?, ?) "
            "ON CONFLICT(doi) DO UPDATE SET work_key = excluded.work_key",
            [(doi, str(work_id)) for doi, work_id in doi_to_work.items()],
        )

        # Effective mapping after this import's upserts.
        effective = _load_doi_map(conn, [d for pair in edges for d in pair])
        for citing, cited in edges:
            if citing not in effective:
                raise InvalidRequest(
                    f"citation DOI {citing!r} has no work assignment; "
                    "import its provenance first"
                )
            if cited not in effective:
                raise InvalidRequest(
                    f"citation DOI {cited!r} has no work assignment; "
                    "import its provenance first"
                )

        inserted = 0
        for part in _chunks(edges):
            before = conn.total_changes
            conn.executemany(
                "INSERT OR IGNORE INTO citations(citing_doi, cited_doi) VALUES (?, ?)",
                part,
            )
            inserted += conn.total_changes - before

        internal = sum(
            1 for citing, cited in edges if effective[citing] == effective[cited]
        )

        _rebuild_index(conn)

        # A re-import may have moved the last DOI out of a work.
        conn.execute(
            "DELETE FROM works WHERE NOT EXISTS "
            "(SELECT 1 FROM work_dois WHERE work_dois.work_key = works.work_key)"
        )

        new_version = (get_version(conn) or 0) + 1
        set_version(conn, new_version)
        index_edges = conn.execute(
            "SELECT COUNT(*) AS n FROM citation_index"
        ).fetchone()["n"]

    return ImportResult(
        version=new_version,
        works=len(work_keys),
        dois=len(doi_to_work),
        citations=inserted,
        internal_citations_ignored=internal,
        duplicate_citations_ignored=raw_duplicates + (len(edges) - inserted),
        index_edges=index_edges,
    )


def _rebuild_index(conn: sqlite3.Connection) -> None:
    """Full rebuild -- used by import only."""
    conn.execute("DELETE FROM citation_index")
    conn.execute(
        """
        INSERT INTO citation_index(citing_work, cited_work, doi_count)
        SELECT citer.work_key, cited.work_key, COUNT(*)
        FROM citations c
        JOIN work_dois citer ON citer.doi = c.citing_doi
        JOIN work_dois cited ON cited.doi = c.cited_doi
        WHERE citer.work_key <> cited.work_key
        GROUP BY citer.work_key, cited.work_key
        """
    )


def _load_doi_map(conn: sqlite3.Connection, dois: Iterable[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    unique = sorted(set(dois))
    for part in _chunks(unique):
        marks = ",".join("?" * len(part))
        for row in conn.execute(
            f"SELECT doi, work_key FROM work_dois WHERE doi IN ({marks})", part
        ):
            mapping[row["doi"]] = row["work_key"]
    return mapping


# ---------------------------------------------------------------------------
# Correction
# ---------------------------------------------------------------------------


def apply_revision(conn: sqlite3.Connection, payload: RevisionIn) -> RevisionResult:
    """Correct the DOI -> work grouping and update the index incrementally.

    Raw citations are read only for the DOIs being regrouped (the only edges
    whose work-level grouping can change).  For each such citation the old
    work-level edge (pre-submission grouping) is decremented and the new edge
    (grouping after the whole submission) is incremented, so a citation whose
    two ends are both regrouped in this batch is counted once, under the new
    works.
    """
    assignments: dict[str, int] = {}
    for raw_doi, work_id in payload.assignments.items():
        doi = _norm(raw_doi)
        if not doi:
            raise InvalidRequest("DOI must not be blank")
        if doi in assignments and assignments[doi] != work_id:
            raise InvalidRequest(f"DOI {doi!r} assigned twice in one submission")
        assignments[doi] = work_id

    with write_transaction(conn):
        current = get_version(conn) or 0
        if payload.version != current:
            # Rejected submissions change nothing, including the version.
            raise StaleVersion(current)

        old_map = _load_doi_map(conn, assignments.keys())
        unknown = sorted(doi for doi in assignments if doi not in old_map)
        if unknown:
            raise InvalidRequest(
                "cannot regroup DOI(s) with no imported provenance: "
                + ", ".join(repr(doi) for doi in unknown)
                + "; import the DOI's ownership first"
            )

        moves = [
            _Move(doi=doi, old_work=old_map.get(doi), new_work=str(work_id))
            for doi, work_id in assignments.items()
            if old_map.get(doi) != str(work_id)
        ]

        if moves:
            conn.executemany(
                "INSERT OR IGNORE INTO works(work_key) VALUES (?)",
                [(move.new_work,) for move in moves],
            )
            conn.executemany(
                "INSERT INTO work_dois(doi, work_key) VALUES (?, ?) "
                "ON CONFLICT(doi) DO UPDATE SET work_key = excluded.work_key",
                [(move.doi, move.new_work) for move in moves],
            )

            deltas = _edge_deltas(conn, moves, {m.doi: m.old_work for m in moves})
            _apply_deltas(conn, deltas)

            # Works that no longer own a DOI are gone.
            conn.execute(
                "DELETE FROM works WHERE NOT EXISTS "
                "(SELECT 1 FROM work_dois WHERE work_dois.work_key = works.work_key)"
            )
        else:
            deltas = {}

        new_version = current + 1
        set_version(conn, new_version)

    added = sum(delta for delta in deltas.values() if delta > 0)
    removed = -sum(delta for delta in deltas.values() if delta < 0)
    affected = {m.old_work for m in moves if m.old_work} | {m.new_work for m in moves}
    return RevisionResult(
        version=new_version,
        dois_reassigned=len(moves),
        works_affected=len(affected),
        citations_added=added,
        citations_removed=removed,
        reindexed=bool(moves),
    )


def _edge_deltas(
    conn: sqlite3.Connection,
    moves: list[_Move],
    old_by_doi: dict[str, str | None],
) -> dict[tuple[str, str], int]:
    """Net index changes per work-level edge, from citations of moved DOIs.

    Must be called after ``work_dois`` has been updated, so the join yields the
    post-submission grouping.
    """
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS moved_dois(doi TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM moved_dois")
    conn.executemany(
        "INSERT OR IGNORE INTO moved_dois(doi) VALUES (?)", [(m.doi,) for m in moves]
    )

    rows = conn.execute(
        """
        SELECT c.citing_doi, c.cited_doi,
               citer.work_key AS citing_work, cited.work_key AS cited_work
        FROM citations c
        JOIN work_dois citer ON citer.doi = c.citing_doi
        JOIN work_dois cited ON cited.doi = c.cited_doi
        WHERE c.citing_doi IN (SELECT doi FROM moved_dois)
           OR c.cited_doi  IN (SELECT doi FROM moved_dois)
        """
    ).fetchall()

    deltas: dict[tuple[str, str], int] = {}

    def bump(edge: tuple[str, str] | None, by: int) -> None:
        if edge is None:
            return
        deltas[edge] = deltas.get(edge, 0) + by

    for row in rows:
        citing, cited = row["citing_doi"], row["cited_doi"]
        # Old grouping: moved DOIs take their pre-submission work; any other
        # DOI was not touched by this submission, so its current work is old.
        old_citing = old_by_doi.get(citing, row["citing_work"])
        old_cited = old_by_doi.get(cited, row["cited_work"])
        new_citing, new_cited = row["citing_work"], row["cited_work"]

        if old_citing is not None and old_cited is not None and old_citing != old_cited:
            bump((old_citing, old_cited), -1)
        if new_citing != new_cited:
            bump((new_citing, new_cited), 1)

    return {edge: delta for edge, delta in deltas.items() if delta != 0}


def _apply_deltas(conn: sqlite3.Connection, deltas: dict[tuple[str, str], int]) -> None:
    for (citing, cited), delta in deltas.items():
        row = conn.execute(
            "SELECT doi_count FROM citation_index "
            "WHERE citing_work = ? AND cited_work = ?",
            (citing, cited),
        ).fetchone()
        if row is None:
            if delta > 0:
                conn.execute(
                    "INSERT INTO citation_index(citing_work, cited_work, doi_count) "
                    "VALUES (?, ?, ?)",
                    (citing, cited, delta),
                )
            continue
        count = row["doi_count"] + delta
        if count < 0:
            raise RuntimeError(
                f"citation index underflow for {citing}->{cited}: {count}"
            )
        if count == 0:
            conn.execute(
                "DELETE FROM citation_index WHERE citing_work = ? AND cited_work = ?",
                (citing, cited),
            )
        else:
            conn.execute(
                "UPDATE citation_index SET doi_count = ? "
                "WHERE citing_work = ? AND cited_work = ?",
                (count, citing, cited),
            )


# ---------------------------------------------------------------------------
# Lookup (index only)
# ---------------------------------------------------------------------------


def _work_dois(conn: sqlite3.Connection, work_id: int) -> list[str]:
    return [
        row["doi"]
        for row in conn.execute(
            "SELECT doi FROM work_dois WHERE work_key = ? ORDER BY doi", (str(work_id),)
        )
    ]


def lookup(conn: sqlite3.Connection, work_id: int, direction: str) -> CitationResponse:
    """Read one direction of the citation index for ``work_id``.

    The caller must hold a read transaction so the index rows and the reported
    version come from the same committed snapshot.
    """
    dois = _work_dois(conn, work_id)
    if not dois:
        raise NotFound(f"work {work_id} not found")

    if direction == "references":
        column, other, label = "citing_work", "cited_work", "references"
    else:
        column, other, label = "cited_work", "citing_work", "cited_by"

    rows = conn.execute(
        f"SELECT {other} AS other_work, doi_count FROM citation_index "
        f"WHERE {column} = ? ORDER BY doi_count DESC, other_work",
        (str(work_id),),
    ).fetchall()

    results = [
        CitationEdge(
            work=WorkRef(
                id=int(row["other_work"]),
                dois=_work_dois(conn, int(row["other_work"])),
            ),
            doi_count=row["doi_count"],
        )
        for row in rows
    ]

    return CitationResponse(
        version=get_version(conn) or 0,
        direction=label,
        work=WorkRef(id=work_id, dois=dois),
        total_works=len(results),
        total_doi_citations=sum(edge.doi_count for edge in results),
        results=results,
    )


def work_detail(conn: sqlite3.Connection, work_id: int) -> WorkDetail:
    dois = _work_dois(conn, work_id)
    if not dois:
        raise NotFound(f"work {work_id} not found")
    return WorkDetail(id=work_id, dois=dois)


def current_version(conn: sqlite3.Connection) -> int:
    return get_version(conn) or 0


# ---------------------------------------------------------------------------
# Citation-report baselines (register + review)
# ---------------------------------------------------------------------------


def _citing_work_ids(
    conn: sqlite3.Connection, work_key: str, max_citation_id: int | None
) -> list[int]:
    """Distinct works with a raw citation into ``work_key``, internal excluded.

    Reads the raw citation table (not the index): a review recomputes from the
    immutable DOI-level records under the grouping of the current snapshot.
    ``max_citation_id`` restricts to the raw-citation range that existed at
    registration; ``None`` counts everything.
    """
    sql = (
        "SELECT DISTINCT citer.work_key AS citing_work "
        "FROM citations c "
        "JOIN work_dois citer ON citer.doi = c.citing_doi "
        "WHERE c.cited_doi IN (SELECT doi FROM work_dois WHERE work_key = ?) "
        "AND citer.work_key <> ?"
    )
    params: list = [work_key, work_key]
    if max_citation_id is not None:
        sql += " AND c.id <= ?"
        params.append(max_citation_id)
    return sorted(int(row["citing_work"]) for row in conn.execute(sql, params))


def register_baseline(
    conn: sqlite3.Connection, payload: ReportBaselineIn
) -> ReportBaseline:
    """Register (or reuse) the citation baseline for one report number.

    The dataset version, the citing-work set and the raw-citation range are
    read inside one serialised write transaction, so all three describe the
    same version.  Raw citations are not copied: only the id boundary of the
    range existing at registration is stored.  Any failure rolls the
    transaction back, so a failed registration leaves no baseline behind.
    Resubmitting the same report number with the same DOI returns the stored
    baseline unchanged; a different DOI for the same report number conflicts.
    """
    report_no = payload.report_no.strip()
    doi = _norm(payload.doi)
    if not report_no:
        raise InvalidRequest("report number must not be blank")
    if "/" in report_no:
        raise InvalidRequest("report number must not contain '/'")
    if not doi:
        raise InvalidRequest("DOI must not be blank")

    with write_transaction(conn):
        row = conn.execute(
            "SELECT * FROM report_baselines WHERE report_no = ?", (report_no,)
        ).fetchone()
        if row is not None:
            if row["representative_doi"] != doi:
                raise BaselineConflict(report_no, row["representative_doi"])
            return _baseline_from_row(row, created=False)

        work_row = conn.execute(
            "SELECT work_key FROM work_dois WHERE doi = ?", (doi,)
        ).fetchone()
        if work_row is None:
            raise InvalidRequest(
                f"representative DOI {doi!r} has no imported provenance; "
                "import it before registering a baseline"
            )
        work_key = work_row["work_key"]

        version = get_version(conn) or 0
        max_citation_id = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS m FROM citations"
        ).fetchone()["m"]
        citing = _citing_work_ids(conn, work_key, max_citation_id)

        conn.execute(
            "INSERT INTO report_baselines("
            "report_no, representative_doi, work_key, baseline_version, "
            "max_citation_id, citing_works) VALUES (?, ?, ?, ?, ?, ?)",
            (report_no, doi, work_key, version, max_citation_id, json.dumps(citing)),
        )

    return ReportBaseline(
        report_no=report_no,
        representative_doi=doi,
        work_id=int(work_key),
        baseline_version=version,
        max_citation_id=max_citation_id,
        citing_works=citing,
        citation_count=len(citing),
        created=True,
    )


def _baseline_from_row(row: sqlite3.Row, created: bool) -> ReportBaseline:
    citing = [int(work) for work in json.loads(row["citing_works"])]
    return ReportBaseline(
        report_no=row["report_no"],
        representative_doi=row["representative_doi"],
        work_id=int(row["work_key"]),
        baseline_version=row["baseline_version"],
        max_citation_id=row["max_citation_id"],
        citing_works=citing,
        citation_count=len(citing),
        created=created,
    )


def review_baseline(conn: sqlite3.Connection, report_no: str) -> ReportReview:
    """Recount a registered baseline against the current dataset.

    The caller holds one read transaction, so the current version, the
    representative DOI's current work and the raw citations all come from the
    same snapshot while imports and corrections keep running.  Three
    statistics are produced, each deduplicated per citing work with internal
    citations excluded:

    * the stored baseline (registration-time grouping and citation range);
    * a recount of the registration-time citation range under the *current*
      grouping -- its difference to the baseline is attributed to regrouping;
    * a recount of all citations under the current grouping -- its difference
      to the previous pass is attributed to backfilled citations.
    """
    report_no = report_no.strip()
    row = conn.execute(
        "SELECT * FROM report_baselines WHERE report_no = ?", (report_no,)
    ).fetchone()
    if row is None:
        raise NotFound(f"no baseline registered for report {report_no!r}")

    doi = row["representative_doi"]
    work_row = conn.execute(
        "SELECT work_key FROM work_dois WHERE doi = ?", (doi,)
    ).fetchone()
    if work_row is None:  # DOI rows are never deleted; guard anyway.
        raise RuntimeError(f"representative DOI {doi!r} lost its work assignment")
    work_key = work_row["work_key"]

    baseline = set(json.loads(row["citing_works"]))
    registration = set(_citing_work_ids(conn, work_key, row["max_citation_id"]))
    full = set(_citing_work_ids(conn, work_key, None))

    return ReportReview(
        report_no=row["report_no"],
        representative_doi=doi,
        work_id=int(work_key),
        work_dois=_work_dois(conn, int(work_key)),
        baseline_version=row["baseline_version"],
        current_version=get_version(conn) or 0,
        counts=ReviewCounts(
            baseline=len(baseline),
            registration_recount=len(registration),
            full_recount=len(full),
        ),
        regrouping=CitingWorkDiff(
            added=sorted(registration - baseline),
            removed=sorted(baseline - registration),
        ),
        backfill=CitingWorkDiff(
            added=sorted(full - registration),
            removed=sorted(registration - full),
        ),
    )
