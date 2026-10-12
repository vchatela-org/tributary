"""Renpho (smart scales): the weigh-ins of one or several Renpho accounts,
from the Renpho cloud, into BodyStats -- with the levels the Renpho app
rates them with, for the users given boundaries.

Ported from a weekly sync job, writing exactly its series (same
measurements, tags, fields, types and times), so a run rewrites that
history in place and never duplicates it (docs/porting.md).

API: Renpho has no public API. renpho-api (the client that job used, same
version) speaks the app's: an encrypted login, then each scale's table,
paged (renpho-aggregation/device/count, then RenphoHealth/scale/
queryBodyCompositionMeasureData, else queryAllMeasureDataList). Every call
returns the whole history: there is no "since". renpho-api sets no timeout;
its session gets one here, or a hung call would hold the scheduler, and every
other connector, forever.

Accounts: one Renpho login per person: TRIBUTARY_RENPHO_EMAIL / _PASSWORD /
_USER, then TRIBUTARY_RENPHO_EMAIL_2 / _PASSWORD_2 / _USER_2, _3... (up to
_9). _USER is the `user` tag (the first account's defaults to
TRIBUTARY_INFLUXDB_USER). An account that fails doesn't stop the others.

Mapping (a record is a weigh-in):
  body_composition  at the record's timeStamp (seconds or milliseconds);
                    tags scale = scaleName, user_id = the Renpho user id
                    (bUserId, else userId), user; a float field per metric
                    measured. Renpho reports an unmeasured metric as 0: left
                    out, never written as a real zero. A record without a
                    time or any metric is skipped. Records sharing a key
                    (same tags, same second) are merged, the later one's
                    values winning, as consecutive writes would (the core
                    would sum them).
  body_levels       same time and tags, for a user given boundaries
                    (TRIBUTARY_RENPHO_THRESHOLDS): per rated metric, the
                    level its value falls in (0 = the lowest; a value on a
                    boundary gets the upper level), as a float.
  body_thresholds   the boundaries themselves, tagged `user` only (no
                    `source`: they rate a person, not a tracker's reading),
                    at the run's time: <metric>_t1.._tN and <metric>_min /
                    _max (a gauge's scale). Written when they change, on a
                    first or full run, and at least once a week, as the job
                    did (a dashboard reading the latest within its time
                    range, or a retention policy, would otherwise lose them).

Cursor: per account, a short digest of each weigh-in written (its time,
tags, values and levels); a run writes the weigh-ins whose digest is new
-- a new weigh-in, one assigned late in the app, every one whose level
new boundaries move -- and nothing when nothing changed. Per user, a digest
of the boundaries and when they were last written. No value, email or token
is kept. Append only, as the job was: a point is never deleted, and a
rewrite keeps the fields it lacks (InfluxDB merges them).
"""
import bisect
import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import dotenv_values
from pydantic import PrivateAttr, SecretStr
from pydantic_settings import SettingsConfigDict
from renpho import RenphoAPIError, RenphoClient

from app.config import settings as app_settings
from app.connectors.base import Connector, ConnectorError, ConnectorSettings, SyncResult
from app.points import Measurement, Point
from app.schema import BODY_COMPOSITION

PREFIX = "TRIBUTARY_RENPHO_"
TIMEOUT_SECONDS = 30
MAX_ACCOUNTS = 9
# body_thresholds is rewritten at least this often, as the weekly job did.
THRESHOLDS_REFRESH_SECONDS = 7 * 86_400

# Renpho's metric -> the field it is written as. Field names follow the
# Renpho API, not always the app: bone_pct is the bone mass in kg,
# lean_body_mass_kg the app's "Muscle Mass", muscle_pct its "Skeletal Muscle
# Percentage", body_age its "Metabolic Age".
BODY_FIELDS: Dict[str, str] = {
    "weight": "weight_kg",
    "bmi": "bmi",
    "bodyfat": "body_fat_pct",
    "water": "body_water_pct",
    "muscle": "muscle_pct",
    "bone": "bone_pct",
    "bmr": "bmr_kcal",
    "visfat": "visceral_fat",
    "subfat": "subcutaneous_fat_pct",
    "protein": "protein_pct",
    "bodyage": "body_age",
    "sinew": "lean_body_mass_kg",
    "fatFreeWeight": "fat_free_weight_kg",
    "heartRate": "heart_rate_bpm",
    "cardiacIndex": "cardiac_index",
    "bodyShape": "body_shape",
}

# How many levels the Renpho app rates each metric with (Below Avg.,
# Average, Above Avg., Over...). The boundaries between them depend on the
# person (Renpho derives them from sex, age and height): they come from
# TRIBUTARY_RENPHO_THRESHOLDS, one fewer than the levels.
LEVELS: Dict[str, int] = {
    "weight_kg": 4, "bmi": 4, "body_fat_pct": 5, "subcutaneous_fat_pct": 3, "visceral_fat": 4,
    "lean_body_mass_kg": 3, "muscle_pct": 3, "bone_pct": 3, "body_age": 2, "bmr_kcal": 2,
    "body_water_pct": 3, "protein_pct": 3,
}

BODY_LEVELS = Measurement(
    database="BodyStats", name="body_levels", tags=BODY_COMPOSITION.tags,
    fields={name: float for name in LEVELS},
)

# They rate a person, not a reading: no `source` tag.
BODY_THRESHOLDS = Measurement(
    database="BodyStats", name="body_thresholds", tags=(), tagged_source=False,
    fields={f"{name}_{suffix}": float for name, levels in LEVELS.items()
            for suffix in [f"t{i}" for i in range(1, levels)] + ["min", "max"]},
)

_NUMBERED = re.compile(rf"^{PREFIX}(EMAIL|PASSWORD|USER)_(\d+)$", re.IGNORECASE)


def numbered_env(env_file: str = ".env") -> Dict[str, str]:
    """TRIBUTARY_RENPHO_{EMAIL,PASSWORD,USER}_<n>, from the environment, else
    from the .env file (like every other setting), names upper-cased. The
    environment wins, even empty; an empty value counts as unset."""
    found: Dict[str, str] = {}
    sources = []
    if Path(env_file).is_file():
        sources.append(dotenv_values(env_file))
    sources.append(os.environ)  # last: the environment wins
    for source in sources:
        for name, value in source.items():
            if _NUMBERED.match(name):
                found[name.upper()] = value or ""
    return {name: value for name, value in found.items() if value}


class RenphoSettings(ConnectorSettings):
    model_config = SettingsConfigDict(env_prefix=PREFIX, env_file=".env", extra="ignore")

    # A weigh-in carries its own time and every run reads the whole history:
    # once a day is plenty.
    interval_minutes: int = 1440
    # The first account: the Renpho app's login (an account created with
    # Google or Apple needs a password set in the app first).
    email: Optional[str] = None
    password: Optional[SecretStr] = None
    # Its `user` tag; TRIBUTARY_INFLUXDB_USER when unset.
    user: Optional[str] = None
    # Level boundaries per user, JSON: {"<user>": {"<metric>": [boundaries]}}.
    # Read at each run, so a mistake fails the runs, not the start.
    thresholds: Optional[str] = None

    # Further accounts (TRIBUTARY_RENPHO_EMAIL_2...): undeclared names, read
    # by numbered_env() at the start like the rest.
    _numbered: Dict[str, str] = PrivateAttr(default_factory=dict)

    def model_post_init(self, context: Any) -> None:
        super().model_post_init(context)
        self._numbered = numbered_env()

    @property
    def numbered(self) -> Dict[str, str]:
        return self._numbered


@dataclass(frozen=True)
class Account:
    number: int
    email: str
    password: str = field(repr=False)
    # The `user` tag; "" for none.
    user: str = ""

    @property
    def label(self) -> str:
        """How runs name it: never the email."""
        return self.user or f"account {self.number}"


@dataclass
class WeighIn:
    epoch: int
    # scale, user_id: the empty ones left out.
    tags: Dict[str, str]
    fields: Dict[str, float]


# ────────────────────────────────────────────────
# Mapping
# ────────────────────────────────────────────────

def normalize_timestamp(raw: Any) -> Optional[int]:
    """Renpho times are epoch seconds or milliseconds: seconds, or None."""
    if raw is None or raw == "":
        return None
    try:
        ts = int(float(raw))
    except (TypeError, ValueError, OverflowError):
        return None
    if ts <= 0:
        return None
    return ts // 1000 if ts > 1_000_000_000_000 else ts


def _to_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    # Renpho reports an unmeasured metric as 0, never a real zero reading.
    if number == 0 or not math.isfinite(number):
        return None
    return number


def _user_id(record: Dict) -> str:
    for key in ("bUserId", "userId", "user_id"):
        value = record.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def weigh_ins(records: List[Dict]) -> List[WeighIn]:
    """The records that are weigh-ins, in their order, those sharing a key
    (same tags, same second) merged: the later one's values win, as when
    each was written in turn."""
    merged: Dict[Tuple, WeighIn] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        epoch = normalize_timestamp(record.get("timeStamp") or record.get("time_stamp"))
        if epoch is None:
            continue
        try:
            datetime.fromtimestamp(epoch, timezone.utc)
        except (OverflowError, OSError, ValueError):
            continue
        fields = {name: value for key, name in BODY_FIELDS.items()
                  if (value := _to_float(record.get(key))) is not None}
        if not fields:
            continue
        tags = {k: v for k, v in (("scale", str(record.get("scaleName") or "")), ("user_id", _user_id(record))) if v}
        key = (epoch, tuple(sorted(tags.items())))
        if key in merged:
            merged[key].fields.update(fields)
        else:
            merged[key] = WeighIn(epoch, tags, fields)
    return list(merged.values())


def level_fields(bounds: Dict[str, Tuple[float, ...]], fields: Dict[str, float]) -> Dict[str, float]:
    """The level of each rated metric of a weigh-in: 0 = the lowest; a value
    on a boundary gets the upper level."""
    return {name: float(bisect.bisect_right(limits, fields[name]))
            for name, limits in bounds.items() if name in fields}


def scale_range(bounds: Tuple[float, ...]) -> Tuple[float, float]:
    """A gauge's min and max: the outer levels as wide as an average inner
    one, never below 0."""
    low, high = bounds[0], bounds[-1]
    if len(bounds) > 1:
        margin = (high - low) / (len(bounds) - 1)
    else:
        margin = abs(low) * 0.25 or 1.0
    return round(max(low - margin, 0.0), 2), round(high + margin, 2)


def threshold_fields(bounds: Dict[str, Tuple[float, ...]]) -> Dict[str, float]:
    fields: Dict[str, float] = {}
    for name, limits in bounds.items():
        for i, limit in enumerate(limits, start=1):
            fields[f"{name}_t{i}"] = limit
        fields[f"{name}_min"], fields[f"{name}_max"] = scale_range(limits)
    return fields


def parse_thresholds(raw: Optional[str]) -> Dict[str, Dict[str, Tuple[float, ...]]]:
    """TRIBUTARY_RENPHO_THRESHOLDS: {"<user>": {"<metric>": [boundaries]}},
    one fewer boundary than the metric's levels, strictly ascending."""
    if not raw or not raw.strip():
        return {}
    name = f"{PREFIX}THRESHOLDS"
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ConnectorError(f"{name} is not valid JSON ({getattr(exc, 'msg', exc)}).") from None
    if not isinstance(data, dict):
        raise ConnectorError(f'{name} must be a JSON object: {{"<user>": {{"<metric>": [boundaries]}}}}.')
    thresholds: Dict[str, Dict[str, Tuple[float, ...]]] = {}
    for user, by_metric in data.items():
        if not isinstance(by_metric, dict):
            raise ConnectorError(f"{name}: {user!r} must map metrics to their boundaries.")
        thresholds[user] = {}
        for metric, limits in by_metric.items():
            if metric not in LEVELS:
                raise ConnectorError(f"{name}: {user}.{metric} is not a rated metric "
                                     f"({', '.join(LEVELS)}).")
            try:
                numbers = isinstance(limits, list) and all(
                    isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in limits)
            except OverflowError:  # an integer too large for a float
                numbers = False
            if not numbers:
                raise ConnectorError(f"{name}: {user}.{metric} must be a list of numbers.")
            if len(limits) != LEVELS[metric] - 1:
                raise ConnectorError(f"{name}: {user}.{metric} has {LEVELS[metric]} levels, so "
                                     f"{LEVELS[metric] - 1} boundaries, not {len(limits)}.")
            if any(a >= b for a, b in zip(limits, limits[1:])):
                raise ConnectorError(f"{name}: {user}.{metric}: boundaries must be strictly ascending.")
            thresholds[user][metric] = tuple(float(x) for x in limits)
    return thresholds


def _digest(*parts: Any) -> str:
    """A short one-way fingerprint: what the cursor keeps instead of values."""
    text = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ────────────────────────────────────────────────
# API
# ────────────────────────────────────────────────

class _Session(requests.Session):
    """renpho-api's session, with a timeout on every call (it sets none)."""

    def request(self, method, url, *args, **kwargs):
        kwargs.setdefault("timeout", TIMEOUT_SECONDS)
        return super().request(method, url, *args, **kwargs)


def _scrub(text: Any, account: Account) -> str:
    """An answer from Renpho, without the account's email or password (an
    error message might quote them), cut short."""
    text = str(text)
    for secret in (account.email, account.password):
        if secret:
            text = re.sub(re.escape(secret), "***", text, flags=re.IGNORECASE)
    return text[:200]


class RenphoConnector(Connector):
    key = "renpho"
    label = "Renpho"
    description = ("Smart-scale weigh-ins (weight, body fat, muscle, water...) of one or several Renpho "
                   "accounts, from the Renpho cloud, rated with the app's levels.")
    icon = "bi-person-standing"
    settings_class = RenphoSettings
    measurements = (BODY_COMPOSITION, BODY_LEVELS, BODY_THRESHOLDS)
    required_env = (f"{PREFIX}EMAIL", f"{PREFIX}PASSWORD")

    def api_configured(self) -> bool:
        return bool(self.settings.email and self.settings.password and self.settings.password.get_secret_value())

    def accounts(self) -> List[Account]:
        """The first account, then the numbered ones; a configuration
        mistake raises, naming the variables (never their values)."""
        s = self.settings
        first_user = (s.user or "").strip() or (app_settings.influxdb_user or "").strip()
        # Emails without stray whitespace (a newline in a secret); passwords as given.
        accounts = [Account(1, (s.email or "").strip(), s.password.get_secret_value() if s.password else "",
                            first_user)]
        env = s.numbered
        problems: List[str] = []
        numbers = sorted({int(m.group(2)) for name in env if (m := _NUMBERED.match(name))})
        if numbers != list(range(2, len(numbers) + 2)) or (numbers and numbers[-1] > MAX_ACCOUNTS):
            problems.append(f"further accounts are numbered _2 to _{MAX_ACCOUNTS} without a gap "
                            f"(found {', '.join(f'_{n}' for n in numbers)})")
        for n in numbers:
            names = [f"{PREFIX}EMAIL_{n}", f"{PREFIX}PASSWORD_{n}", f"{PREFIX}USER_{n}"]
            missing = [name for name in names if not env.get(name, "").strip()]
            if missing:
                problems.append(f"{', '.join(missing)} missing")
            else:
                accounts.append(Account(n, env[names[0]].strip(), env[names[1]], env[names[2]].strip()))
        if len(accounts) > 1:
            if not accounts[0].user:
                problems.append(f"{PREFIX}USER (or TRIBUTARY_INFLUXDB_USER) must name the first account "
                                "when there are several")
            users = [a.user for a in accounts if a.user]
            if len(set(users)) != len(users):
                problems.append(f"{PREFIX}USER* must name each account differently")
        if problems:
            raise ConnectorError("Renpho accounts misconfigured: " + "; ".join(problems) + ".")
        return accounts

    def _client(self, account: Account) -> RenphoClient:
        client = RenphoClient(account.email, account.password)
        client._session.close()
        client._session = _Session()
        return client

    def _records(self, account: Account) -> List[Dict]:
        """Every record of the account's scales. Nothing else of the answers
        (the login's profile, email, token) outlives the call."""
        label = account.label
        client = self._client(account)
        try:
            records = client.get_all_measurements()
        except RenphoAPIError as exc:
            what = f"the login of {label}" if exc.context == "Login" else f"{_scrub(exc.context, account)} ({label})"
            raise ConnectorError(f"Renpho refused {what} (code {_scrub(exc.code, account)}: "
                                 f"{_scrub(exc.msg, account)}).") from None
        except requests.Timeout:
            raise ConnectorError(f"Renpho did not answer within {TIMEOUT_SECONDS} s ({label}).") from None
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "an error"
            raise ConnectorError(f"Renpho answered {status} ({label}).") from None
        except requests.ConnectionError as exc:
            raise ConnectorError(f"Renpho unreachable ({label}): {type(exc).__name__}.") from None
        except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ConnectorError(f"Unexpected answer from Renpho ({label}): {type(exc).__name__}.") from None
        finally:
            session = getattr(client, "_session", None)
            if session is not None:
                session.close()
        if not isinstance(records, list):
            raise ConnectorError(f"Unexpected answer from Renpho ({label}).")
        return records

    # -- Sync ----------------------------------------------------------------

    def sync(self, cursor: Any) -> SyncResult:
        cursor = cursor if isinstance(cursor, dict) else {}
        known_weigh_ins = cursor.get("weigh_ins") if isinstance(cursor.get("weigh_ins"), dict) else {}
        known_bounds = cursor.get("thresholds") if isinstance(cursor.get("thresholds"), dict) else {}
        accounts = self.accounts()
        thresholds = parse_thresholds(self.settings.thresholds)
        users = [a.user for a in accounts if a.user]
        strangers = sorted(set(thresholds) - set(users))
        if strangers:
            raise ConnectorError(f"{PREFIX}THRESHOLDS names no account: {', '.join(map(repr, strangers))} "
                                 f"(the accounts' users: {', '.join(map(repr, users)) or 'none'}).")
        now = datetime.now(timezone.utc)

        points: List[Point] = []
        summary: List[str] = []
        errors: List[str] = []
        new_weigh_ins: Dict[str, List[str]] = {}
        new_bounds: Dict[str, Dict[str, Any]] = {}
        for account in accounts:
            key = account.user
            try:
                records = self._records(account)
            except ConnectorError as exc:
                errors.append(str(exc))
                # Read again by the next run.
                if key in known_weigh_ins:
                    new_weigh_ins[key] = known_weigh_ins[key]
                if key in known_bounds:
                    new_bounds[key] = known_bounds[key]
                continue

            bounds = thresholds.get(account.user) if account.user else None
            known = set(known_weigh_ins.get(key) or [])
            digests: List[str] = []
            fresh = 0
            for weigh_in in weigh_ins(records):
                levels = level_fields(bounds, weigh_in.fields) if bounds else {}
                digest = _digest(weigh_in.epoch, weigh_in.tags, weigh_in.fields, levels, account.user)
                digests.append(digest)
                if digest in known:
                    continue
                fresh += 1
                at = datetime.fromtimestamp(weigh_in.epoch, timezone.utc)
                # The account's own user tag, or none: never another default.
                tags = {**weigh_in.tags, "user": account.user or None}
                points.append(Point(BODY_COMPOSITION, at, tags, dict(weigh_in.fields)))
                if levels:
                    points.append(Point(BODY_LEVELS, at, tags, levels))
            new_weigh_ins[key] = sorted(digests)
            if fresh:
                summary.append(f"{account.label}: {fresh} new or changed weigh-in(s)")

            if bounds:
                digest = _digest(bounds)
                last = known_bounds.get(key) if isinstance(known_bounds.get(key), dict) else {}
                written_at = last.get("written") if isinstance(last.get("written"), (int, float)) else 0
                if last.get("digest") != digest or now.timestamp() - written_at >= THRESHOLDS_REFRESH_SECONDS:
                    points.append(Point(BODY_THRESHOLDS, now, {"user": account.user}, threshold_fields(bounds)))
                    summary.append(f"{account.label}: level boundaries")
                    written_at = int(now.timestamp())
                new_bounds[key] = {"digest": digest, "written": written_at}

        if errors and len(errors) == len(accounts):
            raise ConnectorError(" ".join(errors))
        return SyncResult(points=points, summary=summary, errors=errors,
                          cursor={"weigh_ins": new_weigh_ins, "thresholds": new_bounds})
