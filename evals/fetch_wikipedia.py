"""Build the evaluation corpus: 40 Wikipedia articles pinned to fixed revisions.

The first run resolves each title's current revision id and writes
``evals/data/wikipedia_manifest.json`` (committed). Later runs fetch exactly
those revisions, so every run of the evaluation sees the same text. The article
text itself (CC BY-SA) is written to ``evals/data/wikipedia/`` and not committed.

    python evals/fetch_wikipedia.py
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from deeptrace_agent.tools import html_to_text  # noqa: E402

DATA = ROOT / "evals" / "data"
MANIFEST = DATA / "wikipedia_manifest.json"
OUT = DATA / "wikipedia"
API = "https://en.wikipedia.org/w/api.php"
HEADERS = {"User-Agent": "deeptrace-eval/0.3 (https://github.com/limengge426/deeptrace-research-agent; research evaluation)"}

TOPICS = {
    "energy": [
        "Heat pump", "Solar panel", "Wind turbine", "Nuclear power", "Lithium-ion battery",
        "Hydrogen economy", "Geothermal energy", "Electric vehicle", "Smart grid", "Coal-fired power station",
    ],
    "biomedicine": [
        "MRNA vaccine", "CRISPR gene editing", "Antimicrobial resistance", "Insulin", "Penicillin",
        "Malaria", "Gut microbiota", "Alzheimer's disease", "Type 2 diabetes", "Vaccination",
    ],
    "computing": [
        "Transformer (deep learning architecture)", "Large language model", "Reinforcement learning from human feedback",
        "Quantum computing", "Public-key cryptography", "Blockchain", "Moore's law", "Graphics processing unit",
        "Retrieval-augmented generation", "Convolutional neural network",
    ],
    "history": [
        "Great Depression", "Bretton Woods system", "Industrial Revolution", "Printing press", "Silk Road",
        "Marshall Plan", "Euro", "Hyperinflation", "Black Death", "Green Revolution",
    ],
}

_DROP_SECTIONS = re.compile(r"\n(See also|References|Notes|Further reading|External links|Bibliography|Citations|Sources)\n.*",
                            re.DOTALL)


def _slug(title: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", title).strip("_").lower()


def resolve_revisions(client: httpx.Client) -> dict:
    manifest = {"source": "en.wikipedia.org", "license": "CC BY-SA 4.0", "resolved_at": time.strftime("%Y-%m-%d"),
                "articles": []}
    for topic, titles in TOPICS.items():
        for title in titles:
            r = client.get(API, params={"action": "query", "prop": "revisions", "rvprop": "ids|timestamp",
                                        "titles": title, "redirects": 1, "format": "json", "formatversion": 2})
            r.raise_for_status()
            page = r.json()["query"]["pages"][0]
            if "missing" in page:
                raise SystemExit(f"article not found: {title}")
            rev = page["revisions"][0]
            manifest["articles"].append({"topic": topic, "title": page["title"], "revid": rev["revid"],
                                         "timestamp": rev["timestamp"]})
            time.sleep(0.2)
    return manifest


def fetch_revision(client: httpx.Client, article: dict) -> str:
    r = client.get(API, params={"action": "parse", "oldid": article["revid"], "prop": "text",
                                "disableeditsection": 1, "format": "json", "formatversion": 2})
    r.raise_for_status()
    markup = r.json()["parse"]["text"]
    # Drop tables, reference markers and navigation boxes before extracting text.
    markup = re.sub(r"<table.*?</table>", "", markup, flags=re.DOTALL)
    markup = re.sub(r'<sup[^>]*class="[^"]*reference[^"]*".*?</sup>', "", markup, flags=re.DOTALL)
    markup = re.sub(r'<div[^>]*class="[^"]*(navbox|reflist|hatnote|thumb)[^"]*".*?</div>', "", markup, flags=re.DOTALL)
    _, text = html_to_text(markup)
    text = _DROP_SECTIONS.sub("\n", "\n" + text).strip()
    return f"# {article['title']}\n\n{text}\n"


def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        if MANIFEST.exists():
            manifest = json.loads(MANIFEST.read_text())
        else:
            manifest = resolve_revisions(client)
            MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")
        total = 0
        for article in manifest["articles"]:
            path = OUT / f"{article['topic']}__{_slug(article['title'])}.md"
            if not path.exists():
                path.write_text(fetch_revision(client, article), encoding="utf-8")
                time.sleep(0.3)
            size = path.stat().st_size
            total += size
            print(f"  {article['topic']:<12} {article['title']:<45} rev {article['revid']}  {size / 1024:6.1f} KB")
    print(f"{len(manifest['articles'])} articles, {total / 1024 / 1024:.1f} MB in {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
