# The phone app

The phone page (`pi/deck-ui/m/`, served at `/m`) as an iOS app: the same
deck-api, the same live frame and the same routes, in one column for a thumb.
Built with Expo on the Mac and installed straight onto the owner's phone. No
TestFlight, no App Store, no EAS, no Expo account.

`/m` stays: it is the fallback from any browser.

What the app has that the page does not:

- **The token in the Keychain**, sent in an `Authorization` header. Never in
  an address.
- **The phone's own microphone and speaker**, always: HOLD TO TALK records
  16 kHz PCM and plays his reply through the loudspeaker. No HTTPS page, no
  silent first play, no earpiece.
- **DougVoice still listens too.** A second, smaller button borrows
  DougVoice's ears in the house, as the page's button does on plain HTTP.
- **A share-sheet target, "Ask Douglas".** Sharing a photo, a link or text
  from any app opens the app on a sheet with what was shared and a note,
  filled in with "What can you tell me about this?". SEND puts it through the
  same queue as a typed question.
- **Notifications with the app closed**: a new warning or critical notice, a
  change of Douglas's waiting for a yes, a machine that has stopped
  answering.

The rules of the deck hold here as on the page: everything shown is
observed, every command that starts or stops something is a hold, and a
missing reading is a dash, never a zero.

## What the Mac needs

- Xcode, signed in with the developer account (Xcode > Settings > Accounts),
  with the iOS platform installed (Xcode > Settings > Components)
- CocoaPods: `brew install cocoapods`
- Node (LTS) and git
- Tailscale, on the same tailnet as node1, so the Simulator reaches deck-api

The phone needs the Tailscale app running, as it does for the page.

## Before the first build

Two things in `app.json` are yours to set:

- `expo.ios.bundleIdentifier`: `com.dougmissioncontrol.phone` as it stands.
  It has to be unique to your developer account; change it if Xcode says it
  is taken. The share extension and the push topic follow it.
- `expo.ios.appleTeamId`: add your ten-character Team ID (developer.apple.com
  > Membership details). With it both targets, the app and the "Ask Douglas"
  share extension, are signed without opening Xcode. Without it, open
  `ios/Doug.xcworkspace` once and pick the team for each target under
  Signing & Capabilities.

## Build and install

```bash
cd phone
npm ci
npx expo run:ios                              # the Simulator
npx expo run:ios --device                     # the phone, on a cable or the same network
npx expo run:ios --device --configuration Release   # a build that needs no Mac running afterwards
```

`expo run:ios` generates `ios/` from `app.json` (it is not kept in the
repository), runs `pod install`, builds, signs and installs. A debug build
loads its JavaScript from Metro on the Mac, so the Mac has to be running and
reachable; the `Release` build carries its JavaScript and runs on its own.
An install lasts a year with the paid account.

After changing `app.json` or adding a native module: `npx expo prebuild
--clean --platform ios`, then build again.

## First run

On node1: `sudo ./pi/mobile-https.sh` once (the app needs the HTTPS address;
iOS refuses plain HTTP and the app carries no exception), then
`sudo ./pi/mobile-link.sh`, which prints `https://…/m?token=…` and a QR code.

In the app: SCAN THE QR CODE, or paste the whole link. The app keeps the
address and the token from it. A 403 from any route shows this screen again.
HOLD TO FORGET THIS PHONE'S LINK, at the bottom of the deck, takes both out
of the Keychain.

## Notifications

node1 sends them through Apple's push service, with a key from the developer
account. Once:

1. developer.apple.com > Certificates, Identifiers & Profiles > Keys > +,
   tick "Apple Push Notifications service (APNs)", download the `.p8`, note
   the Key ID.
2. Copy the `.p8` to node1 and run
   `sudo ./pi/push-setup.sh AuthKey_XXXX.p8 <Key ID> <Team ID> [bundle id]`.
3. In the app, on a real phone: NOTICES ON THIS PHONE > TURN ON, then SEND A
   TEST.

deck-api signs with `openssl` and sends with `curl`, both on the Pi already;
nothing is installed. What goes through Apple is the notice's own sentence.
Of a change waiting for a yes only its kind goes: the words that were read
back are read in the app, where the yes is given. `sudo ./pi/push-setup.sh
off` takes the key and its settings away again.

A Simulator has no token from Apple, so this part is tested on the phone.

## Layout

```
App.tsx              the shell: the Keychain, the live frame, the cards in order
src/deck.tsx         the one way the app talks to deck-api: deadlines, the header, the stream
src/douglas.ts       his state, the question queue, this phone's microphone and speaker
src/Gate.tsx         the first-run screen: scan or paste the link
src/ShareSheet.tsx   "Ask Douglas" from another app
src/push.ts          Apple's device token, handed to node1
src/cards/           one file per card of the deck
src/lib/             the pure logic, ported from pi/deck-ui/js: what is said, and when
__tests__/           tests of src/lib
```

`src/lib` has no React and no native module in it, so all of it runs in
node. The wording is the page's, kept on purpose.

## Tests

```bash
npm run typecheck
npm test
```

Both run in CI on every pull request (the `phone` job). The iOS build is not
run there: it needs a Mac and a signing identity.

## What has and has not been run

Run in the iOS Simulator (iPhone 18 Pro, iOS 27.2) against the real node1
over the tailnet: the build, the first-run screen, the live frame, every
card with real readings, the memory list, a hold, and a typed question
taken by Douglas and shown in the log.

Installed on an iPhone 16 Pro as a `Release` build, where the owner reports
that it works.

Not yet run: push. It needs the key from `pi/push-setup.sh` on node1, and a
deck-api there that has the `/api/push` routes.

A device build from the command line needs Xcode to be allowed to make the
provisioning profiles the first time, which `expo run:ios` does not ask for:

```bash
cd phone/ios
xcodebuild -workspace Doug.xcworkspace -scheme Doug -configuration Release \
  -destination "id=<the phone's UDID>" -allowProvisioningUpdates build
xcrun devicectl device install app --device <UDID> \
  ~/Library/Developer/Xcode/DerivedData/Doug-*/Build/Products/Release-iphoneos/Doug.app
```

`xcrun devicectl list devices` gives the UDID. After that first time,
`npx expo run:ios --device --configuration Release` works too.
