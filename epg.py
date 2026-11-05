#!/usr/bin/env python3
"""Build DYLIO-LIVE's XMLTV EPG + mobile web guide from public schedule feeds.

Does not download videos, modify stream URLs, or invent programme listings.
Run AFTER build.py. If no usable listings are fetched, preserve existing guide.
"""
from __future__ import annotations

import gzip
import io
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Public schedule sources: third-party projects; feeds and coverage may change.
# The four additional feeds fill local stations, sports, free TV and UK sport.
SOURCES = [
    ("US", "https://raw.githubusercontent.com/acidjesuz/EPGTalk/master/US_guide.xml.gz"),
    ("Sports", "https://raw.githubusercontent.com/acidjesuz/EPGTalk/master/Sports_guide.xml.gz"),
    ("Free TV", "https://raw.githubusercontent.com/acidjesuz/EPGTalk/master/FreeTV_guide.xml.gz"),
    ("US Local", "https://raw.githubusercontent.com/acidjesuz/EPGTalk/master/US_local_guide.xml.gz"),
    ("UK", "https://raw.githubusercontent.com/acidjesuz/EPGTalk/master/UK_guide.xml.gz"),
]
PLAYLISTS = ("wifi.m3u", "cellular.m3u")
MAX_DAYS = 7
UTC = timezone.utc


@dataclass(frozen=True)
class Channel:
    id: str
    name: str
    group: str
    logo: str


@dataclass(frozen=True)
class Programme:
    start: datetime
    stop: datetime
    title: str
    description: str = ""
    category: str = ""


def attr(meta: str, name: str) -> str:
    match = re.search(r'\b' + re.escape(name) + r'="([^"]*)"', meta)
    return match.group(1) if match else ""


def load_channels() -> dict[str, Channel]:
    channels: dict[str, Channel] = {}
    for filename in PLAYLISTS:
        path = Path(filename)
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if not line.startswith("#EXTINF:"):
                continue
            channel_id = attr(line, "tvg-id")
            # Without a stable tvg-id we cannot safely match a programme guide.
            if not channel_id or channel_id in channels:
                continue
            name = line.split(",", 1)[-1].strip()
            channels[channel_id] = Channel(
                id=channel_id,
                name=name.replace(" (Backup)", ""),
                group=attr(line, "group-title"),
                logo=attr(line, "tvg-logo"),
            )
    return channels


def normalized(s: str) -> str:
    s = s.casefold()
    s = re.sub(r'\b(high definition|hd|sd|1080p|720p|4k|uhd|usa|united states|us)\b', ' ', s)
    s = re.sub(r'[^a-z0-9]+', '', s)
    return s


def is_west(s: str) -> bool:
    return bool(re.search(r'(?:@west|\bwest\b|\bpacific\b)', s, flags=re.I))


def is_east(s: str) -> bool:
    return bool(re.search(r'(?:@east|\beast\b|\beastern\b)', s, flags=re.I))


def match_score(target: Channel, source_id: str, source_names: list[str]) -> int:
    """Strict about West/East; don't show the East schedule for a West feed."""
    target_desc = f"{target.id} {target.name}"
    guide_desc = " ".join([source_id, *source_names])
    tw, sw = is_west(target_desc), is_west(guide_desc)
    te, se = is_east(target_desc), is_east(guide_desc)
    if tw != sw:
        return 0
    if te and sw or se and tw:
        return 0
    # Avoid using an explicitly East guide for a generic/non-East channel.
    if se and not te and not tw:
        return 0
    if target.id.casefold() == source_id.casefold():
        return 110
    # HD and SD generally have the same programme schedule.
    btarget = re.sub(r'@(?:hd|sd)$', '', target.id, flags=re.I)
    bsource = re.sub(r'@(?:hd|sd)$', '', source_id, flags=re.I)
    if btarget.casefold() == bsource.casefold():
        return 105
    # A generic, non-West programme guide usually follows the East schedule.
    btarget = re.sub(r'@east$', '', btarget, flags=re.I)
    bsource = re.sub(r'@east$', '', bsource, flags=re.I)
    if btarget.casefold() == bsource.casefold():
        return 100
    # Source ID can have a different schema. Use only exact channel-name aliases.
    aliases = {normalized(target.name)}
    manual = {
        "espn (hd)": ["ESPN"], "espn 2": ["ESPN2"], "espnu hd": ["ESPNU"],
        "wp xi nbc": ["WPXI"], "wpxi nbc": ["WPXI"],
        "ion tv": ["ION Television"], "mlb": ["MLB Network"],
        "tcm": ["Turner Classic Movies"], "tcm west": ["Turner Classic Movies West"],
        "a&e": ["A&E Network"], "nfl channel": ["NFL Channel"],
        "nbc sports network": ["NBCSN"], "usa network": ["USA Network"],
    }
    aliases.update(normalized(x) for x in manual.get(target.name.casefold(), []))
    # No name-only matching for distinct east/west feeds unless guide says so.
    values = {normalized(n) for n in source_names}
    if aliases & values:
        return 70
    return 0


def xmltv_date(raw: str) -> datetime | None:
    match = re.match(r'^\s*(\d{14}|\d{12})(?:\s*([+-]\d{4}))?', raw or "")
    if not match:
        return None
    digits, offset = match.groups()
    fmt = "%Y%m%d%H%M%S" if len(digits) == 14 else "%Y%m%d%H%M"
    try:
        dt = datetime.strptime(digits, fmt)
        if offset:
            hours, mins = int(offset[1:3]), int(offset[3:5])
            zone = timezone((1 if offset[0] == "+" else -1) * timedelta(hours=hours, minutes=mins))
        else:
            zone = UTC  # XMLTV ideally specifies timezone; assume UTC if absent.
        return dt.replace(tzinfo=zone).astimezone(UTC)
    except ValueError:
        return None


def first_text(el: ET.Element, tag: str) -> str:
    for item in el.findall(tag):
        if item.attrib.get("lang") in ("en", "eng", "en-US", "en-GB") and item.text:
            return item.text.strip()
    for item in el.findall(tag):
        if item.text:
            return item.text.strip()
    return ""


def parse_source(stream, targets: dict[str, Channel], now: datetime):
    """Stream parse: keep only guide entries matching the DYLIO-LIVE playlist."""
    guide_to_targets: dict[str, list[tuple[str, int]]] = {}
    items: dict[tuple[str, str], list[Programme]] = defaultdict(list)
    count = 0
    context = ET.iterparse(stream, events=("start", "end"))
    _, root = next(context)
    for event, el in context:
        if event != "end":
            continue
        if el.tag == "channel":
            guid = el.attrib.get("id", "")
            names = [x.text.strip() for x in el.findall("display-name") if x.text]
            matches = []
            for tid, target in targets.items():
                score = match_score(target, guid, names)
                if score:
                    matches.append((tid, score))
            if matches:
                guide_to_targets[guid] = matches
            el.clear()
        elif el.tag == "programme":
            source_id = el.attrib.get("channel", "")
            match_ids = guide_to_targets.get(source_id)
            if match_ids:
                start = xmltv_date(el.attrib.get("start", ""))
                stop = xmltv_date(el.attrib.get("stop", ""))
                if start and stop and stop > now - timedelta(hours=1) and start < now + timedelta(days=MAX_DAYS) and stop > start:
                    title = first_text(el, "title")
                    if title:
                        p = Programme(start, stop, title, first_text(el, "desc"), first_text(el, "category"))
                        for tid, _ in match_ids:
                            items[(tid, source_id)].append(p)
                            count += 1
            el.clear()
        if el in root:
            root.remove(el)
    selections: dict[str, tuple[int, list[Programme]]] = {}
    for source_id, matches in guide_to_targets.items():
        for tid, score in matches:
            progs = items.get((tid, source_id), [])
            if not progs:
                continue
            current = selections.get(tid)
            if current is None or (score, len(progs)) > (current[0], len(current[1])):
                selections[tid] = (score, progs)
    return selections, count


def download_source(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "DYLIO-LIVE-EPG/1.0"})
    response = urllib.request.urlopen(req, timeout=45)
    # Return a context-managed stream that closes the underlying response.
    if url.endswith(".gz"):
        return response, gzip.GzipFile(fileobj=response, mode="rb")
    return response, response


def fmt_xmltv(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y%m%d%H%M%S +0000")


def save_xml(channels: dict[str, Channel], programmes: dict[str, list[Programme]]) -> None:
    tv = ET.Element("tv", {"generator-info-name": "DYLIO-LIVE EPG"})
    for channel in channels.values():
        elem = ET.SubElement(tv, "channel", {"id": channel.id})
        ET.SubElement(elem, "display-name", {"lang": "en"}).text = channel.name
        if channel.logo:
            ET.SubElement(elem, "icon", {"src": channel.logo})
    for tid, items in programmes.items():
        for p in sorted(items, key=lambda x: x.start):
            elem = ET.SubElement(tv, "programme", {
                "channel": tid, "start": fmt_xmltv(p.start), "stop": fmt_xmltv(p.stop)
            })
            ET.SubElement(elem, "title", {"lang": "en"}).text = p.title
            if p.description:
                ET.SubElement(elem, "desc", {"lang": "en"}).text = p.description
            if p.category:
                ET.SubElement(elem, "category").text = p.category
    ET.ElementTree(tv).write("guide.xml", encoding="utf-8", xml_declaration=True)


def save_html(channels: dict[str, Channel], programmes: dict[str, list[Programme]], now: datetime) -> None:
    data = [
        {
            "id": c.id, "name": c.name, "group": c.group,
            "shows": [
                {"start": p.start.isoformat(), "stop": p.stop.isoformat(),
                 "title": p.title, "desc": p.description, "category": p.category}
                for p in programmes.get(c.id, [])
            ]
        }
        for c in channels.values()
    ]
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    page = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DYLIO-LIVE • Live TV Guide</title><style>
:root{color-scheme:dark;font-family:system-ui,-apple-system,sans-serif;background:#080f1c;color:#e7edf7}
*{box-sizing:border-box}body{margin:0}header{background:#111d30;position:sticky;top:0;z-index:2;border-bottom:1px solid #23344d}
.top{max-width:1100px;padding:20px 18px 14px;margin:auto}.tag{font-size:11px;color:#86a6c9;font-weight:700;letter-spacing:.14em}
h1{margin:5px 0 3px;font-size:26px}.sub{font-size:13px;color:#96abc6}.controls{display:flex;gap:9px;flex-wrap:wrap;margin-top:14px}
input,select{font:inherit;background:#172844;color:#e7edf7;border:1px solid #334968;border-radius:9px;padding:10px 12px;max-width:100%}
input{flex:1;min-width:150px}main{max-width:1100px;margin:auto;padding:18px}.line{display:grid;grid-template-columns:170px 1fr;gap:16px;
 padding:15px 2px;border-bottom:1px solid #26374e}.ch{font-size:15px;font-weight:700;overflow-wrap:anywhere}
.gr{font-size:11px;color:#89a2c2;margin-top:6px}.shows{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:8px}
.show{background:#152640;padding:12px;border-radius:9px;min-width:0}.show.now{background:#234164;border:1px solid #3972a1}
.clock{font-size:12px;color:#a1bada;margin-bottom:6px}.name{font-size:14px;font-weight:650}.desc{font-size:12px;color:#a9bdd5;line-height:1.4;margin-top:6px}
.none{font-size:13px;color:#b2bfce;padding:9px 0}.muted{color:#9bacbf}.empty{padding:20px;text-align:center;color:#b2bfce}
@media(max-width:640px){.line{grid-template-columns:1fr;gap:8px}.shows{grid-template-columns:1fr 1fr} .top{padding:14px}}
@media(max-width:380px){.shows{grid-template-columns:1fr}}
</style></head><body>
<header><div class="top"><div class="tag">DYLIO-LIVE / ELECTRONIC PROGRAM GUIDE</div><h1>What's on TV</h1>
<div class="sub" id="stamp"></div><div class="controls"><input id="search" placeholder="Find a channel or show" aria-label="Search guide">
<select id="group" aria-label="Category"><option value="">All categories</option></select>
<select id="window" aria-label="Upcoming programmes"><option value="6">Next 6 hours</option><option value="24" selected>Next 24 hours</option><option value="72">Next 3 days</option><option value="168">Next 7 days</option></select></div></div></header>
<main><div id="results" aria-live="polite"></div></main>
<script id="data" type="application/json">__PAYLOAD__</script>
<script>
const channels=JSON.parse(document.getElementById('data').textContent);
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const dateTime=s=>new Date(s).toLocaleString(undefined,{weekday:'short',hour:'numeric',minute:'2-digit'});
const groups=[...new Set(channels.map(c=>c.group).filter(Boolean))];
const grp=document.getElementById('group');groups.forEach(g=>{const o=document.createElement('option');o.value=g;o.textContent=g;grp.appendChild(o)});
const q=document.getElementById('search'),w=document.getElementById('window'),out=document.getElementById('results');
function render(){const now=Date.now(),max=now+Number(w.value)*3600e3,query=q.value.toLowerCase();let shown=0;
 document.getElementById('stamp').textContent='Times shown in your device’s local time · Guide generated '+new Date('__GENERATED__').toLocaleString();
 out.innerHTML='';for(const c of channels){if(grp.value&&c.group!==grp.value)continue;
 const progs=c.shows.filter(p=>new Date(p.stop).getTime()>now&&new Date(p.start).getTime()<max);
 if(query&&!c.name.toLowerCase().includes(query)&&!progs.some(p=>p.title.toLowerCase().includes(query)))continue;
 shown++;const section=document.createElement('section');section.className='line';
 const head=document.createElement('div');head.innerHTML='<div class="ch">'+esc(c.name)+'</div><div class="gr">'+esc(c.group)+'</div>';section.appendChild(head);
 const list=document.createElement('div');list.className='shows';
 if(!progs.length){list.innerHTML='<div class="none">No published schedule available</div>'}
 else{for(const p of progs){const active=new Date(p.start).getTime()<=now;const el=document.createElement('div');el.className='show'+(active?' now':'');
 el.innerHTML='<div class="clock">'+(active?'● NOW · ':'')+esc(dateTime(p.start))+' – '+esc(dateTime(p.stop))+'</div><div class="name">'+esc(p.title)+'</div>'+(p.desc?'<div class="desc">'+esc(p.desc)+'</div>':'');list.appendChild(el)}}
 section.appendChild(list);out.appendChild(section)}if(!shown){out.innerHTML='<div class="empty">No matching channels or schedules.</div>'}}
[q,w,grp].forEach(el=>el.addEventListener('input',render));render();setInterval(render,60000);
</script></body></html>"""
    Path("guide.html").write_text(page.replace("__PAYLOAD__", payload).replace("__GENERATED__", now.isoformat()), encoding="utf-8")


def main() -> None:
    now = datetime.now(UTC)
    channels = load_channels()
    if not channels:
        raise SystemExit("No wifi.m3u playlist found. Run build.py first.")
    selected: dict[str, tuple[int, int, int, list[Programme]]] = {}
    report = [f"DYLIO-LIVE EPG report ({now.isoformat()})", f"Playlist IDs: {len(channels)}"]
    sources_ok = 0
    for source_rank, (name, url) in enumerate(SOURCES):
        try:
            raw, content = download_source(url)
            try:
                options, parsed = parse_source(content, channels, now)
            finally:
                raw.close()
            sources_ok += 1
            report.append(f"{name}: {len(options)} matched channel IDs / {parsed} matched programmes")
            for tid, (score, entries) in options.items():
                value = (score, -source_rank, len(entries), entries)
                previous = selected.get(tid)
                if previous is None or value[:3] > previous[:3]:
                    selected[tid] = value
        except Exception as exc:
            report.append(f"{name}: download/parse failed ({type(exc).__name__}: {str(exc)[:140]})")
    usable: dict[str, list[Programme]] = {}
    for tid, value in selected.items():
        # Deduplicate on start/stop/title, because some public providers repeat entries.
        unique = {(p.start, p.stop, p.title): p for p in value[3]}
        usable[tid] = sorted(unique.values(), key=lambda p: p.start)
    report.append(f"Successfully read source files: {sources_ok}/{len(SOURCES)}")
    report.append(f"Playlist IDs with programming: {len(usable)}/{len(channels)}")
    report.append("Without schedule: " + (", ".join(c.name for tid,c in channels.items() if tid not in usable) or "None"))
    Path("epg-report.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    if not usable:
        # Never replace a previously valid guide with an empty guide.
        print("No matching programmes retrieved; previous guide kept.", file=sys.stderr)
        return
    save_xml(channels, usable)
    save_html(channels, usable, now)
    print(f"Created guide.xml and guide.html: {len(usable)}/{len(channels)} channel IDs have schedules")


if __name__ == "__main__":
    main()
