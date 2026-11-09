"""Strict transport contract for research evidence, independent of predictions."""
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, AwareDatetime, HttpUrl, model_validator

Text = Annotated[str, Field(min_length=1, max_length=2000)]
Identifier = Annotated[str, Field(min_length=1, max_length=100, pattern=r'^[A-Za-z0-9_.:-]+$')]
Mode = Literal['full', 'lineup_check']
EvidenceStatus = Literal['reported', 'confirmed', 'conflicting', 'unknown']


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Source(StrictModel):
    id: Identifier
    url: HttpUrl
    publisher: Text | None = None
    title: Text | None = None
    published_at: AwareDatetime | None = None
    accessed_at: AwareDatetime
    kind: Literal['club', 'league', 'federation', 'media', 'other', 'unknown'] = 'unknown'


class Finding(StrictModel):
    category: Literal['news', 'injury', 'suspension', 'lineup', 'form', 'fixture_load', 'other']
    team: Text | None = None
    player: Text | None = None
    statement: Text
    verification_status: EvidenceStatus = 'unknown'
    observed_at: AwareDatetime | None = None
    source_ids: Annotated[list[Identifier], Field(max_length=10)] = Field(default_factory=list)


class Assessment(StrictModel):
    summary: Text | None = None
    verification_status: EvidenceStatus = 'unknown'
    source_ids: Annotated[list[Identifier], Field(max_length=10)] = Field(default_factory=list)


class LineupAssessment(Assessment):
    lineup_status: Literal['predicted', 'official', 'unknown'] = 'unknown'


class ResearchResult(StrictModel):
    schema_version: Literal['1.0']
    job_id: Identifier
    match_id: Identifier
    mode: Mode
    input_version: Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
    skill_name: Literal['football-match-intel']
    skill_version: Annotated[str, Field(min_length=1, max_length=80)]
    researched_at: AwareDatetime
    summary: Text | None = None
    sources: Annotated[list[Source], Field(max_length=40)] = Field(default_factory=list)
    findings: Annotated[list[Finding], Field(max_length=80)] = Field(default_factory=list)
    injury_assessment: Assessment = Field(default_factory=Assessment)
    lineup_assessment: LineupAssessment = Field(default_factory=LineupAssessment)
    risks: Annotated[list[Text], Field(max_length=30)] = Field(default_factory=list)
    limitations: Annotated[list[Text], Field(max_length=30)] = Field(default_factory=list)

    @model_validator(mode='after')
    def evidence_contract(self):
        ids = [source.id for source in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError('Source IDs must be unique')
        evidence = [*self.findings, self.injury_assessment, self.lineup_assessment]
        for item in evidence:
            if not set(item.source_ids) <= set(ids):
                raise ValueError('Evidence references an unknown source')
            if item.verification_status != 'unknown' and not item.source_ids:
                raise ValueError('Reported, confirmed and conflicting claims require sources')
        if self.lineup_assessment.lineup_status == 'official':
            official = {source.id for source in self.sources if source.kind in ('club', 'league', 'federation')}
            if (self.lineup_assessment.verification_status != 'confirmed'
                    or not official.intersection(self.lineup_assessment.source_ids)):
                raise ValueError('Official lineup requires a cited official source and confirmed status')
        future = datetime.now(timezone.utc) + timedelta(minutes=5)
        times = [self.researched_at, *(source.accessed_at for source in self.sources),
                 *(source.published_at for source in self.sources if source.published_at),
                 *(finding.observed_at for finding in self.findings if finding.observed_at)]
        if any(at > future for at in times):
            raise ValueError('Research evidence cannot be future-dated')
        return self
