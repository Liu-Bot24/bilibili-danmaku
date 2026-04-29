#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET


INDEXNOW_ENDPOINT = "https://api.indexnow.org/indexnow"
INDEXNOW_KEY = os.environ.get("INDEXNOW_KEY", "782ce4166c93b3da40b54acae9b34686")
DEFAULT_STATE_PATH = "/www/wwwroot/bilibili_danmaku/var/indexnow_state.json"
DEFAULT_SITEMAPS = (
    "https://danmu.liu-qi.cn/sitemap.xml",
    "https://blog.liu-qi.cn/sitemap.xml",
)
SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
MAX_URLS_PER_REQUEST = 10000


def fetch_xml(url: str) -> ET.Element:
    with urlopen(url, timeout=30) as response:
        return ET.fromstring(response.read())


def read_sitemap(url: str, seen: set[str] | None = None) -> list[dict[str, str]]:
    seen = seen or set()
    if url in seen:
        return []
    seen.add(url)

    root = fetch_xml(url)
    tag = root.tag.rsplit("}", 1)[-1]
    if tag == "sitemapindex":
        entries: list[dict[str, str]] = []
        for sitemap in root.findall("sm:sitemap", SITEMAP_NS):
            loc = sitemap.findtext("sm:loc", default="", namespaces=SITEMAP_NS).strip()
            if loc:
                entries.extend(read_sitemap(loc, seen))
        return entries

    if tag != "urlset":
        return []

    entries = []
    for url_node in root.findall("sm:url", SITEMAP_NS):
        loc = url_node.findtext("sm:loc", default="", namespaces=SITEMAP_NS).strip()
        if not loc:
            continue
        lastmod = url_node.findtext("sm:lastmod", default="", namespaces=SITEMAP_NS).strip()
        entries.append({"url": loc, "lastmod": lastmod})
    return entries


def load_state(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items()}


def save_state(path: Path, state: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(path)


def chunked(items: list[str], size: int) -> list[list[str]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def submit_urls(host: str, urls: list[str]) -> list[dict[str, object]]:
    results = []
    for url_chunk in chunked(urls, MAX_URLS_PER_REQUEST):
        payload = json.dumps(
            {"host": host, "key": INDEXNOW_KEY, "urlList": url_chunk},
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(
            INDEXNOW_ENDPOINT,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8", "replace")
                results.append({"status": response.status, "count": len(url_chunk), "body": body})
        except HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            results.append({"status": exc.code, "count": len(url_chunk), "body": body})
        except URLError as exc:
            results.append({"status": "error", "count": len(url_chunk), "body": str(exc)})
    return results


def collect_entries(sitemaps: tuple[str, ...]) -> list[dict[str, str]]:
    by_url: dict[str, str] = {}
    for sitemap_url in sitemaps:
        for entry in read_sitemap(sitemap_url):
            by_url[entry["url"]] = entry.get("lastmod", "")
    return [{"url": url, "lastmod": lastmod} for url, lastmod in sorted(by_url.items())]


def main() -> int:
    parser = argparse.ArgumentParser(description="Submit sitemap URLs to IndexNow.")
    parser.add_argument("--state", default=DEFAULT_STATE_PATH)
    parser.add_argument("--sitemap", action="append", dest="sitemaps")
    parser.add_argument("--mark-seen", action="store_true", help="Record current URLs without submitting.")
    parser.add_argument("--submit-all", action="store_true", help="Submit all URLs regardless of previous state.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    sitemaps = tuple(args.sitemaps or DEFAULT_SITEMAPS)
    state_path = Path(args.state)
    state = load_state(state_path)
    entries = collect_entries(sitemaps)

    if args.submit_all:
        pending = entries
    else:
        pending = [entry for entry in entries if state.get(entry["url"]) != entry.get("lastmod", "")]

    grouped: dict[str, list[str]] = {}
    for entry in pending:
        host = urlparse(entry["url"]).netloc
        grouped.setdefault(host, []).append(entry["url"])

    result: dict[str, object] = {
        "known_url_count": len(entries),
        "pending_url_count": len(pending),
        "hosts": {host: len(urls) for host, urls in sorted(grouped.items())},
        "submitted": {},
        "mark_seen": args.mark_seen,
        "dry_run": args.dry_run,
    }

    if not args.dry_run and not args.mark_seen:
        submitted: dict[str, list[dict[str, object]]] = {}
        for host, urls in sorted(grouped.items()):
            submitted[host] = submit_urls(host, urls)
        result["submitted"] = submitted

    if not args.dry_run:
        successful_hosts = set(grouped)
        if not args.mark_seen:
            successful_hosts = {
                host
                for host, responses in (result["submitted"] or {}).items()
                if all(item.get("status") in (200, 202) for item in responses)
            }
        for entry in entries:
            host = urlparse(entry["url"]).netloc
            if args.mark_seen or host in successful_hosts:
                state[entry["url"]] = entry.get("lastmod", "")
        save_state(state_path, state)

    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
