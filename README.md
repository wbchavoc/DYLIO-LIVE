# DYLIO-LIVE

A **two-playlist** IPTV channel selection from the existing `wbchavoc/Live-TV` repository. Generated files:

- `wifi.m3u` — best-ranked available source for home Wi-Fi.
- `cellular.m3u` — best-ranked available source for mobile networks.
- `guide.xml` — XMLTV program guide, when public guide listings are available.
- `guide.html` — searchable web program guide, when listings are available.

Both playlists contain the **same requested channel lineup where available**. Their source URLs can be identical when no distinct lower-bandwidth version exists. The builder ranks existing URLs only; **it cannot transcode, rehost, guarantee performance, or bypass geographical and account restrictions**.

## Playlist links (after the first successful GitHub Actions run)

Wi-Fi: `https://raw.githubusercontent.com/wbchavoc/DYLIO-LIVE/main/wifi.m3u`

Cellular: `https://raw.githubusercontent.com/wbchavoc/DYLIO-LIVE/main/cellular.m3u`

Guide: `https://raw.githubusercontent.com/wbchavoc/DYLIO-LIVE/main/guide.xml`

Web guide: `https://wbchavoc.github.io/DYLIO-LIVE/guide.html` (requires GitHub Pages setup)

## Installation

1. Create a **public**, empty GitHub repository named `DYLIO-LIVE` with the default branch `main`.
2. Upload `build.py`, `epg.py` and `README.md` to the repository root.
3. Create the workflow at **`.github/workflows/update.yml`** (the path matters).
4. Open **Actions → Update DYLIO-LIVE playlists and TV guide → Run workflow**.
5. Check generated files and `missing-channels.txt` / `epg-report.txt` for coverage.
6. Add each M3U URL in VLC or AerioTV. If supported, add the XMLTV URL as an EPG source. For Stremio, use a compatible IPTV add-on.

Guide coverage depends on third-party published listings. Do not assume all channels have guides or that links work on all networks.

GitHub Pages guide: in repository **Settings → Pages**, select **Deploy from a branch**, `main`, `/ (root)`; the guide becomes reachable after Pages deployment.
