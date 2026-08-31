<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# 🤝 Contributing

Read this before the first patch. The invariants below are not style
preferences: each one is load-bearing, and the `D-nn` references point at
the design journal described in the [README](README.md).

## 🔒 Seven rules worth knowing before touching the code

**Every ticket read goes through `Ticket.objects.visible_to(user)`.** Never the
bare queryset. The authorisation scale lives in `accounts/authz.py`; a ticket
you may not see returns 404, never 403, because a 403 on a sequential
identifier reveals existence. Attachments are never served from `MEDIA_URL`:
they go through `tickets.views.attachment`, which re-checks the parent ticket.

**Being able to read a ticket is not being able to write to it.** Each write
view names the predicate it needs -- `can_work_on`, `can_widen`,
`can_restrict_to` -- and a refused write answers with the same 404. Two rules
are worth knowing before touching them: assigning somebody grants them read
access, which is why only an admin may assign anyone but themselves; and
uploaded photos have their type read from their bytes, their metadata (so their
GPS coordinates) removed, their long edge capped at 2048 px, and are stored
under a name we chose, never the one they arrived with. A file Pillow cannot
decode is refused rather than kept unscrubbed.

**A notification never crosses `visible_to()` -- and it is checked twice.**
Once when the row is written, once again by the worker just before the Push
leaves, because restricting a ticket is open to everyone and may happen in
between. A notification that became invisible is dropped, never delayed. The
Push payload carries the kind of event and the room, and nothing else: it is
read off a locked screen. The in-app list may say more, being behind the
session.

**There is one administration role, and it is a Django superuser** (D-27).
`role = admin` sets `is_superuser`, maintained in `User.save()` so that a role
changed from the Django admin carries the flag with it. `is_staff` means
"superuser" and nothing else; the question the *application* asks is
`user.is_admin`, which is a different question with the same answer.

**Signing in is not being given access.** Enrolment grants access, and only
enrolment: the login backend creates no account and provisions nobody. Every
failure -- unknown login, wrong password, deactivated or anonymised account --
answers with the same sentence, because two different messages would turn the
form into a way of asking who is enrolled.

**A resolution is offered, never required** (D-29). `Ticket.resolution_comment`
points at the note in the thread that says what actually worked, and nothing
enforces it: a mandatory field teaches people to type "ok" rather than to write
a resolution, and a duplicate or a false alarm has none to give. What replaces
the constraint is the gap being *visible* -- a resolved ticket with no note
marked says so on its page and on its card in the list. The mark also survives
a reopening, where `resolved_by` and `resolved_at` are cleared: "who closed
this" is a question a reopened ticket no longer has, "what worked last time" is
the one its next reader starts from.

**The worker holds the only lmnapi secret.** `parc/lmnapi.py` reads it from the
process environment, never from Django settings, so the web service can share
the code without being able to use it.
