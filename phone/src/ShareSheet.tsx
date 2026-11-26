/* ShareSheet.tsx — "Ask Douglas" from another app's share sheet.

   Sharing opens the app on this sheet, with what was shared and a note to
   go with it, already filled in with a question. SEND puts it through the
   same queue as a typed question: it waits out a lost link, and its answer
   arrives in the conversation. */

import { useEffect, useMemo, useState } from 'react';
import { KeyboardAvoidingView, Modal, ScrollView, StyleSheet, TextInput, View } from 'react-native';
import * as Crypto from 'expo-crypto';
import { Image } from 'expo-image';

import type { Douglas } from './douglas';
import { newId } from './lib/ask';
import { SHARE_PROMPT, sharedOf, shareFits, shareText } from './lib/share';
import { shrink } from './photo';
import { Btn, C, s as ui, T } from './ui';

export function ShareSheet({ intent, dgl, onDone }: { intent: any; dgl: Douglas; onDone: (sent: boolean) => void }) {
  const shared = useMemo(() => sharedOf(intent), [intent]);
  const [note, setNote] = useState(SHARE_PROMPT);
  const [why, setWhy] = useState('');
  const [busy, setBusy] = useState(false);
  useEffect(() => { setNote(SHARE_PROMPT); setWhy(''); setBusy(false); }, [intent]);

  const send = async () => {
    if (shared.kind === 'none' || busy) return;
    setBusy(true);
    setWhy('');
    const body: Record<string, unknown> = { text: shareText(shared, note), via: 'phone',
                                           id: newId(Crypto.getRandomBytes(12)) };
    try {
      if (shared.kind === 'photo') body.image = (await shrink(shared.path, dgl.edge)).b64;
    } catch {
      setBusy(false);
      setWhy('could not open that photo');
      return;
    }
    const no = dgl.queue(body, shared.kind === 'photo' ? 'photo' : 'text', true);
    setBusy(false);
    if (no) setWhy(no); else onDone(true);
  };

  return (
    <Modal visible animationType="slide" presentationStyle="pageSheet" onRequestClose={() => onDone(false)}>
      <KeyboardAvoidingView behavior="padding" style={{ flex: 1, backgroundColor: C.bg }}>
        <ScrollView contentContainerStyle={st.sheet} keyboardShouldPersistTaps="handled">
          <T size={12} ls={0.18} tone="mut">ASK DOUGLAS</T>
          {shared.kind === 'none' ? (
            <T tone="warn">{shared.label}</T>
          ) : (
            <View style={st.what}>
              <T size={11} ls={0.14} tone="cy">{shared.label}</T>
              {shared.kind === 'photo' ? (
                <Image source={{ uri: shared.path }} style={{ width: '100%', height: 200 }} contentFit="contain"
                       accessibilityLabel="the photo that was shared" />
              ) : (
                <T size={13} numberOfLines={8} selectable>{shared.kind === 'link' ? shared.url : shared.text}</T>
              )}
              {shared.kind === 'link' ? <T size={11} tone="mut">Douglas opens the link himself and reads the page.</T> : null}
            </View>
          )}
          {shared.kind !== 'none' ? (
            <>
              <TextInput value={note} onChangeText={setNote} multiline maxLength={1000} editable={!busy}
                         selectTextOnFocus keyboardAppearance="dark" placeholder="A note to go with it…"
                         placeholderTextColor={C.mut} style={[ui.input, { minHeight: 88 }]} />
              {!shareFits(shared, note) ? (
                <T size={12} tone="warn">That is more than one question holds: the end of it will be cut off.</T>
              ) : null}
              {shared.kind === 'photo' && !dgl.eyes ? (
                <T size={12} tone="warn">Douglas has not said he can see just now. It is sent anyway; he will say so if he cannot.</T>
              ) : null}
            </>
          ) : null}
          {why ? <T size={12} tone="bad" accessibilityRole="alert">{why}</T> : null}
          <View style={{ flexDirection: 'row', gap: 8 }}>
            {shared.kind !== 'none' ? (
              <Btn label={busy ? 'SENDING…' : 'SEND'} tone="acc" disabled={busy} onPress={send}
                   style={{ flex: 1, minHeight: 52 }} />
            ) : null}
            <Btn label={shared.kind === 'none' ? 'CLOSE' : 'CANCEL'} onPress={() => onDone(false)}
                 style={{ flex: 1, minHeight: 52 }} />
          </View>
          <T size={12} tone="mut">
            It goes through the same queue as a typed question: if node1 is out of reach it waits and goes by itself,
            and his answer arrives in the conversation.
          </T>
        </ScrollView>
      </KeyboardAvoidingView>
    </Modal>
  );
}

const st = StyleSheet.create({
  sheet: { padding: 16, paddingTop: 24, gap: 12 },
  what: { gap: 8, padding: 10, borderWidth: 1, borderColor: C.cy, backgroundColor: C.well },
});
