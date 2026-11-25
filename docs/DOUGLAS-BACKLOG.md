# Douglas: the backlog

What is not done, what was put off, and what waits for the owner to say.
Built and running: `docs/DOUGLAS-ASSISTANT-PLAN.md` and the README. Nothing
here is needed for what runs today to be safe to use; anything that was would
be under "Blocking" below.

## Blocking

Nothing known. The independent review of the work from the notices onwards
is listed under "From the review" as it comes in; anything it shows that lets
somebody without the owner's own credentials write, approve, run a tool or
read his memory is fixed before it is listed anywhere else.

## Waiting for the owner to say

- **Pushing.** The commits from the pipeline's own user onwards are local.
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
- Looking back through the conversation: one request reads at most 4 MB back
  from the end of the record, so in a record years long the far past cannot
  be reached, and the screen would call that "the beginning of the record".
  It needs the agent to find its place in the file by time.

## Housekeeping

- Douglas has not been rebooted since the last five services went in. They
  are all enabled, and their unit files verify.
- `douglas-voice.py` is one file of seven thousand lines.
- One old check in its tests ("and of one that was cancelled") depends on
  timing and fails now and then on a busy machine.

## From the review

(Filled in when the review reports. Blocking findings are fixed first and
named here with the commit that fixed them; the rest are listed as they are.)
