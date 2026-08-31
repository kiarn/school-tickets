<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# 🎟️ School Tickets

Ticket management for the pupils who repair the hardware of a school running
[linuxmuster.net](https://linuxmuster.net). Built for the phone: it is used
standing up in a classroom.

**Status: first draft. Not ready for production.** The foundation, the estate
reconciliation and every screen -- tickets read and written, profiles, badges,
notifications -- are in place and tested. OIDC and the lmnapi endpoints are
not. See "Not done yet".

It is also still being developed too actively to be deployed to anyone: the
data model and its migrations change from one week to the next, and no upgrade
path between two commits is offered or kept working. Read it, run it, take it
apart -- but do not put a school's tickets in it yet.

## 🚀 Getting started

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
.venv/bin/python manage.py test              # 307 tests, visibility among them
.venv/bin/python manage.py run_worker --once # one pass of every due job
.venv/bin/python manage.py sync_badges       # after an upgrade adds a badge
.venv/bin/python manage.py vapid_keys        # Web Push keys, once per install
```

Translations need GNU gettext (`apt install gettext`), and the compiled
catalogues are a **build artefact**: `locale/*/LC_MESSAGES/*.mo` is not in the
repository, so a fresh clone renders English until they are built.

```sh
.venv/bin/python manage.py compilemessages -i .venv   # after every clone
.venv/bin/python manage.py makemessages --no-wrap -l de -l fr -l en \
    -i '.venv/*' -i 'tools/*' -i 'staticfiles/*'      # after adding a string
```

Both commands walk the current directory, hence the ignores: without them
`compilemessages` rebuilds every catalogue Django itself ships inside the
virtualenv. Forget the compilation entirely and `manage.py check` says so
(`W006`) -- the price of keeping the binaries out of the repository is that
their absence has to be loud.

## ⚙️ The two processes

| | role | lmnapi secret |
|---|---|---|
| `school-tickets-web` | gunicorn behind the reverse proxy | **no** |
| `school-tickets-worker` | `manage.py run_worker`, no listening port | **yes** |

The worker also holds the **VAPID private key**, for the same reason and by the
same means. `manage.py vapid_keys` prints the pair and says which file each key
belongs in; it writes nothing, because guessing would undo the separation.

The separation rests on two environment files with different permissions, not
on the discipline of the code. Units live in [`debian/`](debian/).

## 🔒 Before touching the code

Seven rules carry the security of this application -- ticket visibility, what
separates reading from writing, the notification checked twice, the single
administration role, enrolment, the resolution mark, and the one secret the
worker holds. They are written up in
[`CONTRIBUTING.md`](CONTRIBUTING.md), and nothing in this codebase makes sense
without them.

## 🌍 Language

Code is **English** throughout: identifiers, comments, docstrings, test names
and translatable strings. Source strings are English, which makes English a
translation like any other -- so `locale/en/` exists beside `de` and `fr`, and
its `msgstr` are deliberately empty: gettext falls back to the msgid, which is
already the English.

German and French are complete and are held that way by a test that reads the
`.po` files: a string added without a translation fails the suite on a fresh
clone, before anybody has compiled anything. Both address the reader as **du /
tu** (D-34) -- the application is first the pupils' own tool.

Two traps, both of which cost something before they were understood:

- **the test suite runs in English**, forced in `school_tickets/runner.py`.
  `LANGUAGE_CODE` is German, so the day German was translated, fourteen tests
  that had never mentioned a language began to fail. A test asserts the strings
  this project writes; one that wants a translation asks for it explicitly.
- **`makemessages` writes the English plural rule into every catalogue it
  creates.** French counts zero as singular (`plural=(n > 1)`), so the header
  is corrected by hand after each run -- and a test remembers it.

`--no-wrap` is not cosmetic: without it every entry is folded at 77 columns, one
translated string per handful of lines, and a `.po` diff stops being readable.
gettext folds a string carrying an embedded newline whatever the flag says, so
nothing may assume an entry fits on one line -- the completeness test joins
continuation lines for exactly that reason.

The design journal is French and lives outside this repository (see
"Design journal" below).

## 📓 Design journal

The reasoning behind every decision -- data model, permissions, the parc
synchronisation rules, GDPR, ticket visibility -- is written up as a numbered
set of documents referenced throughout the code as `D-nn` (decisions) and
`Q-nn` (open questions).

Comments in the code cite those documents by path and number, as
`specs/08-visibilite.md` or simply `D-21`, so that a decision can always be
traced back to its reasoning. The `specs/` directory itself is written in
French and **not tracked in this repository**: it is the roadmap, not a shipped
artefact, which is why the paths resolve to nothing in a fresh clone.

## 🚧 Not done yet

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
- **A starter set of tags.** A fresh install still has no tag vocabulary: the
  tag dialog opens on "No tag exists yet". Tags are configurable by design, so
  what is missing is not a mechanism but a **default proposal**, a handful for
  the faults that recur. The badges used to be listed here too and no longer
  are: `badges/catalog.py` ships twenty-one of them, seeded by a migration and
  by `sync_badges`. The tags cannot borrow that answer, and D-15 says why -- a
  catalogue badge is a `msgid` travelling with the code, translated on the
  platform, while a tag is this instance's own vocabulary and is never
  translated, so the two cannot be seeded by the same mechanism.
- **Crowdin.** The catalogues are written and complete, but nothing is wired
  to the translation platform the other linuxmuster projects use (D-15). What
  stood in the way no longer does: D-19 assumed this repository would stay
  private, and the free open-source tier wants a public one -- it is public
  now, so what is left is the wiring itself. Until it exists, a fourth
  language means editing a `.po` by hand.
- **An automatic install prompt, and anything offline.** The manifest is there
  (D-33), so the application installs to a home screen -- but `sw.js` has no
  `fetch` handler, deliberately: no caching, no offline shell (doc 09). Chrome
  has made that handler a condition of *offering* the installation by itself,
  so the prompt may not appear and the phone's "add to home screen" menu is
  what installs it. Whether to add a handler, and how much of the application
  should work with no network, is Q-09 and is not settled.

## ⚖️ Licence

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
