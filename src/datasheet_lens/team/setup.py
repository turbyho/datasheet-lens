"""dslens-server setup: install the team server on a Linux machine with systemd
(Debian or Ubuntu), adapted from the setup wizard of fw-context.

Each step finds the current state first, and asks only when it must change
something. A second run changes nothing that is correct, and makes no new
secrets: it reads them from the settings file. The steps:

1. the server venv /opt/dslens-server/venv (the package from a wheel or PyPI)
2. PostgreSQL: the role and the database dslens
3. Garage: the binary (SHA-256 in this file), its user, configuration, systemd
   unit, layout, bucket and access key
4. the settings file (ENV_FILE) and the systemd unit of the API
5. nginx: the API and the S3 endpoint on two subdomains, one Let's Encrypt
   certificate (certbot webroot: the renewal works with the final configuration)
6. the first admin account and token, and a test through the public HTTPS URLs:
   upload, presigned PUT and GET, removal of the test file
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from ..errors import LensError
from .config import ENV_FILE, parse_env, read_env_file
from .cli import DEFAULT_PORT

GARAGE_VERSION = "v2.5.0"
GARAGE_URL = f"https://garagehq.deuxfleurs.fr/_releases/{GARAGE_VERSION}/x86_64-unknown-linux-musl/garage"
# Garage publishes no checksum. This is the SHA-256 of the official build, and the
# same as the binary of the Docker image dxflrs/garage:v2.5.0 (two sources).
GARAGE_SHA256 = "2ff667286217cd73ebcb1c03af993822365000441dfe31a80cb64c5189a9dea8"
GARAGE_BIN = Path("/usr/local/bin/garage")
GARAGE_CONF = Path("/etc/garage.toml")
GARAGE_LIB = Path("/var/lib/garage")
GARAGE_USER = "garage"
S3_PORT, RPC_PORT, GARAGE_ADMIN_PORT = 3900, 3901, 3903

OPT = Path("/opt/dslens-server")
VENV = OPT / "venv"
SERVICE_USER = "dslens"
DB_NAME = DB_USER = "dslens"
BUCKET = "dslens"
S3_KEY_NAME = "dslens-server"
SYSTEMD = Path("/etc/systemd/system")
NGINX_SITE = Path("/etc/nginx/sites-available/dslens")
NGINX_ENABLED = Path("/etc/nginx/sites-enabled/dslens")
NGINX_RATE = Path("/etc/nginx/conf.d/dslens-rate-limit.conf")
ACME_ROOT = Path("/var/www/dslens-acme")
DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


def check_domain(domain: str) -> str:
    """A DNS name (it goes into the nginx configuration and into commands)."""
    domain = domain.strip().lower()
    if not DOMAIN_RE.match(domain):
        raise LensError(f"not a DNS name: {domain!r}")
    return domain


# The generated files (the tests compare them)

def garage_toml(rpc_secret: str, admin_token: str) -> str:
    return f"""# Garage object store of the datasheet-lens team server (dslens-server setup).
# One node; all ports only on the loopback (nginx gives the S3 API to the clients).
metadata_dir = "{GARAGE_LIB}/meta"
data_dir = "{GARAGE_LIB}/data"
db_engine = "sqlite"
metadata_fsync = true
data_fsync = true
metadata_auto_snapshot_interval = "6h"
replication_factor = 1
rpc_bind_addr = "127.0.0.1:{RPC_PORT}"
rpc_public_addr = "127.0.0.1:{RPC_PORT}"
rpc_secret = "{rpc_secret}"

[s3_api]
s3_region = "garage"
api_bind_addr = "127.0.0.1:{S3_PORT}"

[admin]
api_bind_addr = "127.0.0.1:{GARAGE_ADMIN_PORT}"
admin_token = "{admin_token}"
"""


def garage_unit() -> str:
    return f"""[Unit]
Description=Garage object store (datasheet-lens team server)
After=network-online.target
Wants=network-online.target

[Service]
User={GARAGE_USER}
Group={GARAGE_USER}
ExecStart={GARAGE_BIN} -c {GARAGE_CONF} server
Environment=RUST_LOG=garage=info
Restart=always
RestartSec=5
LimitNOFILE=42000
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
NoNewPrivileges=yes
ReadWritePaths={GARAGE_LIB}

[Install]
WantedBy=multi-user.target
"""


def server_unit(port: int = DEFAULT_PORT) -> str:
    return f"""[Unit]
Description=datasheet-lens team server (API)
After=network-online.target postgresql.service garage.service
Wants=network-online.target garage.service

[Service]
User={SERVICE_USER}
Group={SERVICE_USER}
EnvironmentFile={ENV_FILE}
ExecStart={VENV}/bin/dslens-server run --port {port}
Restart=always
RestartSec=5
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
"""


def env_text(values: dict[str, str]) -> str:
    lines = ["# Settings of the datasheet-lens team server (dslens-server setup). Secret: mode 0640."]
    lines += [f'{name}="{value}"' for name, value in values.items()]
    return "\n".join(lines) + "\n"


def nginx_http(api: str, s3: str) -> str:
    """Port 80 only: the ACME challenge, and a redirect to HTTPS."""
    return f"""# datasheet-lens team server (dslens-server setup)
server {{
    listen 80;
    listen [::]:80;
    server_name {api} {s3};
    location /.well-known/acme-challenge/ {{
        root {ACME_ROOT};
    }}
    location / {{
        return 301 https://$host$request_uri;
    }}
}}
"""


def nginx_site(api: str, s3: str, port: int = DEFAULT_PORT) -> str:
    """The API and the S3 endpoint, with one certificate for the two names. The
    S3 server keeps the Host header: it is part of the signature of a presigned
    URL. It streams the files (no buffer, no size limit of nginx)."""
    tls = f"""    ssl_certificate     /etc/letsencrypt/live/{api}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{api}/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;"""
    proxy = """        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;"""
    return nginx_http(api, s3) + f"""
upstream dslens_api {{
    server 127.0.0.1:{port};
    keepalive 16;
}}

upstream dslens_s3 {{
    server 127.0.0.1:{S3_PORT};
    keepalive 16;
}}

server {{
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name {api};
{tls}
    limit_req zone=dslens_api burst=40 nodelay;
    client_max_body_size 1m;
    location / {{
        proxy_pass http://dslens_api;
{proxy}
        proxy_read_timeout 300s;
    }}
}}

server {{
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name {s3};
{tls}
    limit_req zone=dslens_s3 burst=100 nodelay;
    client_max_body_size 0;
    proxy_request_buffering off;
    proxy_buffering off;
    location / {{
        proxy_pass http://dslens_s3;
{proxy}
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }}
}}
"""


NGINX_RATE_TEXT = ("limit_req_zone $binary_remote_addr zone=dslens_api:10m rate=20r/s;\n"
                   "limit_req_zone $binary_remote_addr zone=dslens_s3:10m rate=50r/s;\n")


class Wizard:
    def __init__(self, yes: bool = False, wheel: str = "", api: str = "", s3: str = "", email: str = "",
                 capacity: str = "", admin: str = "admin"):
        self.yes, self.wheel, self.email, self.capacity, self.admin = yes, wheel, email, capacity, admin
        self.api, self.s3 = api, s3
        self.env: dict[str, str] = {}
        self.upgraded = False  # a new package in the server venv: restart the API
        self.settings_changed = False

    # Helpers

    def say(self, mark: str, text: str) -> None:
        print(f"  [{mark}] {text}", flush=True)

    def ask(self, question: str, default: bool = True) -> bool:
        if self.yes:
            print(f"  > {question} {'yes' if default else 'no'} (--yes)")
            return default
        while True:
            try:
                answer = input(f"  > {question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                raise LensError("setup stopped") from None
            if not answer:
                return default
            if answer in ("y", "yes", "n", "no"):
                return answer.startswith("y")

    def value(self, question: str, default: str = "") -> str:
        if self.yes:
            if not default:
                raise LensError(f"{question}: no default value; give it as an option")
            return default
        try:
            answer = input(f"  > {question}{f' [{default}]' if default else ''}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            raise LensError("setup stopped") from None
        return answer or default

    def run(self, cmd: list[str], *, input: str | None = None, check: bool = True,
            capture: bool = False, timeout: int = 600) -> subprocess.CompletedProcess:
        """Run a command. A sudo command runs in /tmp (root may not read the
        current folder). The input goes through stdin: secrets are not in ps."""
        cwd = tempfile.gettempdir() if cmd[0] == "sudo" else None
        result = subprocess.run(cmd, input=input, text=True, capture_output=capture or input is not None,
                                timeout=timeout, cwd=cwd)
        if check and result.returncode != 0:
            raise LensError(f"command failed ({result.returncode}): {' '.join(cmd)}\n"
                            f"{(result.stderr or '').strip()}")
        return result

    def write(self, path: Path, text: str, mode: str = "0644", owner: str = "root:root") -> None:
        self.run(["sudo", "install", "-m", mode, "-o", owner.split(":")[0], "-g", owner.split(":")[1],
                  "/dev/stdin", str(path)], input=text)

    def exists(self, path: Path) -> bool:
        return self.run(["sudo", "test", "-e", str(path)], check=False).returncode == 0

    def psql(self, sql: str) -> str:
        return self.run(["sudo", "-u", "postgres", "psql", "-tAq", "-v", "ON_ERROR_STOP=1"], input=sql).stdout.strip()

    def garage(self, *args: str) -> str:
        return self.run(["sudo", "-u", GARAGE_USER, str(GARAGE_BIN), "-c", str(GARAGE_CONF), *args],
                        capture=True).stdout

    def garage_has(self, kind: str, name: str) -> bool:
        """True when Garage has the bucket or the key (exit code of info)."""
        return self.run(["sudo", "-u", GARAGE_USER, str(GARAGE_BIN), "-c", str(GARAGE_CONF), kind, "info", name],
                        check=False, capture=True).returncode == 0

    def system_user(self, name: str, home: Path) -> None:
        if self.run(["id", "-u", name], check=False, capture=True).returncode != 0:
            self.run(["sudo", "useradd", "--system", "--home-dir", str(home), "--no-create-home",
                      "--shell", shutil.which("nologin") or "/usr/sbin/nologin", name])

    def port_free(self, port: int) -> bool:
        with socket.socket() as s:
            return s.connect_ex(("127.0.0.1", port)) != 0

    def unit(self, name: str, text: str, restart: bool = False) -> None:
        path = SYSTEMD / f"{name}.service"
        old = self.run(["sudo", "cat", str(path)], check=False, capture=True).stdout
        if old != text:
            self.write(path, text)
            self.run(["sudo", "systemctl", "daemon-reload"])
        self.run(["sudo", "systemctl", "enable", name], capture=True)
        if old != text or restart:
            self.run(["sudo", "systemctl", "restart", name])
        else:
            self.run(["sudo", "systemctl", "start", name])

    # Steps

    def preflight(self) -> None:
        if sys.platform != "linux" or not Path("/run/systemd/system").is_dir():
            raise LensError("the setup needs Linux with systemd")
        if not shutil.which("apt-get"):
            raise LensError("the setup needs Debian or Ubuntu (apt-get); install the parts by hand on others")
        # sudo -v can ask for a password also when the commands need none (verifypw):
        # ask only when a command needs one.
        if self.run(["sudo", "-n", "true"], check=False, capture=True).returncode != 0:
            self.run(["sudo", "-v"])
        self.say("✓", "Linux, systemd, apt, sudo")
        try:
            self.env = read_env_file(ENV_FILE)
        except FileNotFoundError:
            self.env = {}
        except PermissionError:
            text = self.run(["sudo", "cat", str(ENV_FILE)], check=False, capture=True).stdout
            self.env = parse_env(text)
        if self.env:
            self.say("✓", f"settings {ENV_FILE}: the secrets stay")

    def domains(self) -> None:
        public = self.env.get("DSLENS_S3_PUBLIC_URL", "")
        self.api = check_domain(self.api or self.env.get("DSLENS_API_DOMAIN", "") or self.value(
            "Domain of the API (for example dslens.example.com)"))
        self.s3 = check_domain(self.s3 or public.removeprefix("https://") or self.value(
            "Domain of the S3 endpoint", f"s3.{self.api}"))
        for name in (self.api, self.s3):
            try:
                socket.getaddrinfo(name, 443)
                self.say("✓", f"DNS: {name}")
            except socket.gaierror:
                self.say("!", f"DNS: {name} has no address (the certificate needs it)")
                if not self.ask("Continue?", default=False):
                    raise LensError(f"make a DNS record for {name}, then run the setup again")

    def venv(self) -> None:
        from .. import __version__

        if (VENV / "bin" / "dslens-server").exists():
            installed = self.run([str(VENV / "bin" / "python"), "-c",
                                  "import datasheet_lens; print(datasheet_lens.__version__)"], capture=True).stdout.strip()
            self.say("✓", f"server venv {VENV} (datasheet-lens {installed})")
            if not self.wheel or not self.ask(f"Install {Path(self.wheel).name} into it?"):
                return
        else:
            self.say("!", f"server venv {VENV}: not there")
            if not self.ask(f"Make it (datasheet-lens {__version__} with the extra server)?"):
                raise LensError("the server venv is necessary")
            self.run(["sudo", "mkdir", "-p", str(OPT)])
            self.run(["sudo", sys.executable, "-m", "venv", str(VENV)])
        pip = ["sudo", str(VENV / "bin" / "pip"), "install", "--quiet"]
        if self.wheel:
            wheel = Path(self.wheel).resolve()
            self.run([*pip, "--upgrade", f"{wheel}[server]"])
            # pip keeps an installed package of the same version: install the code of the wheel.
            self.run([*pip, "--force-reinstall", "--no-deps", str(wheel)])
        else:
            self.run([*pip, "--upgrade", f"datasheet-lens[server]=={__version__}"])
        self.upgraded = True
        self.say("✓", f"server venv {VENV}")

    def postgresql(self) -> None:
        running = bool(shutil.which("pg_isready")) and self.run(["pg_isready", "-q"], check=False).returncode == 0
        if not running:
            self.say("!", "PostgreSQL: not running")
            if not self.ask("Install PostgreSQL (apt)?"):
                raise LensError("PostgreSQL is necessary")
            self.run(["sudo", "apt-get", "install", "-y", "postgresql"])
            self.run(["sudo", "systemctl", "enable", "--now", "postgresql"])
        self.say("✓", "PostgreSQL: running")
        role = self.psql(f"SELECT 1 FROM pg_roles WHERE rolname = '{DB_USER}'") == "1"
        if not role or "DSLENS_DB_URL" not in self.env:
            what = "reset the password of" if role else "make"
            if not self.ask(f"PostgreSQL: {what} the role {DB_USER}?"):
                raise LensError("the database role is necessary")
            password = secrets.token_urlsafe(24)  # only URL-safe characters: no quoting in the URL
            self.psql(f"{'ALTER' if role else 'CREATE'} ROLE {DB_USER} LOGIN PASSWORD '{password}'")
            self.env["DSLENS_DB_URL"] = f"postgresql://{DB_USER}:{password}@localhost:5432/{DB_NAME}"
        if self.psql(f"SELECT 1 FROM pg_database WHERE datname = '{DB_NAME}'") != "1":
            if not self.ask(f"PostgreSQL: make the database {DB_NAME}?"):
                raise LensError("the database is necessary")
            self.psql(f"CREATE DATABASE {DB_NAME} OWNER {DB_USER}")
        self.say("✓", f"PostgreSQL: role and database {DB_NAME}")

    def garage_binary(self) -> None:
        if GARAGE_BIN.exists():
            digest = hashlib.sha256(GARAGE_BIN.read_bytes()).hexdigest()
            if digest == GARAGE_SHA256:
                self.say("✓", f"Garage {GARAGE_VERSION}: {GARAGE_BIN}")
                return
            self.say("!", f"Garage: {GARAGE_BIN} is not the build {GARAGE_VERSION} (SHA-256 {digest[:12]}…)")
            if not self.ask(f"Replace it with {GARAGE_VERSION}?", default=False):
                raise LensError(f"the setup needs Garage {GARAGE_VERSION}")
        elif not self.ask(f"Garage: download {GARAGE_VERSION} and install it to {GARAGE_BIN}?"):
            raise LensError("Garage is necessary")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "garage"
            with urllib.request.urlopen(GARAGE_URL, timeout=120) as response, target.open("wb") as f:
                shutil.copyfileobj(response, f)
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            if digest != GARAGE_SHA256:
                raise LensError(f"the downloaded Garage has the SHA-256 {digest}, not {GARAGE_SHA256}: not installed")
            self.run(["sudo", "install", "-m", "0755", str(target), str(GARAGE_BIN)])
        self.say("✓", f"Garage {GARAGE_VERSION}: {GARAGE_BIN} (SHA-256 correct)")

    def garage_service(self) -> None:
        self.system_user(GARAGE_USER, GARAGE_LIB)
        for sub in ("meta", "data"):
            self.run(["sudo", "install", "-d", "-m", "0700", "-o", GARAGE_USER, "-g", GARAGE_USER,
                      str(GARAGE_LIB / sub)])
        if not self.exists(GARAGE_CONF):
            for port in (S3_PORT, RPC_PORT, GARAGE_ADMIN_PORT):
                if not self.port_free(port):
                    raise LensError(f"port {port} is in use: Garage needs it")
            self.write(GARAGE_CONF, garage_toml(secrets.token_hex(32), secrets.token_urlsafe(32)),
                       mode="0640", owner=f"root:{GARAGE_USER}")
        self.unit("garage", garage_unit())
        for _ in range(30):
            if not self.port_free(RPC_PORT):
                break
            time.sleep(1)
        status = self.garage("status")
        if "NO ROLE ASSIGNED" in status:
            node = self.garage("node", "id", "-q").strip().split("@")[0]
            free = shutil.disk_usage(GARAGE_LIB.parent).free
            capacity = self.capacity or self.value("Capacity of Garage (for example 50G)", f"{int(free * 0.8) >> 30}G")
            if not self.ask(f"Garage: give this node the capacity {capacity}?"):
                raise LensError("Garage needs a layout")
            self.garage("layout", "assign", "-z", "dc1", "-c", capacity, node)
            version = int(re.search(r"layout version: (\d+)", self.garage("layout", "show")).group(1))
            self.garage("layout", "apply", "--version", str(version + 1))
        self.say("✓", "Garage: service and layout")
        if not self.garage_has("bucket", BUCKET):
            self.garage("bucket", "create", BUCKET)
        if not self.garage_has("key", S3_KEY_NAME):
            self.garage("key", "create", S3_KEY_NAME)
        info = self.garage("key", "info", "--show-secret", S3_KEY_NAME)
        self.env["DSLENS_S3_KEY_ID"] = re.search(r"Key ID:\s*(\S+)", info).group(1)
        self.env["DSLENS_S3_SECRET"] = re.search(r"Secret key:\s*(\S+)", info).group(1)
        self.garage("bucket", "allow", "--read", "--write", "--owner", BUCKET, "--key", S3_KEY_NAME)
        self.say("✓", f"Garage: bucket {BUCKET}, key {S3_KEY_NAME}")

    def settings(self) -> None:
        self.system_user(SERVICE_USER, OPT)
        self.env.update({
            "DSLENS_API_DOMAIN": self.api,
            "DSLENS_S3_ENDPOINT": f"http://127.0.0.1:{S3_PORT}",
            "DSLENS_S3_PUBLIC_URL": f"https://{self.s3}",
            "DSLENS_S3_REGION": "garage",
            "DSLENS_S3_BUCKET": BUCKET,
        })
        self.run(["sudo", "install", "-d", "-m", "0750", "-o", "root", "-g", SERVICE_USER, str(ENV_FILE.parent)])
        old = self.run(["sudo", "cat", str(ENV_FILE)], check=False, capture=True).stdout
        self.settings_changed = old != env_text(self.env)
        if self.settings_changed:
            self.write(ENV_FILE, env_text(self.env), mode="0640", owner=f"root:{SERVICE_USER}")
        self.say("✓", f"settings {ENV_FILE} (0640 root:{SERVICE_USER})")

    def server_service(self) -> None:
        if not self.exists(SYSTEMD / "dslens-server.service") and not self.port_free(DEFAULT_PORT):
            raise LensError(f"port {DEFAULT_PORT} is in use: the API needs it")
        self.unit("dslens-server", server_unit(), restart=self.upgraded or self.settings_changed)
        for _ in range(30):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{DEFAULT_PORT}/health", timeout=2) as r:
                    if json.load(r).get("status") == "ok":
                        break
            except (OSError, ValueError):  # not started yet, or a different answer
                pass
            time.sleep(1)
        else:
            raise LensError(f"the API does not answer on port {DEFAULT_PORT}: see journalctl -u dslens-server")
        self.say("✓", f"API: dslens-server.service on 127.0.0.1:{DEFAULT_PORT}")

    def nginx(self) -> None:
        if not shutil.which("nginx"):
            if not self.ask("nginx: install it (apt)?"):
                raise LensError("nginx is necessary")
            self.run(["sudo", "apt-get", "install", "-y", "nginx"])
        if not shutil.which("certbot"):
            if not self.ask("certbot: install it (apt)?"):
                raise LensError("certbot is necessary for the certificate")
            self.run(["sudo", "apt-get", "install", "-y", "certbot"])
        self.run(["sudo", "install", "-d", "-m", "0755", str(ACME_ROOT)])
        self.write(NGINX_RATE, NGINX_RATE_TEXT)
        certificate = Path(f"/etc/letsencrypt/live/{self.api}/fullchain.pem")
        names = self.run(["sudo", "openssl", "x509", "-noout", "-ext", "subjectAltName", "-in", str(certificate)],
                         check=False, capture=True).stdout
        if f"DNS:{self.api}" not in names or f"DNS:{self.s3}" not in names:
            if not self.ask(f"Let's Encrypt: get a certificate for {self.api} and {self.s3}?"):
                raise LensError("the HTTPS certificate is necessary")
            self.write(NGINX_SITE, nginx_http(self.api, self.s3))
            self.nginx_reload()
            email = self.email or ("" if self.yes else self.value("E-mail for Let's Encrypt (empty: none)", ""))
            self.run(["sudo", "certbot", "certonly", "--webroot", "-w", str(ACME_ROOT), "--cert-name", self.api,
                      "-d", self.api, "-d", self.s3, "--non-interactive", "--agree-tos", "--keep-until-expiring",
                      "--expand", *(["-m", email] if email else ["--register-unsafely-without-email"])])
        self.write(NGINX_SITE, nginx_site(self.api, self.s3))
        self.nginx_reload()
        self.say("✓", f"nginx: https://{self.api} and https://{self.s3}")

    def nginx_reload(self) -> None:
        self.run(["sudo", "ln", "-sfn", str(NGINX_SITE), str(NGINX_ENABLED)])
        self.run(["sudo", "nginx", "-t"], capture=True)
        self.run(["sudo", "systemctl", "reload", "nginx"])

    def admin_account(self) -> str:
        admin_cmd = ["sudo", str(VENV / "bin" / "dslens-admin")]
        listing = self.run([*admin_cmd, "account", "ls"], capture=True).stdout
        if any(line.split()[:2] == [self.admin, "admin"] for line in listing.splitlines()):
            self.say("✓", f"admin account {self.admin}")
            return ""
        if not self.ask(f"Make the admin account {self.admin} with a token?"):
            return ""
        out = self.run([*admin_cmd, "account", "add", self.admin, "-r", "admin", "-d", "setup"], capture=True).stdout
        token = re.search(r"dsl_[0-9a-f]{64}", out).group(0)
        self.say("✓", f"admin account {self.admin}")
        return token

    def self_test(self, token: str) -> None:
        """Through the public HTTPS URLs: the API, a presigned PUT and GET (nginx,
        TLS, Host header), then the removal of the test file."""
        if not token:
            self.say("-", "test through HTTPS: skipped (no new token; run dslens-server test with a token)")
            return
        sha256 = public_test(f"https://{self.api}", token)
        self.run(["sudo", str(VENV / "bin" / "dslens-admin"), "blob", "rm", sha256], capture=True)
        self.say("✓", "test through HTTPS: upload, presigned PUT and GET, removal")


TEST_PREFIX = b"dslens setup test "


def _request(method: str, url: str, token: str = "", body: bytes | None = None,
             headers: dict | None = None) -> tuple[int, bytes]:
    headers = dict(headers or {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def public_test(api_url: str, token: str) -> str:
    """Upload a test file through the API and the presigned URLs, and download
    it again. Give its SHA-256 (remove it with dslens-admin blob rm). A
    LensError for each step that fails."""
    data = TEST_PREFIX + secrets.token_bytes(64 << 10)
    info = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "media_type": "application/pdf"}
    status, body = _request("GET", f"{api_url}/v1/account", token)
    if status != 200:
        raise LensError(f"GET /v1/account: {status} {body[:200]!r}")
    json_headers = {"Content-Type": "application/json"}
    status, body = _request("POST", f"{api_url}/v1/blobs", token, json.dumps(info).encode(), json_headers)
    if status != 200:
        raise LensError(f"POST /v1/blobs: {status} {body[:200]!r}")
    answer = json.loads(body)
    status, body = _request("PUT", answer["url"], body=data, headers=answer["headers"])
    if status != 200:
        raise LensError(f"presigned PUT through the S3 endpoint: {status} {body[:300]!r}")
    status, body = _request("POST", f"{api_url}/v1/blobs/{info['sha256']}/done", token, json.dumps(info).encode(),
                            json_headers)
    if status != 200:
        raise LensError(f"POST done: {status} {body[:200]!r}")
    status, body = _request("GET", f"{api_url}/v1/blobs/{info['sha256']}", token)
    if status != 200:
        raise LensError(f"GET /v1/blobs: {status} {body[:200]!r}")
    status, body = _request("GET", json.loads(body)["url"])
    if status != 200 or body != data:
        raise LensError(f"presigned GET through the S3 endpoint: {status}, {len(body)} bytes")
    return info["sha256"]


def wizard(args) -> int:
    w = Wizard(yes=args.yes, wheel=args.wheel or "", api=args.api or "", s3=args.s3 or "", email=args.email or "",
               capacity=args.capacity or "", admin=args.admin)
    print("\n  datasheet-lens team server: setup\n")
    w.preflight()
    w.domains()
    w.venv()
    w.postgresql()
    w.garage_binary()
    w.garage_service()
    w.settings()
    w.server_service()
    w.nginx()
    token = w.admin_account()
    w.self_test(token)
    print(f"\n  Done. API: https://{w.api}  S3: https://{w.s3}")
    if token:
        print(f"  Token of the admin account {w.admin} (shown one time):\n    {token}")
    print("  Accounts and tokens: sudo dslens-admin account add NAME (see dslens-admin --help)\n")
    return 0
