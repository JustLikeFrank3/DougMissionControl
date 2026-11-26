/* App.tsx — the deck on a phone, as an app.

   The same deck-api, the same live frame and the same routes as the phone
   page (pi/deck-ui/m), in one column for a thumb. Nothing here is a second
   source of truth: every value is the panel's value, every command the
   panel's command. The rules of the deck hold here as there: everything
   shown is observed, every command that starts or stops something is a
   hold, and a missing reading is a dash, never a zero. */

import { useEffect, useMemo, useState } from 'react';
import { KeyboardAvoidingView, Modal, Pressable, ScrollView, StyleSheet, View } from 'react-native';
import * as Notifications from 'expo-notifications';
import { useShareIntent } from 'expo-share-intent';
import { StatusBar } from 'expo-status-bar';
import { SafeAreaProvider, useSafeAreaInsets } from 'react-native-safe-area-context';

import { CamerasCard } from './src/cards/Cameras';
import { DouglasCard } from './src/cards/Douglas';
import { MemoryCard } from './src/cards/Memory';
import { NoticesCard } from './src/cards/Notices';
import { ChatsCard, SwitchesCard } from './src/cards/Owner';
import { PushCard } from './src/cards/Push';
import { EventsCard, FleetCard, NowPlayingCard } from './src/cards/Small';
import { WorkstationCard } from './src/cards/Workstation';
import { Conn, Deck, DeckProvider, useActive, useConnection, useDeck } from './src/deck';
import { useDouglas } from './src/douglas';
import { Gate } from './src/Gate';
import { fmtClock } from './src/lib/format';
import { badge, menuMark, pageForPush, PageId, pageOf, PAGES, titleOf } from './src/lib/pages';
import { ShareSheet } from './src/ShareSheet';
import { forgetLink, getPrefs, Link, loadLink, loadPrefs, saveLink, setPrefs } from './src/store';
import { Btn, C, Hold, Pill, T, Tone, toneColor } from './src/ui';

function Clock() {
  const [now, setNow] = useState(fmtClock());
  useEffect(() => {
    const id = setInterval(() => setNow(fmtClock()), 1000);
    return () => clearInterval(id);
  }, []);
  return <T size={14} tone="mut">{now}</T>;
}

/** The top bar: the menu button, which page this is, whether the deck is
    live, and the clock. The mark on the button says something in the menu
    wants looking at. */
function Top({ conn, title, mark, onMenu }: { conn: Conn; title?: string; mark?: Tone; onMenu?: () => void }) {
  const inset = useSafeAreaInsets();
  return (
    <View style={[st.top, { paddingTop: inset.top + 8 }]}>
      {onMenu ? (
        <Pressable onPress={onMenu} hitSlop={10} accessibilityRole="button" accessibilityLabel="menu: the deck's pages"
                   style={({ pressed }) => [st.burger, pressed && { opacity: 0.6 }]}>
          <View style={st.bar} /><View style={st.bar} /><View style={st.bar} />
          {mark ? <View style={[st.mark, { backgroundColor: toneColor(mark) }]} /> : null}
        </Pressable>
      ) : null}
      <View style={{ flex: 1, minWidth: 0, gap: 2 }}>
        <T ls={0.08} style={{ fontWeight: '600' }} numberOfLines={1}>{title || 'Doug Mission Control'}</T>
        <View style={{ flexDirection: 'row', justifyContent: 'space-between' }}>
          <T size={12} tone={conn.cls === 'live' ? 'ok' : conn.cls === 'dead' ? 'bad' : 'mut'}>{conn.text}</T>
          <Clock />
        </View>
      </View>
    </View>
  );
}

/** The menu: every page of the deck, with what there is to say of it. */
function Menu({ at, state, d, onPick, onClose }:
  { at: PageId; state: any; d: any; onPick: (id: PageId) => void; onClose: () => void }) {
  const inset = useSafeAreaInsets();
  return (
    <Modal visible transparent animationType="fade" onRequestClose={onClose} statusBarTranslucent>
      <Pressable style={st.shade} onPress={onClose} accessibilityLabel="close the menu">
        <Pressable style={[st.menu, { paddingTop: inset.top + 8 }]} onPress={() => {}}>
          <View style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', paddingBottom: 6 }}>
            <T ls={0.08} style={{ fontWeight: '600' }}>Doug Mission Control</T>
            <Btn label="CLOSE" small onPress={onClose} style={{ minHeight: 36 }} />
          </View>
          {PAGES.map((p) => {
            const b = badge(p.id, state, d);
            return (
              <Pressable key={p.id} onPress={() => onPick(p.id)} accessibilityRole="button"
                         accessibilityState={{ selected: p.id === at }}
                         style={({ pressed }) => [st.item, p.id === at && { borderLeftColor: C.acc }, pressed && { opacity: 0.6 }]}>
                <View style={{ flex: 1, minWidth: 0 }}>
                  <T size={14} ls={0.14} tone={p.id === at ? 'acc' : undefined}>{p.title}</T>
                  <T size={11} tone="mut" numberOfLines={1}>{p.says}</T>
                </View>
                {b ? <Pill text={b.text} tone={b.tone} /> : null}
              </Pressable>
            );
          })}
        </Pressable>
      </Pressable>
    </Modal>
  );
}

function Main({ share, conn, onForget }: { share: ReturnType<typeof useShareIntent>; conn: Conn; onForget: () => void }) {
  const { state } = useDeck();
  const dgl = useDouglas();
  const inset = useSafeAreaInsets();
  const [page, setPage] = useState<PageId>(pageOf(getPrefs().page));
  const [menu, setMenu] = useState(false);
  const d = dgl.d || {};
  const voice = d.agent && d.voice ? d.voice : null;
  const go = (id: PageId) => {
    setMenu(false);
    setPage(id);
    setPrefs({ page: id });
  };
  // A notification that was tapped leads to the page it is about.
  useEffect(() => {
    const sub = Notifications.addNotificationResponseReceivedListener((r) => {
      go(pageForPush((r.notification.request.content.data || {}).kind));
    });
    return () => sub.remove();
  }, []);
  return (
    <KeyboardAvoidingView behavior="padding" style={{ flex: 1 }}>
      <Top conn={conn} title={titleOf(page)} mark={menuMark(state, dgl.d)} onMenu={() => setMenu(true)} />
      {/* One page at a time, each scrolling on its own. Only the page in
          front asks node1 for its own readings. */}
      <ScrollView key={page} contentContainerStyle={[st.main, { paddingBottom: inset.bottom + 24 }]}
                  keyboardShouldPersistTaps="handled" keyboardDismissMode="interactive">
        {page === 'workstation' ? <><WorkstationCard /><NowPlayingCard /></> : null}
        {page === 'douglas' ? <DouglasCard dgl={dgl} /> : null}
        {page === 'notices' ? <NoticesCard /> : null}
        {page === 'memory' ? <MemoryCard alone /> : null}
        {page === 'chats' ? <ChatsCard alone chats={voice ? voice.chats || null : null} changed={dgl.refresh} /> : null}
        {page === 'tools' ? <SwitchesCard alone tools={voice ? voice.tools || null : null} changed={dgl.refresh} /> : null}
        {page === 'cameras' ? <CamerasCard alone /> : null}
        {page === 'fleet' ? <><FleetCard /><EventsCard /></> : null}
        {page === 'phone' ? (
          <>
            <PushCard />
            <T size={11} tone="mut" style={{ textAlign: 'center' }}>
              {'node1' + (state && state.surface && state.surface.active
                ? ' · panel showing ' + String(state.surface.active).toUpperCase() : '')}
            </T>
            <Hold onFire={onForget} aria="hold to forget this phone's link to node1" tone="mut"
                  style={{ minHeight: 44, backgroundColor: 'transparent' }}>
              <T size={11} ls={0.14} tone="mut">HOLD TO FORGET THIS PHONE'S LINK</T>
            </Hold>
          </>
        ) : null}
      </ScrollView>
      {menu ? <Menu at={page} state={state} d={dgl.d} onPick={go} onClose={() => setMenu(false)} /> : null}
      {share.hasShareIntent ? (
        <ShareSheet intent={share.shareIntent} dgl={dgl} onDone={(sent) => {
          share.resetShareIntent();
          if (sent) go('douglas');                // to where the answer will appear
        }} />
      ) : null}
    </KeyboardAvoidingView>
  );
}

function Root() {
  // undefined while the Keychain is being read; null when it holds no link.
  const [link, setLink] = useState<Link | null | undefined>(undefined);
  const [refused, setRefused] = useState(false);
  const share = useShareIntent();
  const active = useActive();
  useEffect(() => {
    Promise.all([loadLink(), loadPrefs()]).then(([l]) => setLink(l));
  }, []);
  // A 403 from any route means the token is wrong or gone: the first-run
  // screen again, as the page shows its gate.
  const deck = useMemo(() => (link ? new Deck(link.base, link.token, () => setRefused(true)) : null), [link]);
  const { state, conn } = useConnection(refused ? null : deck, active);
  const gate = link === null || refused;
  return (
    <View style={{ flex: 1, backgroundColor: C.bg }}>
      <StatusBar style="light" />
      {link === undefined || gate || !deck ? <Top conn={gate ? { text: 'no token', cls: 'dead' } : conn} /> : null}
      {link === undefined ? null : gate || !deck ? (
        <Gate known={link || null} refused={refused}
              onSave={(l) => { saveLink(l); setRefused(false); setLink(l); }} />
      ) : (
        <DeckProvider value={{ deck, state, active, conn }}>
          <Main key={link.base + '|' + link.token} share={share} conn={conn}
                onForget={() => { forgetLink(); setLink(null); }} />
        </DeckProvider>
      )}
    </View>
  );
}

export default function App() {
  return <SafeAreaProvider><Root /></SafeAreaProvider>;
}

const st = StyleSheet.create({
  top: { flexDirection: 'row', alignItems: 'center', gap: 14, paddingHorizontal: 16, paddingBottom: 10,
         backgroundColor: C.bg, borderBottomWidth: 1, borderBottomColor: C.ln },
  burger: { width: 44, height: 44, borderWidth: 1, borderColor: C.ln, backgroundColor: C.well, alignItems: 'center',
            justifyContent: 'center', gap: 5 },
  bar: { width: 20, height: 2, backgroundColor: C.fg },
  mark: { position: 'absolute', top: -4, right: -4, width: 11, height: 11, borderRadius: 6 },
  shade: { flex: 1, backgroundColor: 'rgba(0, 0, 0, 0.6)' },
  menu: { backgroundColor: C.bg, borderBottomWidth: 1, borderBottomColor: C.ln, paddingHorizontal: 16, paddingBottom: 12 },
  item: { flexDirection: 'row', alignItems: 'center', gap: 10, minHeight: 52, paddingVertical: 8, paddingLeft: 12,
          borderTopWidth: 1, borderTopColor: C.ln, borderLeftWidth: 2, borderLeftColor: 'transparent' },
  main: { padding: 12, paddingHorizontal: 16, gap: 12 },
});
