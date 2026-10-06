"""
Job-board scanner: fetches roles from company job boards and UK job APIs,
then keeps the graduate roles in London. Used by app.py.
"""
import html
import json
import re
import time
import urllib.robotparser
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

USER_AGENT = "GradJobWatcher/1.0 (personal job alert app)"
REQUEST_DELAY = 1.5   # seconds between requests to the same site: be a polite guest

DEFAULT_SETTINGS = {
    "greenhouse": [],
    "lever": [],
    "ashby": [],
    "smartrecruiters": [],
    "jsonld_pages": [],
    "adzuna_app_id": "",
    "adzuna_app_key": "",
    "reed_api_key": "",
    "search_terms": ["graduate", "graduate scheme"],
    "search_location": "London",
    "include": ["graduate", "grad scheme", "grad programme", "new grad",
                "entry level", "entry-level", "early career", "early-career",
                "trainee", "junior"],
    "exclude": ["senior", "lead", "principal", "manager", "head of", "director",
                "vp", "staff", "experienced"],
    "locations": ["london", "canary wharf", "shoreditch", "kings cross", "king's cross",
                  "holborn", "southwark", "westminster", "stratford", "croydon"],
    "location_exclude": ["new london", "london, on", "london, ontario", "london, ky",
                         "london, oh", "ontario", "canada"],
    "email": {"enabled": False, "smtp_host": "smtp.gmail.com", "smtp_port": 587,
              "username": "", "password": "", "to": ""},
}

session = requests.Session()
session.headers["User-Agent"] = USER_AGENT


_last_hit = {}


def polite_wait(url):
    """Only pause between requests to the same site, so a long list of
    companies still fits inside a serverless time limit."""
    host = urlparse(url).netloc
    wait = REQUEST_DELAY - (time.time() - _last_hit.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    _last_hit[host] = time.time()


def get_json(url, **kwargs):
    polite_wait(url)
    r = session.get(url, timeout=20, **kwargs)
    r.raise_for_status()
    return r.json()


def clean(text):
    text = html.unescape(re.sub(r"<[^>]+>", "", str(text or "")))
    return re.sub(r"\s+", " ", text).strip()


def make_job(source, company, title, location, url, posted=""):
    return {"source": source, "company": clean(company), "title": clean(title),
            "location": clean(location), "url": url or "",
            "posted": str(posted or "")[:10]}


# ----------------------------------------------------------------- sources

def fetch_greenhouse(board, s):
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs")
    return [make_job("Greenhouse", board, j.get("title"),
                     (j.get("location") or {}).get("name"), j.get("absolute_url"),
                     j.get("first_published") or j.get("updated_at"))
            for j in data.get("jobs", [])]


def fetch_lever(company, s):
    data = get_json(f"https://api.lever.co/v0/postings/{company}", params={"mode": "json"})
    jobs = []
    for j in data:
        ts = j.get("createdAt")
        posted = datetime.fromtimestamp(ts / 1000, timezone.utc).date().isoformat() if ts else ""
        jobs.append(make_job("Lever", company, j.get("text"),
                             (j.get("categories") or {}).get("location"),
                             j.get("hostedUrl"), posted))
    return jobs


def fetch_ashby(board, s):
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{board}")
    return [make_job("Ashby", board, j.get("title"), j.get("location"),
                     j.get("jobUrl"), j.get("publishedAt"))
            for j in data.get("jobs", []) if j.get("isListed", True)]


def fetch_smartrecruiters(company, s):
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
                                 (j.get("company") or {}).get("name") or company,
                                 j.get("name"), where,
                                 f"https://jobs.smartrecruiters.com/{company}/{j.get('id')}",
                                 j.get("releasedDate")))
        offset += len(items)
        if not items or offset >= data.get("totalFound", 0):
            return jobs


def fetch_adzuna(_, s):
    jobs = []
    for term in s["search_terms"]:
        params = {"app_id": s["adzuna_app_id"], "app_key": s["adzuna_app_key"],
                  "what": term, "results_per_page": 50, "max_days_old": 3,
                  "sort_by": "date"}
        if s["search_location"]:
            params["where"] = s["search_location"]
        data = get_json("https://api.adzuna.com/v1/api/jobs/gb/search/1", params=params)
        for j in data.get("results", []):
            jobs.append(make_job("Adzuna", (j.get("company") or {}).get("display_name"),
                                 j.get("title"), (j.get("location") or {}).get("display_name"),
                                 j.get("redirect_url"), j.get("created")))
    return jobs


def fetch_reed(_, s):
    jobs = []
    for term in s["search_terms"]:
        params = {"keywords": term, "resultsToTake": 100}
        if s["search_location"]:
            params["locationName"] = s["search_location"]
        data = get_json("https://www.reed.co.uk/api/1.0/search", params=params,
                        auth=(s["reed_api_key"], ""))   # key as username, blank password
        for j in data.get("results", []):
            jobs.append(make_job("Reed", j.get("employerName"), j.get("jobTitle"),
                                 j.get("locationName"), j.get("jobUrl"), j.get("date")))
    return jobs


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


def fetch_jsonld(url, s):
    if not allowed_by_robots(url):
        raise RuntimeError("robots.txt doesn't allow this page")
    polite_wait(url)
    r = session.get(url, timeout=20)
    r.raise_for_status()
    jobs = []
    for block in LD_RE.findall(r.text):
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
            jobs.append(make_job("Careers page", org or urlparse(url).netloc,
                                 p.get("title"), loc, p.get("url") or url,
                                 p.get("datePosted")))
    return jobs


# ------------------------------------------------------------- filtering

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
    if _has_term(title, s["exclude"], whole_word=True):
        return False
    loc = job["location"].lower()
    if loc and _has_term(loc, s["location_exclude"], whole_word=True):
        return False
    if s["locations"] and loc and not _has_term(loc, s["locations"], whole_word=True):
        return False
    return True


def job_key(job):
    norm = lambda x: re.sub(r"[^a-z0-9]+", " ", x.lower()).strip()
    return f"{norm(job['company'])}|{norm(job['title'])}"


def build_tasks(s):
    tasks = [(f"Greenhouse: {b}", fetch_greenhouse, b) for b in s["greenhouse"]]
    tasks += [(f"Lever: {c}", fetch_lever, c) for c in s["lever"]]
    tasks += [(f"Ashby: {b}", fetch_ashby, b) for b in s["ashby"]]
    tasks += [(f"SmartRecruiters: {c}", fetch_smartrecruiters, c) for c in s["smartrecruiters"]]
    tasks += [(f"Careers page: {u}", fetch_jsonld, u) for u in s["jsonld_pages"]]
    if s["adzuna_app_id"] and s["adzuna_app_key"]:
        tasks.append(("Adzuna", fetch_adzuna, None))
    if s["reed_api_key"]:
        tasks.append(("Reed", fetch_reed, None))
    return tasks


def run_scan(s, log, time_budget=240):
    """Fetch every source; return de-duplicated matching jobs. log(str) records progress.
    Stops starting new sources after time_budget seconds (Vercel caps a run at 300s)."""
    started = time.time()
    tasks = build_tasks(s)
    if not tasks:
        log("No sources set up yet. Add companies or API keys in Settings.")
        return []
    found, seen = [], set()
    for i, (label, fn, arg) in enumerate(tasks):
        if time.time() - started > time_budget:
            log(f"Ran out of time: skipped {len(tasks) - i} source(s). Trim the list or check more often.")
            break
        try:
            jobs = fn(arg, s)
            matches = [j for j in jobs if is_match(j, s)]
            log(f"{label}: {len(jobs)} roles, {len(matches)} matched")
            for j in matches:
                k = job_key(j)
                if k not in seen:
                    seen.add(k)
                    j["key"] = k
                    found.append(j)
        except Exception as e:
            log(f"{label}: failed ({e})")
    return found
