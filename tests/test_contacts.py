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
        ("Chief Executive Officer", 1), ("Founder & CEO", 1), ("Co-Owner", 1),
        ("President and COO", 1),                       # the most senior tier named
        ("COO", 2), ("VP Operations", 2), ("Director of Operations", 2),
        ("General Manager", 2), ("Chief Operating Officer", 2),
        ("Vice President of Operations", 2), ("VP, Operations", 2),
        ("SVP of Operations", 2), ("Sr. Director of Operations", 2),
        ("HR Director", 3), ("HR Manager", 3), ("Head of HR", 3), ("Head of People", 3),
        ("Human Resources Manager", 3), ("Talent Acquisition Manager", 3),
        ("Director of Talent Acquisition", 3), ("Talent Acquisition Coordinator", 3),
        ("Plant Manager", 4), ("Operations Manager", 4),
        # Substring matches that used to rank wrongly.
        ("Vice President of Sales", None),              # was P1 ("president")
        ("HR Coordinator", None),                       # was P2 ("coo")
        ("Product Owner", None), ("Principal Engineer", None),  # were P1
        # Not in the Master Plan's POC list.
        ("Managing Director", None), ("Founder", None), ("Warehouse Manager", None),
        ("Facilities Manager", None), ("HR Business Partner", None),
        ("Software Engineer", None), ("Welder", None), (None, None),
    ],
)
def test_priority_ladder(title, expected):
    assert infer_priority(title) == expected


def test_assistant_titles_are_not_decision_makers():
    assert infer_priority("Executive Assistant to the CEO") is None
    assert infer_priority("Owner Operator") is None  # a truck driver, not an owner
    assert infer_priority("Owner/Operator") is None
    assert infer_priority("Assistant Plant Manager") is None
    assert infer_priority("Former CEO") is None


@pytest.mark.parametrize("email,shared", [
    ("hr@acme.com", True), ("careers@acme.com", True), ("recruiting@acme.com", True),
    ("jobs@acme.com", True), ("info@acme.com", True), ("dispatch@acme.com", True),
    ("estimating@acme.com", True), ("orders@acme.com", True),
    ("jane.doe@acme.com", False), ("mreed@acme.com", False),
])
def test_shared_and_personal_mailboxes_are_told_apart(email, shared):
    assert is_role_email(email) is shared


def test_every_observed_email_is_classified_with_provenance():
    from nexbase.email.discovery import EmailDiscovery

    result = EmailDiscovery().discover(
        [{"name": "Jane Whitfield", "title": "CEO", "email": "jane@acme.com",
          "source_url": "https://acme.com/team", "discovery_stage": "PUBLIC_WEB"}],
        extra_emails=[
            {"email": "hr@acme.com", "evidence_url": "https://indeed.com/viewjob?jk=1",
             "source": "indeed", "discovery_stage": "SAME_SOURCE"},
            {"email": "mreed@acme.com", "evidence_url": "https://acme.com/contact",
             "discovery_stage": "PUBLIC_WEB"},
            {"email": "studio@webagency.com", "evidence_url": "https://acme.com/",
             "discovery_stage": "PUBLIC_WEB"},
        ],
        company_domain="acme.com",
    )
    kinds = {o.email: (o.email_class, o.contact_name, o.on_company_domain, o.evidence_url)
             for o in result.observed}
    assert kinds == {
        "jane@acme.com": ("PERSONAL", "Jane Whitfield", True, "https://acme.com/team"),
        "hr@acme.com": ("ROLE", None, True, "https://indeed.com/viewjob?jk=1"),
        # An individual mailbox on the employer's domain with no person beside it.
        "mreed@acme.com": ("PERSONAL", None, True, "https://acme.com/contact"),
        # A web agency's address on the employer's page: kept, low confidence.
        "studio@webagency.com": ("DOMAIN_MISMATCH", None, False, "https://acme.com/"),
    }
    assert result.role == ["hr@acme.com"]
    assert [o.contact_name for o in result.named] == ["Jane Whitfield"]


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


# ---------------------------------------------------------------------------
# Budgeted public page checks
# ---------------------------------------------------------------------------
def test_a_company_never_exceeds_its_page_budget(settings):
    from nexbase.contacts.discovery import ContactDiscovery
    from tests.test_pipeline_e2e import StubAccess

    settings.contacts_max_pages_per_company = 3
    settings.contacts_scan_subdomains = False
    access = StubAccess({}, settings=settings)
    discovery = ContactDiscovery(access=access, settings=settings)
    discovery._host_is_live = lambda base: True
    report = discovery.discover(
        "Acme", "https://acme.com",
        [f"https://www.indeed.com/viewjob?jk={n}" for n in range(5)],
        ["https://www.indeed.com/cmp/Acme"],
    )
    assert len(access.requested) == 3 == report.pages_fetched
    assert report.budget_exhausted is True


def test_profile_pages_come_only_from_urls_the_sources_published(settings):
    """A slug guessed from the name can be a different employer with the same name."""
    from nexbase.contacts.discovery import ContactDiscovery
    from tests.test_pipeline_e2e import StubAccess

    access = StubAccess({}, settings=settings)
    ContactDiscovery(access=access, settings=settings).discover(
        "Summit Construction", None, [], [])
    assert access.requested == []


def test_page_emails_carry_the_page_they_were_seen_on(settings):
    from nexbase.contacts.discovery import ContactDiscovery
    from tests.test_pipeline_e2e import StubAccess

    page = "<html><body><a href='mailto:hr@acme.com'>HR</a></body></html>"
    access = StubAccess({"indeed.com/viewjob": page}, settings=settings)
    report = ContactDiscovery(access=access, settings=settings).discover(
        "Acme", None, ["https://www.indeed.com/viewjob?jk=9"], [])
    assert report.page_emails == [{
        "email": "hr@acme.com", "source": "www.indeed.com", "source_type": "JOB_BOARD",
        "evidence_url": "https://www.indeed.com/viewjob?jk=9",
        "extraction_method": "page_markup", "discovery_stage": "SAME_SOURCE"}]


def test_the_same_page_is_fetched_once_per_company(settings):
    """Live smoke: the homepage and trailing-slash variants were fetched twice."""
    from nexbase.contacts.discovery import ContactDiscovery
    from tests.test_pipeline_e2e import StubAccess

    settings.contacts_scan_subdomains = False
    home = ("<html><body>" + "Acme builds things. " * 40 +
            "<a href='/leadership/'>Leadership</a><a href='/contact'>Contact</a></body></html>")
    access = StubAccess({"acme.com": home}, settings=settings)
    discovery = ContactDiscovery(access=access, settings=settings)
    discovery._host_is_live = lambda base: True
    discovery.discover("Acme", "https://acme.com", [], [])

    keys = [url.rstrip("/") for url in access.requested]
    assert len(keys) == len(set(keys)), sorted(keys)


def test_sitemap_lookups_cannot_overrun_the_page_budget(settings):
    """Live smoke: a sitemap index and its children took a company to 32 of 30 pages."""
    from nexbase.contacts.discovery import ContactDiscovery, ContactDiscoveryReport
    from tests.test_pipeline_e2e import StubAccess

    settings.contacts_max_pages_per_company = 2
    index = ('<sitemapindex><sitemap><loc>https://acme.com/a-sitemap.xml</loc></sitemap>'
             '<sitemap><loc>https://acme.com/b-sitemap.xml</loc></sitemap></sitemapindex>')
    access = StubAccess({"acme.com/sitemap.xml": index}, settings=settings)
    report = ContactDiscoveryReport(candidates=[], pages_fetched=1)
    ContactDiscovery(access=access, settings=settings)._sitemap_contact_pages(
        "https://acme.com", report)

    assert report.pages_fetched == 2 and len(access.requested) == 1
    assert report.budget_exhausted is True


# ---------------------------------------------------------------------------
# Phase 6.1: plain-text name/title pairs on employer-site people pages
# ---------------------------------------------------------------------------
def _pairs(html, plain_text=True):
    return [(c.name, c.title, c.title_priority)
            for c in extract_contacts_from_html(html, base_url="https://acme.com/leadership",
                                                plain_text=plain_text)]


LEADERSHIP_TEXT = """<html><head><title>Leadership Team | Acme</title></head><body>
<nav><a href="/">Home</a> Our Leadership</nav>
<h1>Our Leadership</h1>
<div><h3>Andy Dupuy</h3><p>Chief Executive Officer &amp; President</p></div>
<div><h3>Fred A. McManus</h3><p>Chief Operating Officer</p></div>
<div><h3>Mike Firmin</h3><p>President of Maintenance</p></div>
<p>Dana Reed \u2014 CEO &amp; President</p>
<p>Carl Ortiz, COO</p>
<p>Jane Smith<br>HR Director</p>
<p>Omar Haddad | Plant Manager</p>
<footer>Owner: Acme Holdings</footer>
</body></html>"""


def test_plain_text_pairs_in_the_supported_formats():
    assert _pairs(LEADERSHIP_TEXT) == [
        ("Andy Dupuy", "Chief Executive Officer & President", 1),
        ("Fred A. McManus", "Chief Operating Officer", 2),
        ("Dana Reed", "CEO & President", 1),
        ("Carl Ortiz", "COO", 2),
        ("Jane Smith", "HR Director", 3),
        ("Omar Haddad", "Plant Manager", 4),
    ]


def test_plain_text_is_read_only_when_the_caller_allows_it():
    """Job descriptions, directories and generic pages never set plain_text."""
    assert _pairs(LEADERSHIP_TEXT, plain_text=False) == []


def test_a_plain_text_contact_keeps_its_page_and_method():
    [candidate] = [c for c in extract_contacts_from_html(
        "<html><title>Team</title><body><p>Carl Ortiz, COO</p></body></html>",
        base_url="https://acme.com/our-team", discovery_stage="PUBLIC_WEB",
        source_type="COMPANY_WEBSITE", source_priority=3, plain_text=True)]
    assert candidate.url == candidate.raw["page_url"] == "https://acme.com/our-team"
    assert candidate.raw["extraction"] == "text"
    assert (candidate.discovery_stage, candidate.source_type) == ("PUBLIC_WEB", "COMPANY_WEBSITE")


@pytest.mark.parametrize("title", [
    "President of Maintenance", "Division President", "Regional President",
    "Group President", "President for North America", "President of Construction",
])
def test_division_presidents_are_not_p1(title):
    assert infer_priority(title) is None


@pytest.mark.parametrize("title,tier", [
    ("President", 1), ("CEO & President", 1), ("Owner & President", 1),
    ("President and COO", 1), ("CEO and President of Construction", 1),
])
def test_the_company_president_is_still_p1(title, tier):
    assert infer_priority(title) == tier


# Adversarial pages: every one mentions owner / president / HR / CEO in text
# that is not a person's name and title.
CLOUDFLARE_409 = """<html><head><title>DNS resolution error | www.acme.com | Cloudflare</title></head>
<body><h1>Error 1016</h1><p>Most likely:</p>
<p>if the owner just signed up for Cloudflare it can take a few minutes</p>
<p>What can I do?</p><p>If you are the owner of this website</p><p>Ray ID: 8c1d</p></body></html>"""

SOFT_404_WITHOUT_ERROR_TITLE = """<html><head><title>Acme Leadership</title></head><body>
<h2>Oops</h2><p>The page you were looking for, Our Owner, has moved.</p>
<p>Contact the Site Owner</p><p>President</p>
<p>Jane Smith is the HR Director of our sister company.</p>
<p>If you are the President, please log in.</p>
<p>HR</p><p>Human Resources</p></body></html>"""

JOB_AD_TEXT = """<html><head><title>About the role</title></head><body>
<p>Reports To: Plant Manager</p><p>Hiring Manager: Operations Manager</p>
<p>Department, HR Manager</p><p>Location: Toledo, Ohio</p>
<p>Great Benefits</p><p>General Manager</p><p>Apply Now</p><p>Owner</p></body></html>"""

HEADINGS_AND_ORGS = """<html><head><title>About Acme</title></head><body>
<div><h2>Meet The Team</h2><p>President</p></div><div><h3>Our Leadership</h3><p>Owner</p></div>
<div><h3>Brown Construction Services</h3><p>General Manager</p></div>
<p>Acme Holdings LLC, Owner</p><div><h3>Board Of Directors</h3><p>CEO</p></div>
<div><h3>President Of Maintenance</h3><p>Chief Executive Officer</p></div></body></html>"""
# Known limit: "<strong>Great Benefits</strong> <em>General Manager</em>" on a people
# page is structurally a person card; telling it apart would need a name dictionary.

LOOSE_PARAGRAPHS = """<html><head><title>About Acme</title></head><body>
<p>Great Benefits</p><p>General Manager</p><p>Jane Smith</p><p>HR Director</p></body></html>"""


@pytest.mark.parametrize("html", [CLOUDFLARE_409, SOFT_404_WITHOUT_ERROR_TITLE, JOB_AD_TEXT,
                                  HEADINGS_AND_ORGS, LOOSE_PARAGRAPHS])
def test_error_job_and_heading_text_never_becomes_a_contact(html):
    assert _pairs(html) == []


@pytest.mark.parametrize("page_title", [
    "Page Not Found | Acme", "404", "Access denied", "Just a moment...", "Attention Required! | Cloudflare",
])
def test_a_page_titled_as_an_error_is_not_read_even_with_a_clean_pair(page_title):
    html = f"<html><head><title>{page_title}</title></head><body><p>Carl Ortiz, COO</p></body></html>"
    assert _pairs(html) == []


@pytest.mark.parametrize("url,people", [
    ("https://acme.com/leadership", True), ("https://acme.com/about-us/", True),
    ("https://acme.com/our-team", True), ("https://acme.com/who-we-are", True),
    ("https://acme.com/company/management", True), ("https://acme.com/about/leadership", True),
    ("https://acme.com/", False), ("https://acme.com", False), ("https://acme.com/contact", False),
    ("https://acme.com/post/team-shoutout-city-awards", False),
    ("https://acme.com/blog/meet-the-team", False), ("https://acme.com/careers/about-the-role", False),
    ("https://acme.com/page-sitemap.xml", False), ("https://acme.com/services/steam-cleaning", False),
])
def test_people_page_classification(url, people):
    from nexbase.contacts.discovery import is_people_page

    assert is_people_page(url) is people


def test_text_pairs_are_read_only_on_employer_people_pages(settings):
    """Same text on a job page, a board profile, a blog post and the homepage: no contacts."""
    from nexbase.contacts.discovery import ContactDiscovery
    from tests.test_pipeline_e2e import StubAccess

    settings.contacts_scan_subdomains = False
    page = "<html><title>Acme</title><body><p>Carl Ortiz, COO</p></body></html>"
    access = StubAccess({
        "indeed.com/viewjob": page, "indeed.com/cmp/Acme": page,
        "acme.com/post/team-news": page, "acme.com/leadership": page,
    }, settings=settings)
    discovery = ContactDiscovery(access=access, settings=settings)
    discovery._host_is_live = lambda base: True
    discovery._links_from_homepage = lambda base, report, name: [
        "https://acme.com/post/team-news", "https://acme.com/leadership"]
    report = discovery.discover(
        "Acme", "https://acme.com", ["https://www.indeed.com/viewjob?jk=1"],
        ["https://www.indeed.com/cmp/Acme"])

    assert {c.name for c in report.candidates} == {"Carl Ortiz"}
    assert all(c.url.startswith("https://acme.com/") and "/post/" not in c.url
               and c.discovery_stage == "PUBLIC_WEB" for c in report.candidates)


def test_equivalent_urls_cost_one_fetch():
    from nexbase.contacts.discovery import ContactDiscoveryReport

    report = ContactDiscoveryReport(candidates=[])
    assert report.first_visit("https://www.acme.com/Leadership/")
    for same in ("http://acme.com/leadership", "https://ACME.com/leadership", "https://acme.com/leadership/"):
        assert report.first_visit(same) is False, same
    assert report.first_visit("https://acme.com/leadership?page=2")


def test_the_live_brown_and_root_card_markup_is_read():
    """Markup as served by brownandroot.com/leadership/ on 2026-09-17."""
    html = """<html><head><title>Leadership Team | Brown and Root</title></head><body>
    <a class="content-block type-team" href="/team/andy-dupuy/">
      <h2 class="entry-title h5">Andy Dupuy</h2>
      <p class="title-position">Chief Executive Officer &amp; President</p></a>
    <a class="content-block type-team" href="/team/fred-mcmanus/">
      <h2 class="entry-title h5">Fred A. McManus</h2>
      <p class="title-position">Chief Operating Officer</p></a>
    <a class="content-block type-team" href="/team/mike-firmin/">
      <h2 class="entry-title h5">Mike Firmin</h2>
      <p class="title-position">President of Maintenance</p></a></body></html>"""
    assert _pairs(html) == [("Andy Dupuy", "Chief Executive Officer & President", 1),
                            ("Fred A. McManus", "Chief Operating Officer", 2)]


def test_cards_nested_inside_an_unclosed_site_header_are_read():
    """brownandroot.com leaves <header class="site-header"> open around the whole page."""
    html = """<html><head><title>Leadership Team | Brown and Root</title></head><body>
    <header class="site-header"><nav><a href="/">Home</a><a href="/leadership/">Leadership</a></nav>
    <div class="team-page"><article class="team">
      <a class="content-block type-team" href="/leadership/andy-dupuy/">
        <h2 class="entry-title h5">Andy Dupuy</h2>
        <p class="title-position">Chief Executive Officer &amp; President</p></a>
    </article></div></body></html>"""
    assert _pairs(html) == [("Andy Dupuy", "Chief Executive Officer & President", 1)]


# ===========================================================================
# Live handler smoke 2026-09-17: percent-encoded mailto fragments
# ===========================================================================
def test_a_percent_encoded_mailto_fragment_is_not_an_address():
    """Greenhouse (Jack Morton) published a quoted domain notice as a mailto link."""
    from nexbase.core.models import extract_emails_from_text

    text = ("offers only come from an email address [\u201c@jackmorton.com]"
            "(mailto:%E2%80%9C@jackmorton.com)\u201d. Contact "
            "[jobs@jackmorton.com](mailto:jobs%40jackmorton.com)")
    assert extract_emails_from_text(text) == ["jobs@jackmorton.com"]


def test_email_discovery_rejects_percent_encoded_addresses():
    from nexbase.email.discovery import EmailDiscovery

    found = EmailDiscovery().discover([], extra_emails=[
        {"email": "%e2%80%9c@jackmorton.com", "source": "greenhouse"},
        {"email": "jobs@jackmorton.com", "source": "greenhouse"}], company_domain="jackmorton.com")
    assert [o.email for o in found.observed] == ["jobs@jackmorton.com"]


def test_markdown_escaped_addresses_are_read_whole_and_fragments_dropped():
    """Indeed Markdown: ``fcdi\\-hr@fujifilm.com``; JobSpy's column held ``-hr@fujifilm.com``."""
    from nexbase.core.models import RawJob, extract_emails_from_text

    text = r"direct your inquiries to our HR Department (fcdi\-hr@fujifilm.com)."
    assert extract_emails_from_text(text) == ["fcdi-hr@fujifilm.com"]
    job = RawJob(source_type="JOB_BOARD", source_priority=2, source_site="indeed",
                 external_id="1", title="Warehouse Associate", company_name="Fujifilm",
                 description=text, observed_emails=["-hr@fujifilm.com", "jobs.@x.com"])
    assert job.observed_emails == ["fcdi-hr@fujifilm.com"]
