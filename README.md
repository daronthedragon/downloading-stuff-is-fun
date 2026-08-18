# downloading stuff is fun

Paste a link, get the file. A media downloader that runs on your own machine —
nothing is uploaded, no account, no ads, no watermarks.

**Live site: [downloadingstuffis.fun](https://downloadingstuffis.fun)** ·
**Source: [github.com/daronthedragon/downloading-stuff-is-fun](https://github.com/daronthedragon/downloading-stuff-is-fun)**

Backed by [yt-dlp](https://github.com/yt-dlp/yt-dlp) (1800+ sites: YouTube, SoundCloud, TikTok,
Instagram, X, Twitch, Reddit, Vimeo, Bandcamp, …) and ffmpeg.

## Site or app?

|  | Site | App |
| --- | --- | --- |
| Works on phones | ✅ | ❌ |
| Nothing to install | ✅ | ❌ |
| SoundCloud, Vimeo, Reddit, X, direct links | ✅ | ✅ |
| **YouTube** | ❌ blocked | ✅ works |
| Your own logins for private/age-gated media | ❌ | ✅ |

YouTube refuses downloads coming from servers, so the site can't do it — but the app runs from
your own connection, where it works normally. Everything else works either way.

## Run the app

Download the [latest release](https://github.com/daronthedragon/downloading-stuff-is-fun/releases),
unzip it, and run:

```bash
start.bat
```

First run builds a virtualenv and installs dependencies, then opens
<http://127.0.0.1:7788>. After that it starts in a couple of seconds.

Requires Python 3.10+ and ffmpeg on PATH (`winget install Gyan.FFmpeg`).

## How to use it

1. **Paste a link** in the box — any page with media on it.
2. **Pick a mode:**
   - **auto** — video + audio together (mp4)
   - **audio** — audio only, converted to mp3/opus/m4a/flac/wav, tagged with cover art
   - **mute** — video with no sound
3. **Hit download.** You'll see live progress — percent, size, speed, time left — then the file
   saves through your browser.

Open **settings** for quality cap, output container, subtitles, whole playlists/albums
(delivered as a zip), and which browser to borrow cookies from. Your choices are remembered.

### When a site says no

Most failures mean the site wants you signed in.

1. **Settings → cookies from browser.** Pick a browser you're logged into; the app reuses that
   session. **Quit the browser completely first** — it locks its cookie database while running.
   Chromium 127+ also ties cookie encryption to the browser, so if extraction still fails,
   export a `cookies.txt` with a browser extension instead.
2. **Run `update.bat`.** Sites change constantly and most breakage is just a stale extractor.
3. **DRM-protected tracks can't be downloaded.** Paid tiers (e.g. SoundCloud Go+) serve
   encrypted streams — there is no file to fetch, and that's by design.

## API

The frontend is just a client of these — script against them if you like.

| endpoint | does |
| --- | --- |
| `POST /api/info` `{url}` | title, uploader, duration, thumbnail, available heights |
| `POST /api/download` `{url, mode, quality, audioFormat, videoFormat, playlist, subtitles, cookiesFrom}` | starts a job, returns `{id}` |
| `GET /api/job/{id}` | state, percent, bytes, speed, eta |
| `GET /api/file/{id}` | the finished file (deleted from disk once sent) |
| `DELETE /api/job/{id}` | forget a job and its files |
| `GET /api/health` | yt-dlp version, PO helper status |

Files land in a temp dir and are removed the moment you download them; leftovers are swept
after an hour.

## Config

| env var | default |
| --- | --- |
| `INEEDIT_HOST` | `127.0.0.1` |
| `INEEDIT_PORT` | `7788` |
| `INEEDIT_WORKDIR` | system temp `/ineedit` |
| `INEEDIT_JOB_TTL` | `3600` seconds |
| `INEEDIT_MAX_JOBS` | `4` concurrent jobs |
| `INEEDIT_MAX_PER_IP` | `1` job per visitor |

**Keep it on localhost.** There's no authentication — anyone who can reach the port can make
your machine fetch arbitrary URLs. To expose it, put a reverse proxy with auth in front.

## Help

Found a bug or a site that won't work?
[Open an issue](https://github.com/daronthedragon/downloading-stuff-is-fun/issues).

## Legal

This is a tool; what you point it at is on you. Download things you own, things licensed to
allow it, or things you have permission to take. Taking copyrighted material you have no rights
to generally breaks the site's terms of service and may break copyright law where you live.
DRM-protected content is not supported and won't be.

## License

[AGPL-3.0](LICENSE) — use it, modify it, run it. If you distribute a modified version or run it
as a service, your changes have to stay open under the same license.
