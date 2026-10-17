"""Channel-matching rules shared with the TR TV app.

This is a line-by-line mirror of the Kotlin module ``data/registry`` in the app
(NameNormalizer, JaroWinkler, ChannelMatcher, CandidateParser, PlaybackHeaders).
Both implementations run the same vectors in ``tests/match_vectors.json``; keep
them in sync when a rule changes.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Optional
from urllib.parse import unquote

# --- normalization -----------------------------------------------------------

_TURKISH_FOLD = str.maketrans({
    "İ": "i", "I": "i", "ı": "i",
    "Ş": "s", "ş": "s",
    "Ğ": "g", "ğ": "g",
    "Ü": "u", "ü": "u",
    "Ö": "o", "ö": "o",
    "Ç": "c", "ç": "c",
    "Â": "a", "â": "a",
    "Î": "i", "î": "i",
    "Û": "u", "û": "u",
})
_BRACKETS = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_SEPARATORS = re.compile(r"[^a-z0-9]+")
_RESOLUTION = re.compile(r"\d{3,4}[pi]")
_LETTER_DIGIT = re.compile(r"(?<=[a-z])(?=[0-9])|(?<=[0-9])(?=[a-z])")
_NOISE = {"hd", "fhd", "uhd", "sd", "hq", "4k", "8k", "hevc", "h264", "h265"}
_HEIGHT_HINT = re.compile(r"(\d{3,4})[pi]\b", re.IGNORECASE)
_FOUR_K = re.compile(r"\b4k\b", re.IGNORECASE)


def fold(raw: str) -> str:
    lower = raw.translate(_TURKISH_FOLD).lower()
    decomposed = unicodedata.normalize("NFD", lower)
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def tokens(raw: str) -> list[str]:
    without_notes = _BRACKETS.sub(" ", fold(raw))
    coarse = [
        token for token in _SEPARATORS.split(without_notes)
        if token and token not in _NOISE and not _RESOLUTION.fullmatch(token)
    ]
    result = [part for token in coarse for part in _LETTER_DIGIT.split(token) if part]
    if len(result) > 1 and result[-1] == "tv":
        result = result[:-1]
    return result


def normalize(raw: str) -> str:
    return " ".join(tokens(raw))


def normalize_id(identifier: Optional[str]) -> Optional[str]:
    if identifier is None:
        return None
    value = identifier.split("@", 1)[0].strip().lower()
    return value or None


def height_hint(raw: str) -> Optional[int]:
    match = _HEIGHT_HINT.search(raw)
    if match:
        return int(match.group(1))
    return 2160 if _FOUR_K.search(raw) else None


def numeric_tokens(values: list[str]) -> list[str]:
    return [token for token in values if token.isdigit()]


# --- Jaro-Winkler ------------------------------------------------------------

def jaro_winkler(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    window = max(0, max(len(a), len(b)) // 2 - 1)
    a_matched = [False] * len(a)
    b_matched = [False] * len(b)
    matches = 0
    for i, char in enumerate(a):
        start = max(0, i - window)
        end = min(len(b) - 1, i + window)
        for j in range(start, end + 1):
            if b_matched[j] or char != b[j]:
                continue
            a_matched[i] = b_matched[j] = True
            matches += 1
            break
    if matches == 0:
        return 0.0
    transpositions = 0
    k = 0
    for i, char in enumerate(a):
        if not a_matched[i]:
            continue
        while not b_matched[k]:
            k += 1
        if char != b[k]:
            transpositions += 1
        k += 1
    m = float(matches)
    jaro = (m / len(a) + m / len(b) + (m - transpositions / 2.0) / m) / 3.0
    prefix = 0
    while prefix < min(4, len(a), len(b)) and a[prefix] == b[prefix]:
        prefix += 1
    return jaro + prefix * 0.1 * (1.0 - jaro)


# --- matching ----------------------------------------------------------------

FUZZY_THRESHOLD = 0.92
AMBIGUITY_MARGIN = 0.01
TOKEN_THRESHOLD = 0.88
MIN_FUZZY_TOKEN_LENGTH = 4

TIER_PINNED, TIER_EXACT_ID, TIER_ALIAS, TIER_FUZZY = "PINNED", "EXACT_ID", "ALIAS", "FUZZY"
TIER_CODES = {TIER_PINNED: 0, TIER_EXACT_ID: 1, TIER_ALIAS: 2, TIER_FUZZY: 3}
TIER_WEIGHTS = {TIER_PINNED: 1.0, TIER_EXACT_ID: 1.0, TIER_ALIAS: 0.9, TIER_FUZZY: 0.6}


@dataclass
class Match:
    channel_id: str
    tier: str
    score: float


def fuzzy_score(candidate: list[str], target: list[str]) -> float:
    if len(candidate) != len(target):
        return 0.0
    if numeric_tokens(candidate) != numeric_tokens(target):
        return 0.0
    for a, b in zip(candidate, target):
        if a == b:
            continue
        if (
            len(a) >= MIN_FUZZY_TOKEN_LENGTH
            and len(b) >= MIN_FUZZY_TOKEN_LENGTH
            and any(ch.isalpha() for ch in a)
            and jaro_winkler(a, b) >= TOKEN_THRESHOLD
        ):
            continue
        return 0.0
    return jaro_winkler(" ".join(candidate), " ".join(target))


def _contains_run(values: list[str], run: list[str]) -> bool:
    if len(run) > len(values):
        return False
    for start in range(0, len(values) - len(run) + 1):
        if values[start:start + len(run)] != run:
            continue
        neighbours = []
        if start > 0:
            neighbours.append(values[start - 1])
        if start + len(run) < len(values):
            neighbours.append(values[start + len(run)])
        if not any(token.isdigit() for token in neighbours):
            return True
    return False


class ChannelMatcher:
    def __init__(self, channels: Iterable[dict], fuzzy_threshold: float = FUZZY_THRESHOLD):
        self.fuzzy_threshold = fuzzy_threshold
        self.entries = []
        for channel in channels:
            names = []
            for name in [channel["name"], *channel.get("aliases", [])]:
                value = tokens(name)
                if value and value not in names:
                    names.append(value)
            excludes = [tokens(name) for name in channel.get("exclude", [])]
            self.entries.append({
                "id": channel["id"],
                "id_key": normalize_id(channel["id"]),
                "names": names,
                "excludes": [value for value in excludes if value],
            })

    @staticmethod
    def _excluded(entry: dict, candidate: list[str]) -> bool:
        return any(candidate == exclude or _contains_run(candidate, exclude) for exclude in entry["excludes"])

    def match(self, name: str, tvg_id: Optional[str] = None, alternative_name: Optional[str] = None) -> Optional[Match]:
        id_key = normalize_id(tvg_id)
        names = []
        for raw in [name, alternative_name]:
            if raw:
                value = tokens(raw)
                if value and value not in names:
                    names.append(value)

        if id_key is not None:
            for entry in self.entries:
                if entry["id_key"] == id_key:
                    if any(self._excluded(entry, n) for n in names):
                        return None
                    return Match(entry["id"], TIER_EXACT_ID, 1.0)
        if not names:
            return None

        alias_hits = [
            entry for entry in self.entries
            if not any(self._excluded(entry, n) for n in names) and any(n in entry["names"] for n in names)
        ]
        if len(alias_hits) == 1:
            return Match(alias_hits[0]["id"], TIER_ALIAS, 1.0)
        if len(alias_hits) > 1:
            return None

        if id_key is not None:
            return None
        scored = []
        for entry in self.entries:
            if any(self._excluded(entry, n) for n in names):
                continue
            best = max((fuzzy_score(n, target) for n in names for target in entry["names"]), default=0.0)
            if best >= self.fuzzy_threshold:
                scored.append((entry, best))
        scored.sort(key=lambda item: item[1], reverse=True)
        if not scored:
            return None
        if len(scored) > 1 and scored[0][1] - scored[1][1] < AMBIGUITY_MARGIN:
            return None
        return Match(scored[0][0]["id"], TIER_FUZZY, scored[0][1])


# --- playlist headers and candidates -------------------------------------------

REFERER, USER_AGENT, ORIGIN = "Referer", "User-Agent", "Origin"


def canonical_header(name: str) -> Optional[str]:
    key = name.strip().lower()
    if key in ("referer", "referrer", "http-referrer", "http-referer"):
        return REFERER
    if key in ("user-agent", "useragent", "http-user-agent"):
        return USER_AGENT
    if key in ("origin", "http-origin"):
        return ORIGIN
    return None


def split_kodi_url(raw: str) -> tuple[str, dict]:
    if "|" not in raw:
        return raw.strip(), {}
    url, suffix = raw.split("|", 1)
    if "=" not in suffix:
        return raw.strip(), {}
    headers = {}
    for pair in suffix.split("&"):
        if "=" not in pair or pair.index("=") == 0:
            continue
        key, value = pair.split("=", 1)
        canonical = canonical_header(unquote(key))
        value = unquote(value).strip()
        if canonical and value:
            headers[canonical] = value
    return url.strip(), headers


def headers_from_options(options: dict) -> dict:
    headers = {}
    for key, value in options.items():
        canonical = canonical_header(key)
        if canonical and value.strip():
            headers[canonical] = value.strip()
    return headers


@dataclass
class Candidate:
    source_id: str
    trust: float
    url: str
    name: str
    tvg_id: Optional[str] = None
    alternative_name: Optional[str] = None
    headers: dict = field(default_factory=dict)
    height_hint: Optional[int] = None
    labels: list = field(default_factory=list)


_ATTRIBUTE = re.compile(r'([A-Za-z0-9-]+)="([^"]*)"')
_LABEL = re.compile(r"\[([^\]]+)\]")


def parse_m3u(source_id: str, trust: float, text: str) -> list[Candidate]:
    candidates = []
    attributes, title, options = {}, "", {}
    for raw_line in text.splitlines():
        line = raw_line.lstrip("﻿").strip()
        if not line:
            continue
        if line.upper().startswith("#EXTINF"):
            body = line.split(":", 1)[1] if ":" in line else ""
            prefix, _, title = _split_unquoted_comma(body)
            attributes = {k.lower(): v for k, v in _ATTRIBUTE.findall(prefix)}
            options = {}
            continue
        if line.upper().startswith("#EXTVLCOPT:"):
            key, _, value = line.split(":", 1)[1].partition("=")
            options[key.strip()] = value.strip().strip('"')
            continue
        if line.startswith("#"):
            continue
        url, kodi_headers = split_kodi_url(line)
        if url.lower().startswith(("http://", "https://")):
            display = title.strip() or attributes.get("tvg-name", "")
            headers = headers_from_options(options)
            headers.update(kodi_headers)
            tvg_name = attributes.get("tvg-name", "")
            candidates.append(Candidate(
                source_id=source_id,
                trust=trust,
                url=url,
                name=display,
                tvg_id=attributes.get("tvg-id") or None,
                alternative_name=tvg_name if tvg_name and tvg_name != display else None,
                headers=headers,
                height_hint=height_hint(display),
                labels=[label.strip().lower() for label in _LABEL.findall(display)],
            ))
        attributes, title, options = {}, "", {}
    return candidates


def _split_unquoted_comma(value: str) -> tuple[str, str, str]:
    quoted = False
    for index, char in enumerate(value):
        if char == '"':
            quoted = not quoted
        elif char == "," and not quoted:
            return value[:index], ",", value[index + 1:]
    return value, "", ""


def parse_iptvorg_api(source_id: str, trust: float, filter_country: Optional[str], streams: list) -> list[Candidate]:
    suffix = f".{filter_country.strip().lower()}" if filter_country else None
    candidates = []
    for stream in streams:
        channel = stream.get("channel")
        if suffix and not (channel and channel.lower().endswith(suffix)):
            continue
        url = (stream.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            continue
        title = (stream.get("title") or "").strip()
        headers = {}
        if stream.get("referrer"):
            headers[REFERER] = stream["referrer"]
        if stream.get("user_agent"):
            headers[USER_AGENT] = stream["user_agent"]
        feed = stream.get("feed")
        candidates.append(Candidate(
            source_id=source_id,
            trust=trust,
            url=url,
            name=title,
            tvg_id=f"{channel}@{feed}" if channel and feed else channel,
            headers=headers,
            height_hint=height_hint(stream.get("quality") or "") or height_hint(title),
            labels=[label.lower() for label in stream.get("labels") or []],
        ))
    return candidates


def to_kodi_url(url: str, headers: dict) -> str:
    if not headers:
        return url
    from urllib.parse import quote
    return url + "|" + "&".join(f"{key}={quote(value, safe='-._~/:;() ,')}" for key, value in headers.items())
