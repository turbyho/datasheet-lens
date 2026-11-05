# Team server

The team server keeps one shared store for the datasheets of a team. It keeps only
data: PostgreSQL for the rows, and an S3 object store (Garage) for the files. The
clients do all processing.

The server has the accounts, the tokens, the files, and the shared tables: parts,
documents, links, facts and the relations of manufacturer names. Each person keeps a
local store: the MCP server and the CLI read it, also with no connection. The store
synchronizes with the server (see [Connect a store](#connect-a-store)).

## Parts of the server

| Part | Function | Address |
|---|---|---|
| `dslens-server run` | The API (FastAPI), as the systemd unit `dslens-server` | `127.0.0.1:8100` |
| PostgreSQL | The database `dslens`: accounts, tokens, files | `localhost:5432` |
| Garage | The object store: the files, as the systemd unit `garage` | `127.0.0.1:3900` (S3), `3901` (RPC), `3903` (admin) |
| nginx | HTTPS for the API and for the S3 endpoint, on two subdomains | ports 80 and 443 |

A client never gets the access key of the object store. The API gives it a presigned
URL for each upload and download. A presigned upload signs the SHA-256 and the size
of the file (of each part for a multipart upload): Garage rejects other bytes and a
different size. Thus a client cannot put other content under the key of a file, and
cannot go past the size limit of the server. The nginx server of the S3 endpoint keeps the `Host` header, because the
host is part of the signature of a presigned URL.

## Requirements

- Linux with systemd, Debian or Ubuntu (the wizard uses `apt-get`), x86-64.
- Python 3.11 or newer, and an account with `sudo`. Ubuntu 22.04 has only 3.10 and a
  release candidate of 3.11 (3.11.0rc1): install `python3.12` and `python3.12-venv`
  from the deadsnakes PPA, and use `python3.12` in the steps below.
- Two DNS names for the server, for example `dslens.example.com` (API) and
  `s3.dslens.example.com` (S3 endpoint). Let's Encrypt must reach the server on
  port 80 at these names.
- Free ports: 8100, 3900, 3901 and 3903 on the loopback.

## Install with the setup wizard

The wizard does each step only when it is necessary, and asks before each change.
A second run changes nothing that is correct, and makes no new secrets.

1. Make a temporary virtual environment, and install the package with the extra
   `server`:

   ```sh
   python3.12 -m venv ~/dslens-setup
   ~/dslens-setup/bin/pip install 'datasheet-lens[server]'
   ```

   To install a version that is not on PyPI, give the wheel:
   `~/dslens-setup/bin/pip install './datasheet_lens-VERSION-py3-none-any.whl[server]'`.

2. Run the wizard:

   ```sh
   ~/dslens-setup/bin/dslens-server setup --api dslens.example.com
   ```

   For a wheel, add `--wheel ./datasheet_lens-VERSION-py3-none-any.whl`. Other
   options: `--s3` (default `s3.` and the API domain), `--email` (for Let's
   Encrypt), `--capacity` (the capacity of Garage, for example `50G`), `--admin`
   (the name of the first admin account), `--yes` (the default answer to each
   question).

3. Keep the token of the admin account that the wizard shows at the end. The server
   cannot show it again.

4. Remove the temporary virtual environment: `rm -r ~/dslens-setup`.

The wizard does these steps:

1. It makes the server venv `/opt/dslens-server/venv`.
2. It installs PostgreSQL when it is not there, and makes the role and the database
   `dslens`.
3. It downloads Garage to `/usr/local/bin/garage`, and compares its SHA-256 with the
   value in the code. It makes the user `garage`, the configuration
   `/etc/garage.toml`, the data folder `/var/lib/garage`, the systemd unit, the
   layout, the bucket `dslens` and the access key `dslens-server`.
4. It writes the settings file `/var/lib/dslens-server/server.env` (mode 0640,
   `root:dslens`), and the systemd unit `dslens-server`.
5. It writes the nginx site `/etc/nginx/sites-available/dslens` and the rate limits
   `/etc/nginx/conf.d/dslens-rate-limit.conf`. It gets one Let's Encrypt
   certificate for the two names (certbot with the webroot `/var/www/dslens-acme`:
   the automatic renewal uses the same method).
6. It makes the first admin account with a token. Then it uploads and downloads a
   test file through the public HTTPS URLs, and removes the file.

## Accounts and tokens

Run `dslens-admin` on the server with `sudo`: it reads the settings file, and
connects directly to the database. The access to the settings file is the
permission.

```sh
sudo /opt/dslens-server/venv/bin/dslens-admin account add alice -r contributor -n "Alice Example" -d laptop
sudo /opt/dslens-server/venv/bin/dslens-admin account ls
sudo /opt/dslens-server/venv/bin/dslens-admin account set alice -r reviewer
sudo /opt/dslens-server/venv/bin/dslens-admin account set alice --active off
sudo /opt/dslens-server/venv/bin/dslens-admin token add alice -d workstation --days 365
sudo /opt/dslens-server/venv/bin/dslens-admin token ls alice
sudo /opt/dslens-server/venv/bin/dslens-admin token rm 7
```

- `account add` also makes the first token of the account. `--no-token` makes no
  token.
- A token is `dsl_` and 64 hex digits. The database keeps only its SHA-256. The
  command shows the token one time: give it to the person through a safe channel.
- Make one token for each computer of a person. Then you can revoke the token of one
  computer.
- On the computer of the person, save the token with `datasheet-lens key team`: it
  goes into the key store of the system (or `keys.toml`, mode 0600). For CI, use the
  environment variable `DATASHEET_LENS_TEAM_TOKEN`.
- A disabled account, a revoked token and an expired token get HTTP 401.

The roles:

| Role | Permissions |
|---|---|
| `reader` | Read the files. |
| `contributor` | The permissions of `reader`, and upload files. |
| `reviewer` | The permissions of `contributor`. (The review of facts comes with the synchronization.) |
| `admin` | The permissions of `reviewer`, and the uploads of other accounts. |

Other commands:

| Command | Function |
|---|---|
| `dslens-admin stats` | The number of accounts and files, and the size of the files. |
| `dslens-admin blob rm SHA256` | Remove a file from the server (a wrong or a test file). |
| `dslens-admin cleanup` | Remove the multipart uploads with no completion after 24 hours, and the uploads and staging files that the database does not know after one hour. The server also does this each hour. |
| `dslens-server check` | Test the settings: the database and the object store. |
| `dslens-server test URL` | Upload and download a test file through the public URLs. The token comes from `DSLENS_TOKEN`, or the command asks for it (a token on the command line is in `ps` and in the shell history). Then remove the file with `dslens-admin blob rm`. |

## Connect a store

On the computer of each person:

1. Get a token from the administrator of the server, and save it:

   ```sh
   datasheet-lens key team
   ```

2. Connect the store to the server:

   ```sh
   datasheet-lens server connect https://dslens.example.com
   ```

   The first sync sends all parts, documents (with their files), links, facts and
   relations of manufacturer names of the local store to the server, and gets the rows
   of the server.

WARNING: THE FIRST START OF THIS VERSION CHANGES THE SCHEMA OF THE LOCAL STORE (VERSION
3). AN OLDER VERSION OF DATASHEET-LENS CANNOT OPEN IT AFTER THIS. MAKE A BACKUP OF
`db/lens.db` OF THE STORE BEFORE THE UPDATE.

| Command | Function |
|---|---|
| `datasheet-lens server connect URL` | Connect the store, and do the first sync. |
| `datasheet-lens server sync` | Send the local changes, then get the changes of the server. |
| `datasheet-lens server` | Show the state: the server, the last sync, the local changes that wait, an error, and the notes (conflicts, refused changes). |
| `datasheet-lens server disconnect` | Stop the synchronization. The local rows stay; the local changes that the server did not get are not sent. |

How the synchronization works:

- Each local change of a shared row goes into a queue (the outbox) in the same
  transaction. A sync sends the queue, then gets the changes of the server.
- The MCP server syncs at its start and each 10 minutes. `project_check` gets the
  changes of the server before it compares the parts (no upload in a tool call): a
  datasheet that a different person added is then found. With no connection, the tools work with the local store, and tell when
  the last sync was.
- The rows of all documents come to each store, but not the files. A file comes when
  a tool needs it (for example a page image), and `project_sync` gets the files of the
  documents of the project. Thus a new computer gets the data of a project with one
  `git clone` of the project and one `project_sync`.
- A conflict (a change of a row that a different person changed first): the version of
  the server is kept, and the status tools tell it.
- Two persons add the same part (the same MPN and manufacturer): the server keeps the
  older part, and moves the links and facts of the other part to it. Each store then
  has one part.
- A file that is larger than 32 MiB goes as a multipart upload: after a stop of the
  connection, the next sync continues it.

### Bundles of derived data

The index and the element map of a document go to the server as bundles (gzip JSON).
Thus one store builds them, and the team has the same element IDs:

- When a tool needs the index or the element map of a document, the store first takes
  the canonical bundle of the server. It needs no PDF file for this, and no build.
- A store that builds the derived data itself uploads it at the next sync. The first
  bundle becomes the canonical bundle.
- For the element map, a bundle of docling comes before a bundle of the text backend:
  a docling bundle replaces a text bundle in each store, also when the store uses the
  text backend.
- A fact keeps the region (bbox) of its element. After a new element map, each store
  links its facts again by their regions. A fact whose element is not there any more
  keeps its page and region, and gets the note "element changed; check again".

## API

Each request, except `GET /health`, needs the header `Authorization: Bearer <token>`.
After 20 wrong tokens in 60 seconds from one address, the server answers 429 until
fewer failures are in the last 60 seconds. The address comes from the
`X-Forwarded-For` that nginx adds; the API uses it only for a connection from
`127.0.0.1` (option `--forwarded-allow-ips` of `dslens-server run`). The body of a
request is at most 1 MiB, with a `Content-Length`: the files go to the object store,
not through the API.

| Method and path | Role | Function |
|---|---|---|
| `GET /health` | none | `{"status": "ok", "version": ...}` |
| `GET /v1/account` | `reader` | The name and the role of the account, the version of the server and of its built-in manufacturer names. |
| `POST /v1/blobs` | `contributor` | Body `{sha256, size, media_type}`. `{"exists": true}`, or a presigned PUT: `url`, and `headers` that the client must send. |
| `POST /v1/blobs/{sha256}/done` | `contributor` | After the PUT: the server records the file. |
| `GET /v1/blobs/{sha256}` | `reader` | A presigned GET: `url`, `size`, `media_type`. |
| `POST /v1/uploads` | `contributor` | Start a multipart upload: `upload_id`, `part_size`, number of `parts`. |
| `POST /v1/uploads/{id}/urls` | owner | Body `{parts: [{number, sha256}]}`: a presigned PUT for each part, with the signed SHA-256 of the part. |
| `GET /v1/uploads/{id}` | owner | The parts that the object store has: continue after a stop. `completed: true`: the parts are joined, but the copy did not end; call `complete` again. |
| `POST /v1/uploads/{id}/complete` | owner | The server completes the upload, compares the SHA-256 of the full file, and records it. |
| `DELETE /v1/uploads/{id}` | owner | Stop the upload. |
| `GET /v1/sync/pull?cursor=C&after=P` | `reader` | A page (at most 1000 rows) of the changes after the cursor C, or all rows with no cursor. `more: true`: get the next page with `after`. The last page gives the new cursor. |
| `POST /v1/bundles` | `contributor` | Body `{document_sha, kind, format, backend, blob_sha, versions, created}`: record a bundle of derived data (its file goes first through `POST /v1/blobs`, media type `application/gzip`). Gives the canonical bundle. |
| `POST /v1/sync/push` | see below | Body `{changes: [...]}` (at most 500 rows of the outbox of a client). One result for each change: `applied`, `conflict` (with the row of the server), `merged` (with the gid of the kept part) or `rejected` (with the reason). |

The rules of a push:

- An insert needs the role `contributor`. A document needs its file on the server.
- An update of a part, a document or a fact needs the version that the client had;
  else the result is `conflict`. The size and the media type of a document do not
  change.
- A fact that is not `unchecked`, or a new status of a fact, needs the role
  `reviewer`.
- A delete of a part, a document, a fact or a relation needs the role `admin`.
- Two parts with the same MPN and the same manufacturer (registry of the server):
  the server keeps the older part (`created`, then gid), and moves the links and
  facts of the other part to it. A relation of manufacturer names can merge parts in
  the same way.
- The server applies one push at a time. A wrong change is `rejected`; the other
  changes of the push are applied.

Use the multipart upload on a slow or unstable connection, for example from home.
On the office network, one PUT is sufficient. The server takes a file of at most
2 GiB (`DSLENS_MAX_BLOB_SIZE`). A presigned URL is valid for 3600 seconds
(`DSLENS_URL_EXPIRY`); the object store answers 400 or 403 after that time, and the
client asks for a new URL.

## Settings

The systemd unit reads `/var/lib/dslens-server/server.env`:

| Variable | Content |
|---|---|
| `DSLENS_DB_URL` | `postgresql://dslens:PASSWORD@localhost:5432/dslens` |
| `DSLENS_S3_ENDPOINT` | The object store for the server: `http://127.0.0.1:3900` |
| `DSLENS_S3_PUBLIC_URL` | The object store for the clients: `https://s3.dslens.example.com` |
| `DSLENS_S3_KEY_ID`, `DSLENS_S3_SECRET` | The access key of the bucket |
| `DSLENS_S3_REGION`, `DSLENS_S3_BUCKET` | Default `garage` and `dslens` |
| `DSLENS_MAX_BLOB_SIZE` | Bytes; default 2 GiB, at most 5 GiB (S3 CopyObject) |
| `DSLENS_URL_EXPIRY` | Seconds; default 3600 |

## Upgrade

Install the new version into the server venv, then run the wizard again. It restarts
the API when the package or the settings changed:

```sh
~/dslens-setup/bin/dslens-server setup --api dslens.example.com --wheel ./datasheet_lens-NEW-py3-none-any.whl
```

## Backup

Make a backup of the two stores:

- The database: `sudo -u postgres pg_dump -Fc dslens > dslens.dump`
- The object store: Garage takes a snapshot of its metadata each 6 hours
  (`metadata_auto_snapshot_interval`). Copy `/var/lib/garage` with the files and the
  snapshots, or copy the bucket with an S3 tool (for example `rclone`).

WARNING: THE SETTINGS FILE AND `/etc/garage.toml` CONTAIN SECRETS. KEEP A BACKUP OF
THEM ONLY ON A SAFE MEDIUM.
