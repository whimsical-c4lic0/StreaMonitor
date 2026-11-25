# StreaMonitor
A Python3 application for monitoring and saving (mostly adult) live streams from various websites.

Inspired by [Recordurbate](https://github.com/oliverjrose99/Recordurbate)

## Supported sites
| Site name     | Abbreviation | Aliases                     | Quirks                 | Selectable resolution |
|---------------|--------------|-----------------------------|------------------------|-----------------------|
| Bongacams     | `BC`         |                             |                        | Yes                   |
| Cam4          | `C4`         |                             |                        | Yes                   |
| Cams.com      | `CC`         |                             |                        | Currently only 360p   |
| CamSoda       | `CS`         |                             |                        | Yes                   |
| Chaturbate    | `CB`         |                             |                        | Yes                   |
| DreamCam      | `DC`         |                             |                        | No                    |
| DreamCam VR   | `DCVR`       |                             | for VR videos          | No                    |
| FanslyLive    | `FL`         |                             |                        | Yes                   |
| Flirt4Free    | `F4F`        |                             |                        | Yes                   |
| ManyVids Live | `MV`         |                             |                        | Yes (high/low)         |
| MyFreeCams    | `MFC`        |                             |                        | Yes                   |
| SexChat.hu    | `SCHU`       |                             | use the id as username | No                    |
| StreaMate     | `SM`         | PornHubLive, PepperCams,... |                        | Yes                   |
| StripChat     | `SC`         | XHamsterLive,...            | must add crypto keys   | Yes                   |
| StripChat VR  | `SCVR`       |                             | for VR videos          | No                    |
| XLoveCam      | `XLC`        |                             |                        | No                    |

Currently not supported:
* Amateur.TV (They use Widevine now)
* ImLive (Too strict captcha protection for scraping)
* LiveJasmin (No nudity in free streams)

There are hundreds of clones of the sites above, you can read about them on [this site](https://adultwebcam.site/clone-sites-by-platform/).

## Requirements
* Python 3
  * Install the project dependencies with [uv](https://docs.astral.sh/uv/): `uv sync`.
* FFmpeg and ffprobe

## Usage

The application has the following interfaces:
* Console
* External console via ZeroMQ (sort of working)
* Web interface

#### Starting and console
Start the downloader (it does not fork yet)\
Automatically imports all streamers from the config file.
```
python3 Downloader.py
```

On the console you can use the following commands:
```
add <username> <site> - Add streamer to the list (also starts monitoring)
remove <username> [<site>] - Remove streamer from the list
start <username> [<site>] - Start monitoring streamer
start * - Start all
stop <username> [<site>] - Stop monitoring
stop * - stop all
status - Status display 
status2 - A slightly more readable status table
quit - Clean exit (Pressing CTRL-C also behaves like this)
```
For the `username` input, you usually have to enter the username as represented in the original URL of the room. 
Some sites are case-sensitive.

For the `site` input, you can use either the full or the short format of the site name. (And it is case-insensitive)

#### "Remote" controller
Add or remove a streamer to record (Also saves config file)
```
python3 Controller.py add <username> <website>
python3 Controller.py remove <username>
```

Start/stop recording streamers
```
python3 Controller.py <start|stop> <username>
```

List the streamers in the config
```
python3 Controller.py status
```

By default the controller connects to `127.0.0.1:6969`. Control another host with
`--host` (`-H`):
```
python3 Controller.py --host 192.168.1.10 status
```

`-h` and `--help` now show the controller help. The old `-h <host>` form is
gone; the attached legacy form `-hHOST` is still accepted for compatibility.

#### Web interface

You can access the web interface on port 5000. 
If set password in parameters.py username is admin, password admin, empty password is also allowed.
When you set the WEBSERVER_HOST it is also accesible to from other computers in the network

## Docker support

The image includes everything needed for the supported sites, with no extra
host setup. The published image is maintained at
`ghcr.io/whimsical-c4lic0/streamonitor`.

### Published images

* `ghcr.io/whimsical-c4lic0/streamonitor:latest` tracks the most recent
  validated version tag.
* `ghcr.io/whimsical-c4lic0/streamonitor:<X.Y.Z>` is an immutable release
  built from tag `vX.Y.Z` (the leading `v` is stripped; there is no
  `vX.Y.Z` image tag).
* Both tags point at the same digest for a release. Prefer pinning the digest
  in production: `ghcr.io/whimsical-c4lic0/streamonitor@sha256:<digest>`.

The container runs as the unprivileged `streamonitor` user (UID/GID `1000`).
Its data must stay writable by that user, so when bind-mounting host files and
directories, set ownership once:

```sh
touch config.json stripchat_mouflon_keys.json
chown -R 1000:1000 downloads config.json stripchat_mouflon_keys.json
```

## Configuration

You can set some parameters with environment variables (see
[parameters.py](parameters.py)). `.env` and the default `config.json` are read
from the project root, not the directory you launch the process from. Relative
`STRMNTR_DOWNLOAD_DIR` values are also resolved against the project root.
Override the file locations with `STRMNTR_ENV_FILE` and `STRMNTR_CONFIG`.

`config.json` is created automatically on first run and is ignored by git, so
it never ends up in a commit.

If a recording is interrupted (network error, stalled ffmpeg, expired stream URL), the next attempt continues in a part file and the parts are merged into a single video when the show ends. `STRMNTR_FFMPEG_STALL_TIMEOUT` (default `45` seconds, `0` disables it) stops ffmpeg when it stops producing output so the recording can resume. If the process is killed before the merge, the leftover `.partN` files and the raw HLS download (`<show>.tmp.ts`) are finished the next time the streamer starts, so nothing has to be cleaned up by hand.

You also have to add decryption keys yourself for StripChat in the
`stripchat_mouflon_keys.json` file. This file is ignored by git; override its
location with `STRMNTR_MOUFLON_KEYS` (a filename relative to the project root
or an absolute path).

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md). After `uv sync --frozen`, run
`./scripts/check.sh` for lint, format, and type checks, and
`uv run --frozen --no-sync pytest -m "not e2e"` for the offline suite.

## Disclaimer

This program is only a proof of concept and education project, I don't encourage anybody to use it. \
Most (if not every) streamers disallow recording their shows. Please respect their wish. \
If you don't, and you record them despite this request, please don't ever publish or share any recordings. \
If you either record or share the recorded shows, you might be legally punished. \
Also, please don't use this tool for monetization in any way.
