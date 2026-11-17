"""Posting pages as four employer hosts serve them to a plain fetch, trimmed and anonymised: their structure only
(where the text sits, what sits beside it), with made-up words, a made-up employer and the reserved `.test` names.
`html_record` must find the whole posting in each by code (docs/PRD.md path 49): no network and no browser here."""

import html
import json

PARAS = [
    "You will design and run the programs that keep a large network of stores supplied, and you will carry each one "
    "from the first sketch to the day it ships. We work in small teams that own what they build.",
    "Key responsibilities include planning each quarter's work with partner teams, writing the plans down so that "
    "anyone can follow them, and reviewing the results with the people who depend on them every week.",
    "A day in the life starts with a short stand-up, goes on to design reviews and customer calls, and ends with "
    "time to think. About the team: we are twelve people across three cities and we answer to one director.",
]
QUALIFIED = [
    "- 3+ years of professional experience running programs of more than ten people across several teams",
    "- 2+ years of experience writing plans, budgets and reports that a leadership team signs off on",
]
PREFERRED = [
    "- Experience with supply planning, vendor contracts and the reporting that goes with them, in a large company",
    "- A record of teaching others how to do the same work, through written guides or in person",
]


def _paras(lines: list[str]) -> str:
    return "<br/><br/>".join(lines)


def store_page() -> str:
    """A server-rendered page whose posting is three `div.section` blocks in one `div.content`, behind a first, empty
    `div.section.description` and another `div.content` that is only the menu's."""
    return f"""<!DOCTYPE html><html lang="en"><head><title>Program Lead - Job ID: 4100 | Careers</title>
<script>window.config = {{"page": "job"}};</script></head>
<body>
<a class="sr-only" href="#main" id="skip">Skip to main content</a>
<div class="jobs-navbar"><div class="sb-slidebar" role="dialog"><div class="content"><ul class="nav-list">
<li><a href="/en">Home</a></li><li><a href="/en/teams">Teams</a></li><li><a href="/en/locations">Locations</a></li>
</ul></div></div></div>
<div class="day-one"><div id="main-content">
<div id="job-detail">
<div class="apply-header"><div class="info"><h1 class="title">Program Lead</h1>
<p class="meta">Job ID: 4100 | Brightwell Stores, Inc.</p></div>
<a id="apply-button" class="btn" href="https://careers.example.test/apply/4100">Apply now</a></div>
<div class="container" id="job-detail-body"><div class="row">
<div class="col-12 col-md-7"><div class="content">
<div class="section description"></div>
<div class="section"><h2>Description</h2><p>{_paras(PARAS)}</p></div>
<div class="section"><h2>Basic Qualifications</h2><p>{_paras(QUALIFIED)}</p></div>
<div class="section"><h2>Preferred Qualifications</h2><p>{_paras(PREFERRED)}</p></div>
</div></div>
<div class="col-12 col-md-5"><div class="sidebar"><h2 class="job-details-title">Job details</h2>
<ul class="associations"><li><div class="association"><ul class="association-content"><li>USA, WA, Seattle</li>
</ul></div></li></ul>
<div class="social-media"><h2 class="share-text">Share this job</h2><ul class="buttons">
<li><a class="button" href="https://share.example.test/?u=4100">Share</a></li></ul></div></div></div>
</div></div></div></div>
<footer><a href="/privacy">Privacy and Data</a><span>2026 Brightwell Stores, Inc. or its affiliates</span></footer>
</body></html>"""


def device_page() -> str:
    """A server-rendered page whose posting is several `article.article--details` blocks, the first a short table of
    facts (so the first `article` is not a whole posting), inside one `section`, in a `main`."""
    facts = "".join(f'<div class="field"><div class="label">{k}</div><div class="value">{v}</div></div>'
                    for k, v in [("Req #", "R-100200"), ("Career area:", "Operations"),
                                 ("Country/Region:", "Freedonia"),
                                 ("City:", "Springdale"), ("Date:", "Thursday, October 29, 2026"),
                                 ("Working time:", "Full-time"),
                                 ("Additional Locations:", "Freedonia - Central Region - Springdale; "
                                  "Freedonia - Central Region - Northfield; Freedonia - Coast - Harbortown")])
    why = ("We are a global technology company that does what it says and owns what it does. We serve millions of "
           "customers every day in many markets, and we build things that people use at home and at work.")
    body = "".join(f"<p>{p}</p>" for p in PARAS) + "<ul>" + "".join(f"<li>{q[2:]}</li>" for q in QUALIFIED) + "</ul>"
    return f"""<!DOCTYPE html><html lang="en"><head><title>Operations Specialist</title></head>
<body class="body body--job-details"><div class="body__content">
<header class="header"><div class="header__wrapper"><h1>Gadget Company</h1><nav class="nav" aria-label="Main">
<ul class="nav__list"><li class="nav__item"><a href="/search">Search jobs</a></li>
<li class="nav__item"><a href="/talent">Talent community</a></li></ul>
<ul class="portalLanguages"><li><a href="/de">Deutsch</a></li><li><a href="/fr">Francais</a></li></ul></nav>
</div></header>
<div class="banner"><h2 class="banner__title">Operations Specialist</h2></div>
<main class="main" id="main" role="main"><div class="main__wrapper"><div id="main-panel"><div class="grid">
<div class="grid__item"><section class="section js_views"><div class="section__content">
<article class="article article--details"><h3>General Information</h3>
<div class="article__content">{facts}</div></article>
<article class="article article--details"><h3>Why Work Here</h3>
<div class="article__content"><p>{why}</p></div></article>
<article class="article article--details"><h3>Description and Requirements</h3>
<div class="article__content"><div class="value">{body}</div></div></article>
</div></section></div>
<div class="grid__item"><aside class="sidebar"><a href="/apply">Apply</a></aside></div>
</div></div></div></main>
<footer class="footer"><a href="/legal">Legal</a></footer></div></body></html>"""


def script_drawn_page(*, paragraphs: list[str] | None = None, title: str = "Communications Director",
                closed: str | None = None, valid_through: str | None = None) -> str:
    """A page of the kind a script-driven careers system serves: almost no text in the HTML, and the whole posting in a
    schema.org JobPosting in a `ld+json` script (its description is escaped HTML), beside a breadcrumb list. The page
    also carries a long notice in its footer, and, when `closed` is given, says in plain HTML what that is (a closed
    posting); `valid_through` is the JobPosting's own end date."""
    texts = PARAS + QUALIFIED + PREFERRED if paragraphs is None else paragraphs
    items = "".join(f"<li>{t.lstrip('- ')}</li>" for t in texts[3:])
    description = "<p>" + "</p><p>".join(texts[:3]) + f"</p><ul>{items}</ul>"
    posting = {"@context": "http://schema.org", "@type": "JobPosting", "title": title, "datePosted": "2026-10-29",
               "description": html.escape(description), "identifier": {"@type": "PropertyValue", "value": "770"},
               **({"validThrough": valid_through} if valid_through else {}),
               "hiringOrganization": {"@type": "Organization", "name": "Northfield Health"},
               "jobLocation": {"@type": "Place", "address": {"@type": "PostalAddress", "addressLocality": "Springdale",
                                                             "addressRegion": "Freedonia"}}}
    crumbs = {"@context": "http://schema.org", "@type": "BreadcrumbList", "itemListElement": []}
    notice = ("Beware of recruiting fraud. This company will never ask applicants to pay a fee, to buy equipment, to "
              "deposit a cheque or to give card, bank or other sensitive personal details as part of the recruiting "
              "process. Official messages come only from addresses at our own domain, and offers are made in writing "
              "after a formal interview. Report anything that looks wrong to the security team straight away.")
    return f"""<!DOCTYPE html><html lang="en"><head><title>Careers</title>
<script type="application/ld+json">{json.dumps(crumbs)}</script>
<script type="application/ld+json">{json.dumps(posting)}</script>
<script>window.phApp = {{"ddo": {{"jobDetail": "loaded by script"}}}};</script></head>
<body><div id="ph-page"><main><div id="ph-root">{closed or "Explore our teams and openings."}</div></main>
<footer><a href="/cookies">Career Site Cookie Settings</a><p>{notice}</p></footer></div></body></html>"""
