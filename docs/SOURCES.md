# Internship sources (Bengaluru)

Where NOESIS looks for internships, how each source is read, and what is left to you.

## Read automatically

| Source | How | Interval | Verified as official? |
|---|---|---|---|
| Company job boards (Greenhouse, Lever, Ashby) | The ATS's official public job-board JSON API | 6 h | Yes: the ATS host and the company domain are official domains |
| SimplifyJobs internship list | Public JSON file on GitHub, India filter | 6 h | No: each link is checked by Quality |
| Google News RSS (Bengaluru / India internship and hackathon queries) | Public RSS search feed | 12 h | No (news articles) |

Company boards keep only roles whose **title** contains a whole word from
`intern, interns, internship, internships, trainee, apprentice, apprenticeship, summer analyst`
and whose **location** contains a whole word from `bengaluru, bangalore, india, remote`.
Whole-word matching matters: `intern` does not match "Internal Auditor", and `india`
does not match "Indianapolis, IN" (which is why a naive count of "India" rows in the
SimplifyJobs file looks much larger than the real number).

Board listings rarely state a deadline. Quality marks them **verified** anyway, with the
note "rolling posting on the company's official job board", because the link is on the
company's own ATS.

### Confirmed boards (26 Oct 2026)

Lever: `meesho`, `cred`, `zeta` · Greenhouse: `databricks`, `stripe`, `rubrik`, `mongodb` · Ashby: `sarvam`, `atlan`

These slugs were confirmed by the owner. The cloud environment that built v2.0 could not
reach the ATS hosts (its network policy blocks them), so the board code is tested with
recorded-shape fixtures only. Run `python scripts/check_sources.py` on the server.

### Adding a company

```bash
python scripts/check_sources.py --probe "Razorpay" --domain razorpay.com
```

It tries Greenhouse, Lever and Ashby with common slug spellings and prints the line to
add to `config/sources/feeds.yaml`. Or use the dashboard: **Settings → Company boards →
Add**, where **Test** fetches the board live and shows how many roles and how many
Bengaluru internships it has right now.

Slugs that returned a client error on 26 Oct 2026, to probe again: Razorpay (lever),
Groww (lever), Postman (greenhouse), PhonePe (greenhouse), Zepto (ashby).

Candidates to probe (not confirmed, so not added): Swiggy, Flipkart, Razorpay, Zerodha,
Groww, Postman, Atlassian, Juspay, Freshworks, BrowserStack, PhonePe, Zepto, Uber,
CRED (done), Sprinklr, Rippling, Gojek, ShareChat, Dream11, InMobi, Unacademy.

## Manual: send the link

These companies use Workday or their own careers site, and these platforms have no
public API or feed. NOESIS never scrapes them. When you see a posting, send it:

- Telegram: `/add <link>` (for LinkedIn: `/add <link> <paste the description>`)
- Dashboard: **Internships → Add a link**

The page is fetched once, politely (robots.txt honoured, honest User-Agent), parsed
(with the model when one is available, values kept only when they appear in the page),
put on your shortlist, verified and scored like any other listing. LinkedIn pages are
never fetched: paste the description.

| Company / platform | Why manual | Where to look |
|---|---|---|
| Internshala | No API/feed; terms forbid scraping | internshala.com |
| Unstop | No API/feed; terms forbid scraping | unstop.com |
| LinkedIn | Terms forbid automated access | linkedin.com/jobs (paste the text) |
| Wellfound | No public API | wellfound.com |
| Google | Own careers site | careers.google.com (STEP, SWE intern) |
| Microsoft | Own careers site | careers.microsoft.com |
| Amazon | Own careers site | amazon.jobs |
| Flipkart | Own careers site | flipkartcareers.com |
| Walmart Global Tech | Workday | careers.walmart.com |
| Adobe | Workday | careers.adobe.com |
| Nvidia | Workday | nvidia.wd5.myworkdayjobs.com |
| Intuit | Own site / Workday | jobs.intuit.com |
| Cisco | Own careers site | jobs.cisco.com |
| Uber | Own careers site | uber.com/careers |
| Goldman Sachs, JPMorgan | Own sites | see the hiring-programme radar |

Hiring programmes with yearly windows (Google STEP, Microsoft Engage, Amazon WOW,
Flipkart GRiD, Goldman Sachs and JPMC Code for Good and others) are tracked by the
**drive radar** (`config/drives.yaml`), which pings you before their usual opening month.
