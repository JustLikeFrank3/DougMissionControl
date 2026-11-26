# Job 875c0c1f: why "those improvements" reached the workshop as two words

Douglas looked through this repository and recommended changes. Asked next to
"create a new branch off main and execute those improvements", he started
workshop job 875c0c1f, which added two tests to one file and reported itself
done. This is what happened, what was changed so that it cannot happen the
same way again, and how that job measures up.

## What the evidence is, and what it is not

Read directly:

- The job's branch, `douglas/then-create-a-new-branch-875c0c` (commit
  `128849c`, one commit on `f64e28a`), and its diff.
- Pull request 6, opened from that branch. Its description is written by the
  manager from the job's record: the job's words, the commands the runner
  recorded, and the worker's account.
- The code that ran: `douglas/douglas-voice.py` and `douglas/douglas-work.py`
  at `f64e28a`.

Not read: the job's record on Douglas (`/var/lib/douglas-work/done/875c0c1f.json`),
the record of the look that made the recommendations, the journal, and the
conversation. They are on Douglas, and this investigation ran somewhere that
cannot reach him. So the text of the original recommendations is not in this
document; see "What is still open".

## The path from the message to the job, at `f64e28a`

1. `Pipeline` put the message to `repo_wanted` (douglas-voice.py), which asks
   the model one question, "what does this ask of a repository?", and got
   `WORK`. It returned `{"do": "work", "task": said}`: the person's message,
   cut to 600 characters. The comment on that line read "the person's own
   words are the job".
2. `tool_repo` built the workshop request: `repo`, `task` (those words), and
   `before`, the person's own last three messages, whatever they were about.
   Nothing Douglas had said, and nothing a job had reported, was in it.
3. `Manager.op_work` (douglas-work.py) wrote `job.json` from that: `task`,
   `before`, the repository's notes, limits. The branch name is the first
   five words of the task, which is why it reads `then-create-a-new-branch`.
4. `Worker.loop` gave the model one message: the repository, its top-level
   listing, "What the person said just before", and "The job, in the words of
   the person who asked".

`restated()` exists to make a message that points back stand alone, but it is
used only to classify the message, it sees only the person's previous
message, and it throws away any rewriting with a word the person did not say.
It could never have put Douglas's recommendations in.

## The five questions

**Did the workshop receive only the latest message, or the conversation
too?** The latest message, plus at most three earlier messages of the
person's. Never a reply of Douglas's. Pull request 6 quotes the job as it was
handed over:

```text
Then create a new branch off main and execute those improvements.
```

**Were Douglas's recommendations in the job specification?** No. No field of
the specification could hold them. If they came from a look through the code,
they were in that job's record in the same manager, a few files away, and
nothing read them.

**Was "those improvements" resolved before dispatch?** No. Nothing between
the message and the job tried to.

**Did the subagent understand the full scope and fail to finish?** No. It was
never told the scope, and its own account shows it making one up:

> Created feature branch 'douglas/sim-agent-pattern-coverage' off the main
> line (commit f64e28a, the last job's "fix(sim-agent): rebuild the arrival
> pattern when the plan changes"). Improvement: strengthened test coverage of
> the arrival-pattern logic that the last job's fix depends on

It took the newest commit on main for "the last job" (it is the owner's own
commit, not a job's) and chose something plausible to do about it. It also
says it created a branch that does not exist: the work is on the branch the
runner made. It finished what it had decided the job was. The second look,
shown only the same sentence and the diff, could not know anything was
missing either.

**Was anything truncated?** Not in this job: the message is 65 characters.
But the path did cut a job's words at 600 characters (step 1), and that is
fixed too.

## Root cause

A design assumption, not a malfunction: "the person's own words are the job".
That holds for "Fix the greeting so it keeps the comma". It fails for any
message whose content is somewhere else, and nothing detected that case. The
rule that only the person's words reach a job is there for a reason (text
from a repository or an issue must not become instructions unseen), and it
was applied to Douglas's own recommendations as well, with no way round it.

Two things made it worse:

- The worker had no instruction for a job it could not understand, so it
  guessed.
- Nothing recorded what the job was supposed to achieve, so "done" meant only
  that the worker had stopped. The status, the line on the screens, the
  memory note and the pull request all said the job was done.

## What changed

In the voice pipeline (`douglas/douglas-voice.py`):

- `job_brief`: a job whose words point back is written out by Douglas
  himself before it starts. He is shown the conversation as he had it and
  the last look's report from the workshop's record, and writes each change
  by number with what counts as done, for a worker that was not there.
- In the owner's own repository it starts at once, and the job as handed
  over appears on the screens as it starts. A first version of this fix
  listed the changes with a separate filter and asked for a yes every time;
  that was more caution than the case needed and was taken out.
- Turns in which the owner's records were read are not shown to him for
  this, and a job Douglas thinks of himself is still refused.
- Where he cannot tell what is meant, no job starts and he asks.
- `before` now carries only the person's messages from turns about the
  repository. The whole message is the job, up to 2,000 characters.
- `work_line`, the memory note and `job_shown` say what became of each
  requirement.
- The conversation he keeps is no longer six exchanges with each lookup cut
  to 300 characters. It is kept whole until it no longer fits the model's
  window (`VOICE_HISTORY_CHARS`, 24,000 characters), and then the oldest
  exchanges go.

In the workshop (`douglas/douglas-work.py`):

- `op_work` takes `needs` and `context`, cuts them to size and keeps them in
  the job's own record. `findings` returns the last look's report.
- Where the repository is not the owner's own, `op_work` does not start
  such a job: it reads the requirements back and starts on a yes to exactly
  that list.
- The worker is told where the work goes and that it makes no branch of its
  own, is handed the requirements and their source, must say of each whether
  it is done, outstanding or blocked, and is told to change nothing and say
  so when a job points at something it was not given.
- `finish` keeps each requirement's state and an `outcome` (`complete`,
  `partial`, `unmet`). `report`, `status` and the pull request text do not
  call a job with a requirement left finished.
- "Do the rest" carries the requirements that were left into the next job on
  that branch.

On the screens (`douglas/douglas-agent.py`, `pi/deck-ui/js/owner.js`): the job
line counts requirements, and opens to the specification, the context, each
requirement, the files, the checks and commands, and the worker's account.

What did not change: the fence around a job, who may open the workshop's
socket, that a push needs a yes, that nothing is merged or deployed by any of
this, and that the worker's word on a requirement is marked as its own. The
states are the worker's claims, checked only as far as "a job that handed
over no change did not do it" and by the second look; they are not proof.

## Job 875c0c1f against the recommendations

What it implemented, all of it in `tests/sim-agent-check/Program.cs` (80
lines added, none removed, no source file touched):

1. A test that a plan asking for runway 28R, the other end of the 10L/28R
   runway already in the file, gets the four legs named for 28R, with final
   east of the threshold.
2. A test that a plan naming a runway the facility does not hold (05L) gets an
   empty pattern.

Both follow the existing test beside them and match `BuildVisualPattern` as
it stands. Neither has been compiled or run: the sandbox has no `dotnet`, and
the runner recorded only `git diff`, a brace count and `git status`.

What it omitted: every recommendation, as a recommendation. None was in what
it was handed, so nothing it did was done because Douglas recommended it. If
one of the recommendations happens to be "more tests for the arrival
pattern", these two tests would cover part of it by coincidence; that can
only be settled against the list.

Its account is wrong in two places: the branch
`douglas/sim-agent-pattern-coverage` was never created, and `f64e28a` is not
"the last job's" commit.

Pull request 6 was left untouched by this work, and has since been merged
into main by the owner. It is a small, plausible addition of tests, and
should be read as that: it is not the implementation of the recommendations,
and its two tests have still not been built or run on a machine with the
.NET SDK.

## What is still open

The item-by-item comparison needs the recommendations as Douglas wrote them.
On Douglas:

```bash
sudo jq -r 'select(.kind=="look") | "\(.id)  \(.finished|todate)\n\(.task)\n\(.summary)\n"' /var/lib/douglas-work/done/*.json
```

```bash
sudo jq '{task, before, state, step, looked, ran, summary}' /var/lib/douglas-work/done/875c0c1f.json
```

The first prints what each look reported (the manager keeps the last 40
jobs). The second shows exactly what job 875c0c1f was handed; `before` there
is the one thing this document infers from code and has not seen. If the
recommendations were given in the conversation and not by a look, they are in
the transcript on the DOUGLAS surface.

With the fix deployed, the same request can simply be made again: Douglas
reads the list back, and the job that follows says which of it is done.
