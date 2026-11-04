"""Labelled evaluation corpus: a fictional company ("Helios Robotics") so no model can answer from memory.

Facts live in real PDF / DOCX / XLSX / Markdown / web / YouTube-transcript sources. Each question lists the
exact `facts` (substrings that must appear in a retrieved passage) which define retrieval relevance, the
`answer_has` terms an answer must contain, and `negative=True` for questions whose correct response is
"the sources do not contain this".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

HANDBOOK = [
    ("1. Welcome", ["Helios Robotics was founded in 2014 in Rotterdam and employs about 340 people.",
                    "The company values are curiosity, craftsmanship and candour."]),
    ("2. Leave Policy", ["Full-time employees receive 28 days of paid annual leave per calendar year.",
                         "Unused leave of up to 5 days may be carried over into the first quarter of the next year.",
                         "Parental leave is 16 weeks at full pay for all parents."]),
    ("3. Remote Work", ["Employees may work remotely up to 3 days per week with manager approval.",
                        "A home-office stipend of 400 euros is paid once every two years."]),
    ("4. Expenses", ["Expense reports must be submitted within 30 days of the purchase date."]),
    ("5. Security", ["Passwords must be rotated every 90 days.",
                     "Multi-factor authentication is mandatory for all production systems.",
                     "Lost devices must be reported to the security desk within 4 hours."]),
    ("6. Onboarding", ["New hires complete a 6 month probation period.",
                       "Every new hire is assigned a mentor for the first 90 days.",
                       "The laptop refresh cycle is 36 months."]),
    ("7. Travel", ["Flights over 6 hours may be booked in premium economy.",
                   "Travel must be booked through the Helios portal at least 14 days in advance."]),
    ("8. Contacts", ["The HR helpdesk can be reached at extension 4410.",
                     "The security desk operates 24 hours a day on extension 4999."]),
]
EXPENSE_TABLE = [["Category", "Daily limit", "Receipt required"], ["Meals", "60 USD", "No"], ["Hotel", "220 USD", "Yes"],
                 ["Taxi", "45 USD", "Yes"], ["Conference", "1500 USD", "Yes"]]

MINUTES = """# Steering Committee Minutes

## 12 March

The committee approved the H7 firmware 2.0 release for 14 May. Maria Lopez will own the migration plan.

## 26 March

The budget for the vision project was increased to 2.4 million euros. Decision: the Rotterdam office will close on 24 December.
Tom Becker must deliver the vendor assessment by 5 April.
"""

BLOG = {
    "/eng/": ("Helios Engineering Blog", "<h1>Helios Engineering Blog</h1><p>Posts about how we build search and robotics software at Helios.</p>"
              "<a href='/eng/indexing'>Indexing</a> <a href='/eng/latency'>Latency</a>"),
    "/eng/indexing": ("How we index", "<h1>How we index</h1><p>Our nightly indexing job processes 2 million documents in about 45 minutes.</p>"
                      "<p>We use HNSW graphs with ef_search set to 128 for the production search cluster.</p>"),
    "/eng/latency": ("Keeping latency low", "<h1>Keeping latency low</h1><p>The p95 query latency is 180 milliseconds at 400 queries per second.</p>"
                     "<p>The cache hit rate is 62 percent during business hours.</p>"),
}

FILLER = ["so that's basically how the team approached the problem", "and you know it took a few iterations to get right",
          "we talked to a lot of users along the way", "the architecture diagram is on the next slide",
          "that was an interesting detour honestly", "anyway let me move on to the next topic",
          "there are a lot of moving parts here", "we wrote a lot of tooling around this",
          "I think the main thing is to stay pragmatic", "any questions so far okay great"]
TALK_FACTS = {190: "the first bottleneck we hit was embedding throughput which we solved by batching sixty four documents per request",
              765: "hybrid search combining BM25 and vectors improved recall at ten by eleven percent",
              1290: "after retrieval we rerank the top fifty candidates with a cross encoder",
              1985: "the biggest lesson was to measure with a labelled evaluation set before changing anything"}


def talk_segments():
    segs, t, i = [], 0.0, 0
    while t < 2400:
        text = TALK_FACTS.get(int(t)) or FILLER[(i * 7 + 3) % len(FILLER)] + f" part {i}"
        segs.append((text, t))
        t += 5.0
        i += 1
    return segs


@dataclass
class Q:
    id: str
    type: str
    question: str
    facts: list[str] = field(default_factory=list)
    answer_has: list[str] = field(default_factory=list)
    negative: bool = False
    followup_of: str | None = None
    t_range: tuple[float, float] | None = None
    page: int | None = None
    sources: list[str] | None = None        # source keys; None = all


def questions() -> list[Q]:
    L, T, M = "lookup", "table", "multi"
    qs = [
        Q("l01", L, "How many days of paid annual leave do employees get?", ["28 days of paid annual leave"], ["28"]),
        Q("l02", L, "Can unused vacation be carried over to next year?", ["carried over into the first quarter"], ["5 days"]),
        Q("l03", L, "How long is parental leave?", ["16 weeks at full pay"], ["16 weeks"]),
        Q("l04", L, "How many days a week may I work from home?", ["up to 3 days per week"], ["3 days"]),
        Q("l05", L, "What is the home office allowance?", ["400 euros"], ["400"]),
        Q("l06", L, "How frequently must passwords be changed?", ["rotated every 90 days"], ["90 days"]),
        Q("l07", L, "Is multi-factor authentication required?", ["Multi-factor authentication is mandatory"], ["mandatory"]),
        Q("l08", L, "Within how many hours must a lost device be reported?", ["within 4 hours"], ["4 hours"]),
        Q("l09", L, "How long is the probation period for new hires?", ["6 month probation"], ["6 month"]),
        Q("l10", L, "How often are laptops replaced?", ["36 months"], ["36 months"]),
        Q("l11", L, "How far in advance must travel be booked?", ["at least 14 days in advance"], ["14 days"]),
        Q("l12", L, "What is the HR helpdesk extension?", ["extension 4410"], ["4410"]),
        Q("l13", L, "When and where was the company founded?", ["founded in 2014 in Rotterdam"], ["2014", "Rotterdam"]),
        Q("l14", L, "What are the company values?", ["curiosity, craftsmanship and candour"], ["curiosity"]),
        Q("l15", L, "How soon after a purchase must expense reports be submitted?", ["within 30 days of the purchase date"], ["30 days"]),
        Q("t01", "table", "What is the daily hotel limit?", ["| Hotel | 220 USD |"], ["220"]),
        Q("t02", "table", "Is a receipt required for taxi rides?", ["| Taxi | 45 USD | Yes |"], ["Yes"]),
        Q("t03", "table", "What is the maximum conference spend?", ["| Conference | 1500 USD"], ["1500"]),
        Q("t04", "table", "What is the payload of the H7 robot arm?", ["| Payload | 7 kg |"], ["7 kg"]),
        Q("t05", "table", "How much does the H7 weigh?", ["| Weight | 24 kg |"], ["24 kg"]),
        Q("t06", "table", "What repeatability does the H7 have?", ["| Repeatability | 0.03 mm |"], ["0.03"]),
        Q("t07", "table", "What was the APAC revenue in Q2?", ["| APAC | Q2 | 950000 |"], ["950000"]),
        Q("t08", "table", "How many people work in Support?", ["| Support | 45 |"], ["45"]),
        Q("t09", "table", "What is the highest value of Revenue_EUR?", ["max=1650000"], ["1650000"]),
        Q("s01", L, "How quickly does the H7 stop when it touches something?", ["within 120 milliseconds"], ["120"]),
        Q("s02", L, "Which standard is the H7 certified to?", ["ISO 10218"], ["ISO 10218"]),
        Q("s03", L, "What is the API rate limit?", ["600 requests per minute"], ["600"]),
        Q("s04", L, "How long is the H7 warranty?", ["24 month warranty"], ["24 month"]),
        Q("s05", L, "How often is firmware released?", ["Firmware updates are released quarterly"], ["quarterly"]),
        Q("m01", L, "When was the firmware 2.0 release approved for?", ["firmware 2.0 release for 14 May"], ["14 May"]),
        Q("m02", L, "Who owns the migration plan?", ["Maria Lopez will own the migration plan"], ["Maria Lopez"]),
        Q("m03", L, "What is the new budget for the vision project?", ["2.4 million euros"], ["2.4 million"]),
        Q("m04", L, "On what date does the Rotterdam office close?", ["close on 24 December"], ["24 December"]),
        Q("m05", L, "What must Tom Becker deliver?", ["vendor assessment by 5 April"], ["vendor assessment"]),
        Q("w01", "web", "How many documents does the nightly indexing job process?", ["2 million documents"], ["2 million"]),
        Q("w02", "web", "What ef_search value is used for the HNSW graphs?", ["ef_search set to 128"], ["128"]),
        Q("w03", "web", "What is the p95 query latency?", ["180 milliseconds"], ["180"]),
        Q("w04", "web", "What is the cache hit rate?", ["62 percent"], ["62"]),
        Q("v01", "video", "What was the first bottleneck and how did they solve it?", ["embedding throughput"], ["embedding throughput"],
          t_range=(150, 215)),
        Q("v02", "video", "How much did hybrid search improve recall?", ["eleven percent"], ["eleven percent"], t_range=(735, 800)),
        Q("v03", "video", "How many candidates do they rerank?", ["top fifty candidates"], ["fifty"], t_range=(1260, 1320)),
        Q("v04", "video", "What was the biggest lesson from the talk?", ["labelled evaluation set"], ["evaluation set"], t_range=(1955, 2015)),
        Q("v05", "video", "What does the speaker say at 12:45?", ["eleven percent"], ["eleven percent"], t_range=(735, 800)),
        Q("v06", "video", "What is being said at 21:30 in the talk?", ["top fifty candidates"], ["fifty"], t_range=(1260, 1320)),
        Q("p01", "page", "What does page 5 of the handbook say about passwords?", ["rotated every 90 days"], ["90 days"], page=5),
        Q("p02", "page", "On page 8, what is the security desk extension?", ["extension 4999"], ["4999"], page=8),
        Q("p03", "page", "What does page 3 say about the home-office stipend?", ["400 euros"], ["400"], page=3),
        Q("x01", "scatter", "What are the leave benefits: annual leave and parental leave?", ["28 days of paid annual leave", "16 weeks at full pay"], ["28", "16 weeks"]),
        Q("x02", "scatter", "What are the security rules for passwords and multi-factor authentication?", ["rotated every 90 days", "Multi-factor authentication is mandatory"], ["90 days", "mandatory"]),
        Q("x03", "scatter", "How do remote work days and the home office stipend work?", ["up to 3 days per week", "400 euros"], ["3 days", "400"]),
        Q("d01", M, "Which is longer, the H7 warranty or the laptop refresh cycle?", ["24 month warranty", "36 months"], ["24", "36"]),
        Q("d02", M, "What did AMER earn in Q1 and how many engineers are there?", ["| AMER | Q1 | 1500000 |", "| Engineering | 140 |"], ["1500000", "140"]),
        Q("d03", M, "What is the H7 API limit and what is the p95 latency from the engineering blog?", ["600 requests per minute", "180 milliseconds"], ["600", "180"]),
        Q("c01", "compare", "Compare how the handbook and the H7 spec describe time periods such as warranty and probation.", ["24 month warranty", "6 month probation"], ["24", "6 month"]),
        Q("f1b", "followup", "And how many days of annual leave do we get?", ["28 days of paid annual leave"], ["28"], followup_of="f1a"),
        Q("f2b", "followup", "What about its reach?", ["| Reach | 850 mm |"], ["850"], followup_of="f2a"),
        Q("f3b", "followup", "Is anything else required for production systems?", ["Multi-factor authentication is mandatory"], ["mandatory"], followup_of="f3a"),
        Q("f4b", "followup", "How much was it increased to?", ["2.4 million euros"], ["2.4 million"], followup_of="f4a"),
        Q("f5b", "followup", "When is that scheduled?", ["14 May"], ["14 May"], followup_of="f5a"),
    ]
    firsts = [
        Q("f1a", "followup_seed", "How long is parental leave?", ["16 weeks at full pay"], ["16 weeks"]),
        Q("f2a", "followup_seed", "What is the payload of the H7?", ["| Payload | 7 kg |"], ["7 kg"]),
        Q("f3a", "followup_seed", "How often must passwords be rotated?", ["rotated every 90 days"], ["90 days"]),
        Q("f4a", "followup_seed", "What was decided about the vision project budget?", ["2.4 million euros"], ["2.4 million"]),
        Q("f5a", "followup_seed", "What did the committee approve on 12 March?", ["firmware 2.0 release for 14 May"], ["firmware 2.0"]),
    ]
    neg = [
        ("n01", "What is the CEO's personal phone number?"), ("n02", "Does Helios offer a pet insurance benefit?"),
        ("n03", "What is the maximum IP dust and water rating of the H7?"), ("n04", "What is the retail price of the H7 robot arm?"),
        ("n05", "How many employees work in Marketing?"), ("n06", "What was EMEA revenue in Q3?"),
        ("n07", "What did the speaker say about GPU pricing in the talk?"), ("n08", "Who won the 2022 football World Cup?"),
        ("n09", "What is the ef_construction parameter used by the indexing job?"),
        ("n10", "What is the company policy on sabbatical leave?"), ("n11", "What does the handbook say about stock options?"),
        ("n12", "When will firmware 3.0 be released?"),
    ]
    return firsts + qs + [Q(i, "negative", t, negative=True) for i, t in neg]


def build(tmp: Path, web_base: str):
    """Create the files; returns {key: Path-or-url}."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from factories import make_pdf
    out = {}
    pages = [[h] + b for h, b in HANDBOOK]
    out["handbook"] = make_pdf(tmp / "Helios_Employee_Handbook.pdf", pages, table_on_page={3: EXPENSE_TABLE})

    from docx import Document
    d = Document()
    d.add_heading("Helios H7 Robot Arm Specification", 0)
    d.add_heading("1. Overview", 1)
    d.add_paragraph("The H7 is a six-axis collaborative robot arm designed for light assembly.")
    d.add_heading("2. Performance", 1)
    rows = [["Parameter", "Value"], ["Payload", "7 kg"], ["Reach", "850 mm"], ["Repeatability", "0.03 mm"], ["Weight", "24 kg"]]
    t = d.add_table(rows=len(rows), cols=2)
    for r, row in enumerate(rows):
        for c, v in enumerate(row):
            t.cell(r, c).text = v
    d.add_heading("3. Safety", 1)
    d.add_paragraph("The H7 stops within 120 milliseconds when contact force exceeds 150 newtons.")
    d.add_paragraph("It is certified to ISO 10218.")
    d.add_heading("4. API and Firmware", 1)
    d.add_paragraph("The REST API allows 600 requests per minute per key.")
    d.add_paragraph("Firmware updates are released quarterly.")
    d.add_heading("5. Warranty", 1)
    d.add_paragraph("The H7 carries a 24 month warranty covering parts and labour.")
    d.save(str(tmp / "Helios_H7_Spec.docx"))
    out["spec"] = tmp / "Helios_H7_Spec.docx"

    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Revenue"
    ws.append(["Region", "Quarter", "Revenue_EUR"])
    for row in [("EMEA", "Q1", 1200000), ("EMEA", "Q2", 1350000), ("APAC", "Q1", 800000), ("APAC", "Q2", 950000),
                ("AMER", "Q1", 1500000), ("AMER", "Q2", 1650000)]:
        ws.append(list(row))
    w2 = wb.create_sheet("Headcount")
    w2.append(["Department", "Headcount"])
    for row in [("Engineering", 140), ("Sales", 60), ("Support", 45), ("Operations", 95)]:
        w2.append(list(row))
    wb.save(str(tmp / "Helios_Financials.xlsx"))
    out["fin"] = tmp / "Helios_Financials.xlsx"

    (tmp / "Steering_Minutes.md").write_text(MINUTES)
    out["minutes"] = tmp / "Steering_Minutes.md"
    out["blog"] = web_base + "/eng/"
    out["talk"] = "youtube"
    return out
