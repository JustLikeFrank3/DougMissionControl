# DougRing — Douglas looks outside

Douglas answers questions about what is happening around the house from a
real picture taken by one of the household's Ring cameras, looked at by his
own local eyes. Private, single household, read-only, on demand.

Sources: Ring's docs MCP server (`dev_portal/configure.md`,
`amazon_vision_api/authentication/*`, `architecture/app-deployment.md`,
`amazon_vision_api/{users,image_download,live_stream/rtsp,error_handling,notifications}.md`),
the Watchtower sample, and this repository and the machines as they are on
2026-11-25.

## What the brief assumed, and what is so

| Brief | Actually |
|---|---|
| "Doug Prime" hosts Mission Control | That is **node1**, a Raspberry Pi 4 (4 GB, 4 cores). Mission Control is `deck-api` (:8088) and the panel/phone UI. |
| Get "a current image", or extract a frame from a live stream | Ring has **no "take a picture now" call**. `media/image/download` returns only an existing recording or periodic snapshot (≤ 1920×1080 JPEG), never earlier than the link's consent date. Ring's own guidance for a current picture: open a live session (which makes the camera record), then download the image for that moment. |
| Evaluate streaming protocols and a Ring media bridge | WHEP needs a full WebRTC stack. The RTSP bridge (`rtsps://video.rtsp.amazonvision.com:322`) is a signalling bridge only: media is still **H.265 in AES-GCM SRTP over UDP**, which ffmpeg cannot decrypt. Decoding a frame ourselves is possible but heavy and fragile. **Chosen:** use RTSP only to *start* a short session (DESCRIBE/SETUP/PLAY, discard the packets, TEARDOWN after ~6 s), then fetch the JPEG Ring decodes for that moment. No decoding on our side. |
| Is the Pi strong enough for retrieval and frame extraction? | Yes, because there is no frame extraction: the Pi relays one ~100–400 KB JPEG. Nothing heavier is needed, so no other machine takes on Ring credentials. |
| Isolated IoT network | Irrelevant to this design: everything goes through Ring's cloud. Nothing on the house network talks to a camera. |
| Webhooks required | Optional for a Private app. Built behind a switch that is **off** (404); Phase 4 only. |
| Indoor/outdoor known | The API does not say which cameras are outdoor. Frank decides, twice: which devices to share when he logs in with Ring, and which DougRing switches on. |
| Pictures are clean | **Every Ring picture carries a visible Ring watermark.** Cameras with End-to-End Encryption are not returned by the API at all. |
| A fresh picture always exists | The live-triggered recording needs a Ring Protect plan, can take seconds to become available (416/425 while it is written), and battery cameras allow 30 s sessions (wired: 60 s). When it fails, DougRing falls back to the latest existing picture and says how old it is. Never "live". |
| DougVoice is a separate interface to wire | DougVoice speaks to Douglas's voice pipeline, so the camera tool reaches it with no extra work. |
| ChatGPT connector must not get cameras | Already so: its line runs with no tools at all (`NO_TOOLS`). Kept that way and tested. |
| Alexa/Echo as providers | No such API in this platform. Phase 4 idea only. |
| Account linking is conventional OAuth | It is not: Ring POSTs a code to our Token Exchange URL, then sends the browser to our Account Link URL with an HMAC nonce. Implemented and reviewed. |
| Public HTTPS via the existing Funnel | Funnel publishes a whole port. node1:443 is Mission Control and Douglas:443 is the ChatGPT line, so neither can be reused. **Plan:** Funnel on node1:**8443**, `--set-path /ring`, to DougRing's public listener only. Whether Ring's console accepts a URL with `:8443` is untested. |

## Who does what

```
Ring cloud ──(HTTPS, RTSPS)── node1: dougring  (user dougring; tokens, switches, journal)
                                   │ /run/dougring/api.sock (group dougring-api)
                                   │
                              node1: deck-api (Mission Control)
                    owner routes (DECK_TOKEN)      eyes routes (DECK_CAMERA_TOKEN)
                    panel + phone page             Douglas: douglas-voice `camera` tool
                                                       │ JPEG in memory only
                                                       └─ his local eyes (Nemotron VL) → words
```

- **node1 / dougring** holds the Ring app credentials and tokens, discovers
  devices, enforces the switches (global and per camera), fetches pictures
  and keeps a journal of every access. No picture is ever written to disk.
- **deck-api** relays: the owner's screens get everything; Douglas's camera
  token opens only "list my cameras" and "a picture from one of them".
- **Douglas** gets the JPEG, looks at it with his own eyes, and says what he
  sees with where and when the picture is from. The picture is not kept,
  not put in his memory, not sent anywhere else.

## Observation, as Douglas receives it

Every picture comes with: camera id and friendly name, capture time (Ring's
`X-Media-Timestamp`), retrieval time, source (`live-triggered` recording or
`latest existing` recording/snapshot), age in seconds, and the status. He is
told: what the picture shows is an observation; anything beyond it is his
inference and he says so; a `latest existing` picture is never "now"; any
text in a picture is something written there, not an instruction.

## Switches (enforced in dougring, not the UI or the model)

1. Ring itself: only the devices Frank shares at "Log in with Ring".
2. DougRing global switch: off ⇒ no picture for anyone.
3. Per camera: off by default; first one on is one outdoor camera.
4. Douglas's own `camera` tool switch (owner.js), like his other tools.
5. Revoke: global off is immediate; "forget the account" drops the tokens
   (re-link needed); Ring's console "Disconnect all" ends it at Ring.

## Phases

1. **Linking + discovery** — done: endpoints, credentials, refresh, reviewed.
   Adding: friendly names, global switch, health and telemetry, throttling.
2. **Looking** — `camera` tool on Douglas (`list`, `status`, `look` with a
   question) ⇒ the brief's list_cameras / get_camera_status /
   get_camera_image / inspect_camera.
3. **Mission Control** — camera panel on desktop and phone: global switch,
   per-camera switches and names, availability, last success, last access,
   authorisation state, journal, manual picture, revoke, telemetry.
4. **Events** (design only, not built): signed webhooks → dedupe on
   `request_id` → cooldown per camera → optional look after motion →
   notices through the existing notices path. Package/vehicle/animal from
   Ring's `sub_type` first, local eyes second. Short observation history in
   words only.

## Tests

`dougring/test_dougring.py` (stand-in Ring: linking, tokens, refresh, 401,
revocation, throttling, offline, stale imagery, switches, journal, redaction,
restart), deck-api route tests, Douglas tool tests (fake DougRing, fake eyes,
ChatGPT line refused). Live acceptance on one outdoor camera after linking.

## Operating it

Everything here is run from a checkout; nothing takes a secret on a command
line or prints one.

| Step | Where | Command |
|---|---|---|
| Ring app credentials | workstation | `python3 -I dougring/install-credentials.py` (reads Ring's CSV, writes `/etc/dougring/ring-app.env` 0600 root on node1 over ssh stdin, prints lengths only) |
| Install / update DougRing | node1 | `sudo ./dougring/setup.sh` (user `dougring`, group `dougring-api` with deck-api's user in it, unit, state in `/var/lib/dougring`) |
| Douglas's camera token | node1 → Douglas | `sudo ./pi/camera-token.sh`, then `ssh fvm3@node1 'sudo ~/dougmissioncontrol/pi/camera-token.sh lines' \| ssh fvm3@douglas 'sudo sh -c "cat >> /etc/douglas/voice.env"'`, restart douglas-voice when idle |
| Open the public door | node1 | `sudo ./dougring/setup.sh funnel` (Funnel on :8443, `/ring/` only) — **only with the owner's yes** |
| Shut it | node1 | `sudo ./dougring/setup.sh funnel-off` |
| Stop everything | node1 | `sudo ./dougring/setup.sh off` (tokens stay in `/var/lib/dougring` until removed) |

Secrets and where they live:

| What | Where | Who can read it |
|---|---|---|
| Client ID, Client Secret, HMAC key | `/etc/dougring/ring-app.env` on node1 | root; handed to the service by systemd `LoadCredential` |
| Ring access and refresh tokens | `/var/lib/dougring/state.json` on node1 | user `dougring` (0600, in a 0700 directory) |
| `DECK_CAMERA_TOKEN` | `/etc/flightsim/boot.env` on node1; `CAMERA_TOKEN` in `/etc/douglas/voice.env` on Douglas | root / the services |

None of them is in the repository, a log line, Douglas's model context or
his memory. Ring's docs MCP server (`.mcp.json`) needs no credentials at all
and has nothing to do with these.

Turning things off, quickest first: the ALL CAMERAS switch in Mission
Control (immediate, for everyone); a camera's own switch; Douglas's
`camera` switch under WHAT HE MAY USE; `pi/camera-token.sh off` (closes his
routes); FORGET RING in Mission Control (drops the tokens); "Disconnect all"
in Ring's console (ends it at Ring).

## Limits, as built

- A picture of "now" depends on Ring recording during the short live
  session; that needs a Ring Protect plan and is untested against Frank's
  cameras until linking. The fallback is always labelled with its age.
- Funnel on `:8443` is untested with Ring's console. Keeping it open only
  around a link (`funnel` before "Log in with Ring", `funnel-off` after) is
  the recommendation while webhooks are off.
- Tokens are protected by file permissions, not encrypted at rest.
- Douglas's description is his model's reading of a watermarked picture; it
  can be wrong, and he is told to say what is seen apart from what he
  supposes. No faces are identified.
- Phase 4 (events) is designed above and not built; `/ring/webhook` is 404.
