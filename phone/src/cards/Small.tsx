/* Small.tsx — the cards that only show: now playing, the fleet, events. */

import { useMemo, useState } from 'react';
import { StyleSheet, View } from 'react-native';
import { Image } from 'expo-image';

import { useDeck, usePoll } from '../deck';
import { fleetRows } from '../lib/fleet';
import { fmtClock, fmtTime } from '../lib/format';
import { Btn, C, Card, H2, T } from '../ui';

/** Now playing, from whichever OS is up. */
export function NowPlayingCard() {
  const { deck } = useDeck();
  const [got, refresh] = usePoll('/api/media', 3000);
  const [artFailed, setArtFailed] = useState('');
  const m = got || {};
  const art = useMemo(() => (m.active && m.art_id ? deck.image('/api/media/art?v=' + encodeURIComponent(m.art_id)) : null),
                      [deck, m.active, m.art_id]);
  const can = m.can || {};
  const command = (action: string) => {
    deck.post('/api/media/command', { action }).then(() => setTimeout(refresh, 350));
  };
  return (
    <Card>
      <View style={{ flexDirection: 'row', alignItems: 'center', gap: 12 }}>
        {art && artFailed !== m.art_id ? (
          <Image source={art} style={{ width: 64, height: 64 }} contentFit="cover" cachePolicy="memory"
                 onError={() => setArtFailed(m.art_id)} />
        ) : null}
        <View style={{ flex: 1, minWidth: 0 }}>
          <T size={16} tone={m.active ? undefined : 'mut'} numberOfLines={1}>
            {got === undefined ? 'no source' : m.active ? m.title || '(untitled)' : 'nothing playing'}
          </T>
          {m.active && m.artist ? <T size={13} tone="cy" numberOfLines={1}>{m.artist}</T> : null}
          {m.active ? (
            <T size={12} tone="mut" style={{ marginTop: 2 }}>
              {(m.playing ? '▶ ' : '❚❚ ') + fmtTime(m.position_s) + (m.duration_s ? ' / ' + fmtTime(m.duration_s) : '')}
            </T>
          ) : null}
        </View>
      </View>
      {m.active ? (
        <View style={{ flexDirection: 'row', gap: 8 }}>
          <Btn label="⏮" aria="previous" disabled={!can.prev} onPress={() => command('prev')} style={st.np} />
          <Btn label="⏯" aria="play or pause" disabled={!can.play_pause} onPress={() => command('play_pause')} style={st.np} />
          <Btn label="⏭" aria="next" disabled={!can.next} onPress={() => command('next')} style={st.np} />
        </View>
      ) : null}
    </Card>
  );
}

/** Who is up, and what each machine reports about itself: the same reading
    Douglas is given. A reading that has stopped arriving is dropped, not
    left on screen. */
export function FleetCard() {
  const { state } = useDeck();
  const [fleet] = usePoll('/api/fleet', 15000);
  if (!state) return null;
  const rows = fleetRows(state, fleet, Date.now() / 1000);
  return (
    <Card>
      <H2>FLEET</H2>
      <View>
        {rows.map((r, i) => (
          <View key={r.name} style={[st.host, i === 0 && { borderTopWidth: 0 }]}>
            <View style={{ flexDirection: 'row', alignItems: 'center', gap: 10 }}>
              <View style={[st.dot, r.up && { backgroundColor: C.gr }, r.bad && { backgroundColor: C.rd }]} />
              <T ls={0.1}>{r.name}</T>
              <T size={13} tone="mut" numberOfLines={1} style={{ flex: 1, textAlign: 'right' }}>{r.says}</T>
            </View>
            {r.more.text ? <T size={11} tone={r.more.bad ? 'bad' : 'mut'} style={{ paddingLeft: 19 }}>{r.more.text}</T> : null}
          </View>
        ))}
      </View>
    </Card>
  );
}

export function EventsCard() {
  const { state } = useDeck();
  const evs: any[] = ((state && state.events) || []).slice(0, 8);
  return (
    <Card>
      <H2>EVENTS</H2>
      <View style={{ gap: 6 }}>
        {evs.map((e, i) => (
          <View key={i} style={{ flexDirection: 'row', gap: 8 }}>
            <T size={13} tone="mut">{fmtClock(e.ts || e.t)}</T>
            <T size={13} tone="cy">{e.source || ''}</T>
            <T size={13} tone={e.level === 'warn' ? 'warn' : e.level === 'error' ? 'bad' : undefined}
               style={{ flex: 1 }}>{e.text || ''}</T>
          </View>
        ))}
        {!evs.length ? <T size={13} tone="mut">nothing yet</T> : null}
      </View>
    </Card>
  );
}

const st = StyleSheet.create({
  np: { flex: 1, minHeight: 48, backgroundColor: C.well },
  host: { paddingVertical: 9, borderTopWidth: 1, borderTopColor: C.ln, gap: 2 },
  dot: { width: 9, height: 9, borderRadius: 5, backgroundColor: C.ln },
});
