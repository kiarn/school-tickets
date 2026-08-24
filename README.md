<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# School Tickets

Ticket management for the pupils who repair the hardware of a school running
[linuxmuster.net](https://linuxmuster.net). Built for the phone: it is used
standing up in a classroom.

**Status: first draft.** The foundation, the estate reconciliation and every
screen -- tickets read and written, profiles, badges, notifications -- are in
place and tested. OIDC and the lmnapi endpoints are not. See "Not done yet".

## Getting started

```sh
python3 -m venv .venv && .venv/bin/pip install -e .
export ST_DB=sqlite                 # production runs MariaDB in utf8mb4 (D-03)
.venv/bin/python manage.py migrate
.venv/bin/python manage.py enroll --cn <login> --role admin --password <pw>
.venv/bin/python manage.py runserver
```

Then sign in at `/login/` with that `cn` and password. `manage.py set_password
--cn <login>` sets one on an account that has none, and asks at the terminal
when `--password` is omitted.

A working copy shared between several machines -- a synchronised folder, a
different Python version on each -- wants one environment per machine instead,
because a virtualenv hardcodes its own absolute path and does not survive being
moved or reused elsewhere:

```sh
python3 -m venv .venv/"$(hostname -s)"
.venv/"$(hostname -s)"/bin/pip install -e .
```

`.gitignore` covers either layout. Read every `.venv/bin/...` below as that
path.

The stylesheet is versioned, so installing the project needs none of what
follows. It is only for changing `assets/app.css` or a template, and the first command fetches the
pinned Tailwind binary (Linux x86-64) alongside the daisyUI sources:

```sh
./tools/fetch-css-deps.sh                                      # pinned, curl --fail
./tools/tailwindcss -i assets/app.css -o static/app.css --minify
python3 tools/check-classes.py                                 # see below
```

`check-classes.py` reports class names the templates use and the stylesheet
does not define. It exists because that failure is completely silent: Tailwind
drops an unknown class, the browser ignores it, the page renders without it and
nothing says a word. Three such classes were live in this repository on
2026-08-24, one of them the whole daisyUI colour palette.

Load a sophomorix estate without touching lmnapi:

```sh
.venv/bin/python manage.py sync_parc --from-file devices.csv --dry-run
```

`enroll` answers the bootstrap problem: **only explicitly enrolled accounts may
log in**, so on a fresh install nobody can get in until this command has run.
Local passwords are a stopgap until OIDC (D-05, D-26); the login page creates
nothing and provisions nobody, it only checks that an already-enrolled person
is who they say they are.

```sh
.venv/bin/python manage.py test              # 153 tests, visibility among them
.venv/bin/python manage.py run_worker --once # one pass of every due job
.venv/bin/python manage.py vapid_keys        # Web Push keys, once per install
```

Working on translations additionally needs GNU gettext (`apt install gettext`),
otherwise `makemessages` fails on `msguniq`.

## The two processes

| | role | lmnapi secret |
|---|---|---|
| `school-tickets-web` | gunicorn behind the reverse proxy | **no** |
| `school-tickets-worker` | `manage.py run_worker`, no listening port | **yes** |

The worker also holds the **VAPID private key**, for the same reason and by the
same means. `manage.py vapid_keys` prints the pair and says which file each key
belongs in; it writes nothing, because guessing would undo the separation.

The separation rests on two environment files with different permissions, not
on the discipline of the code. Units live in [`debian/`](debian/).

## Six rules worth knowing before touching the code

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

**The worker holds the only lmnapi secret.** `parc/lmnapi.py` reads it from the
process environment, never from Django settings, so the web service can share
the code without being able to use it.

## Language

Code is **English** throughout: identifiers, comments, docstrings, test names
and translatable strings. Source strings are English, which makes English a
translation like any other -- an `en` catalogue is needed alongside `de` and
`fr`.

The design journal is French and lives outside this repository (see
"Design journal" below).

## Design journal

The reasoning behind every decision -- data model, permissions, the parc
synchronisation rules, GDPR, ticket visibility -- is written up as a numbered
set of documents referenced throughout the code as `D-nn` (decisions) and
`Q-nn` (open questions).

Comments in the code cite those documents by path and number, as
`specs/08-visibilite.md` or simply `D-21`, so that a decision can always be
traced back to its reasoning. The `specs/` directory itself is written in
French and **not tracked in this repository**: it is the roadmap, not a shipped
artefact, which is why the paths resolve to nothing in a fresh clone.

## Not done yet

Three of these wait on the same thing and are meant to be done in one pass, the
day there are credentials for Keycloak and for lmnapi: the OIDC backend, the
lmnapi adapter, and the account cleanup that D-31 makes urgent. None of them can
be written honestly before somebody has read a real response from either system
-- which is the whole lesson of `rows_from_api` standing there raising
`NotImplementedError` rather than guessing.

- **OIDC.** A local password login stands in for it (D-26), and
  `bind_oidc_sub()` is written; the Keycloak backend is not (D-05). What it
  has to do is settled, though, and it is no longer a gate. Whoever the
  provider authenticates gets an account, `reporter` by default (D-31): the
  filter deciding who may authenticate at all belongs in the Keycloak client,
  where each school's administrator sets it and owns the consequence. First
  login matches on `cn` and has four answers -- no row, enrol one; a row with
  no `sub`, bind it; a row already bound, let them in; **a deactivated row,
  refuse**. That last branch is the only one that still needs a page, and it
  is the reason deactivating somebody keeps meaning something. Everyone else
  lands on the ticket list they can already read, with the `+` in the corner,
  which is why there is no landing page to build.
- **The lmnapi adapter.** The reconciliation engine is written and tested, and
  `sync_parc --from-file` exercises it end to end; only
  `parc/sources.py:rows_from_api` is unwritten -- it stands there and raises
  `NotImplementedError`, because the shape of the JSON response has not been
  read yet and guessing it would produce an adapter that looks finished and
  reconciles nothing (Q-03).
- **A holiday calendar.** The worker will notify -- and sweep the estate --
  during the school holidays. Noise, not damage; the same data answers both
  (D-20, D-28).
- **End-of-year certificate, account cleanup, GDPR erasure.** Three things that
  are one thing: they all happen in July, and they happen in an order.
  `User.anonymize()` is written and tested, down to the badge rule of R-17;
  nothing calls it yet -- no screen, no command, not even an admin action. The
  certificate has no code at all: it reads badges and assignments, which are in
  place. The cleanup has none either, and D-31 is what makes it urgent -- an
  account now appears the first time somebody logs in, and nothing ever says
  they left. D-32 settles its shape: an administrator presses a button, the
  application asks lmnapi which of the `cn` **it already holds** no longer
  exist, and proposes -- never applies. The proposal writes itself, because
  three `PROTECT` foreign keys (`Ticket.created_by`, `Comment.author`,
  `Attachment.uploaded_by`) split the candidates in two: an account that never
  wrote anything is deleted outright, one that did is anonymised. The order is
  the trap: `anonymize()` drops the badges the certificate is generated from,
  so certificates come first. Waiting on the lmnapi endpoint, whose semantics
  are Q-08 -- sophomorix moves leavers rather than deleting them, so "this `cn`
  exists" and "this person is still here" are not the same question.
- **A comment marked as the resolution.** Nothing carries one today: a
  `Comment` has a body and an author, a `Ticket` has `resolved_by` and
  `resolved_at`, and the two are not tied together. Closing a ticket therefore
  costs nothing and teaches nothing, and the next person to meet the same fault
  reads twenty notes to find what worked. The mark will hang off the ticket, as
  a `resolution_comment` beside `resolved_by`, and resolving will **offer** it
  without ever requiring it: a mandatory field teaches people to type "ok", and
  some tickets legitimately have no resolution to write -- a duplicate, a false
  alarm, a machine replaced. Showing the gap is the lever, not blocking the
  form (D-12).
- **Empty translation catalogues.** `locale/` holds no `.po` at all, so the
  five catalogue badges shipped with the demo run their names through
  `gettext()` and come back in English, next to a school badge in German.
  Nothing is broken; nothing is translated either (D-15).
- **A configurable title and logo.** `school-tickets` is written into
  `templates/base.html` (the `<title>` and the header),
  `templates/accounts/login.html` and `templates/notifications/sw.js`. Every
  school wants its own acronym and crest, the login page included. One instance
  serves one school -- `school_id` exists to avoid a hardcoded path, not to
  promise multi-school management (D-06) -- so this is instance configuration,
  and nothing rendered before login needs to know which school it is. The same
  asset would give the application a favicon and the Push notification an icon,
  neither of which exists.

## Licence

    School Tickets - ticket management for pupil hardware repairers
    Copyright (C) 2026  Arnaud Kientz

    This program is free software: you can redistribute it and/or modify it
    under the terms of the GNU General Public License as published by the Free
    Software Foundation, either version 3 of the License, or (at your option)
    any later version.

    This program is distributed in the hope that it will be useful, but WITHOUT
    ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
    FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License for
    more details.

    You should have received a copy of the GNU General Public License along
    with this program.  If not, see <https://www.gnu.org/licenses/>.

[`LICENSE`](LICENSE) holds the official GPLv3 text **verbatim**: the licence
forbids altering its own text, so the copyright notice lives here and in the
`SPDX-License-Identifier` headers of the source files, never in `LICENSE`.
