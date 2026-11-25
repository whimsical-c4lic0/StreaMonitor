# End-to-end recording tests

These tests are opt-in because they visit live adult streaming sites and
download a short sample. A normal `pytest` run collects them but skips them.

Each test:

1. discovers a currently live model for its site,
2. creates the real bot and confirms its status is `PUBLIC`,
3. asks the bot for its recording URL and records for a few seconds (a
   recording that fails mid-stream is retried once with a fresh URL, because
   live CDNs occasionally drop a segment or rotate a host),
4. stops the downloader through `stopDownload()`,
5. verifies the recorded file with `ffprobe` (one video stream and one audio
   stream) and decodes a few frames from each stream with `ffmpeg`.

Each run enables `STRMNTR_DEBUG` so ffmpeg's stderr is written next to the
recording; a failed attempt includes its tail in the assertion.

## Requirements

`ffmpeg` and `ffprobe` must be installed. If they are not on `PATH`, point
`STRMNTR_FFMPEG_PATH` at `ffmpeg`; the sibling `ffprobe` is found
automatically, or set `STRMNTR_FFPROBE_PATH` explicitly.

ManyVids recordings use the Python Agora WebRTC worker. Install the project
dependencies with uv (or run the tests inside the Docker image); the ManyVids
test skips with a clear reason when `aiortc`, `av`, `numpy` or `websockets` is
missing.

AmateurTV can return either a legacy `fstream` URL or an EasyStream
WebSocket descriptor. The EasyStream path needs the `websockets` dependency
and ffmpeg with MP4/ClearKey support; its test skips when the backend is
unavailable. A successful EasyStream test must finalize playable video and
audio, not merely connect to the edge. Live discovery deliberately keeps
EasyStream shows in the candidate pool, and a recording failure is a test
failure rather than a skip. Guest tokens may cap the selectable resolution;
the test reports the actual recorded dimensions rather than assuming the
wanted one was granted.

Install the application and test dependencies:

```bash
uv sync --frozen
```

## Run

```bash
STRMNTR_E2E=1 uv run pytest tests/e2e -v
```

To run several sites at once, set `STRMNTR_E2E_WORKERS` (uses
[pytest-xdist](https://pytest-xdist.readthedocs.io/)):

```bash
STRMNTR_E2E=1 STRMNTR_E2E_WORKERS=3 uv run pytest tests/e2e -v
```

Each worker is a separate process with its own temporary downloads directory,
so sites do not share recordings. `3` keeps a typical laptop or CI runner busy
without saturating the connection or the CPU; the default is `1` (sequential).

Useful environment variables:

- `STRMNTR_E2E_WORKERS` – number of sites to record at once (default `1`,
  sequential), for example `3`.
- `STRMNTR_E2E_SITES` – comma-separated site slugs to run, for example
  `CB,SC,SCVR`.
- `STRMNTR_E2E_RECORD_SECONDS` – recording duration per site (default `20`,
  minimum `5`).
- `STRMNTR_E2E_MODEL_<SLUG>` – pin a model instead of discovering one, for
  example `STRMNTR_E2E_MODEL_CB=some_model`.
- `STRMNTR_E2E_ROOM_ID_<SLUG>` – optional room ID for sites such as `FL`
  (Fansly) and `SC` (StripChat) when pinning a model.
- `STRMNTR_FFMPEG_PATH` / `STRMNTR_FFPROBE_PATH` – binary locations.

Examples:

```bash
STRMNTR_E2E=1 STRMNTR_E2E_SITES=SC STRMNTR_E2E_RECORD_SECONDS=15 \
  uv run pytest tests/e2e -v

STRMNTR_E2E=1 STRMNTR_E2E_MODEL_FL=some_model \
  STRMNTR_E2E_ROOM_ID_FL=123456789012345678 \
  uv run pytest tests/e2e -k FL -v
```

## Discovery coverage

Live-model discovery is implemented for AmateurTV, BongaCams, Cam4, Cams.com,
CamSoda, Chaturbate, DreamCam, DreamCam VR, FanslyLive, ManyVids, MyFreeCams,
StreaMate, StripChat, and StripChat VR.

Flirt4Free, SexChat.hu, and XLoveCam do not have discovery helpers
(officially unsupported, or their public directories are not usable from this
test harness). They are skipped with a reason unless a model is pinned through
`STRMNTR_E2E_MODEL_<SLUG>`.

A site is also skipped when it is unreachable, when its discovery endpoint
returns no live candidates, or when no candidate reports `PUBLIC` during the
test run. A recording failure is retried once with a fresh stream URL; only a
second failure, or a problem with the recorded media, fails the test.
