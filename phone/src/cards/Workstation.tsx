/* Workstation.tsx — the one machine this deck can start and stop. Every
   command here is a hold. */

import { useEffect, useRef, useState } from 'react';
import { StyleSheet, View } from 'react-native';

import { useDeck } from '../deck';
import { INTENTS, wsView } from '../lib/workstation';
import { C, Card, Hold, Note, Pill, T } from '../ui';

export function WorkstationCard() {
  const { deck, state } = useDeck();
  const [note, setNote] = useState('');
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);
  const v = wsView(state || {});
  const said = (r: any) => {
    const msg = !r ? 'deck-api did not answer' : !r.ok && r.message ? r.message : '';
    if (!msg) return;
    setNote(msg);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setNote(''), 6000);
  };
  return (
    <Card>
      <View style={{ flexDirection: 'row', alignItems: 'center', gap: 10 }}>
        <Pill text={state ? v.pill : '—'} tone={v.cls} />
        <T size={12} tone="mut" numberOfLines={1} style={{ flex: 1 }}>{v.meta}</T>
      </View>
      <T size={34} ls={0.04} style={{ lineHeight: 38 }}>{state ? v.big : '—'}</T>
      {v.phase ? <T size={13} tone="warn">{v.phase}</T> : null}
      <View style={st.tiles}>
        {INTENTS.map(([intent, name, sub]) => (
          <Hold key={intent} disabled={v.inFlight || !state} aria={'hold to start ' + name}
                onFire={() => deck.post('/api/boot', { intent }).then(said)} style={st.tile}>
            <T ls={0.1}>{name}</T>
            <T size={11} tone="mut" style={{ marginTop: 3 }}>{sub}</T>
          </Hold>
        ))}
      </View>
      {v.canOff ? (
        <Hold tone="warn" aria={v.shutdown.toLowerCase()} style={st.wide}
              onFire={() => deck.post('/api/shutdown', {}).then(said)}>
          <T size={14} ls={0.14} tone="warn">{v.shutdown}</T>
        </Hold>
      ) : null}
      {v.inFlight ? (
        <Hold tone="bad" aria="hold to abort" style={[st.wide, { borderColor: C.rd }]}
              onFire={() => { deck.post('/api/abort', {}); }}>
          <T size={14} ls={0.14} tone="bad">HOLD TO ABORT</T>
        </Hold>
      ) : null}
      <Note text={note} bad />
    </Card>
  );
}

const st = StyleSheet.create({
  tiles: { flexDirection: 'row', flexWrap: 'wrap', gap: 8 },
  tile: { flexBasis: '47%', flexGrow: 1, minHeight: 64, paddingVertical: 14, paddingHorizontal: 12,
          alignItems: 'flex-start' },
  wide: { minHeight: 52 },
});
