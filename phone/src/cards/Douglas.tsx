/* Douglas.tsx — talk to him, see what he said, see what it costs. */

import { ComponentRef, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ActionSheetIOS, Linking, Modal, PanResponder, Pressable, ScrollView, StyleSheet, TextInput, useWindowDimensions, View,
} from 'react-native';
import { Image } from 'expo-image';

import { useDeck } from '../deck';
import type { Douglas } from '../douglas';
import { douglasHead, meters } from '../lib/ask';
import { earlierLabel, earlierPath, mergeTurns, oldestTs, Turn, turnKey, whoLabel } from '../lib/convo';
import { fmtClock } from '../lib/format';
import { jobLine, jobRows, jobShort, left, waitingHead } from '../lib/owner';
import { pick } from '../photo';
import { Btn, C, Card, Head, Hold, s as ui, T, Tone, toneColor } from '../ui';

/* ── a picture he drew ──────────────────────────────────────────────────── */
// It comes as an id on his line. The image is kept by its address, so a log
// drawn again does not fetch it again: a few hundred kilobytes each time,
// over a link that is sometimes a relay. A tap fills the screen with it;
// another puts it back.
function Picture({ id }: { id: string }) {
  const { deck } = useDeck();
  const [ratio, setRatio] = useState(1);
  const [failed, setFailed] = useState(0);
  const [big, setBig] = useState(false);
  const source = useMemo(() => deck.image('/api/douglas/picture?id=' + encodeURIComponent(id)), [deck, id]);
  // One that did not load is tried again after a quarter of a minute.
  useEffect(() => {
    if (!failed) return;
    const t = setTimeout(() => setFailed(0), 15000);
    return () => clearTimeout(t);
  }, [failed]);
  if (failed) return <T size={12} tone="mut">his picture did not load</T>;
  return (
    <>
      <Pressable onPress={() => setBig(true)} accessibilityLabel="a picture Douglas drew" style={st.pic}>
        <Image source={source} style={{ width: '100%', aspectRatio: ratio }} contentFit="contain"
               cachePolicy="memory-disk" recyclingKey={id}
               onLoad={(e) => { if (e.source.width && e.source.height) setRatio(e.source.width / e.source.height); }}
               onError={() => setFailed(Date.now())} />
      </Pressable>
      <Modal visible={big} animationType="fade" onRequestClose={() => setBig(false)} statusBarTranslucent>
        <Pressable style={{ flex: 1, backgroundColor: '#000' }} onPress={() => setBig(false)}
                   accessibilityLabel="put the picture back">
          <Image source={source} style={{ flex: 1 }} contentFit="contain" cachePolicy="memory-disk" />
        </Pressable>
      </Modal>
    </>
  );
}

function TurnLine({ t }: { t: Turn }) {
  const me = t.who === 'user';
  return (
    <View style={[st.turn, me ? { alignSelf: 'flex-end', alignItems: 'flex-end' } : { alignSelf: 'flex-start' }]}>
      <T size={10} ls={0.14} tone="mut">
        <T size={10} ls={0.14} tone={me ? 'cy' : 'acc'}>{whoLabel(t)}</T>
        {' · ' + (t.via || 'voice') + (t.ts ? ' · ' + fmtClock(t.ts) : '')}
      </T>
      <View style={st.txt}><T selectable>{t.text || ''}</T></View>
      {!me && t.image ? <Picture id={t.image} /> : null}
    </View>
  );
}

/* ── the log ────────────────────────────────────────────────────────────── */
// What /api/douglas carries is only the last few lines. Scrolled to the
// top, or with EARLIER touched, the log asks for the lines before the oldest
// it shows and puts them above. It scrolls in its own box, so the page
// around it stays put while the conversation is read back; only a log
// already at the bottom follows a new line down.
function Log({ recent, chat }: { recent: Turn[] | null; chat: string }) {
  const { deck } = useDeck();
  const { height } = useWindowDimensions();
  const [, redraw] = useState(0);
  const m = useRef({ older: [] as Turn[], more: true, loading: false, failed: false, chat: '', stuck: true }).current;
  const scroll = useRef<ComponentRef<typeof ScrollView>>(null);
  if (chat !== m.chat) {
    // Another conversation is in front of him: the log starts again, with
    // none of the last one's earlier lines.
    Object.assign(m, { older: [], more: true, loading: false, failed: false, chat, stuck: true });
  }
  // Earlier lines, once there are any, keep every line seen since: what
  // /api/douglas carries slides on, and a line slid out of it must not leave
  // a hole between the earlier ones and the newest.
  const shown = recent === null ? m.older : m.older.length ? (m.older = mergeTurns(m.older, recent)) : recent;

  const earlier = useCallback(() => {
    const before = oldestTs(shown);
    if (m.loading || !m.more || before == null) return;
    m.loading = true;
    m.failed = false;
    redraw((n) => n + 1);
    const asked = m.chat;
    deck.get(earlierPath(before, 0, m.chat)).then((r) => {
      m.loading = false;
      if (asked === m.chat) {
        if (r && r.ok && Array.isArray(r.lines)) {
          m.older = mergeTurns(r.lines, m.older.length ? m.older : shown);
          // An answer with nothing in it ends the asking even if it says
          // more remains: asking again with the same time would get the
          // same nothing.
          m.more = !!r.more && r.lines.length > 0;
        } else {
          m.failed = true;
        }
      }
      redraw((n) => n + 1);
    });
  }, [deck, m, shown]);

  if (!shown.length) return null;
  return (
    <ScrollView ref={scroll} style={{ maxHeight: height * 0.55 }} contentContainerStyle={{ gap: 8 }} nestedScrollEnabled
                scrollEventThrottle={64} maintainVisibleContentPosition={{ minIndexForVisible: 1 }}
                onContentSizeChange={() => { if (m.stuck) scroll.current?.scrollToEnd({ animated: false }); }}
                onScroll={(e) => {
                  const n = e.nativeEvent;
                  m.stuck = n.contentSize.height - n.layoutMeasurement.height - n.contentOffset.y <= 24;
                  if (n.contentOffset.y <= 4) earlier();
                }}>
      <Pressable onPress={m.more && !m.loading ? earlier : undefined} style={{ alignSelf: 'center', padding: 6 }}>
        <T size={10} ls={0.14} tone={!m.more && !m.failed ? 'mut' : 'cy'}>{earlierLabel(m.loading, m.failed, m.more)}</T>
      </Pressable>
      {shown.map((t) => <TurnLine key={turnKey(t)} t={t} />)}
    </ScrollView>
  );
}

/* ── a change of his that is waiting for a yes ──────────────────────────── */
// A hold on APPROVE, bound to the number of exactly what was read back, or a
// tap on REFUSE.
function Approval({ pending }: { pending: any }) {
  const { deck } = useDeck();
  const p = pending && pending.id ? pending : null;
  const [note, setNote] = useState('');
  const [, tick] = useState(0);
  const busy = useRef(false);
  const id = p ? p.id : '';
  useEffect(() => { setNote(''); }, [id]);
  useEffect(() => {
    if (!p) return;
    const t = setInterval(() => tick((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, [id]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!p) return null;
  const answer = (yes: boolean) => {
    if (busy.current) return;
    busy.current = true;
    setNote('');
    // The number is the one that was on the screen when it was pressed.
    deck.post('/api/douglas/approval', { id: p.id, yes, via: 'phone' }).then((a) => {
      busy.current = false;
      if (!a || !a.ok) setNote((a && (a.message || a.text)) || 'node1 did not answer');
    });
  };
  return (
    <View style={st.apv}>
      <T size={11} ls={0.14} tone="acc">{waitingHead(p)}</T>
      <T size={11} tone="mut">{left(p.until, Date.now() / 1000)}</T>
      <ScrollView style={{ maxHeight: 280 }} nestedScrollEnabled><T selectable>{p.text || ''}</T></ScrollView>
      <View style={ui.acts}>
        <Hold onFire={() => answer(true)} aria="hold to approve" tone="acc"
              style={{ flex: 1, minHeight: 44, borderColor: C.acc }}>
          <T size={12} ls={0.12} tone="acc">HOLD TO APPROVE</T>
        </Hold>
        <Btn label="REFUSE" onPress={() => answer(false)} style={{ flex: 1 }} />
      </View>
      {note ? <T size={12} tone="bad">{note}</T> : null}
    </View>
  );
}

/* ── the job in his workshop, while there is one and for an hour after ──── */
function Job({ job }: { job: any }) {
  const [open, setOpen] = useState(false);
  if (!job) return null;
  const rows = job.report ? jobRows(job.report) : [];
  const amber = !!job.running || jobShort(job);
  const stateTone = (s?: string): Tone => (s === 'done' ? 'ok' : s === 'outstanding' || s === 'blocked' ? 'warn' : 'mut');
  return (
    <Pressable onPress={rows.length ? () => setOpen(!open) : undefined}
               style={[st.job, amber && { borderLeftColor: C.am }]}>
      <T size={11} ls={0.12} tone={amber ? 'warn' : 'mut'}>
        {jobLine(job, Date.now() / 1000) + (rows.length ? (open ? ' · tap to close' : ' · tap for more') : '')}
      </T>
      <T size={13} numberOfLines={3}>{job.what || ''}</T>
      {open && rows.length ? (
        <View style={{ gap: 4, paddingTop: 6, borderTopWidth: 1, borderTopColor: C.ln }}>
          {rows.map((r, i) => r[0] === 'k'
            ? <T key={i} size={11} ls={0.14} tone="mut" style={{ marginTop: 6 }}>{r[1]}</T>
            : r[0] === 'v' ? <T key={i} size={13} selectable>{r[1]}</T>
            : (
              <View key={i} style={{ flexDirection: 'row', gap: 10 }}>
                <T size={11} ls={0.1} tone={stateTone(r[2])} style={{ width: 92 }}>{String(r[2]).toUpperCase()}</T>
                <T size={13} style={{ flex: 1 }} selectable>{r[1]}</T>
              </View>
            ))}
        </View>
      ) : null}
    </Pressable>
  );
}

/* ── hold to talk ───────────────────────────────────────────────────────── */
// The press is kept until the thumb lifts: the page does not scroll from
// under it. Lifting sends; a press the phone takes away (a call, a
// notification pulled down) is called off, not sent.
function Talk({ title, sub, where, state, big, disabled, onIn, onOut }:
  { title: string; sub: string; where: string; state: string; big?: boolean; disabled?: boolean;
    onIn: () => void; onOut: (sent: boolean) => void }) {
  const [down, setDown] = useState(false);
  const fn = useRef({ onIn, onOut, disabled });
  fn.current = { onIn, onOut, disabled };
  const pan = useRef(PanResponder.create({
    onStartShouldSetPanResponder: () => !fn.current.disabled,
    onPanResponderTerminationRequest: () => false,
    onPanResponderGrant: () => { setDown(true); fn.current.onIn(); },
    onPanResponderRelease: () => { setDown(false); fn.current.onOut(true); },
    onPanResponderTerminate: () => { setDown(false); fn.current.onOut(false); },
  })).current;
  // Its colour is the observed state, never the press.
  const tone: Tone = state === 'idle' ? 'ok' : state === 'listening' ? 'acc' : state === 'thinking' ? 'warn'
    : state === 'speaking' ? 'cy' : 'mut';
  const edge = state === 'listening' ? C.acc : state === 'thinking' ? C.am : state === 'speaking' ? C.cy
    : down ? C.acc : C.ln;
  return (
    <View {...pan.panHandlers} accessible accessibilityRole="button" accessibilityLabel={title + ', ' + where}
          accessibilityState={{ disabled: !!disabled }}
          style={[st.ptt, { minHeight: big ? 112 : 64, borderColor: edge },
                  state === 'listening' && { backgroundColor: '#22142a' }, disabled && { opacity: 0.35 }]}>
      <T size={big ? 20 : 13} ls={big ? 0.2 : 0.14}>{title}</T>
      <T size={12} ls={0.12} style={{ color: toneColor(tone) }}>{sub}</T>
      <T size={10} ls={0.1} tone="mut" style={{ textAlign: 'center' }}>{where}</T>
    </View>
  );
}

/* ── the card ───────────────────────────────────────────────────────────── */

export function DouglasCard({ dgl }: { dgl: Douglas }) {
  const { state } = useDeck();
  const head = douglasHead(state && state.douglas);
  const d = dgl.d || {};
  const agent = !!d.agent;
  const voice = agent && d.voice ? d.voice : null;
  const chats = voice ? voice.chats || null : null;
  const v = dgl.view;
  const g = (state && state.douglas) || {};

  const camera = () => {
    ActionSheetIOS.showActionSheetWithOptions(
      { options: ['Take a photo', 'Choose from the library', 'Cancel'], cancelButtonIndex: 2,
        title: 'Show Douglas a photo', userInterfaceStyle: 'dark' },
      (i) => {
        if (i > 1) return;
        pick(i === 0 ? 'camera' : 'library', dgl.edge).then(
          (got) => { if (typeof got === 'string') dgl.say(got, 8000); else if (got) dgl.setShot(got); },
          () => dgl.say('could not open that photo'));
      });
  };

  return (
    <Card>
      <Head title="DOUGLAS" pill={head.pill} tone={head.cls} />
      <T size={13} tone={head.subCls || 'mut'}>{head.sub}</T>
      {agent ? (
        <View style={st.meters}>
          {meters(d).map((m) => (
            <View key={m[0]} style={st.m}>
              <T size={10} ls={0.14} tone="mut">{m[0]}</T>
              <T size={20} tone={m[1] == null ? 'mut' : undefined} numberOfLines={1}>
                {m[1] == null ? '—' : m[1]}<T size={11} tone="mut">{' ' + m[2]}</T>
              </T>
            </View>
          ))}
        </View>
      ) : null}
      <Approval pending={voice ? voice.pending : null} />
      <Job job={voice ? voice.job : null} />
      <Log recent={agent ? d.transcript || [] : null} chat={chats ? chats.id : ''} />
      {dgl.shot ? (
        <View style={st.shot}>
          <Image source={{ uri: dgl.shot.uri }} style={{ width: 56, height: 56 }} contentFit="cover"
                 accessibilityLabel="the photo about to be sent" />
          <T size={11} ls={0.14} tone="cy" style={{ flex: 1 }}>PHOTO ATTACHED</T>
          {!v.locked ? (
            <Btn label="×" aria="remove the photo" onPress={() => dgl.setShot(null)} style={{ width: 44 }} />
          ) : null}
        </View>
      ) : null}
      <View style={{ flexDirection: 'row', gap: 8 }}>
        {dgl.eyes ? (
          <Btn label="PHOTO" aria="show Douglas a photo" tone="cy" onPress={camera} disabled={v.locked}
               style={{ backgroundColor: C.well }} />
        ) : null}
        <TextInput value={dgl.text} onChangeText={dgl.setText} editable={!v.locked} maxLength={2000}
                   placeholder={v.placeholder} placeholderTextColor={C.mut} returnKeyType="send"
                   onSubmitEditing={dgl.send} autoCorrect keyboardAppearance="dark" submitBehavior="submit"
                   style={[ui.input, { flex: 1, minWidth: 0 }, v.locked && { opacity: 0.4 }]} />
        <Btn label={v.go} tone="acc" onPress={dgl.send} style={{ backgroundColor: C.well, paddingHorizontal: 16 }} />
      </View>
      {/* Why a question is waiting, how a send is going, or what went wrong:
          directly under the box, where it is still on screen with the
          keyboard up. The line keeps its height when empty, so the talk
          button below does not jump under a thumb. */}
      <View style={{ minHeight: 17 }} accessibilityLiveRegion="polite">
        <T size={12} tone={dgl.bad ? 'bad' : 'mut'}>{v.note}</T>
      </View>
      <Talk big title="HOLD TO TALK" sub={v.ptt.sub} state={v.ptt.state}
            where="this phone's microphone and speaker" onIn={dgl.pttIn} onOut={dgl.pttOut} />
      <Talk title="HOLD · DOUGVOICE LISTENS" sub={v.house.sub} state={v.house.state} disabled={v.house.disabled}
            where="in the house, not this phone"
            onIn={dgl.houseIn} onOut={(sent) => dgl.houseOut(sent ? 'stop' : 'cancel')} />
      {g.up && g.chat ? (
        <Btn label="OPEN CHAT" tone="cy" style={{ backgroundColor: C.well, minHeight: 52 }}
             onPress={() => { Linking.openURL('http://' + (g.host || 'douglas') + ':8080/').catch(() => {}); }} />
      ) : null}
    </Card>
  );
}

const st = StyleSheet.create({
  meters: { flexDirection: 'row', flexWrap: 'wrap', gap: 8 },
  m: { borderWidth: 1, borderColor: C.ln, padding: 8, flexBasis: '30%', flexGrow: 1, minWidth: 0 },
  turn: { maxWidth: '88%', gap: 2 },
  txt: { paddingHorizontal: 10, paddingVertical: 8, backgroundColor: C.well, borderWidth: 1, borderColor: C.ln },
  pic: { alignSelf: 'stretch', marginTop: 4, backgroundColor: C.well, borderWidth: 1, borderColor: C.ln },
  apv: { gap: 6, padding: 12, borderWidth: 1, borderColor: C.acc, backgroundColor: '#17141d' },
  job: { gap: 4, paddingVertical: 10, paddingHorizontal: 12, borderLeftWidth: 2, borderLeftColor: C.ln },
  shot: { flexDirection: 'row', alignItems: 'center', gap: 10, padding: 6, borderWidth: 1, borderColor: C.cy,
          backgroundColor: C.well },
  ptt: { borderWidth: 2, backgroundColor: C.well, alignItems: 'center', justifyContent: 'center', gap: 6,
         paddingVertical: 8, paddingHorizontal: 8 },
});
