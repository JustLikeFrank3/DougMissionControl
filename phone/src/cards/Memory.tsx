/* Memory.tsx — what Douglas remembers, for the owner to read, put right and
   take away, and under it the record of what he did. He can do neither
   himself: both come through a door to his memory that he has no key to.

   Closed until tapped, so the cards below stay within reach. Closed, the
   card asks only how much there is. Open, it reads the list and the record,
   and looks again now and then: a conversation that has gone quiet turns
   into memories a minute or so later.

   A deletion can be undone, so it takes a tap; a purge cannot, so it takes
   a hold, like everything else on this deck that cannot be taken back. */

import { useCallback, useEffect, useRef, useState } from 'react';
import { Pressable, ScrollView, StyleSheet, TextInput, View } from 'react-native';

import { useDeck } from '../deck';
import { fmtClock } from '../lib/format';
import {
  didLine, emptyWords, KINDS, listPath, MEMORY_ROUTE, memoryCount, pastLine, provenance, STATES,
} from '../lib/memory';
import { getPrefs, setPrefs } from '../store';
import { Btn, C, Card, Chip, Empty, Head, Hold, s as ui, T } from '../ui';

type St = { status: string; kind: string; q: string };

export function MemoryCard({ alone }: { alone?: boolean }) {
  const { deck, active } = useDeck();
  const [opened, setOpen] = useState(!!(getPrefs().open || {}).mem);
  const open = alone || opened;      // on a page of its own it is simply open
  const [st, setSt] = useState<St>({ status: 'active', kind: '', q: '' });
  const [rows, setRows] = useState<any[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [stats, setStats] = useState<any>(null);
  const [did, setDid] = useState<any>(undefined);
  const [openId, setOpenId] = useState(0);
  const [detail, setDetail] = useState<any>(null);
  const [edit, setEdit] = useState<{ id: number; text: string } | null>(null);
  const [note, setNote] = useState({ text: '', bad: false });
  const [find, setFind] = useState('');
  const seq = useRef(0), now = Date.now() / 1000;
  const live = useRef({ st, rows, edit });
  live.current = { st, rows, edit };

  const say = (text: string, bad?: boolean) => setNote({ text, bad: !!bad });

  const load = useCallback((with_?: St, further?: boolean) => {
    const q = with_ || live.current.st;
    const mine = ++seq.current;
    return deck.get(listPath(q, further ? live.current.rows.length : 0)).then((r) => {
      if (mine !== seq.current) return;         // a newer question is out
      if (!r || !r.ok) {
        // Not read is not empty: what was on screen stays, and it says so.
        say((r && r.text) || 'node1 did not answer', true);
        return;
      }
      setRows(further ? live.current.rows.concat(r.memories || []) : r.memories || []);
      setTotal(r.total || 0);
      setNote((n) => (n.bad ? { text: '', bad: false } : n));
    });
  }, [deck]);

  const glance = useCallback(() => {
    deck.get(MEMORY_ROUTE + '?op=stats').then((r) => { if (r && r.ok) setStats(r); });
  }, [deck]);

  const refresh = useCallback((force?: boolean) => {
    if (live.current.edit && !force) return;    // something is being typed
    glance();
    load();
    deck.get(MEMORY_ROUTE + '?op=events&limit=12').then(setDid);
  }, [deck, glance, load]);

  useEffect(() => {
    if (!active) return;
    const look = () => (open ? refresh() : glance());
    look();
    const id = setInterval(look, 30000);
    return () => clearInterval(id);
  }, [active, open, refresh, glance]);

  const pickSt = (next: St) => { setSt(next); setOpenId(0); load(next); };
  const change = (body: object, done: string | ((r: any) => string), then?: () => void) => {
    deck.post(MEMORY_ROUTE, body).then((r) => {
      if (!r) say('node1 did not answer', true);
      else if (!r.ok) say(r.text || 'refused', true);
      else { say(typeof done === 'string' ? done : done(r)); if (then) then(); refresh(true); }
    });
  };

  const toggle = () => {
    const next = !opened;
    setOpen(next);
    setPrefs({ open: { ...(getPrefs().open || {}), mem: next } });
  };

  const by = (stats && stats.by_kind) || {};
  const have = KINDS.filter((k) => by[k] || st.kind === k);

  const row = (m: any, first: boolean) => {
    const isOpen = openId === m.id, editing = edit && edit.id === m.id;
    const past: any[] = (isOpen && detail && detail.id === m.id && detail.history) || [];
    return (
      <Pressable key={m.id} style={[sty.mem, first && { borderTopWidth: 0 }, isOpen && sty.memOpen]}
                 onPress={() => {
                   if (edit) return;
                   setOpenId(isOpen ? 0 : m.id);
                   setDetail(null);
                   if (!isOpen) {
                     deck.get(MEMORY_ROUTE + '?op=get&id=' + m.id).then((d) => { if (d && d.ok) setDetail(d); });
                   }
                 }}>
        <View style={{ flexDirection: 'row', gap: 8, alignItems: 'baseline' }}>
          <T size={10} ls={0.14} tone="acc">{String(m.kind || '').toUpperCase()}</T>
          <T size={12} tone="mut" numberOfLines={1} style={{ flex: 1 }}>{m.subject || ''}</T>
        </View>
        {editing ? (
          <TextInput value={edit!.text} onChangeText={(text) => setEdit({ id: m.id, text })} multiline autoFocus
                     maxLength={600} keyboardAppearance="dark"
                     style={[ui.input, { borderColor: C.acc, backgroundColor: C.bg, minHeight: 88 }]} />
        ) : (
          <T tone={m.done ? 'mut' : undefined} style={m.done ? { textDecorationLine: 'line-through' } : undefined}>
            {m.text || ''}
          </T>
        )}
        <T size={11} tone="mut">{provenance(m, now)}</T>
        {isOpen ? past.slice(0, 4).filter((h) => h.action !== 'kept').map((h, i) => (
          <T key={i} size={11} tone="mut" style={sty.past}>{pastLine(h, m, now)}</T>
        )) : null}
        {isOpen && editing ? (
          <View style={ui.acts}>
            <Btn label="SAVE" tone="acc" style={{ flex: 1 }} onPress={() => {
              const text = edit!.text.trim();
              if (!text || text === m.text) { setEdit(null); return; }
              change({ op: 'correct', id: m.id, text }, 'Corrected. It is your word from now on.', () => setEdit(null));
            }} />
            <Btn label="CANCEL" style={{ flex: 1 }} onPress={() => setEdit(null)} />
          </View>
        ) : isOpen && m.status === 'active' ? (
          <View style={ui.acts}>
            <Btn label="CORRECT" style={{ flex: 1 }} onPress={() => setEdit({ id: m.id, text: m.text || '' })} />
            {m.kind === 'task' && !m.done ? (
              <Btn label="DONE" style={{ flex: 1 }}
                   onPress={() => change({ op: 'correct', id: m.id, done: true }, 'Marked as done.')} />
            ) : null}
            <Btn label="DELETE" tone="warn" style={{ flex: 1, borderColor: C.ln }}
                 onPress={() => change({ op: 'delete', id: m.id },
                                       'Deleted. It is under DELETED, to bring back or to purge.')} />
          </View>
        ) : isOpen ? (
          <View style={ui.acts}>
            <Btn label="BRING BACK" style={{ flex: 1 }}
                 onPress={() => change({ op: 'restore', id: m.id }, 'Brought back.')} />
            <Hold tone="bad" aria="hold to purge" style={{ flex: 1, minHeight: 44, borderColor: C.rd, backgroundColor: C.pan }}
                  onFire={() => change({ op: 'purge', id: m.id }, (r) => (r.lingering
                    // Something else was reading the file (a backup): the row
                    // is gone, and its text leaves the log within the half minute.
                    ? 'Purged. Something was reading the file, so its text leaves the log in a moment.'
                    : 'Purged. It is gone from his memory for good, with what it had replaced.'),
                    () => setOpenId(0))}>
              <T size={12} ls={0.12} tone="bad">HOLD TO PURGE</T>
            </Hold>
          </View>
        ) : null}
        {/* What is said about a change is said where the eye is: in the row that is open. */}
        {isOpen && note.text ? <T size={12} tone={note.bad ? 'bad' : 'mut'}>{note.text}</T> : null}
      </Pressable>
    );
  };

  return (
    <Card>
      <Head title="MEMORY" pill={memoryCount(stats)} onPress={alone ? undefined : toggle} open={open} />
      {open ? (
        <View style={{ gap: 10 }}>
          <TextInput value={find} onChangeText={(v) => { setFind(v); if (!v && st.q) pickSt({ ...st, q: '' }); }}
                     placeholder="Search what he remembers…" placeholderTextColor={C.mut} maxLength={80}
                     returnKeyType="search" clearButtonMode="while-editing" autoCorrect={false}
                     keyboardAppearance="dark" style={ui.input}
                     onSubmitEditing={() => pickSt({ ...st, q: find.trim() })} />
          <View style={{ gap: 6 }}>
            <View style={{ flexDirection: 'row', gap: 6 }}>
              {STATES.map(([key, word]) => (
                <Chip key={key} label={word + (stats && stats[key] ? ' ' + stats[key] : '')} on={st.status === key}
                      onPress={() => pickSt({ ...st, status: key })} />
              ))}
            </View>
            {/* The kinds are one row that slides under a thumb: wrapped,
                nine of them put the first memory a screen away. Only the
                kinds he has any of. */}
            {have.length > 1 || st.kind ? (
              <ScrollView horizontal showsHorizontalScrollIndicator={false} style={{ marginHorizontal: -14 }}
                          contentContainerStyle={{ gap: 6, paddingHorizontal: 14 }}>
                <Chip label="ALL" on={!st.kind} onPress={() => pickSt({ ...st, kind: '' })} />
                {have.map((k) => (
                  <Chip key={k} label={k.toUpperCase() + (st.status === 'active' && by[k] ? ' ' + by[k] : '')}
                        on={st.kind === k} onPress={() => pickSt({ ...st, kind: st.kind === k ? '' : k })} />
                ))}
              </ScrollView>
            ) : null}
          </View>
          {!openId && note.text ? <T size={12} tone={note.bad ? 'bad' : 'mut'}>{note.text}</T> : null}
          <View>
            {rows.map((m, i) => row(m, i === 0))}
            {!rows.length && total !== null ? <Empty text={emptyWords(st)} /> : null}
          </View>
          {total !== null && total > rows.length ? (
            <Btn label="MORE" tone="cy" onPress={() => load(undefined, true)}
                 style={{ borderColor: C.ln, backgroundColor: 'transparent' }} />
          ) : null}
          <View style={sty.sub2}><T size={12} ls={0.18} tone="mut">WHAT HE DID</T></View>
          {/* The record of what he did, newest first: which tool, to do
              what, on whose asking, whether it worked and how long it took. */}
          <View style={{ gap: 6 }}>
            {did === undefined ? null : !did || !did.ok ? (
              <T size={13} tone="mut">{(did && did.text) || 'node1 did not answer'}</T>
            ) : (did.events || []).length ? (did.events as any[]).map((e, i) => (
              <View key={i} style={{ flexDirection: 'row', gap: 8 }}>
                <T size={13} tone="mut">{fmtClock(e.at)}</T>
                <T size={13} tone="cy">{e.name || e.kind || ''}</T>
                <T size={13} tone={e.ok ? undefined : 'warn'} style={{ flex: 1 }}>{didLine(e)}</T>
              </View>
            )) : <T size={13} tone="mut">nothing yet</T>}
          </View>
        </View>
      ) : null}
    </Card>
  );
}

const sty = StyleSheet.create({
  mem: { gap: 4, paddingVertical: 10, borderTopWidth: 1, borderTopColor: C.ln },
  memOpen: { marginHorizontal: -14, paddingHorizontal: 14, backgroundColor: C.well },
  past: { paddingLeft: 8, borderLeftWidth: 1, borderLeftColor: C.ln },
  sub2: { marginTop: 6, paddingTop: 12, borderTopWidth: 1, borderTopColor: C.ln },
});
