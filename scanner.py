"""
Job-board scanner: fetches roles from company careers sites and UK job APIs,
then keeps graduate-level marketing / data / consulting roles in London.

Companies are added as careers links. The scanner works out which job system
each one uses (Greenhouse, Lever, Ashby, SmartRecruiters, Workday) and reads
its job feed; for anything else it reads standard JobPosting data on the page,
or follows a link to a known job system it finds there.
"""
import html
import json
import re
import threading
import time
import urllib.robotparser
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, wait as wait_futures, FIRST_COMPLETED
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

import requests

USER_AGENT = "GradJobWatcher/1.1 (personal job alert app)"
REQUEST_DELAY = 1.5          # seconds between requests to the same site
WORKERS = 6                  # different sites are checked in parallel

SETTINGS_VERSION = 2
DEFAULT_SETTINGS = {
    "settings_version": SETTINGS_VERSION,
    # Careers links, one per line. Optional name first: "PwC https://..."
    "watch_urls": [],
    # Advanced: board names (older style; still supported)
    "greenhouse": [], "lever": [], "ashby": [], "smartrecruiters": [], "jsonld_pages": [],
    # Words searched on Workday sites (each is one search per company)
    "workday_terms": ["graduate", "junior", "trainee", "entry level"],
    # Job sites
    "adzuna_app_id": "", "adzuna_app_key": "", "reed_api_key": "",
    "reed_direct_only": True,      # skip recruitment agencies on Reed
    "reed_graduate_only": False,   # Reed's own graduate tag (narrower)
    "search_terms": ["graduate marketing", "graduate data analyst", "junior data analyst",
                     "graduate consultant", "junior marketing", "graduate insight"],
    "search_location": "London",
    # A match needs a level word...
    "include": ["graduate", "grad scheme", "grad programme", "new grad",
                "entry level", "entry-level", "early career", "early-career",
                "trainee", "junior", "assistant", "account executive",
                "media executive", "analyst programme", "associate consultant"],
    # ...a field word (empty list = any field)...
    "fields": ["marketing", "data", "analyst", "analytics", "insight", "media", "digital",
               "strategy", "consult", "research", "crm", "brand", "growth", "commercial",
               "social", "content", "seo", "ppc", "planner", "planning", "account",
               "client", "business", "advisory", "communications", "campaign"],
    # ...and none of these.
    "exclude": ["senior", "lead", "principal", "manager", "head of", "director",
                "vp", "staff", "experienced"],
    "locations": ["london", "canary wharf", "shoreditch", "kings cross", "king's cross",
                  "holborn", "southwark", "westminster", "stratford", "croydon"],
    "location_exclude": ["new london", "london, on", "london, ontario", "london, ky",
                         "london, oh", "ontario", "canada"],
    "email": {"enabled": False, "smtp_host": "smtp.gmail.com", "smtp_port": 587,
              "username": "", "password": "", "to": ""},
}

# Lists that gain the new suggestions when older saved settings are upgraded
UPGRADE_UNION_KEYS = ["include", "search_terms"]


def upgrade_settings(saved):
    """Bring settings saved by an older version up to date without losing edits."""
    if saved.get("settings_version", 1) < 2:
        for k in UPGRADE_UNION_KEYS:
            if k in saved:
                saved[k] = saved[k] + [t for t in DEFAULT_SETTINGS[k] if t not in saved[k]]
        saved["settings_version"] = SETTINGS_VERSION
    return saved


# ----------------------------------------------------------------- HTTP

session = requests.Session()
session.headers["User-Agent"] = USER_AGENT
_last_hit = {}
_host_locks = defaultdict(threading.Lock)
_guard = threading.Lock()


def polite_request(method, url, **kwargs):
    """One request at a time per site, with a pause between them."""
    host = urlparse(url).netloc
    with _guard:
        lock = _host_locks[host]
    with lock:
        pause = REQUEST_DELAY - (time.time() - _last_hit.get(host, 0))
        if pause > 0:
            time.sleep(pause)
        try:
            r = session.request(method, url, timeout=20, **kwargs)
        finally:
            _last_hit[host] = time.time()
    r.raise_for_status()
    return r


def get_json(url, **kwargs):
    return polite_request("GET", url, **kwargs).json()


def clean(text):
    text = html.unescape(re.sub(r"<[^>]+>", "", str(text or "")))
    return re.sub(r"\s+", " ", text).strip()


def make_job(source, company, title, location, url, posted=""):
    return {"source": source, "company": clean(company), "title": clean(title),
            "location": clean(location), "url": url or "",
            "posted": str(posted or "")[:10]}


# ----------------------------------------------------------------- job systems

def fetch_greenhouse(board, name=None):
    hosts = ["boards-api.greenhouse.io", "boards-api.eu.greenhouse.io"]
    for i, host in enumerate(hosts):
        try:
            data = get_json(f"https://{host}/v1/boards/{board}/jobs")
            break
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404 and i == 0:
                continue                      # try the EU data centre
            raise
    return [make_job("Greenhouse", name or board, j.get("title"),
                     (j.get("location") or {}).get("name"), j.get("absolute_url"),
                     j.get("first_published") or j.get("updated_at"))
            for j in data.get("jobs", [])]


def fetch_lever(company, name=None):
    for i, host in enumerate(["api.lever.co", "api.eu.lever.co"]):
        try:
            data = get_json(f"https://{host}/v0/postings/{company}", params={"mode": "json"})
            break
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404 and i == 0:
                continue
            raise
    jobs = []
    for j in data:
        ts = j.get("createdAt")
        posted = datetime.fromtimestamp(ts / 1000, timezone.utc).date().isoformat() if ts else ""
        jobs.append(make_job("Lever", name or company, j.get("text"),
                             (j.get("categories") or {}).get("location"),
                             j.get("hostedUrl"), posted))
    return jobs


def fetch_ashby(board, name=None):
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{board}")
    return [make_job("Ashby", name or board, j.get("title"), j.get("location"),
                     j.get("jobUrl"), j.get("publishedAt"))
            for j in data.get("jobs", []) if j.get("isListed", True)]


def fetch_smartrecruiters(company, name=None):
    jobs, offset = [], 0
    while True:
        data = get_json(f"https://api.smartrecruiters.com/v1/companies/{company}/postings",
                        params={"limit": 100, "offset": offset})
        items = data.get("content", [])
        for j in items:
            loc = j.get("location") or {}
            where = ", ".join(x for x in (loc.get("city"), loc.get("country")) if x)
            if loc.get("remote"):
                where += " (remote)"
            jobs.append(make_job("SmartRecruiters",
                                 name or (j.get("company") or {}).get("name") or company,
                                 j.get("name"), where,
                                 f"https://jobs.smartrecruiters.com/{company}/{j.get('id')}",
                                 j.get("releasedDate")))
        offset += len(items)
        if not items or offset >= data.get("totalFound", 0):
            return jobs


def _workday_date(text):
    """Workday says 'Posted Today', 'Posted Yesterday', 'Posted 5 Days Ago'."""
    t = (text or "").lower()
    today = date.today()
    if "today" in t:
        return today.isoformat()
    if "yesterday" in t:
        return (today - timedelta(days=1)).isoformat()
    m = re.search(r"(\d+)\s+days? ago", t)
    if m and "+" not in t:
        return (today - timedelta(days=int(m.group(1)))).isoformat()
    return ""


def fetch_workday(spec, settings, name=None):
    """spec = (base_url, tenant, site, job_page_base). Reads the job feed behind a
    Workday careers site: the same data the site's own pages load."""
    base, tenant, site, job_base = spec
    api = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    found = {}
    for term in settings["workday_terms"]:
        offset, total = 0, None
        while offset < 100:               # first 100 results per search is plenty
            data = polite_request("POST", api, headers={"Accept": "application/json"},
                                  json={"appliedFacets": {}, "limit": 20,
                                        "offset": offset, "searchText": term}).json()
            posts = data.get("jobPostings") or []
            if total is None:
                total = data.get("total") or 0
            for p in posts:
                url = f"{job_base}{p.get('externalPath') or ''}"
                found[url] = make_job("Workday", name or tenant, p.get("title"),
                                      p.get("locationsText"), url,
                                      _workday_date(p.get("postedOn")))
            offset += len(posts)
            if not posts or offset >= total:
                break
    return list(found.values())


def fetch_adzuna(settings):
    jobs = []
    for term in settings["search_terms"]:
        params = {"app_id": settings["adzuna_app_id"], "app_key": settings["adzuna_app_key"],
                  "what": term, "results_per_page": 50, "max_days_old": 3, "sort_by": "date"}
        if settings["search_location"]:
            params["where"] = settings["search_location"]
        data = get_json("https://api.adzuna.com/v1/api/jobs/gb/search/1", params=params)
        for j in data.get("results", []):
            jobs.append(make_job("Adzuna", (j.get("company") or {}).get("display_name"),
                                 j.get("title"), (j.get("location") or {}).get("display_name"),
                                 j.get("redirect_url"), j.get("created")))
    return jobs


def fetch_reed(settings):
    jobs = []
    for term in settings["search_terms"]:
        params = {"keywords": term, "resultsToTake": 100}
        if settings["search_location"]:
            params["locationName"] = settings["search_location"]
        if settings.get("reed_direct_only"):
            params["postedByDirectEmployer"] = "true"
        if settings.get("reed_graduate_only"):
            params["graduate"] = "true"
        data = get_json("https://www.reed.co.uk/api/1.0/search", params=params,
                        auth=(settings["reed_api_key"], ""))   # key as username
        for j in data.get("results", []):
            jobs.append(make_job("Reed", j.get("employerName"), j.get("jobTitle"),
                                 j.get("locationName"), j.get("jobUrl"), j.get("date")))
    return jobs


# ----------------------------------------------------------------- careers links

def parse_workday_url(url):
    u = urlparse(url if "://" in url else "https://" + url)
    host = u.netloc.lower()
    parts = [p for p in u.path.split("/") if p]
    if parts and re.fullmatch(r"[a-z]{2}-[A-Za-z]{2}", parts[0]):
        parts = parts[1:]                 # drop the language part, e.g. en-GB
    if re.fullmatch(r"[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com", host):
        tenant, prefix = host.split(".")[0], ""
    elif re.fullmatch(r"wd\d+\.myworkdaysite\.com", host) and len(parts) >= 3 and parts[0] == "recruiting":
        tenant, prefix, parts = parts[1], f"/recruiting/{parts[1]}", parts[2:]
    else:
        return None
    if not parts or parts[0] in ("wday", "job"):
        return None
    base = f"https://{host}"
    return (base, tenant, parts[0], f"{base}{prefix}/{parts[0]}")


def detect_board(url):
    """Work out which job system a careers link belongs to.
    Returns (kind, identifier) or None for an ordinary web page."""
    wd = parse_workday_url(url)
    if wd:
        return ("workday", wd)
    u = urlparse(url if "://" in url else "https://" + url)
    host = u.netloc.lower()
    parts = [p for p in u.path.split("/") if p]
    query = u.query
    if host.endswith("greenhouse.io"):
        m = re.search(r"(?:^|&)for=([\w-]+)", query)
        if m:
            return ("greenhouse", m.group(1))
        if parts and parts[0] != "embed":
            return ("greenhouse", parts[0])
    if host in ("jobs.lever.co", "jobs.eu.lever.co") and parts:
        return ("lever", parts[0])
    if host == "jobs.ashbyhq.com" and parts:
        return ("ashby", parts[0])
    if host in ("jobs.smartrecruiters.com", "careers.smartrecruiters.com") and parts:
        return ("smartrecruiters", parts[0])
    return None


# Links to job systems that are often embedded in company careers pages
SNIFF = [
    re.compile(r"https?://[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Za-z]{2}/)?[\w-]+", re.I),
    re.compile(r"https?://wd\d+\.myworkdaysite\.com/(?:[a-z]{2}-[A-Za-z]{2}/)?recruiting/[\w-]+/[\w-]+", re.I),
    re.compile(r"greenhouse\.io/embed/job_board(?:/js)?\?for=[\w-]+", re.I),
    re.compile(r"(?:job-)?boards(?:\.eu)?\.greenhouse\.io/(?!embed)[\w-]+", re.I),
    re.compile(r"jobs(?:\.eu)?\.lever\.co/[\w-]+", re.I),
    re.compile(r"jobs\.ashbyhq\.com/[\w.-]+", re.I),
    re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/[\w-]+", re.I),
]

LD_RE = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                   re.S | re.I)
_robots = {}


def allowed_by_robots(url):
    root = "{0.scheme}://{0.netloc}".format(urlparse(url))
    if root not in _robots:
        rp = urllib.robotparser.RobotFileParser(root + "/robots.txt")
        try:
            rp.read()
        except Exception:
            rp = None
        _robots[root] = rp
    rp = _robots[root]
    return rp is None or rp.can_fetch(USER_AGENT, url)


def _postings(obj):
    if isinstance(obj, list):
        for item in obj:
            yield from _postings(item)
    elif isinstance(obj, dict):
        t = obj.get("@type")
        if "JobPosting" in (t if isinstance(t, list) else [t]):
            yield obj
        for v in obj.values():
            if isinstance(v, (list, dict)):
                yield from _postings(v)


def _describe_location(loc):
    if isinstance(loc, list):
        return "; ".join(filter(None, (_describe_location(l) for l in loc)))
    if not isinstance(loc, dict):
        return str(loc or "")
    addr = loc.get("address") or {}
    if not isinstance(addr, dict):
        return str(addr)
    country = addr.get("addressCountry")
    if isinstance(country, dict):
        country = country.get("name")
    return ", ".join(x for x in (addr.get("addressLocality"), country) if x)


def fetch_page(url, settings, name=None, log=None):
    """An ordinary careers page: read its JobPosting data, or follow an
    embedded link to a job system we know."""
    if not allowed_by_robots(url):
        raise RuntimeError("the site's robots.txt asks tools not to read this page")
    page = polite_request("GET", url).text
    jobs = []
    for block in LD_RE.findall(page):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        for p in _postings(data):
            org = p.get("hiringOrganization") or {}
            org = org.get("name") if isinstance(org, dict) else org
            loc = _describe_location(p.get("jobLocation"))
            if p.get("jobLocationType") == "TELECOMMUTE":
                loc = (loc + " (remote)").strip()
            jobs.append(make_job("Careers page", name or org or urlparse(url).netloc,
                                 p.get("title"), loc, p.get("url") or url, p.get("datePosted")))
    if jobs:
        return jobs
    for pattern in SNIFF:
        m = pattern.search(page)
        if m:
            link = m.group(0)
            board = detect_board(link)
            if board:
                if log:
                    log(f"  found a {board[0].title()} job board on {urlparse(url).netloc}")
                return run_board(board, settings, name or urlparse(url).netloc.replace("www.", ""))
    raise RuntimeError("no job listings found on this page. Try the link to the "
                       "page that lists the jobs (often after clicking 'Search jobs')")


def run_board(board, settings, name=None):
    kind, ident = board
    if kind == "workday":
        return fetch_workday(ident, settings, name)
    return {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby,
            "smartrecruiters": fetch_smartrecruiters}[kind](ident, name)


def split_watch_line(line):
    """'PwC https://...' -> ('PwC', 'https://...'); a bare link -> (None, link)."""
    m = re.search(r"(https?://\S+|\S+\.\S+/\S*)", line)
    if not m:
        return None, None
    name = (line[:m.start()] + line[m.end():]).strip(" -:|,\t") or None
    url = m.group(1)
    return name, url if "://" in url else "https://" + url


# ----------------------------------------------------------------- filtering

def _has_term(text, terms, whole_word):
    for t in terms:
        t = t.strip().lower()
        if t and re.search(r"\b" + re.escape(t) + (r"\b" if whole_word else ""), text):
            return True
    return False


def is_match(job, s):
    title = job["title"].lower()
    if not _has_term(title, s["include"], whole_word=False):
        return False
    if s.get("fields") and not _has_term(title, s["fields"], whole_word=False):
        return False
    if _has_term(title, s["exclude"], whole_word=True):
        return False
    loc = job["location"].lower()
    if not loc or re.fullmatch(r"\d+ locations", loc):
        return True                       # unknown or several locations: keep it
    if _has_term(loc, s["location_exclude"], whole_word=True):
        return False
    if s["locations"] and not _has_term(loc, s["locations"], whole_word=True):
        return False
    return True


def job_key(job):
    norm = lambda x: re.sub(r"[^a-z0-9]+", " ", x.lower()).strip()
    return f"{norm(job['company'])}|{norm(job['title'])}"


# ----------------------------------------------------------------- running

def build_tasks(s, log):
    tasks, seen = [], set()

    def add(label, fn):
        if label not in seen:
            seen.add(label)
            tasks.append((label, fn))

    for line in s.get("watch_urls", []):
        name, url = split_watch_line(line)
        if not url:
            log(f"Skipped '{line}': no web link found on that line")
            continue
        board = detect_board(url)
        label = name or urlparse(url).netloc.replace("www.", "")
        if board:
            add(f"{label} ({board[0].title()})", lambda b=board, n=name: run_board(b, s, n))
        else:
            add(f"{label} (careers page)", lambda u=url, n=name: fetch_page(u, s, n, log))
    for b in s["greenhouse"]:
        add(f"{b} (Greenhouse)", lambda b=b: fetch_greenhouse(b))
    for c in s["lever"]:
        add(f"{c} (Lever)", lambda c=c: fetch_lever(c))
    for b in s["ashby"]:
        add(f"{b} (Ashby)", lambda b=b: fetch_ashby(b))
    for c in s["smartrecruiters"]:
        add(f"{c} (SmartRecruiters)", lambda c=c: fetch_smartrecruiters(c))
    for u in s["jsonld_pages"]:
        add(f"{urlparse(u).netloc} (careers page)", lambda u=u: fetch_page(u, s, None, log))
    if s["adzuna_app_id"] and s["adzuna_app_key"]:
        add("Adzuna", lambda: fetch_adzuna(s))
    if s["reed_api_key"]:
        add("Reed", lambda: fetch_reed(s))
    return tasks


def run_scan(s, log, time_budget=240):
    """Fetch every source (several sites at once); return de-duplicated matches.
    Gives up on unfinished sources after time_budget seconds (Vercel caps a run at 300s)."""
    tasks = build_tasks(s, log)
    if not tasks:
        log("No sources set up yet. Add careers links or job-site keys in Settings.")
        return []
    results = {}
    pool = ThreadPoolExecutor(max_workers=WORKERS)
    futures = {pool.submit(fn): label for label, fn in tasks}
    deadline = time.time() + time_budget
    pending = set(futures)
    while pending and time.time() < deadline:
        done, pending = wait_futures(pending, timeout=deadline - time.time(),
                                     return_when=FIRST_COMPLETED)
        for f in done:
            results[futures[f]] = f
    pool.shutdown(wait=False, cancel_futures=True)

    found, keys = [], set()
    for label, _ in tasks:                 # report in the order they were listed
        f = results.get(label)
        if f is None:
            log(f"{label}: ran out of time; will try again next check")
            continue
        try:
            jobs = f.result()
        except Exception as e:
            log(f"{label}: failed ({e})")
            continue
        matches = [j for j in jobs if is_match(j, s)]
        log(f"{label}: {len(jobs)} roles, {len(matches)} matched")
        for j in matches:
            k = job_key(j)
            if k not in keys:
                keys.add(k)
                j["key"] = k
                found.append(j)
    return found
