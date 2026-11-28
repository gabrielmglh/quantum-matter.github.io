"""Collect the latest relevant arXiv papers separately for each followed author."""
import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AUTHORS_FILE = ROOT / "followed-authors.json"
OUTPUT_FILE = ROOT / "arxiv-feed.json"
PAPERS_PER_AUTHOR = 10
PAGE_SIZE = 60
MAX_PAGES = 5
REQUEST_INTERVAL = 3.5
CATEGORIES = ("hep-th", "cond-mat.str-el", "quant-ph", "math-ph")
API_URL = "https://export.arxiv.org/api/query"
NS = {"atom": "http://www.w3.org/2005/Atom",
      "os": "http://a9.com/-/spec/opensearch/1.1/"}


def normalize(name):
    text = unicodedata.normalize("NFKD", name).casefold()
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.findall(r"[a-z]+", text)


def matches_author(name, followed):
    actual, expected = normalize(name), normalize(followed)
    if not actual or not expected:
        return False
    # Accept hyphen/space differences, accents, and extra middle initials.
    # Do not accept a surname or initials alone.
    if "".join(actual) == "".join(expected):
        return True
    if "," in name:
        surname, given = name.split(",", 1)
        return matches_author(given.strip() + " " + surname.strip(), followed)
    if actual[-1] != expected[-1]:
        return False
    return "".join(w for w in actual[:-1] if len(w) > 1) == "".join(w for w in expected[:-1] if len(w) > 1) and bool(actual[:-1]) and any(len(w) > 1 for w in expected[:-1])


def author_query(author):
    words = normalize(author)
    if len(words) < 2:
        raise ValueError(f"Expected a full author name: {author}")
    # Combine phrase variants with the old token-based search and arXiv's
    # surname/given-name syntax. Always verify the returned author metadata.
    original = " ".join(author.split())
    if any(c in original for c in '\"\\'):
        raise ValueError(f"Unsupported character in author name: {author}")
    variants = [original, " ".join(words), "".join(words[:-1]) + " " + words[-1],
                words[-1] + ", " + " ".join(words[:-1])]
    # Keep the original hyphenated surname intact where possible.
    given, surname = original.rsplit(" ", 1)
    variants.append(surname + ", " + given)
    terms = [f'au:"{v}"' for v in dict.fromkeys(variants)]
    terms.append("(" + " AND ".join(f'au:"{w}"' for w in words) + ")")
    terms.append(f'au:{words[-1]}_{words[0]}')
    cats = " OR ".join(f"cat:{c}" for c in CATEGORIES)
    return "(" + " OR ".join(terms) + ") AND (" + cats + ")"


class ArxivClient:
    def __init__(self):
        self.last_request = None

    def fetch(self, query, start):
        params = urllib.parse.urlencode({"search_query": query, "start": start,
                                         "max_results": PAGE_SIZE, "sortBy": "submittedDate",
                                         "sortOrder": "descending"})
        request = urllib.request.Request(f"{API_URL}?{params}", headers={
            "User-Agent": "UEL-QMQFT-ResearchGroup/1.0", "Accept": "application/atom+xml"})
        for attempt in range(3):
            if self.last_request is not None:
                time.sleep(max(0, REQUEST_INTERVAL - (time.monotonic() - self.last_request)))
            try:
                with urllib.request.urlopen(request, timeout=90) as response:
                    root = ET.fromstring(response.read())
                if root.tag != f'{{{NS["atom"]}}}feed':
                    raise ValueError("Unexpected arXiv response (not an Atom feed)")
                total = int(root.findtext("os:totalResults", namespaces=NS))
                entries = root.findall("atom:entry", NS)
                if any("/abs/" not in e.findtext("atom:id", "", NS) or "/api/errors" in e.findtext("atom:id", "", NS) for e in entries):
                    raise ValueError("arXiv returned an error entry")
                if total < 0 or (start < total and not entries):
                    raise ValueError("Inconsistent arXiv result count")
                return total, entries
            except (urllib.error.URLError, TimeoutError, OSError, ET.ParseError, ValueError, TypeError) as exc:
                if attempt == 2:
                    raise RuntimeError(f"arXiv request failed: {exc}") from exc
                delay = 15 * (2 ** attempt)
                if isinstance(exc, urllib.error.HTTPError):
                    retry = exc.headers.get("Retry-After", "") if exc.headers else ""
                    if retry.isdigit():
                        delay = max(delay, int(retry))
                    if exc.code not in (408, 429, 500, 502, 503, 504):
                        raise
                print(f"  Request failed; retrying in {delay}s: {exc}", flush=True)
                time.sleep(delay)
            finally:
                self.last_request = time.monotonic()


def parse_entry(entry, followed_authors):
    raw_id = entry.findtext("atom:id", "", NS)
    arxiv_id = re.sub(r"v\d+$", "", raw_id.split("/abs/")[-1])
    if not re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-zA-Z.-]+/\d{7})", arxiv_id):
        raise ValueError(f"Invalid arXiv identifier: {raw_id}")
    authors = [a.findtext("atom:name", "", NS) for a in entry.findall("atom:author", NS)]
    matched = [f for f in followed_authors if any(matches_author(a, f) for a in authors)]
    subjects = [c.attrib.get("term", "") for c in entry.findall("atom:category", NS)]
    if not matched or not set(subjects).intersection(CATEGORIES):
        return None
    title = " ".join(entry.findtext("atom:title", "", NS).split())
    published = entry.findtext("atom:published", "", NS)
    date = datetime.fromisoformat(published.replace("Z", "+00:00")).date().isoformat()
    if not title or not authors:
        raise ValueError(f"Missing metadata for {arxiv_id}")
    return {"title": title, "category": "recent-preprint", "date": date,
            "author": ", ".join(authors), "reference": f"arXiv:{arxiv_id}",
            "subject": ", ".join(subjects),
            "abstract": " ".join(entry.findtext("atom:summary", "", NS).split()),
            "followedAuthors": matched, "image": "",
            "link": f"https://arxiv.org/abs/{arxiv_id}", "linkText": "arXiv",
            "pdf": f"https://arxiv.org/pdf/{arxiv_id}"}


def collect_author(client, author, followed_authors):
    query = author_query(author)
    selected = {}
    start = 0
    for _ in range(MAX_PAGES):
        total, entries = client.fetch(query, start)
        for entry in entries:
            paper = parse_entry(entry, followed_authors)
            if paper and author in paper["followedAuthors"]:
                selected.setdefault(paper["reference"], paper)
                if len(selected) >= PAPERS_PER_AUTHOR:
                    return list(selected.values())
        start += len(entries)
        if not entries or start >= total:
            break
    return list(selected.values())


def collect(client, followed_authors):
    papers = {}
    counts = {}
    errors = []
    for index, author in enumerate(followed_authors, 1):
        print(f"[{index}/{len(followed_authors)}] Fetching {author}...", flush=True)
        try:
            found = collect_author(client, author, followed_authors)
            counts[author] = len(found)
            for paper in found:
                papers.setdefault(paper["reference"], paper)
            print(f"  {author}: {len(found)} papers selected.", flush=True)
            if not found:
                print(f"  WARNING: no verified papers for {author}; check search and author variants.", flush=True)
        except Exception as exc:
            errors.append(f"{author}: {exc}")
            print(f"  ERROR: {errors[-1]}", flush=True)
    if errors:
        raise RuntimeError("Collection incomplete; previous feed preserved. " + "; ".join(errors))
    if not papers:
        raise RuntimeError("No verified papers; previous feed preserved.")
    return sorted(papers.values(), key=lambda p: (p["date"], p["reference"]), reverse=True), counts


def main():
    followed_authors = json.loads(AUTHORS_FILE.read_text(encoding="utf-8"))
    if not isinstance(followed_authors, list) or not followed_authors or not all(isinstance(a, str) and a.strip() for a in followed_authors):
        raise ValueError("Invalid followed-authors.json")
    followed_authors = list(dict.fromkeys(followed_authors))
    started = time.monotonic()
    papers, counts = collect(ArxivClient(), followed_authors)
    # Protect existing coverage if a query unexpectedly stops matching a known author.
    if OUTPUT_FILE.exists():
        old = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
        previously_present = {a for p in old.get("posts", []) for a in p.get("followedAuthors", [])}
        vanished = [a for a, n in counts.items() if n == 0 and a in previously_present]
        if vanished:
            raise RuntimeError("Previously matched authors now have zero results; previous feed preserved: " + ", ".join(vanished))
    data = {"updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"), "posts": papers}
    temporary = OUTPUT_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(OUTPUT_FILE)
    print("\nCollection summary:", flush=True)
    for author, count in counts.items():
        print(f"  {author}: {count} selected; {sum(author in p['followedAuthors'] for p in papers)} in final feed", flush=True)
    print(f"Unique papers: {len(papers)}. Elapsed: {time.monotonic() - started:.1f}s.", flush=True)
    print(f"Feed saved to: {OUTPUT_FILE}", flush=True)


if __name__ == "__main__":
    main()
