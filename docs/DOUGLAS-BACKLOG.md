# Douglas: the backlog

What is not done, what was put off, and what waits for the owner to say.
Built and running: `docs/DOUGLAS-ASSISTANT-PLAN.md` and the README. Nothing
here is needed for what runs today to be safe to use; anything that was would
be under "Blocking" below.

## Blocking

Nothing known. The independent review found three things in ChatGPT's line
that blocked, and they are fixed: see "From the review" at the end.

## Waiting for the owner to say

- **Pushing.** The commits from the pipeline's own user onwards are local.
  The eight pictures in `docs/img/douglas*.png` were made from made-up data
  on a workstation; two were looked at again after they came in, and the
  other six should be before they are published.
- **A message typed in ChatGPT itself.** ChatGPT's line was tried from the
  internet's side with a stand-in for ChatGPT's connector client. A
  conversation in ChatGPT takes the owner's account. If ChatGPT still shows
  the old description of the tool ("no tools, will not discuss his owner"),
  refresh the connector there: its name and what it takes are unchanged.
- **Open WebUI's own page.** `douglas` should be in its list of models; it
  was not seen, since that takes the owner's sign-in.
- **`get_session_context` from his records.** He is held to the stories and
  writing samples (`JOBCONTEXT_AREAS=stories`), by the owner's earlier word.
  That one call returns everything at once: the résumé, the tone profile,
  every story, the job hunt as it stands, and the log of people and contacts.
  It would fill his memory quickly, and it would also put the job hunt and
  other people's details in front of him, and through him in front of ChatGPT
  now that ChatGPT can ask what he remembers. A narrower read (the résumé
  alone) would do most of the good. One setting either way; the owner's call.
- **Which program gets `douglas-mcp`'s token.** Nobody has it. It answers on
  the tailnet only, so ChatGPT cannot use it as it stands, and does not need
  to: it reaches what Douglas remembers by asking him.
- **Notices on a phone whose page is closed.** That goes through somebody
  else's push service, or one more thing to run at home.
- **Saying a notice aloud.** Built, off for every kind until switched on in
  Mission Control, and never tried in the room.
- **The house (phase 6).** Not without being asked for by name.

## Finding memories by their sense (docs/DOUGLAS-MEMORY-SEARCH.md)

- **Camera observations.** The schema, expiry and labels are ready, and
  storing them is off (`MEMORY_OBSERVATIONS=0`). Switch it on only with
  DougRing, and only when asked. Before then, restart `douglas-voice` so that
  he shows an observation as "seen at …, not checked since". That label is
  committed but not yet running live.
- **Mission Control.** The memory page's kind filter knows `place` and
  `observation`, but it has not been deployed to node1. There is no button to
  rebuild the vectors (`reindex`): adding one would widen what the deck can
  ask for.
- **Thresholds on real data.** `EMBED_MIN` 0.30 was measured on 38 made-up
  memories. Once he has a few hundred real ones, check that unrelated
  questions still bring back nothing.
- **A reranker.** Not justified at 0.97 found-in-top-3. Measure again if the
  memory grows.

## ChatGPT's line

- Tools on that line. There are none, on purpose. Readings (the weather, the
  machines) could be let through one at a time; each is something an
  internet-facing line would then be able to set going.
- Notes from ChatGPT's conversations. Nothing ChatGPT says is noted. If that
  is ever wanted, it should be a note that waits for a yes in Mission Control.
- The secret in the address is the only thing between the internet and the
  connector. ChatGPT's connectors can also sign in with OAuth: more to run,
  and worth it only if the address is ever thought to have got out (changing
  `CHATGPT_SECRET` revokes it today).
- The connector keeps a session's conversation in memory only: a restart of
  it, or two hours of quiet, and ChatGPT starts afresh with him.

## What he notices

- A week of ordinary days has not passed: whether the lines are right for
  this house (how hot, how full) is not known yet. They are settings.
- The Pi's own "Douglas is not answering" was tried with stand-ins, not by
  taking Douglas off the network.
- A machine being back is not a notice of its own.
- The Pi's k3s workloads, the Macs and DougVoice beyond "answers or not".

## Approvals, switches, what is open

- The hold on APPROVE was not pressed by a finger: what the buttons send was
  sent to Mission Control's route.
- Nothing in the panel's top bar says that something is waiting for a yes.
- Earlier jobs of the workshop are not listed: one line, the latest. The
  line itself was not seen with a real job.
- A switch is a tool, not a line: there is no switch for "the chat page" or
  "ChatGPT" in Mission Control. Those are `webui-setup.sh off` and
  `chatgpt-line.sh off` on Douglas.
- The first turn after a switch changes takes about forty seconds.

## The same Douglas elsewhere

- Open WebUI: a picture he draws is not shown there; his answer arrives in
  one piece; a message is one line of 2,000 characters at most; its separate
  conversations are one conversation to him.
- Looking back through the conversation (built: at the top of the log, or
  on EARLIER, thirty older lines at a time): one request reads at most 4 MB
  back from the end of the record, so in a record years long the far past
  cannot be reached, and the screen would call that "the beginning of the
  record". It needs the agent to find its place in the file by time. Lines
  that were loaded are kept for as long as the page is open.
- Seen while taking the README's pictures, and left as they are: on the
  phone's MEMORY card the second row of filter chips runs past the card's
  edge; the panel's conversation shows the time as "09:25 PM" where the rest
  of the panel uses the 24-hour clock; under the box of something waiting
  for a yes the conversation can begin in the middle of a bubble.

## Housekeeping

- Douglas has not been rebooted since the last five services went in. They
  are all enabled, and their unit files verify.
- `douglas-voice.py` is one file of seven thousand lines.
- One old check in its tests ("and of one that was cancelled") depends on
  timing and fails now and then on a busy machine.

## From the review

An independent review of everything from the notices onwards, ChatGPT's line
first. (The first reviewer stopped on a model limit without a report; this
is the second, on another model.) It was asked to separate what blocks from
what is advice.

Blocking, and fixed before this was written (the commit "what a review
found in ChatGPT's line"):

- A turn of the house's that began while ChatGPT's was under way (cancel,
  then talk) ran with ChatGPT's conversation in front of him and the house's
  tools; and a turn of the house's that had been cut short and was still
  unwinding could pick ChatGPT's conversation up the other way. Shown with a
  small program. Now ChatGPT's turn has the conversation alone: it waits
  until none of the house's is under way, the house's wait for it, and what
  a job, an edit or an answer on a screen adds to the conversation waits too.
  The house comes first: its turn is taken at once and ChatGPT's is cut
  short. The reviewer's program no longer does what it did.
- ChatGPT's turn could begin while he was speaking to the house and cut him
  short. It begins only when he is doing nothing, and cuts nothing short.
- What was looked up in his memory for ChatGPT was marked as used, which
  decides what is kept longest. It is not marked for a line that is not the
  owner's.
- With those, two of its smaller points: what ChatGPT says its conversation
  was has its square brackets taken out, so it cannot pass for a note of the
  program's own; and the pipeline's own token is compared in constant time.

Advice, not done:

- Whoever holds the secret address can still take his time: sixty messages
  an hour, each a few seconds of his thinking. The house is no longer kept
  waiting by it, and no notice is hidden by it; lowering `CHATGPT_PER_HOUR`
  or a gap between messages would cut it further. The cap is counted before
  a message has waited its turn, and several messages can come in one
  request.
- The secret address is now as good as reading what he remembers and what
  his instruction files say of the owner, and it is kept in a URL. See
  ChatGPT's line, above.
- `announce()` says a notice was said aloud even if it was cut short. (Said
  aloud is off.)
- The pipeline's own routes would be open to the tailnet if
  `VOICE_PTT_TOKEN` were ever empty: it should refuse to start without one.
- `chatgpt-line.sh off` and `webui-setup.sh off` take a token out of the
  settings, but it works until `douglas-voice` is next restarted. Both say so.
- `mcp-setup.sh` stops the memory to hand it its third door and has no trap:
  a failure in between would leave it stopped. `webui-setup.sh` has one.
- `chatgpt-line.sh` writes its two settings files one after the other, not
  both or neither.

What it tried to break and could not: who a message is from is set by the
token alone; a chat page's errands reach the bare model only from the chat
page; no tool runs on ChatGPT's line but his memory, read; nothing is noted
or forgotten there; no yes is taken, and what waits for one is neither
answered nor dropped; "new conversation" from there ends only its own; the
owner's notices are not handed over; what ChatGPT said is not gone through
afterwards; the connector's journal has no words; the line answers on
Douglas only; an approval is a yes to one number, runs out, and travels the
owner's path; the pipeline can neither write the switches nor reach their
door; only the watch raises and clears a notice; nothing is said aloud
unless switched on; the memory's door for readers and `douglas-mcp` write
nothing; the setup scripts show no secret but the one `mcp-setup.sh show`
is for.
