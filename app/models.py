from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator


class WorkRef(BaseModel):
    """A research output and the DOIs it currently owns."""

    id: int
    dois: list[str]


class ImportWorkIn(BaseModel):
    work_id: int = Field(gt=0)
    dois: list[str] = Field(min_length=1)


class ImportCitationIn(BaseModel):
    citing: str = Field(min_length=1)
    cited: str = Field(min_length=1)


class ImportRequest(BaseModel):
    works: list[ImportWorkIn] = Field(default_factory=list)
    citations: list[ImportCitationIn] = Field(default_factory=list)

    @model_validator(mode="after")
    def _non_empty(self) -> "ImportRequest":
        if not self.works and not self.citations:
            raise ValueError("import must contain works or citations")
        return self


class ImportResult(BaseModel):
    version: int
    works: int
    dois: int
    citations: int
    internal_citations_ignored: int
    duplicate_citations_ignored: int
    index_edges: int


class RevisionIn(BaseModel):
    """A librarian submission: the version being looked at plus DOI -> work
    assignments that correct the grouping."""

    version: int = Field(ge=1)
    assignments: dict[str, int] = Field(min_length=1)

    @field_validator("assignments")
    @classmethod
    def _positive_work_ids_and_dois(cls, v: dict[str, int]) -> dict[str, int]:
        for raw_doi, work_id in v.items():
            if not raw_doi.strip():
                raise ValueError("DOI keys must not be blank")
            if work_id <= 0:
                raise ValueError(f"work id for DOI {raw_doi!r} must be positive")
        return v


class RevisionResult(BaseModel):
    version: int
    dois_reassigned: int
    works_affected: int
    citations_added: int
    citations_removed: int
    reindexed: bool


class CitationEdge(BaseModel):
    work: WorkRef
    doi_count: int


class CitationResponse(BaseModel):
    version: int
    direction: str
    work: WorkRef
    total_works: int
    total_doi_citations: int
    results: list[CitationEdge]


class WorkDetail(BaseModel):
    id: int
    dois: list[str]


class ReportBaselineIn(BaseModel):
    """A librarian's baseline registration: the report number plus one
    representative DOI (already imported) of the output being reported on."""

    report_no: str = Field(min_length=1, max_length=200)
    doi: str = Field(min_length=1)


class ReportBaseline(BaseModel):
    """The stored baseline for one report number.

    ``work_id`` is the output the representative DOI belonged to at
    registration; ``citing_works`` is the deduplicated set of outputs citing
    it (internal citations excluded) under the grouping of
    ``baseline_version``; ``max_citation_id`` is the boundary of the raw
    citation range that existed at registration.
    """

    report_no: str
    representative_doi: str
    work_id: int
    baseline_version: int
    max_citation_id: int
    citing_works: list[int]
    citation_count: int
    created: bool


class ReviewCounts(BaseModel):
    """The three statistics of a review, each a count of distinct citing
    works (internal citations excluded):

    * ``baseline`` -- as registered, under the baseline version's grouping;
    * ``registration_recount`` -- only citations that already existed at
      registration, recounted under the current grouping;
    * ``full_recount`` -- all citations, recounted under the current grouping.
    """

    baseline: int
    registration_recount: int
    full_recount: int


class CitingWorkDiff(BaseModel):
    """Citing outputs present in one statistic but not the other."""

    added: list[int]
    removed: list[int]


class ReportReview(BaseModel):
    """Baseline recounted against the current dataset.

    ``regrouping`` is baseline vs. the registration-range recount (same
    citations, different grouping); ``backfill`` is the registration-range
    recount vs. the full recount (same grouping, more citations).
    """

    report_no: str
    representative_doi: str
    work_id: int
    work_dois: list[str]
    baseline_version: int
    current_version: int
    counts: ReviewCounts
    regrouping: CitingWorkDiff
    backfill: CitingWorkDiff
