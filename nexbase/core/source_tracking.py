"""Source tracking helper.

Every record carries ``source_type`` + ``source_priority``. This module
provides a small wrapper to keep those two fields consistent.
"""
from __future__ import annotations

from dataclasses import dataclass

from nexbase.core.enums import SourcePriority, SourceType


@dataclass(frozen=True)
class SourceInfo:
    source_type: SourceType
    source_priority: SourcePriority

    def as_dict(self) -> dict:
        return {
            "source_type": self.source_type.value,
            "source_priority": int(self.source_priority.value),
        }


ATS = SourceInfo(SourceType.ATS, SourcePriority.FIRST_PARTY)
JOB_BOARD = SourceInfo(SourceType.JOB_BOARD, SourcePriority.JOB_BOARD)
COMPANY_WEBSITE = SourceInfo(SourceType.COMPANY_WEBSITE, SourcePriority.PUBLIC_WEB)
PUBLIC_DIRECTORY = SourceInfo(SourceType.PUBLIC_DIRECTORY, SourcePriority.PUBLIC_WEB)
PUBLIC_WEB = SourceInfo(SourceType.PUBLIC_WEB, SourcePriority.PUBLIC_WEB)
ZOOMINFO = SourceInfo(SourceType.ZOOMINFO, SourcePriority.PAID_ENRICHMENT)
APOLLO = SourceInfo(SourceType.APOLLO, SourcePriority.PAID_ENRICHMENT)