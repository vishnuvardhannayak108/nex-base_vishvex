"""Canonical enumerations used across the pipeline.

These are the single source of truth for source types, source priorities,
qualification status, contact priority and verification status.
"""
from __future__ import annotations

from enum import Enum, IntEnum


class SourceType(str, Enum):
    ATS = "ATS"
    JOB_BOARD = "JOB_BOARD"
    COMPANY_WEBSITE = "COMPANY_WEBSITE"
    PUBLIC_DIRECTORY = "PUBLIC_DIRECTORY"
    PUBLIC_WEB = "PUBLIC_WEB"
    APOLLO = "APOLLO"
    ZOOMINFO = "ZOOMINFO"
    ZEROBOUNCE = "ZEROBOUNCE"
    MANUAL = "MANUAL"
    UNKNOWN = "UNKNOWN"


class SourcePriority(IntEnum):
    FIRST_PARTY = 1
    JOB_BOARD = 2
    PUBLIC_WEB = 3
    PAID_ENRICHMENT = 4


class EmailStatus(str, Enum):
    """What public discovery actually found for a company.

    A lead is never hidden for having only a role mailbox - that is still a
    reachable, explicitly observed address, and the operator decides whether to
    use it.
    """

    PERSONAL_EMAIL_FOUND = "PERSONAL_EMAIL_FOUND"
    ROLE_EMAIL_FOUND = "ROLE_EMAIL_FOUND"
    MULTIPLE_EMAILS_FOUND = "MULTIPLE_EMAILS_FOUND"
    #: Only portal-generated, external or domain-mismatched addresses were seen:
    #: kept as evidence, not the employer's own mailbox.
    LOW_CONFIDENCE_EMAIL_FOUND = "LOW_CONFIDENCE_EMAIL_FOUND"
    NO_PUBLIC_EMAIL_FOUND = "NO_PUBLIC_EMAIL_FOUND"


class QualificationStatus(str, Enum):
    PENDING = "PENDING"
    QUALIFIED = "QUALIFIED"
    REJECTED = "REJECTED"
    # Brief: "Flag borderline cases for review", "unless manually approved".
    NEEDS_REVIEW = "NEEDS_REVIEW"


class ContactPriority(IntEnum):
    P1 = 1  # Owner, CEO, President, Managing Partner
    P2 = 2  # COO, VP Ops, Director of Ops, General Manager
    P3 = 3  # HR Director/Manager, Head of HR/People, TA Director/Manager
    P4 = 4  # Plant Manager, Operations Manager


class VerificationStatus(str, Enum):
    PENDING = "PENDING"
    VALID = "VALID"
    INVALID = "INVALID"
    RISKY = "RISKY"
    UNKNOWN = "UNKNOWN"


class DiscoveryStage(str, Enum):
    SAME_SOURCE = "SAME_SOURCE"
    OTHER_SOURCE = "OTHER_SOURCE"
    PUBLIC_WEB = "PUBLIC_WEB"
    ENRICHMENT = "ENRICHMENT"


class ClientIndustry(str, Enum):
    """Sectors a run may target (the user-selected Sector input)."""

    MANUFACTURING = "Manufacturing"
    CONSTRUCTION = "Construction"
    LOGISTICS_TRANSPORT = "Logistics & Transportation"
    WAREHOUSING_DISTRIBUTION = "Warehousing & Distribution"
    FOOD_BEVERAGE = "Food & Beverage Manufacturing"
    PLASTICS_RUBBER = "Plastics/Rubber"
    INDUSTRIAL_MACHINERY = "Industrial Equipment/Machinery"
    HOSPITALITY_HOTELS = "Hospitality/Hotels"
    RETAIL = "Retail"
    PROPERTY_REAL_ESTATE = "Property Management/Real Estate"

