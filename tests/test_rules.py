"""Runs the shared vectors against the Python rules. The app's Kotlin tests run the same file."""

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import registry_rules as rules  # noqa: E402

VECTORS = json.loads((ROOT / "tests" / "match_vectors.json").read_text(encoding="utf-8"))
REGISTRY = [dict(channel, rank=index + 1) for index, channel in enumerate(VECTORS["registry"])]


def test_normalization_vectors():
    failures = [
        f'normalize("{case["input"]}") = "{rules.normalize(case["input"])}", expected "{case["expected"]}"'
        for case in VECTORS["normalize"]
        if rules.normalize(case["input"]) != case["expected"]
    ]
    assert not failures, "\n".join(failures)


def test_match_vectors():
    matcher = rules.ChannelMatcher(REGISTRY)
    failures = []
    for case in VECTORS["match"]:
        result = matcher.match(case["name"], case.get("tvgId") or None)
        channel_id = result.channel_id if result else None
        if channel_id != case.get("expected"):
            failures.append(f'match("{case["name"]}", {case.get("tvgId")!r}) = {channel_id}, expected {case.get("expected")}')
        elif case.get("tier") and (result is None or result.tier != case["tier"]):
            failures.append(f'match("{case["name"]}") tier = {result and result.tier}, expected {case["tier"]}')
        elif case.get("notExpected") and channel_id == case["notExpected"]:
            failures.append(f'match("{case["name"]}") must not be {case["notExpected"]}')
    assert not failures, "\n".join(failures)


def test_headers_formats():
    url, headers = rules.split_kodi_url(
        "https://cdn.example/atv.m3u8|Referer=https%3A%2F%2Fwww.atv.com.tr%2F&User-Agent=TV&Cookie=x"
    )
    assert url == "https://cdn.example/atv.m3u8"
    assert headers == {"Referer": "https://www.atv.com.tr/", "User-Agent": "TV"}

    playlist = "\n".join([
        "#EXTM3U",
        '#EXTINF:-1 tvg-id="KanalD.tr@SD",Kanal D (1080p)',
        "#EXTVLCOPT:http-referrer=https://www.kanald.com.tr/",
        "#EXTVLCOPT:http-user-agent=Mozilla/5.0",
        "https://cdn.example/kanald.m3u8",
    ])
    [candidate] = rules.parse_m3u("iptvorg-tr", 0.9, playlist)
    assert candidate.tvg_id == "KanalD.tr@SD"
    assert candidate.height_hint == 1080
    assert candidate.headers == {"Referer": "https://www.kanald.com.tr/", "User-Agent": "Mozilla/5.0"}


def test_kodi_round_trip():
    headers = {"Referer": "https://x.tr/", "User-Agent": "Mozilla/5.0 (X11; Linux)"}
    assert rules.split_kodi_url(rules.to_kodi_url("https://a/b.m3u8", headers)) == ("https://a/b.m3u8", headers)
