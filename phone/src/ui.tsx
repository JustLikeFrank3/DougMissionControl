/* ui.tsx — the primitives every card is built from. The same palette and
   the same rule as the panel: colour means state (green up, amber busy, red
   fault) and nothing else. One column, thumb-sized targets. */

import { ReactNode, useRef, useState } from 'react';
import {
  Animated, Easing, PanResponder, Platform, Pressable, StyleProp, StyleSheet, Text, TextProps, View, ViewStyle,
} from 'react-native';
import * as Haptics from 'expo-haptics';

export const C = {
  bg: '#0b0f15', pan: '#131a24', ln: '#232e3e', fg: '#e2e9f2', mut: '#7f8da2', acc: '#d95bb8',
  cy: '#56cfe1', gr: '#4fc48a', am: '#e6a339', rd: '#e2564d', well: '#0f151e',
};
export const MONO = Platform.select({ ios: 'Menlo', default: 'monospace' });

/** Hold-to-arm duration. Long enough that a brush past a tile cannot boot a
    machine, short enough that a deliberate press does not feel broken. */
export const HOLD_MS = 1500;

export type Tone = '' | 'ok' | 'busy' | 'bad' | 'warn' | 'cy' | 'acc' | 'mut';
export function toneColor(t?: Tone): string {
  return t === 'ok' ? C.gr : t === 'busy' || t === 'warn' ? C.am : t === 'bad' ? C.rd : t === 'cy' ? C.cy
    : t === 'acc' ? C.acc : t === 'mut' ? C.mut : C.fg;
}

export function T(props: TextProps & { tone?: Tone; size?: number; ls?: number }) {
  const { tone, size, ls, style, ...rest } = props;
  return (
    <Text {...rest} style={[{ color: toneColor(tone), fontFamily: MONO, fontSize: size ?? 15, lineHeight: (size ?? 15) * 1.35,
                               letterSpacing: ls ? ls * (size ?? 15) : 0 }, style]} />
  );
}

export function Card({ children, style }: { children: ReactNode; style?: StyleProp<ViewStyle> }) {
  return <View style={[s.card, style]}>{children}</View>;
}

export function Pill({ text, tone }: { text: string; tone?: Tone }) {
  const color = tone ? toneColor(tone) : C.mut;
  return (
    <View style={[s.pill, { borderColor: color }]}>
      <T size={11} ls={0.14} style={{ color }} numberOfLines={1}>{text}</T>
    </View>
  );
}

export function H2({ children }: { children: ReactNode }) {
  return <T size={12} ls={0.18} tone="mut">{children}</T>;
}

/** A card's head: its name and a pill. With onPress it is the card's
    switch: a whole row to tap, nothing to aim at. */
export function Head({ title, pill, tone, onPress, open }:
  { title: string; pill?: string; tone?: Tone; onPress?: () => void; open?: boolean }) {
  const inner = (
    <View style={s.head}>
      <H2>{title}</H2>
      {pill != null ? <Pill text={pill} tone={tone} /> : null}
    </View>
  );
  if (!onPress) return inner;
  return (
    <Pressable onPress={onPress} accessibilityRole="button" accessibilityState={{ expanded: !!open }} hitSlop={8}>
      {inner}
    </Pressable>
  );
}

export function Note({ text, bad, style }: { text?: string | null; bad?: boolean; style?: StyleProp<ViewStyle> }) {
  if (!text) return null;
  return <View style={style}><T size={12} tone={bad ? 'bad' : 'mut'} accessibilityRole={bad ? 'alert' : undefined}>{text}</T></View>;
}

export function Empty({ text }: { text: string }) {
  return <T size={13} tone="mut" style={{ paddingVertical: 6 }}>{text}</T>;
}

/** A plain touch. For everything that can be taken back. */
export function Btn({ label, onPress, tone, style, disabled, small, aria }:
  { label: string; onPress?: () => void; tone?: Tone; style?: StyleProp<ViewStyle>; disabled?: boolean;
    small?: boolean; aria?: string }) {
  const color = tone ? toneColor(tone) : C.mut;
  return (
    <Pressable onPress={onPress} disabled={disabled} accessibilityRole="button" accessibilityLabel={aria || label}
               style={({ pressed }) => [s.btn, small && s.btnSmall, { borderColor: tone ? color : C.ln },
                                        (disabled || pressed) && { opacity: disabled ? 0.35 : 0.6 }, style]}>
      <T size={small ? 11 : 12} ls={0.12} style={{ color }} numberOfLines={1}>{label}</T>
    </Pressable>
  );
}

/**
 * Hold-to-arm, with the fill sweep as the progress indicator. Everything on
 * this deck that starts or stops something, or cannot be taken back, is a
 * hold: a brush of the thumb cannot boot a machine.
 *
 * The press is kept even if the thumb drifts: the page around it does not
 * scroll from under a hold, and letting go early is the way to call it off.
 */
export function Hold({ onFire, disabled, tone, style, children, aria }:
  { onFire: () => void; disabled?: boolean; tone?: Tone; style?: StyleProp<ViewStyle>; children: ReactNode;
    aria: string }) {
  const fill = useRef(new Animated.Value(0)).current;
  const run = useRef<Animated.CompositeAnimation | null>(null);
  const [arming, setArming] = useState(false);
  const fire = useRef(onFire);
  fire.current = onFire;
  const off = useRef(!!disabled);
  off.current = !!disabled;
  const pan = useRef(PanResponder.create({
    onStartShouldSetPanResponder: () => !off.current,
    onPanResponderTerminationRequest: () => false,
    onPanResponderGrant: () => {
      setArming(true);
      fill.setValue(0);
      run.current = Animated.timing(fill, { toValue: 1, duration: HOLD_MS, easing: Easing.linear, useNativeDriver: false });
      run.current.start(({ finished }) => {
        setArming(false);
        fill.setValue(0);
        if (finished) {
          Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success).catch(() => {});
          fire.current();
        }
      });
    },
    onPanResponderRelease: () => run.current?.stop(),
    onPanResponderTerminate: () => run.current?.stop(),
  })).current;
  const color = tone ? toneColor(tone) : C.acc;
  return (
    <View {...pan.panHandlers} accessible accessibilityRole="button" accessibilityLabel={aria}
          accessibilityState={{ disabled: !!disabled }}
          style={[s.hold, arming && { borderColor: color }, disabled && { opacity: 0.35 }, style]}>
      <Animated.View pointerEvents="none"
                     style={[s.sweep, { backgroundColor: color,
                                        width: fill.interpolate({ inputRange: [0, 1], outputRange: ['0%', '100%'] }) }]} />
      {children}
    </View>
  );
}

/** A chip: one of a row of switches. */
export function Chip({ label, on, onPress, aria }: { label: string; on?: boolean; onPress: () => void; aria?: string }) {
  return (
    <Pressable onPress={onPress} accessibilityRole="button" accessibilityLabel={aria || label}
               accessibilityState={{ selected: !!on }}
               style={({ pressed }) => [s.chip, on && { borderColor: C.cy }, pressed && { opacity: 0.6 }]}>
      <T size={11} ls={0.12} tone={on ? 'cy' : 'mut'} numberOfLines={1}>{label}</T>
    </Pressable>
  );
}

/** A row with words on the left and a control on the right. */
export function Row({ title, sub, subBad, dim, first, children, onTitle }:
  { title: string; sub?: string; subBad?: boolean; dim?: boolean; first?: boolean; children?: ReactNode;
    onTitle?: () => void }) {
  return (
    <View style={[s.row, first && { borderTopWidth: 0 }]}>
      <View style={{ flex: 1, minWidth: 0, gap: 2 }}>
        <T tone={dim ? 'mut' : undefined} onPress={onTitle}
           style={onTitle ? { textDecorationLine: 'underline', textDecorationStyle: 'dotted' } : undefined}>{title}</T>
        {sub ? <T size={11} tone={subBad ? 'bad' : 'mut'}>{sub}</T> : null}
      </View>
      {children}
    </View>
  );
}

/** The right-hand control of a row that is a hold: "OFF · HOLD". */
export function HoldSwitch({ label, aria, onFire, tone }: { label: string; aria: string; onFire: () => void; tone?: Tone }) {
  return (
    <Hold onFire={onFire} aria={aria} tone={tone || 'warn'} style={[s.sw, { borderColor: toneColor(tone || 'warn') }]}>
      <T size={11} ls={0.12} tone={tone || 'warn'}>{label}</T>
    </Hold>
  );
}

export function TapSwitch({ label, aria, onPress, tone }: { label: string; aria: string; onPress: () => void; tone?: Tone }) {
  return <Btn label={label} aria={aria} onPress={onPress} tone={tone || 'ok'} style={s.sw} small />;
}

export const s = StyleSheet.create({
  card: { backgroundColor: C.pan, borderWidth: 1, borderColor: C.ln, padding: 14, gap: 10 },
  head: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', gap: 10 },
  pill: { borderWidth: 1, paddingHorizontal: 7, paddingVertical: 1 },
  btn: { minHeight: 44, paddingHorizontal: 10, backgroundColor: C.pan, borderWidth: 1, alignItems: 'center',
         justifyContent: 'center' },
  btnSmall: { minHeight: 40, backgroundColor: C.well },
  hold: { overflow: 'hidden', borderWidth: 1, borderColor: C.ln, backgroundColor: C.well, alignItems: 'center',
          justifyContent: 'center' },
  sweep: { position: 'absolute', left: 0, top: 0, bottom: 0, opacity: 0.28 },
  chip: { minHeight: 36, paddingHorizontal: 10, backgroundColor: C.well, borderWidth: 1, borderColor: C.ln,
          alignItems: 'center', justifyContent: 'center' },
  row: { flexDirection: 'row', alignItems: 'center', gap: 10, paddingVertical: 8, borderTopWidth: 1,
         borderTopColor: C.ln },
  sw: { minWidth: 104, minHeight: 40, paddingHorizontal: 10 },
  acts: { flexDirection: 'row', flexWrap: 'wrap', gap: 8, marginTop: 6 },
  input: { paddingHorizontal: 12, paddingVertical: 12, backgroundColor: C.well, color: C.fg, borderWidth: 1,
           borderColor: C.ln, fontFamily: MONO, fontSize: 16 },
});
