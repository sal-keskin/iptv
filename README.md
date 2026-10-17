# TR TV channel registry

This repository is the source of truth for the **TR TV** Android TV app (a fork of M3UAndroid). It
lists Turkish free-to-air channels ranked by audience share and says where their stream links come
from. The app reads it, finds fresh links, **tests every link on the TV itself** and keeps the best
one plus up to three fallbacks for each channel.

Only free, official, publicly available streams belong here. This is the same policy as
[iptv-org](https://github.com/iptv-org/iptv). Never add paid, pirated or token-stealing links.

## Files

| File | Edited by | Purpose |
|---|---|---|
| `channels.json` | you | The channel list: ids, numbers, names, aliases, exclusions, pinned urls |
| `sources.json` | you | Where candidate links come from |
| `playlist.m3u` | the Action | One link per channel in channel-number order. The app's `self` source |
| `candidates.json` | the Action | Every matched link per channel, with source, tier, score and a CI hint |
| `scripts/registry_rules.py` | rarely | Matching rules. A mirror of the app's Kotlin rules |
| `scripts/build_registry.py` | rarely | Builds `playlist.m3u` and `candidates.json` |
| `tests/match_vectors.json` | when rules change | Test cases shared by this repo **and** the app |

The Action (`.github/workflows/build.yml`) runs every night and on every push. It:

1. tests the rules;
2. fetches all sources;
3. matches links to channels;
4. does a soft liveness check;
5. commits `playlist.m3u` and `candidates.json`.

GitHub's runners are outside Türkiye, so geo-blocked streams fail the check there. The result is
stored as `ciCheck` in `candidates.json` as a hint only. It never removes a link.

## Add a channel

Add an entry to `channels.json`:

```json
{
  "id": "TV100.tr",
  "rank": 27,
  "name": "TV100",
  "aliases": ["TV 100"],
  "exclude": [],
  "group": "Haber",
  "logo": "https://…/tv100.png",
  "officialLivePage": "https://www.tv100.com/canli-yayin",
  "headers": {},
  "pinnedUrls": [],
  "enabled": true
}
```

- **`id`** is the canonical [iptv-org channel id](https://iptv-org.github.io/api/channels.json),
  without the feed suffix: `ATV.tr`, not `ATV.tr@SD`. Ids compare case-insensitively (`tv2.tr`).
  If iptv-org has no entry, invent a stable one (`BiKanal.tr`) and say so in `note`.
- **`rank`** is the channel number on the remote. Keep ranks unique. A disabled channel keeps its
  number, and the lineup just skips it.
- **`enabled: false`** hides a channel that has no official free stream. Explain why in `note`.
- The app keeps a channel's identity (favourites, history, position) by `id`. Renaming a channel
  or changing its rank is safe. **Changing its id makes it a new channel.**

## Add an alias or an exclusion

Matching runs in this order:

1. **Exact id:** the source's `tvg-id` equals `id`.
2. **Alias:** the normalized name equals `name` or one of `aliases`.
3. **Fuzzy:** a strict Jaro-Winkler match of at least 0.92, with the same number of words and
   identical numbers. Fuzzy matches rank lower and are flagged in the app's Diagnostics screen.

Names are normalized before they are compared:

- Turkish-aware lowercase: `İ/I/ı→i`, `ş→s`, `ğ→g`, `ü→u`, `ö→o`, `ç→c`.
- `(720p)`, `[Not 24/7]`, `HD`, `FHD` and similar tags are dropped.
- Letters and digits are split (`TV8` → `tv 8`).
- A trailing `TV` is dropped (`Habertürk TV` → `haberturk`).

To try a name:

```bash
python3 -c "import sys; sys.path.insert(0,'scripts'); import registry_rules as r; print(r.normalize('Habertürk TV (720p)'))"
```

Add to **`aliases`** names that sources use for the channel ("Teve2" for TV2). Add to **`exclude`**
names that must **never** match, even by id: regional or sister channels such as `ATV Avrupa` or
`Kanal D Drama`. An exclusion also matches longer names that contain it (`ATV Avrupa HD`), but
never across a number (`TV8` does not exclude `TV 8.5`).

When you change a rule or find a tricky name, add a case to `tests/match_vectors.json`. Both this
repo's tests and the app's tests read that file.

## Pin a link or add headers

`pinnedUrls` are manual overrides. They are tried first and win whenever they work on the TV. A
pinned url can carry headers in Kodi form:

```json
"pinnedUrls": ["https://cdn.example.tr/live/master.m3u8|Referer=https://www.example.com.tr/"]
```

`headers` applies `Referer`, `User-Agent` and `Origin` to every link of the channel, unless a link
brings its own. Sources can also carry headers with `#EXTVLCOPT:http-referrer=…`,
`#EXTVLCOPT:http-user-agent=…` or `url|Referer=…&User-Agent=…`.

## Official live pages (resolvers)

Some broadcasters offer their free live stream only on their own website, through a player whose
link carries an expiring token. A `resolver` lets the app find that link on the viewer's TV:

```json
"resolver": {
  "type": "webview",
  "page": "https://www.showtv.com.tr/canli-yayin",
  "match": "showtv/showtv\\.m3u8",
  "hosts": ["daioncdn.net"],
  "ttlMinutes": 60,
  "terms": "https://www.showtv.com.tr/info/kullanim-kosullari",
  "termsReviewed": "2026-10-17: personal viewing allowed; no redistribution or archiving"
}
```

| Field | Meaning |
|---|---|
| `webview` | Reads the page once like a browser. If the link is built by script, loads the page in a hidden WebView and captures the player's own request |
| `youtube` | The page embeds the broadcaster's own YouTube live. The app hands the video to the YouTube app and never extracts YouTube media |
| `match` | A pattern the link must contain, so a sister channel on the same page is never picked |
| `hosts` | The broadcaster's own CDN hosts. Links on other hosts are ignored |
| `ttlMinutes` | A resolved link older than this is resolved again just before playback |

Rules:

- **Review first.** Before adding a resolver, read the site's terms of use and its `robots.txt`, and
  write what you found in `termsReviewed`. Do not add one when either forbids automated access to
  the stream. CNN Türk's `robots.txt`, for example, disallows `/*index.m3u8`, so it has none.
- **Resolved links never leave the viewer's TV.** The Action never runs resolvers. They never
  appear in `playlist.m3u` or `candidates.json`.
- **Viewers opt in.** The app runs resolvers only when **Settings → Live TV → Links from official
  websites** is on. Streams read off an official page are never cached to disk.

## Add a source

Adding a source is a data change only: no app release is needed. Add an entry to `sources.json`:

```json
{ "id": "my-list", "type": "m3u", "url": "https://example.org/turkey.m3u", "trust": 0.6 }
```

| Field | Meaning |
|---|---|
| `type` | `m3u` (any M3U/M3U8), or `iptvorg-api` (the iptv-org `streams.json` format) |
| `trust` | 0–1. Multiplied by the match-tier weight when ranking |
| `filterCountry` | `iptvorg-api` only: keep channel ids ending in `.<country>` |
| `{registry}` in a url | Replaced with the registry base url configured in the app (`self` uses it) |

- **`officialHosts`** lists the broadcasters' own CDNs. When it is set, links on other hosts keep
  only 75 % of their source trust, so a restream never outranks the official stream.
- **`blockedHosts`** are never used.

## Run locally

```bash
python3 -m pytest -q tests
python3 scripts/build_registry.py --check
```

Point the app at your fork of this repository in **Settings → Live TV → Registry URL**. Use the raw
folder url, for example `https://raw.githubusercontent.com/<user>/iptv/main`.
