<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Running School Tickets in containers

The second installation channel. The first is the Debian package in
[`debian/`](../debian/), which is what a linuxmuster.net integration would use;
this one exists for trying the application out and for schools that would
rather run containers.

Both channels ship the same two processes and the same separation between
them. Nothing here is a different application.

## Quick start

```sh
cd docker
cp env/db.env.example     env/db.env
cp env/web.env.example    env/web.env
cp env/worker.env.example env/worker.env
$EDITOR env/*.env                    # see "Three values you must change"
docker compose up -d --build
docker compose run --rm web manage enroll --cn <login> --role admin --password <pw>
```

Then open <http://127.0.0.1:8080/> and sign in with that `cn`.

`enroll` is not optional: **only explicitly enrolled accounts may log in**
(D-22), so until it has run, nobody can get in -- not even you.

Web Push needs one key pair, generated once and pasted into the two
environment files:

```sh
docker compose run --rm web manage vapid_keys
```

The command prints which key goes in which file. Follow it literally; see the
next section for why.

## Three values you must change

| where | what | why |
|---|---|---|
| `env/db.env`, `env/web.env`, `env/worker.env` | the database password, in all three | they must match, and the example value is public |
| `env/web.env`, `env/worker.env` | `ST_SECRET_KEY` | it signs sessions; a shared example key means forgeable sessions |
| `env/web.env` | `ST_ALLOWED_HOSTS` | Django refuses any request for a host that is not listed |

## The two environment files are the design, not a convention

`web.env` and `worker.env` are separate on purpose, and it is the one thing not
to simplify away.

The worker is the only process that may hold the **lmnapi host key** -- the
credential that can read the whole school estate -- and the **VAPID private
key**, which can push a notification to every subscribed phone. The web service
is the process exposed to the network, so it holds neither. In the Debian
package the rule is enforced by two Unix users and file permissions; here it is
enforced by which `env_file` each service names.

This means:

- **never point `worker` at `web.env`**, and never merge the two files. Nothing
  will fail if you do -- that is exactly the problem. The application keeps
  working, and the exposed process silently gains both secrets;
- `ST_VAPID_PUBLIC_KEY` goes in **both** files; `ST_VAPID_PRIVATE_KEY` goes in
  `worker.env` **only**;
- keep `worker.env` at mode `0600`.

You can check the rule holds at any time:

```sh
grep -c "ST_LMNAPI_HOST_KEY\|ST_VAPID_PRIVATE_KEY" env/web.env   # must print 0
```

## Volumes: what you lose if you delete them

Three named volumes, and two of them hold data that does not exist anywhere
else:

| volume | holds | if lost |
|---|---|---|
| `db-data` | the whole database | everything: tickets, comments, enrolments, badges |
| `media` | the attached photos, kept for ever (R-11) | the photos, permanently -- the database keeps rows pointing at nothing |
| `static` | the compiled stylesheet and scripts | nothing: regenerated on every start |

`docker compose down` keeps them. **`docker compose down -v` deletes them**, and
so can a `docker volume prune`. Back both up:

```sh
docker compose exec db mariadb-dump -u root -p"$MARIADB_ROOT_PASSWORD" \
    --single-transaction school_tickets > backup-$(date +%F).sql
docker run --rm -v school-tickets_media:/media -v "$PWD":/out alpine \
    tar czf /out/media-$(date +%F).tar.gz -C /media .
```

A database dump without the photos is not a backup of this application: a
ticket whose photo is gone has lost the part a pupil actually looked at.

## Behind your own TLS

`nginx` publishes on `127.0.0.1:8080` and speaks plain HTTP. It is meant to sit
behind your own reverse proxy, which terminates TLS -- the application is
reachable from outside the school (pupils read on the bus), so TLS is not
optional.

**Known limitation, and it will bite you today:** the application does not yet
read `SECURE_PROXY_SSL_HEADER` or `CSRF_TRUSTED_ORIGINS` from its environment.
Behind an HTTPS proxy, Django compares the browser's `https://` origin against
a request it believes to be `http://`, and **every form submission answers
403** -- opening a ticket, commenting, logging in. Three lines in
`school_tickets/settings.py` fix it for both channels, the Debian package
included. Until they are there, this channel is usable over plain HTTP on a
trusted network only.

## Everyday commands

```sh
docker compose logs -f worker            # the estate sweeps and the Push sends
docker compose run --rm web manage <cmd> # enroll, set_password, sync_parc, vapid_keys...
docker compose up -d --build             # upgrade: rebuild, restart, migrate on start
docker compose down                      # stop, keep the data
```

Migrations run automatically when the `web` container starts, and only there:
two processes migrating the same database at once is how a half-applied
migration happens. The worker waits for the schema before it starts.

## What this channel does not do

- **No TLS**, on purpose: see above.
- **No OIDC yet.** Accounts sign in with a local password (D-26), which is a
  stopgap. Keycloak is not wired up in either channel.
- **lmnapi is wired, but nothing shows it.** The worker syncs the estate and
  sweeps LINBO once `ST_LMNAPI_BASE_URL` and `ST_LMNAPI_HOST_KEY` are set;
  `sync_parc --from-file` still imports a sophomorix `devices.csv` for an
  instance out of reach of the server. What it collects is visible in
  `/admin` only.
