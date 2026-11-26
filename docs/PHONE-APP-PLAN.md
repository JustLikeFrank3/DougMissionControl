# The phone app: plan

The phone page (`pi/deck-ui/m/`, served at `/m`) becomes an iOS app, built
with Expo on the Mac and installed straight onto the owner's phone. No
TestFlight and no App Store. The last phase adds a share-sheet target, so a
photo or a link can be sent to Douglas from any other app.

This document is the brief for whoever builds it. It was written from a read
of the code on the Windows workstation, where no iOS build is possible;
nothing in it has been run on a Mac or a phone yet.

## Decisions already made

- **Built on the Mac**, with the paid Apple Developer account. `npx expo
  run:ios` does the native build and Xcode the signing. An install lasts a
  year. EAS cloud builds are not needed.
- **A native React Native app, not a WebView around `/m`.** The reasons to
  leave the browser are the ones a WebView keeps: the token in browser
  storage, the microphone workarounds, no share sheet.
- **It lives in `phone/`** in this repository, in TypeScript.
- **`/m` stays.** It is the fallback from any browser, and it costs nothing.
- **deck-api is the only backend.** The app is a second client of the routes
  the page already uses. It does not talk to Douglas, DougRing or the
  workstation directly.
- **The rules of the deck hold in the app as on the page:** everything shown
  is observed, every command that starts or stops something is a hold, and a
  missing reading is a dash, never a zero.

## What the Mac needs

- Xcode, signed in with the developer account
- Node (LTS) and git
- Tailscale, on the same tailnet as node1, so the Simulator reaches deck-api
- This repository

The phone needs the Tailscale app running, as it does for the page today.

## Reaching deck-api

- **Address.** The HTTPS one that `pi/mobile-https.sh` sets up:
  `https://<node1>.<tailnet>.ts.net`. iOS refuses plain HTTP unless the app
  carries an exception, and the HTTPS front already exists, so use it and add
  no exception. `/api/state` reports it as `https_url`.
- **Token.** `DECK_TOKEN`, sent as `Authorization: Bearer <token>` on every
  request. deck-api takes it there as well as in `?token=` (the page's way);
  where a request has both, `?token=` is the one read. Keep the token in the
  Keychain (`expo-secure-store`), never in a URL.
- **Getting the token onto the phone.** `sudo ./pi/mobile-link.sh` on node1
  prints `https://…/m?token=…` and a QR code. The first-run screen takes that
  whole link, pasted or scanned, and keeps the address and the token from it.
- **A 403 from any route** means the token is wrong or gone: show the
  first-run screen again, as the page shows its gate.
- **Every request gets a deadline.** A phone's link to node1 stalls for a
  minute or two when it changes access point. The page gives reads 8 s and
  commands 30 s, and treats a miss as "no answer" (`request()` in `m.js`).

## The live frame

`GET /api/events` is a server-sent event stream. Each frame is one
`data: <json>\n\n` line holding the whole deck state; one arrives on every
change and at least every 15 s. The body is close-delimited, with no named
events and no ids.

- React Native has no `EventSource`. Use a library that can send headers
  (`react-native-sse`), or read the stream with `expo/fetch`.
- Probe `GET /api/state` first: a stream cannot show a 403.
- Close the stream when the app goes to the background and open it again on
  return, polling once straight away, as the page does on
  `visibilitychange`. A frame from an hour ago must not look live.

## Routes the page uses

All under the token. The request and reply shapes are in `m.js` and
`deck_api.py`; read those rather than trusting a copy here.

| Card | Read | Change |
|---|---|---|
| Workstation | the SSE frame | `POST /api/boot {intent}` (`sim`, `squadrons`, `plain`, `code`), `/api/shutdown`, `/api/abort` |
| Notices, events | the SSE frame | none |
| Douglas | `GET /api/douglas` (polled) | `POST /api/douglas/say`, `/hear`, `/ptt`, `/approval`, `/chat` |
| Pictures | `GET /api/douglas/picture?id=` | none |
| Earlier turns | `GET /api/douglas/transcript` | none |
| Memory | `GET /api/douglas/memory` | `POST /api/douglas/memory` |
| What he may use | in `/api/douglas` | `POST /api/douglas/switch` |
| Cameras | `GET /api/cameras` | `POST /api/cameras`, `POST /api/cameras/image` |
| Now playing | `GET /api/media`, `/api/media/art?v=` | `POST /api/media/command {action}` |
| Fleet | `GET /api/fleet` | none |

Images (`/picture`, `/media/art`, `/cameras/image`) need the header too, so
an image component has to be given it; a bare URL will get a 403.

### Asking Douglas

`POST /api/douglas/say` with `{text, via: "phone", id, image?}`.

- `id` is made on the phone (`q-` and 24 hex digits). A question sent twice
  with the same id is asked once, so a retry after a lost reply is safe.
- `image` is base64 JPEG, with no `data:` prefix. Shrink it on the phone to
  1280 px on the long side at quality 0.85, or to the larger edge Douglas
  asks for (`voice.shot` in `/api/douglas`). Offer the camera only when
  `voice.eyes` is true.
- The reply to the POST says only whether he took it: `{ok, message, retry}`.
  His answer arrives in the conversation log, by the poll.
- Port the page's queue (`trySend()` in `m.js`) as it is: one question at a
  time, retried while `retry` is true or there is no answer, given up after
  five minutes with the words and photo still in the box.

### Talking to him

`POST /api/douglas/hear` with `{audio, rate: 16000, via: "phone", id}`:
`audio` is base64 of raw 16-bit mono PCM at 16 kHz, with no WAV header. At
most 20 s; under 0.4 s is not worth sending.

His reply comes back as sound: poll `GET /api/douglas/speech?id=<id>` once a
second. It answers with `audio/*` (a WAV to play) when ready, or JSON whose
`state` is `nothing` (no words heard), `failed` or `unknown` (he answered in
the log, without sound). Give up after two minutes.

Play it through the loudspeaker, not the earpiece: set the audio session to
playback once recording has stopped. The page's workarounds for this (the
separate audio context, the silent first play) are browser matters and do
not carry over.

`POST /api/douglas/ptt {action}` is the other kind of talking, where
DougVoice in the house listens and not the phone. Keep it only if it is
still wanted once the phone's own microphone always works.

## What carries over from the page

- `pi/deck-ui/js/format.js` and `fleet.js` are pure and can be shared or
  copied.
- `owner.js`, `memory.js`, `notices.js`, `cameras.js`, `convo.js` and `ui.js`
  build DOM. Their logic (what counts as a notice, how a turn is labelled,
  how a hold is timed) is the specification; the rendering is rewritten as
  components.
- The wording. The page's notes and labels were chosen with care; keep them.

## Phases

Each one ends with something usable on the phone.

1. **Scaffold and connect.** `phone/` with Expo, TypeScript and
   `expo-dev-client`. The first-run screen, the token in the Keychain, the
   probe and the SSE stream, the connection state and the clock. A
   `phone/README.md` with the build and install commands.
2. **Douglas.** His state and meters, the conversation log with pictures, a
   typed question, a photo from the camera or library, the retry queue, an
   approval waiting for a yes, the workshop job.
3. **Voice.** Hold to talk with the phone's microphone, and his reply from
   the loudspeaker.
4. **The rest of the deck.** Workstation tiles and holds, notices, memory
   and what he did, conversations, what he may use, cameras, now playing,
   fleet, events.
5. **Share to Douglas.** A share-sheet target (`expo-share-intent`): sharing
   opens the app on a sheet with what was shared and room for a note, and
   SEND puts it through the same queue as a typed question.
   - A photo goes in `image`, shrunk as above.
   - A link goes in `text`. Douglas has a `read_page` tool and can open it.
   - Other kinds of file (a PDF, a video) have no route yet. Leave them out
     of the first version, and say so on the sheet instead of failing.

Phases 1 and 2 are worth installing before the rest is built.

## Decided since

- **Push notifications: yes.** deck-api sends them itself, through Apple's
  push service, with a key from the developer account
  (`pi/push-setup.sh`): a new warning or critical notice, a change waiting
  for a yes, a machine that has stopped answering. It signs with `openssl`
  and sends with `curl`, so nothing is installed on the Pi and no Expo
  service is involved. The routes are `/api/push`, `/api/push/register`,
  `/api/push/forget` and `/api/push/test`.
- **The share sheet opens the app.** The sheet there shows what was shared
  and a note already filled in with "What can you tell me about this?", so
  sending is one touch and the question can be changed first. No separate
  share extension screen and no token in an App Group.
- **`ptt` stays.** The app has two buttons: HOLD TO TALK through the phone,
  and a smaller one that has DougVoice listen in the house.

## Built

`phone/` holds the app; `phone/README.md` has the build, install and push
setup. All five phases are written.
