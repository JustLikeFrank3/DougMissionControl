/* Cameras.tsx — DougRing in the app: the owner's camera switches.

   DougRing, on node1, holds the Ring link and keeps the rules; this card
   only shows them and asks for changes. A touch switches off, a hold
   switches on. LOOK is a picture taken now, shown here and nowhere else: it
   lives in the app's memory until it is closed, and is never saved.

   Read when the card is opened and every fifteen seconds while it is. */

import { useRef, useState } from 'react';
import { Alert, View } from 'react-native';
import { Image } from 'expo-image';

import { useDeck, usePoll } from '../deck';
import { cameraSub, healthRows, journalLine, obsLine } from '../lib/cameras';
import { toBase64 } from '../lib/pcm';
import { Btn, C, Card, Empty, Head, HoldSwitch, Note, Row, T, TapSwitch } from '../ui';

type Shot = { busy?: string; error?: string; uri?: string; ratio?: number; cap?: string; name?: string };

export function CamerasCard({ alone }: { alone?: boolean }) {
  const { deck } = useDeck();
  const [opened, setOpen] = useState(false);
  const open = alone || opened;
  const [v, refresh] = usePoll('/api/cameras', 15000, open);
  const [note, setNote] = useState({ text: '', bad: false });
  const [shot, setShot] = useState<Shot | null>(null);
  const looking = useRef<Record<string, boolean>>({});

  const change = (body: object, done: string) => {
    setNote({ text: '', bad: false });
    deck.post('/api/cameras', body).then((a) => {
      const ok = !!(a && a.ok);
      setNote({ text: ok ? done : (a && (a.message || a.error || a.code)) || 'node1 did not answer', bad: !ok });
      if (ok) refresh();
    });
  };

  const look = (c: any) => {
    if (looking.current[c.id]) return;
    looking.current[c.id] = true;
    setShot({ busy: 'Asking ' + c.name + ' for a picture of now… (up to a minute)' });
    deck.picture({ camera: c.id }, toBase64).then((r) => {
      looking.current[c.id] = false;
      if ('error' in r) setShot({ error: 'No picture: ' + r.error });
      else setShot({ uri: 'data:' + r.type + ';base64,' + r.b64, cap: obsLine(r.obs), name: c.name, ratio: 16 / 9 });
      refresh();
    });
  };

  const close = () => { setOpen(false); setShot(null); };
  const st = (v && v.status) || {};
  const cams: any[] = (v && v.cameras) || [];
  const on = cams.filter((c) => c.enabled).length;
  const ready = v && v.installed && v.up;
  const acct = (st.accounts || [])[0];
  const names: Record<string, string> = {};
  cams.forEach((c) => { names[c.id] = c.name; });

  return (
    <Card>
      <Head title="CAMERAS" pill={!open && v === undefined ? '—' : !ready ? '—' : st.global_on ? on + ' on' : 'OFF'}
            onPress={alone ? undefined : () => (open ? close() : setOpen(true))} open={open} />
      {!open || v === undefined ? null : !v || !v.installed ? (
        <Empty text={v ? 'DougRing is not installed on node1 (dougring/setup.sh).' : 'node1 did not answer.'} />
      ) : !v.up ? (
        <Empty text={'DougRing is not answering on node1: ' + (v.error || '')} />
      ) : (
        <View style={{ gap: 8 }}>
          {/* the switch for all of them */}
          <Row first title="All cameras"
               sub={st.global_on ? 'Douglas may look through the cameras switched on below'
                 : 'nobody can have a picture, Douglas included'}>
            {st.global_on ? (
              <TapSwitch label="ON" aria="switch all cameras off"
                         onPress={() => change({ op: 'switch', on: false }, 'All cameras are off.')} />
            ) : (
              <HoldSwitch label="OFF · HOLD" aria="hold to switch all cameras on"
                          onFire={() => change({ op: 'switch', on: true }, 'Cameras are on.')} />
            )}
          </Row>
          <View>
            {!cams.length ? (
              <Empty text={(st.accounts || []).length
                ? (v.cameras_error ? 'Ring did not answer: ' + v.cameras_error : 'Ring shares no camera with DougRing.')
                : 'No Ring account is linked yet: "Log in with Ring" in the Ring Developer Console.'} />
            ) : null}
            {cams.map((c, i) => (
              <Row key={c.id} first={i === 0} title={c.name} sub={cameraSub(c)} subBad={!c.online}
                   onTitle={() => Alert.prompt('Name for ' + (c.ring_name || 'this camera'), undefined, (name) => {
                     if (name != null) change({ op: 'name', id: c.id, name }, 'Renamed.');
                   }, 'plain-text', c.name)}>
                {c.enabled && st.global_on && c.online ? (
                  <Btn label="LOOK" aria={'take a picture from ' + c.name} tone="ok" small onPress={() => look(c)}
                       style={{ minHeight: 40 }} />
                ) : null}
                {c.enabled ? (
                  <TapSwitch label="ON" aria={'switch ' + c.name + ' off'}
                             onPress={() => change({ op: 'disable', id: c.id }, c.name + ' is off.')} />
                ) : (
                  <HoldSwitch label="OFF · HOLD" aria={'hold to switch ' + c.name + ' on'}
                              onFire={() => change({ op: 'enable', id: c.id }, c.name + ' is on.')} />
                )}
              </Row>
            ))}
          </View>
          {shot ? (
            <View style={{ gap: 6 }}>
              {shot.busy ? <T size={12} tone="mut">{shot.busy}</T> : null}
              {shot.error ? <T size={12} tone="bad">{shot.error}</T> : null}
              {shot.uri ? (
                <>
                  <Image source={{ uri: shot.uri }} contentFit="contain" cachePolicy="none"
                         accessibilityLabel={'what ' + shot.name + ' saw'}
                         style={{ width: '100%', aspectRatio: shot.ratio, borderWidth: 1, borderColor: C.ln }}
                         onLoad={(e) => {
                           if (e.source.width && e.source.height) setShot((s) => (s ? { ...s, ratio: e.source.width / e.source.height } : s));
                         }} />
                  <T size={12} tone={/NOT LIVE/.test(shot.cap || '') ? 'warn' : 'ok'}>{shot.cap}</T>
                </>
              ) : null}
              {!shot.busy ? <Btn label="CLOSE" aria="close the picture" tone="ok" small onPress={() => setShot(null)} /> : null}
            </View>
          ) : null}
          <Note text={note.text} bad={note.bad} />
          {/* the link and the service */}
          <View style={{ gap: 4, paddingTop: 6, borderTopWidth: 1, borderTopColor: C.ln }}>
            {healthRows(st).map((r) => (
              <View key={r[0]} style={{ flexDirection: 'row', gap: 10 }}>
                <T size={12} tone="mut" style={{ width: 92 }}>{r[0]}</T>
                <T size={12} tone={r[2] ? 'bad' : undefined} style={{ flex: 1 }}>{r[1]}</T>
              </View>
            ))}
            {acct ? (
              <Row title="Forget the Ring account"
                   sub="drops the tokens and switches everything off; linking again is the way back">
                <HoldSwitch label="FORGET · HOLD" aria="hold to forget Ring"
                            onFire={() => change({ op: 'forget' }, 'Forgotten here. To end it at Ring too: ' +
                              '"Disconnect all" in the Ring Developer Console.')} />
              </Row>
            ) : null}
          </View>
          {/* the journal */}
          <View>
            <T size={12} ls={0.08} tone="mut" style={{ marginTop: 8, marginBottom: 2 }}>WHO LOOKED</T>
            {((v.journal || []) as any[]).slice(0, 15).map((j, i) => {
              const line = journalLine(j, names);
              return <T key={i} size={12} tone={line.bad ? 'bad' : undefined} style={{ paddingVertical: 2 }}>{line.text}</T>;
            })}
            {!(v.journal || []).length ? <Empty text="Nobody has looked yet." /> : null}
          </View>
        </View>
      )}
    </Card>
  );
}
