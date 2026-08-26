# Task board — phased plan

Building the Cursor/Antigravity-style multi-task board **across separate
sessions** to conserve tokens, same pattern as the hybrid build. Do ONE phase
per session. Resume with **`/taskboard`** (or just ask to continue it).

Context: the app's core assumption today is one conversation = one task =
one project, handled serially (one `abortCtrl`, one open `_proj`, one preview
panel). A real task board needs actual concurrency, not just a new screen —
so this is scoped to get real, incremental value at every phase without
requiring the big concurrency rewrite up front.

## Status
- [x] **Phase 1 — Read-only board view (no concurrency)** ✅ DONE — a "⊞"
      button next to "Code tabs" in the sidebar opens a Kanban modal
      (`#board-modal`) with four columns (Not started / In progress / Needs
      fix / Done), derived purely from `codeTabStatus()` — no new state.
      Clicking a card calls `selectChat()` and closes the board. Pure
      frontend (`renderBoard()`/`openBoard()`/`closeBoard()` in
      `app/static/index.html`), zero backend changes. Verified live with
      three code tabs in three different states — all sorted into the
      correct column, click-to-jump confirmed.
- [x] **Phase 2 — Manual task state + richer cards** ✅ DONE — each code tab
      can now carry `c.board = { manual, goal }`, created lazily so old chats
      need no migration. A card can be dragged (native HTML5 drag/drop)
      between columns to pin it there — manual placement wins over the
      live-derived status until reset via a small "↺ auto" button. Each
      card has an inline-editable goal note (click "+ add goal", same
      commit-on-Enter/blur pattern as file renaming elsewhere in the app —
      no `window.prompt`). Verified live: edited a goal, dragged a card to
      "Not started" (confirmed manual override beat its actual passing run),
      then reset via "↺ auto" and confirmed it snapped back to "Done".
- [x] **Phase 3 — Real concurrency** ✅ DONE — the singular `abortCtrl` is now
      `_inFlight` (`Map<convId, AbortController>`), so a Build in one
      conversation keeps streaming while the user switches to and works in
      another. Every DOM-touching line in the stream loop is guarded by
      `isActive()` (`current().id === convId`); Send/Stop reflects whichever
      conversation is on screen (`syncSendUIForCurrent()`, called on every
      conv switch/create/delete); Stop only aborts the visible conversation.
      Deliberate boundary: the auto-run/live-preview-check step still only
      runs for the conversation actually on screen (`maybeAutoRun()`), since
      it drives the one shared preview panel/iframe/`_proj` — a background
      Build's text finishes and saves normally, and its check step fires
      automatically the moment the user switches back to it (`selectChat()`
      calls `maybeAutoRun()` on the newly-active conversation). This is a
      real constraint of the sandboxed-iframe design, not a shortcut: making
      the live-preview check itself concurrent would need a hidden iframe
      per background task, which is Phase 4 territory if ever needed.
      Verified live: fired two Builds in two different code tabs without
      waiting for either, confirmed both were in `_inFlight` simultaneously,
      both completed with correct content in the correct conversation (no
      cross-contamination), the backgrounded one's preview check was
      deferred until switched to, then ran automatically on switch.
- [x] **Phase 4 — Background live-preview checks** ✅ DONE (scoped down from
      the original "isolated per-task project state" framing — see note
      below) — `backgroundCheckProject()` runs a backgrounded conversation's
      web-project check for real, in a disposable, invisible `<iframe>`
      registered in the same `_ipRegistry` that already safely routes
      console messages for several simultaneous inline previews. It never
      touches the shared `_proj`/editor/visible preview panel at all, so
      there's nothing to isolate — the foreground path is untouched and the
      background path never shares state with it. `maybeAutoRun()` now
      branches: foreground behaves exactly as before; background runs the
      hidden-iframe check for a web project, or defers to focus-time for a
      local-script answer (deliberately — a real interpreter running
      unattended for a tab nobody's watching is a different risk than JS in
      a throwaway sandboxed frame nobody can see). A `msg.checking` guard
      makes the whole thing idempotent if the user switches to the tab
      mid-check. Verified live: fired a Build with a deliberate JS error,
      immediately navigated away and never switched back — confirmed the
      message ended up `previewed: true, run: {ok:false, errors:1}` purely
      in the background, the board correctly filed it under "Needs fix",
      the hidden iframe and its registry entry were fully cleaned up, and
      the actually-visible preview panel (New chat, nothing open) was
      completely undisturbed throughout.

      **Scope note:** "merge review across tasks that touch overlapping
      files" (the other half of the original Phase 4 framing) doesn't apply
      to this app's data model — each code tab always has its own
      independent project; there's no mechanism for two conversations to
      share a file store, so there's nothing that could conflict to merge.
      That part of the plan was aspirational without a matching capability
      and is dropped rather than built speculatively.

- [x] **Phase 5 — Real merge review** ✅ DONE (added after Phase 4's scope
      note — user asked to make the dropped "merge across overlapping
      files" idea real instead of leaving it out). `c.taskGroup` links
      conversations that share an origin — the only way two tasks' files
      can ever actually overlap in this app's data model, which is what
      makes merge review meaningful instead of speculative:
      - **⑂ Fork** (board card action, `forkTask()`) clones a code tab's
        *persisted* project into a brand-new linked code tab, tagging both
        with a shared `taskGroup`. Usable directly with Phase 3's
        concurrency to run both simultaneously.
      - **⇄ Merge** (appears on a card once it has a linked sibling) opens
        a modal (`openMergeWith()`/`renderMerge()`) that diffs the two
        tasks' *persisted* `conv.project.files` — deliberately not the
        shared in-memory `_proj`, which reflects whichever conversation
        last loaded the editor, not necessarily either side of the
        comparison — file by file, reusing the existing `lineDiff` engine.
        Per changed file: "Keep mine" / "Keep theirs" (whole-file, no
        line-level splicing). "Apply merge" (`applyMerge()`) writes the
        chosen files into the current task's persisted project and reloads
        it via `reopenProject()`. A picker view handles the rare case of
        more than one linked sibling.
      - Verified live: forked a task, diverged both independently (each
        edited via `reopenProject()` + `persistProject()` to avoid the
        shared-`_proj` trap above), opened merge, confirmed the diff showed
        exactly the two divergent lines, chose "Keep theirs" for the file,
        applied, and confirmed the base task's persisted project became an
        exact copy of the fork's content.

## Decision log
- Chose to front-load a real, useful Phase 1 (a working board UI) rather than
  starting with the hard concurrency problem, so the feature delivers value
  immediately instead of requiring the full rewrite before anything ships.
- Phases 3–4 are the actual "different app shape" — re-architecting the
  single-task assumption. Not started until explicitly resumed.
