/* Notices.tsx — what the watch on Douglas has raised: a machine gone, a
   service down, a disk nearly full. Above Douglas, because it is what one
   would want to know first. Rules, not a model; nothing here acts.

   What can be done is what an owner does with a notice: see it, say it has
   been seen (it is then not mentioned or said aloud again; it stays until
   it is over), and say where each severity is to go. */

import { useState } from 'react';
import { StyleSheet, View } from 'react-native';

import { useDeck, usePoll } from '../deck';
import { hourStep, meta, NOTICE_ROUTE, SEV_TONE, SEVERITIES, summary } from '../lib/notices';
import { Btn, C, Card, Chip, Empty, Head, Note, T, Tone } from '../ui';

export function NoticesCard() {
  const { deck } = useDeck();
  // Asked for more often than the memory: a notice is the one thing here that is news.
  const [got, refresh] = usePoll(NOTICE_ROUTE + '?op=notices&limit=30', 15000);
  const [prefsOpen, setPrefsOpen] = useState(false);
  const [note, setNote] = useState('');
  const sum = summary(got);
  const ok = got && got.ok;
  const now = Date.now() / 1000;

  const change = (body: object) => {
    deck.post(NOTICE_ROUTE, body).then((a) => {
      if (!a || !a.ok) setNote((a && a.text) || 'node1 did not answer');
      else { setNote(''); refresh(); }
    });
  };

  const row = (n: any, first: boolean) => {
    const tone = (n.cleared ? '' : SEV_TONE[n.severity] || '') as Tone;
    const edge = tone === 'bad' ? C.rd : tone === 'warn' ? C.am : C.ln;
    return (
      <View key={(n.cleared ? 'o' : 'n') + n.id}
            style={[st.ntc, { borderLeftColor: edge }, first && { borderTopWidth: 0 }, n.cleared && { opacity: 0.55 }]}>
        <View style={{ flex: 1, minWidth: 0, gap: 3 }}>
          <View style={{ flexDirection: 'row', gap: 8, alignItems: 'baseline' }}>
            <T size={10} ls={0.14} tone={tone || 'mut'}>{String(n.severity || '').toUpperCase()}</T>
            <T size={12} tone="mut" numberOfLines={1} style={{ flex: 1 }}>{n.subject || ''}</T>
          </View>
          <T>{n.text || ''}</T>
          <T size={11} tone="mut">{meta(n, now)}</T>
        </View>
        {!n.cleared && !n.acked ? (
          <Btn label="SEEN" tone="cy" small onPress={() => change({ op: 'notice_ack', id: n.id })}
               style={{ minWidth: 56, minHeight: 36, alignSelf: 'flex-start', borderColor: C.ln }} />
        ) : null}
      </View>
    );
  };

  const p = ok ? got.prefs : null;
  const open: any[] = ok ? got.open || [] : [];
  const over: any[] = ok ? (got.over || []).slice(0, 5) : [];
  return (
    <Card>
      <Head title="NOTICES" pill={sum.text} tone={sum.cls} />
      {got === undefined ? null : !ok ? (
        // Not read is not none: what was asked for could not be shown.
        <Empty text={(got && got.text) || 'node1 did not answer'} />
      ) : (
        <View>
          {open.map((n, i) => row(n, i === 0))}
          {!open.length ? (
            <Empty text={'Nothing is raised. The watch on Douglas looks every twenty seconds: at him, and at what ' +
                         'Mission Control sees of the rest.'} />
          ) : null}
          {over.map((n) => row(n, false))}
        </View>
      )}
      {p ? (
        <View style={{ gap: 8, alignItems: 'flex-start' }}>
          <Chip label="WHERE THEY GO" on={prefsOpen} onPress={() => setPrefsOpen(!prefsOpen)} />
          {prefsOpen ? (
            <>
              <T size={11} tone="mut">
                On these screens: always. Whether Douglas mentions one with his next answer, and whether it is said
                aloud in the room, is yours to say for each kind.
              </T>
              {SEVERITIES.map((sev) => (
                <View key={sev} style={st.pref}>
                  <T size={10} ls={0.14} tone={(SEV_TONE[sev] || 'mut') as Tone} style={{ width: '100%' }}>
                    {sev.toUpperCase()}
                  </T>
                  {([['tell', 'MENTIONED'], ['aloud', 'ALOUD']] as const).map(([key, word]) => {
                    const on = !!(p[sev] && p[sev][key]);
                    return (
                      <Chip key={key} label={word + (on ? ' · ON' : ' · OFF')} on={on}
                            onPress={() => change({ op: 'notice_prefs', severity: sev, [key]: !on })} />
                    );
                  })}
                </View>
              ))}
              {/* The hours nothing is said aloud in, an hour at a touch. */}
              <View style={st.pref}>
                <T size={10} ls={0.14} tone="mut" style={{ width: '100%' }}>NOTHING ALOUD</T>
                {([['quiet_from', 'FROM'], ['quiet_to', 'TO']] as const).map(([key, word]) => (
                  <View key={key} style={{ flexDirection: 'row', alignItems: 'center', gap: 6 }}>
                    <Chip label="−" aria={'an hour earlier, ' + word.toLowerCase()}
                          onPress={() => change({ op: 'notice_prefs', [key]: hourStep(p[key], -1) })} />
                    <T size={12} ls={0.08} style={{ minWidth: 82, textAlign: 'center' }}>{word + ' ' + p[key] + ':00'}</T>
                    <Chip label="+" aria={'an hour later, ' + word.toLowerCase()}
                          onPress={() => change({ op: 'notice_prefs', [key]: hourStep(p[key], 1) })} />
                  </View>
                ))}
              </View>
            </>
          ) : null}
        </View>
      ) : null}
      <Note text={note} bad />
    </Card>
  );
}

const st = StyleSheet.create({
  ntc: { flexDirection: 'row', gap: 10, paddingVertical: 10, paddingLeft: 10, borderTopWidth: 1, borderTopColor: C.ln,
         borderLeftWidth: 2 },
  pref: { flexDirection: 'row', flexWrap: 'wrap', alignItems: 'center', gap: 6 },
});
