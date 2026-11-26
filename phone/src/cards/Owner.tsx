/* Owner.tsx — his conversations, and what he may use.

   Conversations: the one in front of him, the ones kept, a new one. Opening
   one is a touch, deleting one a hold.

   Switches: each thing he could use, and whether he has it. A touch takes
   one away and giving it back takes a hold; he finds out at his next turn
   and cannot reach the switches himself. A switch takes a tool away, not a
   subject: with web pages or the search still on, he can read by them what
   the tool read. */

import { useState } from 'react';
import { Alert, View } from 'react-native';

import { useDeck } from '../deck';
import { chatRows, chatsCount, switchesPill, switchRows } from '../lib/owner';
import { Btn, C, Card, Empty, Head, HoldSwitch, Note, Row, s as ui, T, TapSwitch } from '../ui';

export function ChatsCard({ chats, changed, alone }: { chats: any; changed: () => void; alone?: boolean }) {
  const { deck } = useDeck();
  const [opened, setOpen] = useState(false);
  const open = alone || opened;
  const [note, setNote] = useState({ text: '', bad: false });
  const ask = (body: object, done: string) => {
    setNote({ text: '', bad: false });
    deck.post('/api/douglas/chat', body).then((a) => {
      const ok = !!(a && a.ok);
      setNote({ text: ok ? done : (a && (a.message || a.text)) || 'node1 did not answer', bad: !ok });
      if (ok) changed();
    });
  };
  const rows = chatRows(chats, Date.now() / 1000);
  return (
    <Card>
      <Head title="CONVERSATIONS" pill={chats ? chatsCount(chats) : '—'} onPress={alone ? undefined : () => setOpen(!open)}
            open={open} />
      {chats && chats.title ? <T size={13} tone="mut" numberOfLines={1}>{chats.title}</T> : null}
      {open ? (
        <View style={{ gap: 8 }}>
          <View style={{ flexDirection: 'row', alignItems: 'center', gap: 14 }}>
            {chats ? (
              <Btn label="NEW CONVERSATION" tone="ok" small style={{ paddingHorizontal: 18 }}
                   onPress={() => ask({ op: 'new' }, 'A new one is in front of him.')} />
            ) : null}
            <Note text={note.text} bad={note.bad} style={{ flex: 1 }} />
          </View>
          {!chats ? (
            <Empty text={'His conversations are not kept: the voice pipeline on Douglas is not answering, is older ' +
                         'than this, or has VOICE_CHATS_DIR set to "-".'} />
          ) : !rows.length ? (
            <Empty text="None is kept yet. One is, from the first thing said in it." />
          ) : null}
          {rows.map((r, i) => (
            <View key={r.id} style={[{ paddingVertical: 8, gap: 8, borderTopWidth: i ? 1 : 0, borderTopColor: C.ln }]}>
              <View style={{ gap: 2 }}>
                <T tone={r.here ? 'acc' : undefined}>{r.title}</T>
                <T size={11} tone="mut">{r.sub}</T>
              </View>
              <View style={{ flexDirection: 'row', gap: 8 }}>
                {r.here ? (
                  <View style={[ui.btn, ui.btnSmall, { flex: 1, borderColor: C.ln, borderStyle: 'dashed' }]}>
                    <T size={10} ls={0.12} tone="mut">IN FRONT OF HIM</T>
                  </View>
                ) : (
                  <Btn label="OPEN" aria={'open ' + r.title} tone="ok" small style={{ flex: 1 }}
                       onPress={() => ask({ op: 'switch', id: r.id }, 'It is in front of him.')} />
                )}
                <Btn label="NAME" small style={{ flex: 1 }} onPress={() => {
                  Alert.prompt('Name the conversation', undefined, (text) => {
                    if (text && text.trim()) ask({ op: 'rename', id: r.id, title: text }, 'Named.');
                  }, 'plain-text', r.title);
                }} />
                <HoldSwitch label="HOLD · DELETE" aria={'hold to delete ' + r.title}
                            onFire={() => ask({ op: 'delete', id: r.id }, 'Deleted.')} />
              </View>
            </View>
          ))}
        </View>
      ) : null}
    </Card>
  );
}

export function SwitchesCard({ tools, changed, alone }:
  { tools: Record<string, string> | null; changed: () => void; alone?: boolean }) {
  const { deck } = useDeck();
  const [opened, setOpen] = useState(false);
  const open = alone || opened;
  const [note, setNote] = useState({ text: '', bad: false });
  const pill = switchesPill(tools);
  const rows = switchRows(tools);
  const set = (tool: string, on: boolean) => {
    setNote({ text: '', bad: false });
    deck.post('/api/douglas/switch', { tool, on }).then((a) => {
      const ok = !!(a && a.ok);
      setNote({ text: ok ? 'He finds out at his next turn.' : (a && (a.message || a.text)) || 'node1 did not answer',
                bad: !ok });
      if (ok) changed();
    });
  };
  return (
    <Card>
      <Head title="WHAT HE MAY USE" pill={pill.text} tone={pill.cls} onPress={alone ? undefined : () => setOpen(!open)}
            open={open} />
      {open ? (
        <View>
          {!rows.length ? (
            <Empty text={'His switches could not be read: the voice pipeline on Douglas is not answering, or is ' +
                         'older than the switches.'} />
          ) : null}
          {rows.map((r, i) => (
            <Row key={r.name} first={i === 0} title={r.label} dim={r.state === 'absent'}
                 sub={r.state === 'absent' ? 'not set up on Douglas' : r.does}>
              {r.state === 'on' ? (
                <TapSwitch label="ON" aria={'switch ' + r.label + ' off'} onPress={() => set(r.name, false)} />
              ) : r.state === 'off' ? (
                // Giving something back takes a hold; taking it away, a touch.
                <HoldSwitch label="OFF · HOLD" aria={'hold to switch ' + r.label + ' on'}
                            onFire={() => set(r.name, true)} />
              ) : (
                <View style={[ui.btn, ui.btnSmall, ui.sw, { borderColor: C.ln, borderStyle: 'dashed' }]}>
                  <T size={11} tone="mut">—</T>
                </View>
              )}
            </Row>
          ))}
          <Note text={note.text} bad={note.bad} />
        </View>
      ) : null}
    </Card>
  );
}
