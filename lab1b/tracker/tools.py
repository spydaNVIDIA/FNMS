"""Standalone search, guarded fetch, and finish tools.

Examples:
    python -m tracker.tools search_web "OpenVLA release"
    python -m tracker.tools fetch_article https://github.com/openvla/openvla
    python -m tracker.tools finish report.json
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import socket
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from .config import TrackerConfig, load_config
from .errors import PolicyViolation, TrackerError, classify_http_failure
from .types import ArticleRecord, Failure

_TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "source",
}


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise PolicyViolation(Failure("policy", False, "URL must include a hostname"))
    try:
        port = parts.port
    except ValueError as exc:
        raise PolicyViolation(Failure("policy", False, "URL port is invalid")) from exc
    default_port = (parts.scheme.lower() == "http" and port == 80) or (
        parts.scheme.lower() == "https" and port == 443
    )
    netloc = host if port is None or default_port else f"{host}:{port}"
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_QUERY_KEYS
        ],
        doseq=True,
    )
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), netloc, path, query, ""))


def _host_allowed(host: str, allowed_hosts: tuple[str, ...]) -> bool:
    return any(host == allowed or host.endswith(f".{allowed}") for allowed in allowed_hosts)


def _resolve_public_ips(host: str, port: int) -> list[str]:
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise TrackerError(Failure("dns", True, f"DNS resolution failed for {host}")) from exc
    addresses = sorted({answer[4][0].split("%")[0] for answer in answers})
    if not addresses:
        raise TrackerError(Failure("dns", True, f"DNS returned no addresses for {host}"))
    for value in addresses:
        address = ipaddress.ip_address(value)
        if not address.is_global:
            raise PolicyViolation(
                Failure("policy", False, f"Host resolves to forbidden address {address}")
            )
    return addresses


def validate_fetch_url(url: str, config: TrackerConfig) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in config.allowed_schemes:
        raise PolicyViolation(Failure("policy", False, "Only HTTP(S) URLs are allowed"))
    if parts.username is not None or parts.password is not None:
        raise PolicyViolation(Failure("policy", False, "URL user information is forbidden"))
    host = (parts.hostname or "").lower().rstrip(".")
    if not _host_allowed(host, config.allowed_hosts):
        raise PolicyViolation(Failure("policy", False, f"Host is not allowlisted: {host}"))
    try:
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise PolicyViolation(Failure("policy", False, "URL port is invalid")) from exc
    _resolve_public_ips(host, port)
    return normalize_url(url)


def _article_text(content: bytes, content_type: str) -> tuple[str, str]:
    decoded = content.decode("utf-8", errors="replace")
    if content_type in {"text/html", "application/xhtml+xml"}:
        soup = BeautifulSoup(decoded, "html.parser")
        for element in soup(["script", "style", "noscript", "svg"]):
            element.decompose()
        title = soup.title.get_text(" ", strip=True) if soup.title else "Untitled"
        text = "\n".join(line for line in soup.get_text("\n", strip=True).splitlines() if line)
        return title[:500], text
    return "Untitled", decoded


def fetch_article(
    url: str,
    config: TrackerConfig | None = None,
    client: httpx.Client | None = None,
) -> ArticleRecord:
    config = config or load_config()
    owns_client = client is None
    client = client or httpx.Client(
        timeout=float(config.fetch["timeout_seconds"]),
        follow_redirects=False,
        headers={"User-Agent": "FNMS-Robotics-Tracker/1.0"},
    )
    current_url = url
    try:
        for redirect_count in range(int(config.fetch["max_redirects"]) + 1):
            canonical_url = validate_fetch_url(current_url, config)
            try:
                with client.stream("GET", canonical_url) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        if redirect_count >= int(config.fetch["max_redirects"]):
                            raise PolicyViolation(
                                Failure("policy", False, "Redirect limit exceeded")
                            )
                        location = response.headers.get("location")
                        if not location:
                            raise PolicyViolation(
                                Failure("policy", False, "Redirect has no Location header")
                            )
                        current_url = urljoin(canonical_url, location)
                        continue
                    if response.status_code >= 400:
                        body = response.read().decode("utf-8", errors="replace")
                        raise TrackerError(
                            classify_http_failure(response.status_code, response.headers, body)
                        )
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                    if content_type not in set(config.fetch["allowed_content_types"]):
                        raise PolicyViolation(
                            Failure("policy", False, f"Unsupported content type: {content_type}")
                        )
                    max_bytes = int(config.fetch["max_bytes"])
                    try:
                        declared_size = int(response.headers.get("content-length", "0"))
                    except ValueError:
                        declared_size = 0
                    if declared_size > max_bytes:
                        raise PolicyViolation(
                            Failure("policy", False, "Response exceeds configured size limit")
                        )
                    payload = bytearray()
                    for chunk in response.iter_bytes():
                        payload.extend(chunk)
                        if len(payload) > max_bytes:
                            raise PolicyViolation(
                                Failure("policy", False, "Response exceeds configured size limit")
                            )
            except httpx.TimeoutException as exc:
                raise TrackerError(Failure("timeout", True, "Fetch timed out")) from exc
            except httpx.NetworkError as exc:
                raise TrackerError(Failure("network", True, f"Network failure: {exc}")) from exc
            title, text = _article_text(bytes(payload), content_type)
            digest = hashlib.sha256(bytes(payload)).hexdigest()
            return ArticleRecord(
                title=title,
                url=url,
                canonical_url=canonical_url,
                fetched_at=datetime.now(UTC).isoformat(),
                status="fetched",
                content_hash=digest,
                text=(
                    "BEGIN_UNTRUSTED_WEB_CONTENT\n"
                    f"Source URL: {canonical_url}\n"
                    f"{text[:120000]}\n"
                    "END_UNTRUSTED_WEB_CONTENT"
                ),
            )
        raise PolicyViolation(Failure("policy", False, "Redirect limit exceeded"))
    finally:
        if owns_client:
            client.close()


def search_web(
    query: str,
    config: TrackerConfig | None = None,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    config = config or load_config()
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        raise TrackerError(Failure("authentication", False, "TAVILY_API_KEY is not set"))
    owns_client = client is None
    client = client or httpx.Client(timeout=float(config.search["timeout_seconds"]))
    try:
        try:
            response = client.post(
                str(config.search["url"]),
                json={
                    "api_key": api_key,
                    "query": query,
                    "max_results": int(config.search["max_results"]),
                    "search_depth": "advanced",
                    "include_domains": list(config.allowed_hosts),
                },
            )
        except httpx.TimeoutException as exc:
            raise TrackerError(Failure("timeout", True, "Search timed out")) from exc
        except httpx.NetworkError as exc:
            raise TrackerError(Failure("network", True, f"Search network failure: {exc}")) from exc
        if response.status_code >= 400:
            raise TrackerError(
                classify_http_failure(response.status_code, response.headers, response.text)
            )
        data = response.json()
        return [
            {
                "title": str(item.get("title", "Untitled"))[:500],
                "url": str(item.get("url", "")),
                "snippet": str(item.get("content", ""))[:3000],
                "published_date": item.get("published_date"),
                "score": item.get("score"),
            }
            for item in data.get("results", [])
            if item.get("url")
        ]
    finally:
        if owns_client:
            client.close()


def finish(report: dict[str, Any], top_k: int = 5) -> dict[str, Any]:
    if not isinstance(report, dict):
        raise ValueError("report must be an object")
    developments = report.get("developments")
    if not isinstance(developments, list):
        raise ValueError("report.developments must be a list")
    if len(developments) != top_k:
        raise ValueError(f"report must have exactly top_k={top_k} developments")
    required = {"key", "title", "summary", "score", "source_urls"}
    for index, item in enumerate(developments):
        if not isinstance(item, dict) or not required.issubset(item):
            raise ValueError(f"development {index} is missing required fields")
        if not isinstance(item["source_urls"], list) or not item["source_urls"]:
            raise ValueError(f"development {index} has no source URLs")
    return report


def _main() -> None:
    parser = argparse.ArgumentParser(description="Call tracker tools without a model")
    parser.add_argument("--config", help="Path to config.yaml")
    subparsers = parser.add_subparsers(dest="tool", required=True)
    search_parser = subparsers.add_parser("search_web")
    search_parser.add_argument("query")
    fetch_parser = subparsers.add_parser("fetch_article")
    fetch_parser.add_argument("url")
    finish_parser = subparsers.add_parser("finish")
    finish_parser.add_argument("report_json", type=Path)
    args = parser.parse_args()
    config = load_config(args.config)
    load_dotenv(config.path.parent / ".env")
    try:
        if args.tool == "search_web":
            result: Any = search_web(args.query, config)
        elif args.tool == "fetch_article":
            article = fetch_article(args.url, config)
            result = article.persistence_dict() | {"text": article.text}
        else:
            result = finish(
                json.loads(args.report_json.read_text(encoding="utf-8")), config.top_k
            )
    except (TrackerError, ValueError, OSError, json.JSONDecodeError) as exc:
        category = exc.failure.category if isinstance(exc, TrackerError) else "validation"
        print(
            json.dumps({"status": "failed", "error_class": category, "message": str(exc)}),
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    _main()
