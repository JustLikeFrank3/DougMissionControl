/* Push.tsx — whether this phone is told about a notice or a waiting yes
   when the app is closed, and the switch for it. */

import { useEffect, useRef, useState } from 'react';
import { View } from 'react-native';

import { useDeck, usePoll } from '../deck';
import { allowed, deviceToken, turnOff, turnOn } from '../push';
import { getPrefs, setPrefs } from '../store';
import { Btn, Card, Head, Note, T } from '../ui';

export function PushCard() {
  const { deck } = useDeck();
  const [on, setOn] = useState(!!getPrefs().push);
  const [note, setNote] = useState({ text: '', bad: false });
  const [busy, setBusy] = useState(false);
  const token = useRef('');
  const [got, refresh] = usePoll('/api/push', 60000);

  // Apple may hand out a new token at any time, and node1 drops a phone
  // Apple says is gone: so a phone that was switched on registers again
  // each time the app starts.
  useEffect(() => {
    if (!getPrefs().push) return;
    allowed().then((may) => {
      if (!may) { setOn(false); setPrefs({ push: false }); return; }
      turnOn(deck, false).then((why) => { if (!why) deviceToken().then((t) => { token.current = t; }); });
    });
  }, [deck]);

  const act = (what: Promise<string>, done: string, after?: () => void) => {
    setBusy(true);
    setNote({ text: '', bad: false });
    what.then((why) => {
      setBusy(false);
      setNote({ text: why || done, bad: !!why });
      if (!why && after) after();
      refresh();
    });
  };

  const ready = got && got.configured;
  return (
    <Card>
      <Head title="NOTICES ON THIS PHONE" pill={!on ? 'OFF' : ready ? 'ON' : 'ON · NODE1 NOT SET UP'}
            tone={!on ? '' : ready ? 'ok' : 'busy'} />
      <T size={12} tone="mut">
        With the app closed: a new warning or critical notice, a change of Douglas's waiting for a yes, a machine that
        has stopped answering. Sent by node1 through Apple. Of a change waiting for a yes, only its kind is sent; the
        words are read here.
      </T>
      {got && !got.configured ? (
        <T size={12} tone="warn">node1 cannot push yet: {got.why}. See phone/README.md.</T>
      ) : null}
      <View style={{ flexDirection: 'row', gap: 8 }}>
        {on ? (
          <Btn label="TURN OFF" style={{ flex: 1 }} disabled={busy}
               onPress={() => act(turnOff(deck), 'Off. node1 has forgotten this phone.',
                                  () => { setOn(false); setPrefs({ push: false }); })} />
        ) : (
          <Btn label="TURN ON" tone="ok" style={{ flex: 1 }} disabled={busy}
               onPress={() => act(turnOn(deck, true), 'On. node1 knows this phone.',
                                  () => { setOn(true); setPrefs({ push: true }); })} />
        )}
        {on && ready ? (
          <Btn label="SEND A TEST" tone="cy" style={{ flex: 1 }} disabled={busy}
               onPress={() => act(deck.post('/api/push/test', {}).then((r) =>
                 (!r ? 'node1 did not answer' : r.ok ? '' : 'Apple took none: ' + (r.message || 'no reason given'))),
                 'Sent. It should arrive in a moment.')} />
        ) : null}
      </View>
      <Note text={note.text} bad={note.bad} />
    </Card>
  );
}
