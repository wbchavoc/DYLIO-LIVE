#!/usr/bin/env python3
"""DYLIO-LIVE: generate Wi-Fi and cellular M3U playlists.

Uses ONLY the channel URLs already in wbchavoc/Live-TV. Never relays,
transcodes, circumvents restrictions, or invents streams. Source URLs are
not tested for playback from a viewer's home or mobile network.
"""
from __future__ import annotations

import ipaddress
import re
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

SOURCE = 'https://raw.githubusercontent.com/wbchavoc/Live-TV/main/wifi.m3u'

# The EXACT channel names and IDs from the original DYLIO-LIVE builder.
# Names are ordered for consistent grouping in VLC/AerioTV.
CHANNELS = '''
ACC Network|1 - Sports|ACCNetwork.us
Big Ten Network|1 - Sports|BigTenNetwork.us
SEC Network|1 - Sports|SECNetwork.us
CBS Sports Golazo! Network|1 - Sports|CBSSportsGolazoNetwork.us
CBS Sports HQ|1 - Sports|CBSSportsHQ.us
ESPN (HD)|1 - Sports|ESPN.us@HD
ESPN 2|1 - Sports|ESPN2.us
ESPNU HD|1 - Sports|ESPNU.us@HD
Fox Sports 1|1 - Sports|FoxSports1.us
Fox Sports 2|1 - Sports|FoxSports2.us
NBC Sports Network|1 - Sports|NBCSportsNetwork.us;NBCSN.us
NFL Network|1 - Sports|NFLNetwork.us
NFL Channel|1 - Sports|NFLChannel.us
NBA TV|1 - Sports|NBATV.us
NHL Network|1 - Sports|NHLNetwork.us
MLB|1 - Sports|MLB.us
TBS|1 - Sports|TBS.us@East
TBS (West)|1 - Sports|TBS.us@West
TNT (USA)|1 - Sports|TNT.us@East
TNT (USA West)|1 - Sports|TNT.us@West
TNT Sports 4|1 - Sports|TNTSports4.uk
TruTV|1 - Sports|truTV.us@East
TruTV West|1 - Sports|truTV.us@West
USA Network|1 - Sports|USANetwork.us
CNN|2 - News & Local|CNN.us@SD
CNN HD|2 - News & Local|CNN.us@HD
Fox News Channel|2 - News & Local|FoxNewsChannel.us
NBC News Now|2 - News & Local|NBCNewsNOW.us
USA Today|2 - News & Local|USATODAY.us
WPXI NBC|2 - News & Local|NBC.us@WPXI
AMC|3 - Entertainment|AMC.us
A&E|3 - Entertainment|AE.us
AWE|3 - Entertainment|AWE.us
AFV|3 - Entertainment|AFV.us
Bravo|3 - Entertainment|Bravo.us
Charge!|3 - Entertainment|Charge.us
Food Network|3 - Entertainment|FoodNetwork.us
Game Show Network|3 - Entertainment|GameShowNetwork.us
History Channel (United States)|3 - Entertainment|History.us
Judge Nosey|3 - Entertainment|JudgeNosey.us
ION TV|3 - Entertainment|ION.us;IONTV.us;IONTelevision.us
More True Crime|3 - Entertainment|MoreTrueCrime.us
MTV|3 - Entertainment|MTV.us
Nosey|3 - Entertainment|Nosey.us
Paramount Network|3 - Entertainment|ParamountNetwork.us
TMZ|3 - Entertainment|TMZ.us
Cinemax (United States) (West)|4 - Movies|Cinemax.us@West
HBO|4 - Movies|HBO.us@East
HBO Hits|4 - Movies|HBOHits.us
Lifetime Movies|4 - Movies|LifetimeMovies.us
Starz West|4 - Movies|Starz.us@West
Starz Cinema|4 - Movies|StarzCinema.us
Starz Comedy|4 - Movies|StarzComedy.us
TCM|4 - Movies|TCM.us@East
TCM West|4 - Movies|TCM.us@West
Boomerang|5 - Kids|Boomerang.us
Cartoon Network (United States)|5 - Kids|CartoonNetwork.us
Disney Channel (United States)|5 - Kids|DisneyChannel.us
Disney XD|5 - Kids|DisneyXD.us
Nickelodeon (United States)|5 - Kids|Nickelodeon.us
Nickelodeon Pluto|5 - Kids|NickelodeonPlutoTV.us
Nicktoons|5 - Kids|Nicktoons.us
Teen Nick|5 - Kids|TeenNick.us
PBS Kids|5 - Kids|PBSKids.us
PBS Kids Eastern|5 - Kids|PBSKidsEasternCentral.us
PBS Kids Pacific|5 - Kids|PBSKidsPacific.us
'''


@dataclass(frozen=True)
class Entry:
    tvgid: str
    meta: str
    extra: tuple[str, ...]
    url: str
    original_group: str
    source_order: int

    @property
    def display(self) -> str:
        return self.meta.rsplit(',', 1)[-1].lower()

    @property
    def quality(self) -> int:
        # Labels are hints, NOT verified bitrate or video dimensions.
        match = re.search(r'\b(\d{3,4})p\b', self.display)
        return int(match.group(1)) if match else 0

    @property
    def scheme(self) -> str:
        return urlsplit(self.url).scheme.lower()

    @property
    def host_is_name(self) -> bool:
        host = urlsplit(self.url).hostname
        if not host:
            return False
        try:
            ipaddress.ip_address(host)
            return False
        except ValueError:
            return True

    @property
    def is_hls(self) -> bool:
        path = urlsplit(self.url).path.lower()
        return path.endswith('.m3u8') or '.m3u8/' in path

    @property
    def looks_like_master(self) -> bool:
        # A URL called master/variant may still contain a single bitrate.
        path = urlsplit(self.url).path.lower()
        return self.is_hls and bool(re.search(r'(master|index\.m3u8|playlist\.m3u8)', path))

    @property
    def warning(self) -> bool:
        return bool(re.search(r'geo.?block|not 24/7|offline|unstable', self.display))


def spec_rows():
    for row in CHANNELS.strip().splitlines():
        name, group, ids = [p.strip() for p in row.split('|')]
        yield name, group, ids.split(';')


def parse_entries(playlist: str):
    """Parse IPTV extended M3U without altering signed or parameterized URLs."""
    meta, extra = None, []
    order = 0
    for raw in playlist.splitlines():
        line = raw.strip()
        if line.startswith('#EXTINF:'):
            meta, extra = line, []
        elif meta is not None and line.startswith(('#EXTVLCOPT:', '#KODIPROP:', '#EXTHTTP:')):
            extra.append(line)
        elif meta is not None and line and not line.startswith('#'):
            if line.startswith(('http://', 'https://')):
                m_id = re.search(r'\btvg-id="([^"]*)"', meta)
                m_group = re.search(r'\bgroup-title="([^"]*)"', meta)
                yield Entry(
                    tvgid=m_id.group(1) if m_id else '',
                    meta=meta,
                    extra=tuple(extra),
                    url=line,
                    original_group=m_group.group(1) if m_group else '',
                    source_order=order,
                )
                order += 1
            meta, extra = None, []


def matches(actual: str, allowed: str) -> bool:
    return actual == allowed or ('@' not in allowed and actual.startswith(allowed + '@'))


def allowed_for(ids: list[str]) -> list[str]:
    # Accept Canadian channel IDs only when they are the SAME channel, not
    # similar names; keep the requested UK TNT Sports exception unchanged.
    result = ids[:]
    result += [item.replace('.us', '.ca', 1) for item in ids if '.us' in item]
    return result


def candidate_country(entry: Entry) -> int:
    # Corrects an earlier bug that ranked US IDs without @ below Canadian IDs.
    if '.us' in entry.tvgid:
        return 2
    if '.ca' in entry.tvgid:
        return 1
    return 0


def eligible(entry: Entry, ids: list[str]) -> bool:
    if not any(matches(entry.tvgid, allowed) for allowed in allowed_for(ids)):
        return False
    # Avoid misidentified alternate-language feeds even if channel ID was bad.
    bad_language = r'latin america|latam|en espa[nñ]ol|spanish audio|portuguese audio|french audio'
    return not re.search(bad_language, entry.display)


def mobile_quality(quality: int) -> int:
    if 400 <= quality <= 576:
        return 5
    if 577 <= quality <= 720:
        return 4
    if 300 <= quality < 400:
        return 3
    if quality == 0:
        return 2
    if 721 <= quality <= 1080:
        return 1
    return 0


def rank(entry: Entry, mode: str) -> tuple:
    # Each ranking is a heuristic; NO source is probed, so we never claim
    # verified availability, English audio, or actual adaptive bitrate.
    common = (
        candidate_country(entry),
        int(not entry.warning),
        int(entry.is_hls),
        int(entry.scheme == 'https'),
        int(entry.host_is_name),
        int(entry.looks_like_master),
    )
    if mode == 'cellular':
        return common + (mobile_quality(entry.quality), -entry.source_order)
    return common + (entry.quality, -entry.source_order)


def with_label(meta: str, name: str, group: str) -> str:
    # Preserve tvg-id and tvg-logo, plus any upstream HTTP headers/options.
    front, _, _ = meta.rpartition(',')
    front = re.sub(r'\s+group-title="[^"]*"', '', front)
    return f'{front} group-title="{group}",{name}'


def playlist(items: list[tuple[str, str, Entry]]) -> str:
    lines = ['#EXTM3U x-tvg-url="https://raw.githubusercontent.com/wbchavoc/DYLIO-LIVE/main/guide.xml"']
    for name, group, entry in items:
        lines.extend([with_label(entry.meta, name, group), *entry.extra, entry.url])
    return '\n'.join(lines) + '\n'


def build_from_text(source_text: str):
    source_entries = list(parse_entries(source_text))
    if not source_entries:
        raise RuntimeError('Empty source. Existing playlists will not be overwritten.')

    output_standard, output_mobile = [], []
    missing, changed_mobile = [], []
    for name, group, ids in spec_rows():
        candidates = [entry for entry in source_entries if eligible(entry, ids)]
        # Keep only one entry per exact URL (group duplicates are NOT backups).
        unique = {}
        for entry in candidates:
            unique.setdefault(entry.url, entry)
        candidates = list(unique.values())

        if not candidates:
            missing.append(name)
            continue
        standard = max(candidates, key=lambda item: rank(item, 'standard'))
        cellular = max(candidates, key=lambda item: rank(item, 'cellular'))
        output_standard.append((name, group, standard))
        output_mobile.append((name, group, cellular))
        if cellular.url != standard.url:
            changed_mobile.append(name)


    if not output_standard:
        raise RuntimeError('No requested channels matched. Existing files retained.')

    count = len(list(spec_rows()))
    report = [
        'DYLIO-LIVE stream selection report (NOT a live playback check)',
        f'Source: {SOURCE}',
        f'Channels found: {len(output_standard)}/{count}',
        f'Cellular URL differs from standard: {len(changed_mobile)}',
        '',
        'Missing channel IDs from source:', *(missing or ['None']),
        '',
        'Cellular URL differs (chosen by format/security/quality hints):',
        *(changed_mobile or ['None']),
        '',
        'Notes: A channel listed here can still be offline, geo-restricted,',
        'subscription-protected, fixed-bitrate or inaccessible on your network.',
        'Switching playlists is manual; GitHub is not a live-video proxy.',
    ]
    status = [f'Source: {SOURCE}', f'Found: {len(output_standard)}/{count}', '',
              'Missing from upstream playlist:', *(missing or ['None'])]
    return {
        'wifi.m3u': playlist(output_standard),
        'cellular.m3u': playlist(output_mobile),
        'missing-channels.txt': '\n'.join(status) + '\n',
        'stream-report.txt': '\n'.join(report) + '\n',
    }


def main():
    request = urllib.request.Request(SOURCE, headers={'User-Agent': 'DYLIO-LIVE-Playlist/2.0'})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read().decode('utf-8-sig', 'replace')
    results = build_from_text(data)
    for filename, content in results.items():
        Path(filename).write_text(content, encoding='utf-8')
    print('Updated DYLIO-LIVE: ' + ', '.join(
        f'{name} ({content.count(chr(10) + "#EXTINF:")} channels)'
        for name, content in results.items() if name.endswith('.m3u')))

if __name__ == '__main__':
    main()
