/* Gate.tsx — the first-run screen, and the one shown again when deck-api
   refuses this phone.

   `sudo ./pi/mobile-link.sh` on node1 prints https://…/m?token=… and a QR
   code. The whole link, pasted or scanned, gives the app both things it
   needs: where node1 is, and the token. Both go in the Keychain. */

import { useState } from 'react';
import { KeyboardAvoidingView, ScrollView, StyleSheet, TextInput, View } from 'react-native';
import { CameraView, useCameraPermissions } from 'expo-camera';

import { Deck } from './deck';
import { parseLink } from './lib/link';
import type { Link } from './store';
import { Btn, C, s as ui, T } from './ui';

export function Gate({ known, refused, onSave }: { known: Link | null; refused: boolean; onSave: (l: Link) => void }) {
  const [text, setText] = useState('');
  const [addr, setAddr] = useState(known ? known.base : '');
  const [note, setNote] = useState(refused ? 'node1 did not accept that. Paste the link again, or scan it.' : '');
  const [scanning, setScanning] = useState(false);
  const [busy, setBusy] = useState(false);
  const [permission, askPermission] = useCameraPermissions();

  const take = (given: string) => {
    const got = parseLink(given, addr);
    if (got.base) setAddr(got.base);
    if (got.why) { setNote(got.why); return; }
    setBusy(true);
    setNote('Asking node1…');
    // Tried before it is kept: a token node1 refuses is not worth keeping.
    let no = false;
    new Deck(got.base, got.token, () => { no = true; }).get('/api/state').then((state) => {
      setBusy(false);
      if (no) { setNote('node1 did not accept that token. Run mobile-link.sh again and use the link it prints.'); return; }
      // No answer is not a verdict on the link: it is kept, and the app
      // goes on trying. Tailscale being off looks exactly like this.
      if (!state) setNote('');
      setText('');
      onSave({ base: got.base, token: got.token });
    });
  };

  const scan = async () => {
    const may = permission && permission.granted ? permission : await askPermission();
    if (!may.granted) { setNote('This app is not allowed the camera. Allow it in Settings, under Doug, or paste the link.'); return; }
    setNote('');
    setScanning(true);
  };

  return (
    <KeyboardAvoidingView behavior="padding" style={{ flex: 1 }}>
      <ScrollView contentContainerStyle={st.gate} keyboardShouldPersistTaps="handled">
        <T size={18} ls={0.1}>Needs the deck token</T>
        <T tone="mut">
          This phone reaches node1 over the tailnet, and deck-api wants its <T>DECK_TOKEN</T>. Run{' '}
          <T>sudo ./pi/mobile-link.sh</T> on node1, then scan the QR code it prints, or paste the link.
        </T>
        {scanning ? (
          <View style={{ gap: 8 }}>
            <CameraView style={st.cam} facing="back" barcodeScannerSettings={{ barcodeTypes: ['qr'] }}
                        onBarcodeScanned={(e) => { setScanning(false); take(e.data); }} />
            <Btn label="STOP SCANNING" onPress={() => setScanning(false)} />
          </View>
        ) : (
          <Btn label="SCAN THE QR CODE" tone="cy" onPress={scan} disabled={busy} style={{ minHeight: 52 }} />
        )}
        <TextInput value={text} onChangeText={setText} secureTextEntry autoCapitalize="none" autoCorrect={false}
                   textContentType="oneTimeCode" autoComplete="off"
                   placeholder="the whole link, or DECK_TOKEN" placeholderTextColor={C.mut} keyboardAppearance="dark"
                   returnKeyType="done" onSubmitEditing={() => take(text)} style={ui.input} />
        <TextInput value={addr} onChangeText={setAddr} autoCapitalize="none" autoCorrect={false} keyboardType="url"
                   placeholder="https://<node1>.<tailnet>.ts.net" placeholderTextColor={C.mut} keyboardAppearance="dark"
                   style={ui.input} />
        <T size={12} tone="mut">The address is filled in from the link. It is only typed when the token is pasted alone.</T>
        <Btn label="SAVE" tone="acc" onPress={() => take(text)} disabled={busy} style={{ minHeight: 52 }} />
        {note ? <T size={12} tone={busy ? 'mut' : 'bad'} accessibilityRole="alert">{note}</T> : null}
        <T size={12} tone="mut">
          The link is a credential. It is kept in this phone's Keychain and sent to node1 in a header, never in an
          address.
        </T>
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

const st = StyleSheet.create({
  gate: { padding: 16, paddingTop: 24, gap: 12 },
  cam: { width: '100%', aspectRatio: 1, borderWidth: 1, borderColor: C.cy },
});
