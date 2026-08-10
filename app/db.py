"""Database extension and canonical PostgreSQL routing shared by all consumers."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import atexit
import ipaddress
import os
from pathlib import Path
import stat
import tempfile
from urllib.parse import parse_qs, quote, urlencode, unquote, urlsplit, urlunsplit

from flask_sqlalchemy import SQLAlchemy


db = SQLAlchemy()


# libpq reads every PG* variable.  Clearing the whole prefix is safer than
# maintaining a list that can miss a newly added routing knob.
PG_ENV_PREFIX = "PG"
_PG_QUERY_KEYS = frozenset(
    {
        "application_name",
        "channel_binding",
        "connect_timeout",
        "gssencmode",
        "host",
        "hostaddr",
        "keepalives",
        "keepalives_count",
        "keepalives_idle",
        "keepalives_interval",
        "options",
        "port",
        "sslcert",
        "sslkey",
        "sslmode",
        "service",
        "servicefile",
        "sslrootcert",
        "target_session_attrs",
        "user",
        "password",
        "dbname",
    }
)
# Keep the public allowlist free of accidental Unicode or unsupported keys.
_PG_QUERY_KEYS = frozenset(key for key in _PG_QUERY_KEYS if key.isascii())
_ROUTING_QUERY_KEYS = frozenset({"host", "hostaddr", "port", "target_session_attrs", "service", "servicefile"})


@dataclass(frozen=True)
class PostgresRoute:
    """One credential-free route shared by SQLAlchemy, psycopg, and libpq."""

    sqlalchemy_url: str
    settings: tuple[tuple[str, str], ...]
    connect_kwargs: tuple[tuple[str, str], ...]
    _password: str | None = field(default=None, repr=False, compare=False)

    def environment(self) -> dict[str, str]:
        return dict(self.settings)

    def psycopg_kwargs(self) -> dict[str, str]:
        return dict(self.connect_kwargs)


def _reject_list(value: str, label: str) -> None:
    if "," in value or any(character.isspace() for character in value):
        raise RuntimeError(f"PostgreSQL URL has unsupported multi-{label} routing")


def _reject_authority_host(value: str) -> None:
    """Reject path/socket syntax in the URL authority; sockets use query host."""
    if not value or value in {".", ".."}:
        raise RuntimeError("PostgreSQL authority host is invalid")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value) or any(
        character in value for character in "/\\?#[%"
    ):
        raise RuntimeError("PostgreSQL authority host contains a reserved path/socket character")
    if ":" in value:
        try:
            ipaddress.ip_address(value)
        except ValueError as exc:
            raise RuntimeError("PostgreSQL authority host contains a reserved path/socket character") from exc
        return
    labels = value.split(".")
    if any(
        not label
        or label.startswith("-")
        or label.endswith("-")
        or not all(character.isascii() and (character.isalnum() or character == "-") for character in label)
        for label in labels
    ):
        raise RuntimeError("PostgreSQL authority host contains a reserved path/socket character")


def _reject_query_host(value: str) -> None:
    if not value or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise RuntimeError("PostgreSQL query host is invalid")
    if any(character in value for character in "\\?#%"):
        raise RuntimeError("PostgreSQL query host contains an unsafe character")
    if "/" in value and not value.startswith("/"):
        raise RuntimeError("PostgreSQL query host must use an absolute socket path")
    if any(part in {".", ".."} for part in value.split("/")):
        raise RuntimeError("PostgreSQL query host contains path traversal")
    if value == "/" or value.startswith("//"):
        raise RuntimeError("PostgreSQL query host is invalid")


def _query_value(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if not values:
        return None
    if len(values) != 1:
        raise RuntimeError(f"PostgreSQL URL has multiple values for {key}")
    value = values[0]
    if any(character in value for character in "\x00\r\n"):
        raise RuntimeError(f"PostgreSQL URL has an unsafe value for {key}")
    return value


def _route(value: object) -> PostgresRoute:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("DATABASE_URL is required")
    raw = value.strip()
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise RuntimeError("DATABASE_URL is invalid") from exc
    if parsed.scheme not in {"postgres", "postgresql", "postgresql+psycopg"}:
        raise RuntimeError("DATABASE_URL must use PostgreSQL")
    if parsed.fragment:
        raise RuntimeError("DATABASE_URL must not include a fragment")

    query = parse_qs(parsed.query, keep_blank_values=True)
    unknown = sorted(set(query) - _PG_QUERY_KEYS)
    if unknown:
        raise RuntimeError(f"DATABASE_URL has unsupported query parameter {unknown[0]}")
    if "service" in query or "servicefile" in query:
        raise RuntimeError("DATABASE_URL must not use PostgreSQL service or servicefile routing")

    try:
        authority_port = parsed.port
    except ValueError as exc:
        raise RuntimeError("DATABASE_URL has an invalid port") from exc
    authority_host = unquote(parsed.hostname) if parsed.hostname is not None else None
    if authority_host is not None:
        _reject_authority_host(authority_host)
        _reject_list(authority_host, "host")
    if authority_port is not None:
        _reject_list(str(authority_port), "port")
        if not 1 <= authority_port <= 65535:
            raise RuntimeError("DATABASE_URL has an invalid port")
    query_host = _query_value(query, "host")
    query_hostaddr = _query_value(query, "hostaddr")
    query_port = _query_value(query, "port")
    query_user = _query_value(query, "user")
    query_password = _query_value(query, "password")
    query_database = _query_value(query, "dbname")
    target_session_attrs = _query_value(query, "target_session_attrs")
    if target_session_attrs is not None and target_session_attrs.casefold() != "any":
        raise RuntimeError("DATABASE_URL must use target_session_attrs=any")
    if query_host is not None:
        _reject_list(query_host, "host")
        _reject_query_host(query_host)
    if query_hostaddr is not None:
        _reject_list(query_hostaddr, "hostaddr")
    if query_port is not None:
        _reject_list(query_port, "port")
        try:
            parsed_query_port = int(query_port)
        except ValueError as exc:
            raise RuntimeError("DATABASE_URL has an invalid query port") from exc
        if not 1 <= parsed_query_port <= 65535:
            raise RuntimeError("DATABASE_URL has an invalid query port")
    else:
        parsed_query_port = None
    if authority_host is not None and query_host is not None and authority_host != query_host:
        raise RuntimeError("DATABASE_URL has conflicting host values")
    if authority_port is not None and parsed_query_port is not None and authority_port != parsed_query_port:
        raise RuntimeError("DATABASE_URL has conflicting port values")
    host = query_host if query_host is not None else authority_host
    if host is None:
        raise RuntimeError(
            "Unix socket PostgreSQL routes must specify host in the query string"
        )
    if query_host is not None and query_host.startswith("/") and authority_host is not None:
        raise RuntimeError("Unix socket PostgreSQL routes must not use an authority host")
    port = authority_port or parsed_query_port or 5432

    authority_user = unquote(parsed.username) if parsed.username is not None else None
    authority_password = unquote(parsed.password) if parsed.password is not None else None
    if authority_user is not None and query_user is not None and authority_user != query_user:
        raise RuntimeError("DATABASE_URL has conflicting user values")
    if authority_password is not None and query_password is not None and authority_password != query_password:
        raise RuntimeError("DATABASE_URL has conflicting password values")
    user = query_user if query_user is not None else authority_user
    password = query_password if query_password is not None else authority_password

    path_database = unquote(parsed.path.lstrip("/"))
    if path_database and query_database is not None and path_database != query_database:
        raise RuntimeError("DATABASE_URL has conflicting database values")
    database = path_database or query_database
    if not database:
        raise RuntimeError("DATABASE_URL must include a database")

    settings: dict[str, str] = {
        "PGHOST": host,
        "PGPORT": str(port),
        "PGDATABASE": database,
        "PGTARGETSESSIONATTRS": "any",
    }
    if query_hostaddr is not None:
        settings["PGHOSTADDR"] = query_hostaddr
    if user is not None:
        settings["PGUSER"] = user
    for key in sorted(_PG_QUERY_KEYS - {"host", "hostaddr", "port", "target_session_attrs", "user", "password", "dbname"}):
        setting = _query_value(query, key)
        if setting is not None:
            settings["PG" + key.upper()] = setting

    kwargs: dict[str, str] = {
        "host": host,
        "port": str(port),
        "dbname": database,
        "target_session_attrs": "any",
    }
    if query_hostaddr is not None:
        kwargs["hostaddr"] = query_hostaddr
    if user is not None:
        kwargs["user"] = user
    for key in sorted(_PG_QUERY_KEYS - {"host", "hostaddr", "port", "target_session_attrs", "user", "password", "dbname"}):
        setting = _query_value(query, key)
        if setting is not None:
            kwargs[key] = setting

    username = quote(user, safe="") if user is not None else ""
    userinfo = f"{username}@" if user is not None and authority_host is not None else ""
    if authority_host is None:
        netloc = ""
    else:
        authority_name = authority_host
        if ":" in authority_name and not authority_name.startswith("["):
            authority_name = f"[{authority_name}]"
        netloc = f"{userinfo}{authority_name}:{port}"
    canonical_query: list[tuple[str, str]] = []
    if authority_host is None and query_host is not None:
        canonical_query.append(("host", query_host))
    if authority_host is None and user is not None:
        canonical_query.append(("user", user))
    if query_hostaddr is not None:
        canonical_query.append(("hostaddr", query_hostaddr))
    if authority_host is None:
        canonical_query.append(("port", str(port)))
    for key in sorted(_PG_QUERY_KEYS - _ROUTING_QUERY_KEYS - {"user", "password", "dbname"}):
        setting = _query_value(query, key)
        if setting is not None:
            canonical_query.append((key, setting))
    canonical_query.append(("target_session_attrs", "any"))
    encoded_database = quote(database, safe="")
    query_string = urlencode(canonical_query)
    if authority_host is None:
        # urlunsplit emits scheme:/path for an empty netloc. SQLAlchemy needs
        # the authority-less PostgreSQL form with three slashes instead.
        canonical = f"postgresql+psycopg:///{encoded_database}?{query_string}"
    else:
        canonical = urlunsplit(
            (
                "postgresql+psycopg",
                netloc,
                "/" + encoded_database,
                query_string,
                "",
            )
        )
    return PostgresRoute(
        sqlalchemy_url=canonical,
        settings=tuple(sorted(settings.items())),
        connect_kwargs=tuple(sorted(kwargs.items())),
        _password=password,
    )


def postgres_route(value: object) -> PostgresRoute:
    """Parse and canonicalize one PostgreSQL route, rejecting ambiguity."""
    return _route(value)


def normalize_database_url(value: object) -> str:
    return postgres_route(value).sqlalchemy_url


def _pgpass_escape(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise RuntimeError("PostgreSQL credentials must not contain newlines")
    return value.replace("\\", "\\\\").replace(":", "\\:")


def _pgpass_line(route: PostgresRoute) -> str:
    if route._password is None:
        raise RuntimeError("PostgreSQL password is unavailable")
    settings = route.environment()
    return "*:*:*:%s:%s" % (
        _pgpass_escape(settings.get("PGUSER", "*")),
        _pgpass_escape(route._password),
    )


def _inherited_pgpass(path_value: str | None) -> str:
    if not path_value:
        return ""
    path = Path(path_value)
    try:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.geteuid()
        ):
            return ""
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""


def _write_pgpass(route: PostgresRoute, *, inherited: str = "") -> Path:
    try:
        descriptor, filename = tempfile.mkstemp(prefix="before-after-pgpass-")
        path = Path(filename)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            if inherited:
                stream.write(inherited.rstrip("\n") + "\n")
            stream.write(_pgpass_line(route) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return path
    except OSError as exc:
        raise RuntimeError("could not create protected PostgreSQL password file") from exc


_EPHEMERAL_PASSFILES: set[Path] = set()


def _cleanup_ephemeral_passfiles() -> None:
    for path in tuple(_EPHEMERAL_PASSFILES):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


atexit.register(_cleanup_ephemeral_passfiles)


def sanitized_postgres_environment(
    value: object,
    *,
    base: dict[str, str] | None = None,
    include_database_url: bool = True,
    create_passfile: bool = True,
) -> dict[str, str]:
    """Return child settings with a credential-free URL and no PGPASSWORD."""
    route = postgres_route(value)
    source = dict(os.environ if base is None else base)
    existing_passfile = source.get("PGPASSFILE")
    environment = {
        key: setting
        for key, setting in source.items()
        if not key.startswith(PG_ENV_PREFIX) and key != "DATABASE_URL"
    }
    environment.update(route.environment())
    if include_database_url:
        environment["DATABASE_URL"] = route.sqlalchemy_url
    if route._password is not None and create_passfile:
        inherited = _inherited_pgpass(existing_passfile)
        passfile = _write_pgpass(route, inherited=inherited)
        _EPHEMERAL_PASSFILES.add(passfile)
        environment["PGPASSFILE"] = str(passfile)
    elif existing_passfile and route._password is None:
        environment["PGPASSFILE"] = existing_passfile
    environment.pop("PGPASSWORD", None)
    return environment


@contextmanager
def scoped_postgres_environment(value: object):
    """Temporarily replace process PostgreSQL routing with the canonical route."""
    route = postgres_route(value)
    previous = {
        key: os.environ[key]
        for key in os.environ
        if key.startswith(PG_ENV_PREFIX) or key == "DATABASE_URL"
    }
    passfile: Path | None = None
    if route._password is not None:
        passfile = _write_pgpass(route)
    environment = sanitized_postgres_environment(
        value,
        base=previous,
        create_passfile=False,
    )
    if passfile is not None:
        environment["PGPASSFILE"] = str(passfile)
    for key in tuple(previous):
        os.environ.pop(key, None)
    os.environ.update(environment)
    try:
        yield route
    finally:
        if passfile is not None:
            try:
                passfile.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        for key in tuple(os.environ):
            if key.startswith(PG_ENV_PREFIX) or key == "DATABASE_URL":
                os.environ.pop(key, None)
        os.environ.update(previous)
