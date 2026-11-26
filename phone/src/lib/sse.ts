/* sse.ts — deck-api's event stream, taken apart.

   Each frame is one `data: <json>` line and a blank line, holding the whole
   deck state. There are no named events and no ids. The bytes arrive in
   whatever pieces the link delivers them in, so a frame is only a frame once
   its blank line has come. */

export function frameReader() {
  let kept = '';
  return {
    /** Feed what arrived; get back the data of every frame now complete. */
    feed(text: string): string[] {
      kept += text.replace(/\r\n/g, '\n');
      const out: string[] = [];
      let at = kept.indexOf('\n\n');
      while (at >= 0) {
        const block = kept.slice(0, at);
        kept = kept.slice(at + 2);
        const data = block.split('\n').filter((l) => l.indexOf('data:') === 0)
          .map((l) => l.slice(5).replace(/^ /, '')).join('\n');
        if (data) out.push(data);
        at = kept.indexOf('\n\n');
      }
      return out;
    },
  };
}
