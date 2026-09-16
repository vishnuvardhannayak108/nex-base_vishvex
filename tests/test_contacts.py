"""Contact extraction, the P1-P4 ladder, and ranking/selection."""
from __future__ import annotations

import pytest

from nexbase.contacts.extraction import (
    extract_contacts_from_html,
    infer_priority,
    is_role_email,
)
from nexbase.contacts.models import ContactCandidate
from nexbase.contacts.ranking import (
    ContactRanker,
    dominant_hiring_priority,
    rank_contacts,
    select_contacts,
)


# ---------------------------------------------------------------------------
# Priority ladder (mirrors the brief exactly)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "title,expected",
    [
        ("Owner", 1), ("CEO", 1), ("President", 1), ("Managing Partner", 1),
        ("Chief Executive Officer", 1), ("Founder & CEO", 1),
        ("COO", 2), ("VP Operations", 2), ("Director of Operations", 2),
        ("General Manager", 2),
        ("HR Director", 3), ("HR Manager", 3), ("Head of People", 3),
        ("Talent Acquisition Manager", 3), ("Director of Talent Acquisition", 3),
        ("Plant Manager", 4), ("Operations Manager", 4),
        ("Software Engineer", None), ("Welder", None), (None, None),
    ],
)
def test_priority_ladder(title, expected):
    assert infer_priority(title) == expected


def test_assistant_titles_are_not_decision_makers():
    assert infer_priority("Executive Assistant to the CEO") is None
    assert infer_priority("Owner Operator") is None  # a truck driver, not an owner


def test_role_mailboxes_identified():
    assert is_role_email("info@acme.com") is True
    assert is_role_email("careers@acme.com") is True
    assert is_role_email("jane.doe@acme.com") is False


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
JSONLD_PAGE = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Person","name":"Jane Whitfield",
 "jobTitle":"Chief Executive Officer","email":"jane@acme.com",
 "url":"https://linkedin.com/in/janewhitfield"}
</script>
</head><body><p>Team</p></body></html>
"""

VCARD_PAGE = """
<html><body>
<div class="vcard">
  <span class="fn">Marcus Reed</span>
  <span class="job-title">Director of Operations</span>
  <a class="u-email" href="mailto:m.reed@acme.com">email</a>
</div>
<div class="vcard">
  <span class="fn">Priya Raman</span>
  <span class="job-title">HR Manager</span>
</div>
</body></html>
"""


def test_jsonld_person_extracted():
    contacts = extract_contacts_from_html(JSONLD_PAGE, base_url="https://acme.com/team")
    assert len(contacts) == 1
    c = contacts[0]
    assert c.name == "Jane Whitfield"
    assert c.title_priority == 1
    assert c.email == "jane@acme.com"
    assert c.meaningful


def test_microformat_contacts_extracted():
    contacts = extract_contacts_from_html(VCARD_PAGE, base_url="https://acme.com/team")
    by_name = {c.name: c for c in contacts}
    assert by_name["Marcus Reed"].title_priority == 2
    assert by_name["Marcus Reed"].email == "m.reed@acme.com"
    assert by_name["Priya Raman"].title_priority == 3


def test_role_mailbox_never_attached_to_a_person():
    html = '<div class="vcard"><span class="fn">Sam Ortiz</span>' \
           '<span class="job-title">Owner</span>' \
           '<a class="u-email" href="mailto:info@acme.com">mail</a></div>'
    contact = extract_contacts_from_html(html)[0]
    assert contact.name == "Sam Ortiz"
    assert contact.email is None


def test_no_contacts_from_empty_or_garbage_html():
    assert extract_contacts_from_html("") == []
    assert extract_contacts_from_html(None) == []
    assert extract_contacts_from_html("<html><body>nothing here</body></html>") == []


def test_duplicate_person_merged_across_methods():
    html = JSONLD_PAGE + '<a href="https://linkedin.com/in/x">Jane Whitfield</a>'
    contacts = extract_contacts_from_html(html)
    assert len([c for c in contacts if c.name == "Jane Whitfield"]) == 1


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------
def _c(name, title, priority, stage="PUBLIC_WEB", email=None, confidence=0.8):
    return ContactCandidate(
        name=name, title=title, title_priority=priority,
        email=email, discovery_stage=stage, confidence=confidence,
    )


def test_rank_score_is_a_declared_field():
    """It used to be an undeclared ad-hoc attribute lost by asdict()."""
    from dataclasses import asdict, fields

    assert "rank_score" in {f.name for f in fields(ContactCandidate)}
    ranked = rank_contacts([_c("A", "CEO", 1)])
    assert asdict(ranked[0])["rank_score"] is not None


def test_priority_ladder_is_strict():
    candidates = [
        _c("D", "Plant Manager", 4), _c("B", "COO", 2),
        _c("A", "Owner", 1), _c("C", "HR Director", 3),
    ]
    assert [c.priority for c in rank_contacts(candidates)] == [1, 2, 3, 4]


def test_weak_contacts_never_padded_to_hit_quota(settings):
    """Brief: 'Do not force weak contacts simply to hit a quota.'"""
    candidates = [
        _c("Real Person", "Owner", 1),
        _c("No Title", None, None),
        _c("Also Untitled", "Warehouse Associate", None),
    ]
    selected = select_contacts(candidates, settings=settings)
    assert len(selected) == 1
    assert selected[0].name == "Real Person"


def test_returns_more_than_three_when_genuinely_meaningful(settings):
    """Brief says '3+', so three is a target, not a cap."""
    candidates = [
        _c("A", "Owner", 1), _c("B", "COO", 2), _c("C", "HR Director", 3),
        _c("D", "Plant Manager", 4), _c("E", "General Manager", 2),
    ]
    selected = select_contacts(candidates, settings=settings)
    assert len(selected) == 5
    assert len(selected) >= settings.contacts_target


def test_selection_respects_max(settings):
    candidates = [_c(f"P{i}", "Operations Manager", 4) for i in range(20)]
    assert len(select_contacts(candidates, settings=settings)) == settings.contacts_max


def test_hiring_relevance_breaks_ties(settings):
    ranker = ContactRanker(settings)
    candidates = [_c("Ops Person", "Director of Operations", 2),
                  _c("HR Person", "HR Director", 3)]
    selected = ranker.select(candidates, job_titles=["Operations Manager", "Plant Manager"])
    assert selected[0].name == "Ops Person"


def test_dominant_hiring_priority():
    assert dominant_hiring_priority(["Plant Manager", "HR Manager"]) == 3
    assert dominant_hiring_priority(["Welder", "Machinist"]) is None


def test_duplicate_people_collapsed(settings):
    candidates = [_c("Jane Doe", "CEO", 1), _c("jane doe", "Chief Executive", 1)]
    assert len(select_contacts(candidates, settings=settings)) == 1


# ---------------------------------------------------------------------------
# Table-based staff directories — the layout most SMB employers actually use.
# This markup shape returned ZERO contacts before the table extractor existed.
# ---------------------------------------------------------------------------
STAFF_TABLE = """
<html><body><table>
<tr><th>Name</th><th>Title</th><th>Ext</th><th>Phone</th><th>Email</th></tr>
<tr><td>Cynthia Slezak</td><td>CEO</td><td>136</td><td>234-200-0780</td>
    <td><a href="mailto:cslezak@rjscorp.com">cslezak@rjscorp.com</a></td></tr>
<tr><td>Tadd Schwarz</td><td>COO</td><td>201</td><td>234-200-8132</td>
    <td><a href="mailto:tschwarz@rjscorp.com">tschwarz@rjscorp.com</a></td></tr>
<tr><td>Jeff Bercsik</td><td>Director of Engineering</td><td>123</td><td>234-218-0300</td>
    <td><a href="mailto:jbercsik@rjscorp.com">jbercsik@rjscorp.com</a></td></tr>
</table></body></html>
"""


def test_staff_table_yields_named_decision_makers():
    contacts = extract_contacts_from_html(STAFF_TABLE, base_url="https://rjscorp.com/contact")
    by_name = {c.name: c for c in contacts if c.name}
    assert "Cynthia Slezak" in by_name
    assert by_name["Cynthia Slezak"].title_priority == 1
    assert by_name["Cynthia Slezak"].email == "cslezak@rjscorp.com"
    assert by_name["Tadd Schwarz"].title_priority == 2
    assert by_name["Tadd Schwarz"].email == "tschwarz@rjscorp.com"


def test_staff_table_ranks_ceo_first(settings):
    contacts = extract_contacts_from_html(STAFF_TABLE)
    selected = ContactRanker(settings).select(contacts, ["Plant Manager"])
    assert selected[0].name == "Cynthia Slezak"
    assert [c.priority for c in selected] == sorted(c.priority for c in selected)


def test_table_row_without_email_is_skipped():
    html = "<table><tr><td>Jane Doe</td><td>CEO</td></tr></table>"
    assert extract_contacts_from_html(html) == []


def test_extract_emails_captures_everything_observed():
    from nexbase.contacts.extraction import extract_emails_from_html

    emails = extract_emails_from_html(STAFF_TABLE)
    assert "cslezak@rjscorp.com" in emails
    assert "jbercsik@rjscorp.com" in emails
    assert len(emails) == 3


def test_extract_emails_never_invents():
    from nexbase.contacts.extraction import extract_emails_from_html

    assert extract_emails_from_html("<html><body>no addresses</body></html>") == []
    assert extract_emails_from_html(None) == []


def test_stacked_block_layout():
    html = """
    <div class="staff">
      <div class="member"><h3>Maria Gonzalez</h3><p>General Manager</p>
        <a href="mailto:mgonzalez@acme.com">Email</a></div>
    </div>
    """
    contacts = extract_contacts_from_html(html)
    named = [c for c in contacts if c.name == "Maria Gonzalez"]
    assert named
    assert named[0].title_priority == 2
    assert named[0].email == "mgonzalez@acme.com"


def test_person_name_heuristic():
    from nexbase.contacts.extraction import _looks_like_person_name

    assert _looks_like_person_name("Cynthia Slezak") is True
    assert _looks_like_person_name("Jeff Bercsik") is True
    assert _looks_like_person_name("CEO") is False           # a title
    assert _looks_like_person_name("234-200-0780") is False  # a phone number
    assert _looks_like_person_name("a@b.com") is False
    assert _looks_like_person_name("Director of Operations") is False
