#!/usr/bin/env python3
"""Builds playlist.m3u and candidates.json from channels.json and sources.json.

Standard library only. Runs nightly in GitHub Actions and on every push:

    python scripts/build_registry.py            # fetch, match, write
    python scripts/build_registry.py --check    # also record a soft liveness hint

The liveness check runs from GitHub's runners, outside Türkiye, so geo-blocked
streams fail there. It is recorded as a hint only and never removes a url: the
TV app validates every candidate on the viewer's own network.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import json
import pathlib
import sys
import urllib.error
import urllib.request
from typing import Optional

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import registry_rules as rules  # noqa: E402

USER_AGENT = "Mozilla/5.0 (Linux; Android 9; MIBOX4) TR-TV-registry/1"
FETCH_TIMEOUT = 20
CHECK_TIMEOUT = 6
CHECKS_PER_CHANNEL = 3
SELF_PLACEHOLDER = "{registry}"
MAX_PREFERRED_HEIGHT = 1080
TALL_STREAM_PENALTY = 0.85


def fetch(url: str, headers: Optional[dict] = None, timeout: int = FETCH_TIMEOUT, limit: Optional[int] = None) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(limit) if limit else response.read()


def height_preference(height: Optional[int]) -> int:
    # an unknown height ranks like 720p: above SD variants, below a known 1080p
    if height is None:
        return 720
    return height if height <= MAX_PREFERRED_HEIGHT else MAX_PREFERRED_HEIGHT - 1


def priority(candidate: dict) -> float:
    height = candidate.get("heightHint")
    factor = TALL_STREAM_PENALTY if height and height > MAX_PREFERRED_HEIGHT else 1.0
    return candidate["trust"] * rules.TIER_WEIGHTS[candidate["tier"]] * factor


def load_candidates(sources: dict) -> tuple[list[rules.Candidate], dict]:
    candidates, report = [], {}
    for source in sources.get("sources", []):
        if not source.get("enabled", True):
            continue
        if SELF_PLACEHOLDER in source["url"]:
            # "self" is this repository's own output; the app reads it, the build does not
            report[source["id"]] = {"skipped": "this repository's output"}
            continue
        try:
            body = fetch(source["url"])
            if source["type"] == "iptvorg-api":
                parsed = rules.parse_iptvorg_api(
                    source["id"], source.get("trust", 0.5), source.get("filterCountry"), json.loads(body)
                )
            else:
                parsed = rules.parse_m3u(source["id"], source.get("trust", 0.5), body.decode("utf-8", "replace"))
            candidates.extend(parsed)
            report[source["id"]] = {"ok": True, "candidates": len(parsed)}
        except Exception as error:  # one broken source never stops the others
            report[source["id"]] = {"ok": False, "error": str(error)[:200]}
            print(f"warning: source {source['id']} failed: {error}", file=sys.stderr)
    return candidates, report


UNOFFICIAL_HOST_FACTOR = 0.75


def host_matches(url: str, hosts: list[str]) -> bool:
    host = (urllib.request.urlparse(url).hostname or "").lower()
    suffixes = [entry.strip().lower() for entry in hosts if entry.strip()]
    return bool(host) and any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)


def host_trust(url: str, trust: float, official_hosts: list[str]) -> float:
    # a restream of the same channel never outranks the broadcaster's own cdn
    if not official_hosts or host_matches(url, official_hosts):
        return trust
    return trust * UNOFFICIAL_HOST_FACTOR


def build_pools(
    registry: dict,
    candidates: list[rules.Candidate],
    blocked_hosts: list[str],
    official_hosts: list[str],
) -> dict:
    channels = [channel for channel in registry["channels"] if channel.get("enabled", True)]
    matcher = rules.ChannelMatcher(channels)
    pools = {channel["id"]: {} for channel in channels}

    def add(channel_id: str, entry: dict):
        pool = pools[channel_id]
        existing = pool.get(entry["url"])
        if existing is None or (existing["tier"] != rules.TIER_PINNED and priority(entry) > priority(existing)):
            pool[entry["url"]] = entry

    for channel in channels:
        channel_headers = {rules.canonical_header(k): v for k, v in channel.get("headers", {}).items() if rules.canonical_header(k)}
        for raw in channel.get("pinnedUrls", []):
            url, headers = rules.split_kodi_url(raw)
            add(channel["id"], {
                "url": url, "source": "pinned", "tier": rules.TIER_PINNED, "score": 1.0, "trust": 1.0,
                "headers": {**channel_headers, **headers}, "heightHint": None, "labels": [],
            })
    for candidate in candidates:
        if host_matches(candidate.url, blocked_hosts):
            continue
        match = matcher.match(candidate.name, candidate.tvg_id, candidate.alternative_name)
        if match is None:
            continue
        channel = next(c for c in channels if c["id"] == match.channel_id)
        channel_headers = {rules.canonical_header(k): v for k, v in channel.get("headers", {}).items() if rules.canonical_header(k)}
        add(match.channel_id, {
            "url": candidate.url, "source": candidate.source_id, "tier": match.tier, "score": round(match.score, 4),
            "trust": round(host_trust(candidate.url, candidate.trust, official_hosts), 4),
            "headers": {**channel_headers, **candidate.headers},
            "heightHint": candidate.height_hint, "labels": candidate.labels,
            "name": candidate.name, "tvgId": candidate.tvg_id,
        })
    return pools


def soft_check(entry: dict) -> dict:
    try:
        body = fetch(entry["url"], entry.get("headers"), timeout=CHECK_TIMEOUT, limit=64 * 1024)
        ok = body.lstrip(b"\xef\xbb\xbf").lstrip().startswith(b"#EXTM3U")
        return {"ok": ok, "reason": None if ok else "not a playlist"}
    except urllib.error.HTTPError as error:
        return {"ok": False, "reason": f"HTTP {error.code}"}
    except Exception as error:
        return {"ok": False, "reason": type(error).__name__}


def ordered(pool: dict) -> list[dict]:
    return sorted(
        pool.values(),
        key=lambda entry: (
            entry["tier"] != rules.TIER_PINNED,
            -priority(entry),
            -height_preference(entry.get("heightHint")),
            not (entry.get("ciCheck") or {}).get("ok", False),
            entry["url"],
        ),
    )


def extinf(channel: dict, entry: dict) -> list[str]:
    attributes = [
        f'tvg-id="{channel["id"]}"',
        f'tvg-chno="{channel["rank"]}"',
        f'tvg-name="{channel["name"]}"',
    ]
    if channel.get("logo"):
        attributes.append(f'tvg-logo="{channel["logo"]}"')
    attributes.append(f'group-title="{channel.get("group", "")}"')
    lines = [f'#EXTINF:-1 {" ".join(attributes)},{channel["name"]}']
    headers = entry.get("headers") or {}
    options = {rules.REFERER: "http-referrer", rules.USER_AGENT: "http-user-agent", rules.ORIGIN: "http-origin"}
    for header, option in options.items():
        if headers.get(header):
            lines.append(f"#EXTVLCOPT:{option}={headers[header]}")
    lines.append(entry["url"])
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="record a soft liveness hint (never drops urls)")
    parser.add_argument("--root", default=str(ROOT), help="registry folder (default: this repository)")
    args = parser.parse_args()
    root = pathlib.Path(args.root)

    registry = json.loads((root / "channels.json").read_text(encoding="utf-8"))
    sources = json.loads((root / "sources.json").read_text(encoding="utf-8"))
    candidates, source_report = load_candidates(sources)
    pools = build_pools(registry, candidates, sources.get("blockedHosts", []), sources.get("officialHosts", []))

    if args.check:
        targets = [
            entry for pool in pools.values()
            for entry in ordered(pool)[:CHECKS_PER_CHANNEL]
        ]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            for entry, result in zip(targets, executor.map(soft_check, targets)):
                entry["ciCheck"] = result

    channels = sorted((c for c in registry["channels"] if c.get("enabled", True)), key=lambda c: c["rank"])
    playlist = ["#EXTM3U"]
    report_channels = []
    for channel in channels:
        entries = ordered(pools[channel["id"]])
        if entries:
            playlist.extend(extinf(channel, entries[0]))
        report_channels.append({
            "id": channel["id"],
            "rank": channel["rank"],
            "name": channel["name"],
            "candidates": [
                {
                    "url": entry["url"],
                    "source": entry["source"],
                    "tier": entry["tier"],
                    "tierCode": rules.TIER_CODES[entry["tier"]],
                    "score": entry["score"],
                    "trust": entry["trust"],
                    "priority": round(priority(entry), 4),
                    "heightHint": entry.get("heightHint"),
                    "headers": entry.get("headers") or {},
                    "labels": entry.get("labels") or [],
                    "matchedName": entry.get("name"),
                    "matchedTvgId": entry.get("tvgId"),
                    "ciCheck": entry.get("ciCheck"),
                }
                for entry in entries
            ],
        })

    (root / "playlist.m3u").write_text("\n".join(playlist) + "\n", encoding="utf-8")
    candidates_doc = {
        "schemaVersion": 1,
        "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ciCheckIsHintOnly": True,
        "sources": source_report,
        "channels": report_channels,
    }
    (root / "candidates.json").write_text(json.dumps(candidates_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    with_links = sum(1 for channel in report_channels if channel["candidates"])
    print(f"{with_links}/{len(report_channels)} enabled channels have at least one candidate")
    for channel in report_channels:
        if not channel["candidates"]:
            print(f"  no candidate: {channel['rank']:>2} {channel['name']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
