#!/usr/bin/env python3
"""Populate an OpenCloud instance with the Weyland Energy demo dataset.

    python3 populate.py            # asks for server URL and admin credentials (Enter = default)
    python3 populate.py --yes      # no questions, uses the SETTINGS below / environment variables

Creates 1,000 users with profile pictures, groups, 25 project spaces with members and
folder shares, ~5,400 files with version history, personal files, tags, shares between
users and public links – everything from the data/ folder next to this script.

Needs only Python 3.9+ (no extra packages). Safe to run again: it only adds what is
missing and updates what changed. A failing item never stops the run: errors are
collected and summarised at the end (exit code 1). `python3 populate.py --reset`
removes the dataset again.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, unquote

# =============================================================================
# SETTINGS – change these if you do not want to type them every time
# (environment variables OC_URL, OC_ADMIN_USER, OC_ADMIN_PASSWORD override them)
# =============================================================================
SETTINGS = {
    "url": "https://host.docker.internal:9200",   # your OpenCloud instance
    "admin_user": "admin",                         # an admin account
    "admin_password": "admin",                     # its password (or an app token)
    "demo_user_password": "demo",                  # password that all 1,000 demo users get
    "link_password": "Weyland-2026!",          # password for public links (often enforced)
    "verify_tls": False,                           # False = accept self-signed certificates
}
# =============================================================================

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
SUPPORTED_FORMATS = {1}          # metadata.json "format_version" values this script understands

ROLE = {  # OpenCloud unified role ids
    "space_viewer": "a8d5fe5e-96e3-418d-825b-534dbdf22b99",
    "space_editor": "58c63c02-1d89-4572-916a-870abc5a1b7d",
    "space_manager": "312c0871-5ef7-4b3a-85b6-0e4074c64049",
    "file_viewer": "b1e2218d-eef8-4d4c-b82d-0f1a1b48f3b5",
    "file_editor": "2d00ce52-1fc2-4dbc-8b95-a73b73395f5a",
    "folder_editor": "fb6c3e19-e378-47e5-b277-9732f9de6e21",
}
SPACE_ROLE = {"manager": ROLE["space_manager"], "editor": ROLE["space_editor"], "viewer": ROLE["space_viewer"]}
NS = {"d": "DAV:", "oc": "http://owncloud.org/ns"}
PROPFIND_BODY = (b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns"><d:prop>'
                 b'<d:resourcetype/><d:getcontentlength/><d:getlastmodified/><oc:fileid/><oc:tags/>'
                 b'</d:prop></d:propfind>')
FAVORITE_BODY = (b'<?xml version="1.0"?><d:propertyupdate xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
                 b'<d:set><d:prop><oc:favorite>1</oc:favorite></d:prop></d:set></d:propertyupdate>')
# Requests that are safe to send twice. POST (create user, invite, createLink) is never retried,
# otherwise a timeout after the server already processed it would create duplicates.
IDEMPOTENT = {"GET", "HEAD", "PUT", "DELETE", "PATCH", "PROPFIND", "PROPPATCH", "MKCOL"}
RETRY_STATUS = {423, 429, 500, 502, 503, 504}


# =============================================================================
# HTTP
# =============================================================================

class HTTPError(Exception):
    def __init__(self, status, msg):
        super().__init__(f"{status}: {msg}")
        self.status = status


class Client:
    def __init__(self, base: str, admin: tuple[str, str], user_password: str, verify: bool):
        self.base = base.rstrip("/")
        self.admin = admin
        self.user_password = user_password
        self.ctx = ssl.create_default_context()
        if not verify:
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE
        self._logins: dict[str, bool] = {}
        self._lock = threading.Lock()

    def _auth(self, user):
        u, p = self.admin if user is None else (user, self.user_password)
        return "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()

    def req(self, method, path, user=None, body=None, headers=None, ok=(200, 201, 204, 207), attempts=5):
        url = (path if path.startswith("http") else self.base + path).replace(" ", "%20").replace("'", "%27")
        hdrs = {"Authorization": self._auth(user)}
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        attempts = attempts if method in IDEMPOTENT else 1
        who = user or "admin"
        for attempt in range(attempts):
            last = attempt == attempts - 1
            try:
                r = urllib.request.Request(url, data=body, method=method, headers=hdrs)
                with urllib.request.urlopen(r, context=self.ctx, timeout=180) as resp:
                    return resp.status, dict(resp.headers), resp.read()
            except urllib.error.HTTPError as e:
                data = e.read()
                if e.code in ok:
                    return e.code, dict(e.headers), data
                if e.code in RETRY_STATUS and not last:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise HTTPError(e.code, f"{method} {path} as {who}: {data[:300]!r}") from None
            except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
                if not last:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise HTTPError(0, f"{method} {path} as {who}: {e}") from None

    def json(self, method, path, user=None, body=None, ok=(200, 201, 204, 207)):
        _, _, data = self.req(method, path, user=user, body=body, ok=ok)
        return json.loads(data) if data else {}

    def paged(self, path, user=None):
        out = []
        while path:
            d = self.json("GET", path, user=user)
            out += d.get("value", [])
            path = d.get("@odata.nextLink")
        return out

    def can_login(self, user) -> bool:
        """Whether the demo password works for `user` (cached, success and failure)."""
        with self._lock:
            if user in self._logins:
                return self._logins[user]
        try:
            self.req("GET", "/graph/v1.0/me", user=user)
            ok = True
        except HTTPError:
            ok = False
        with self._lock:
            self._logins[user] = ok
        return ok


# =============================================================================
# Small pure helpers (unit tested)
# =============================================================================

def dav(drive_id, rel=""):
    return f"/dav/spaces/{quote(drive_id, safe='')}" + ("/" + quote(rel) if rel else "")


def item_path(drive_id, fid, action="permissions"):
    return f"/graph/v1beta1/drives/{drive_id}/items/{quote(fid, safe='')}/{action}"


def granted_ids(perms) -> set[str]:
    """User and group ids that already have one of the given permissions."""
    out = set()
    for p in perms:
        g = p.get("grantedToV2", {})
        for kind in ("user", "group"):
            if kind in g:
                out.add(g[kind]["id"])
    return out


def link_names(perms) -> set[str]:
    return {p["link"].get("@libre.graph.displayName") for p in perms if "link" in p}


def ts(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp())


def upload_state(current: dict | None, size: int, mtime: int) -> str:
    """'new' (not on the server), 'same' (size and mtime match) or 'changed'."""
    if not current:
        return "new"
    if current["size"] == size and abs(current["mtime"] - mtime) <= 1:
        return "same"
    return "changed"


def folders_of(paths) -> list[str]:
    """All parent folders of the given relative paths, parents before children."""
    out = set()
    for p in paths:
        parts = p.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            out.add("/".join(parts[:i]))
    return sorted(out, key=lambda f: (f.count("/"), f))


def expand(members, groups) -> set[str]:
    """Member list (user ids and 'group:<id>') -> user ids."""
    out = set()
    for m in members:
        out |= set(groups.get(m[6:], [])) if m.startswith("group:") else {m}
    return out


def check_metadata(meta: dict) -> None:
    version = meta.get("format_version", 1)
    if version not in SUPPORTED_FORMATS:
        sys.exit(f"✗ data/metadata.json has format version {version}; this populate.py supports "
                 f"{sorted(SUPPORTED_FORMATS)}. Update the script (git pull).")
    missing = [k for k in ("users", "groups", "spaces", "personal", "files", "summary", "reference_date") if k not in meta]
    if missing:
        sys.exit(f"✗ data/metadata.json is incomplete (missing: {', '.join(missing)}).")


# =============================================================================
# Progress and report
# =============================================================================

def log(msg=""):
    print(msg, flush=True)


def fmt_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s:02d}s" if m else f"{s}s"


class Progress:
    """One self-updating line per phase when attached to a terminal; silent otherwise."""

    def __init__(self, label: str, total: int):
        self.label, self.total, self.done = label, total, 0
        self.tty = sys.stdout.isatty()
        self._lock = threading.Lock()
        self._last = 0.0

    def tick(self, n=1):
        with self._lock:
            self.done += n
            now = time.time()
            if self.tty and (now - self._last > 0.25 or self.done >= self.total):
                self._last = now
                print(f"\r  {self.label}: {self.done:,}/{self.total:,} ", end="", flush=True)

    def close(self):
        if self.tty and self.total:
            print("\r" + " " * (len(self.label) + 30) + "\r", end="", flush=True)


class Report:
    """Expected vs. achieved per category, plus collected errors and skips."""

    def __init__(self):
        self.expected: dict[str, int] = {}
        self.failed: dict[str, int] = {}
        self.skipped: dict[str, int] = {}
        self.skip_reasons: dict[str, set] = {}
        self.errors: list[tuple[str, str, str]] = []
        self._lock = threading.Lock()

    def expect(self, cat, n):
        self.expected[cat] = self.expected.get(cat, 0) + n

    def fail(self, cat, item, err, n=1):
        with self._lock:
            self.failed[cat] = self.failed.get(cat, 0) + n
            self.errors.append((cat, str(item), str(err)[:300]))

    def skip(self, cat, reason, n=1):
        with self._lock:
            self.skipped[cat] = self.skipped.get(cat, 0) + n
            self.skip_reasons.setdefault(cat, set()).add(reason)

    @property
    def has_errors(self):
        return bool(self.errors)

    def print(self):
        log("\nSummary")
        width = max((len(c) for c in self.expected), default=10)
        for cat, exp in self.expected.items():
            failed, skipped = self.failed.get(cat, 0), self.skipped.get(cat, 0)
            okay = exp - failed - skipped
            mark = "✓" if not failed and not skipped else ("✗" if failed else "–")
            extra = []
            if failed:
                extra.append(f"{failed:,} failed")
            if skipped:
                extra.append(f"{skipped:,} skipped ({'; '.join(sorted(self.skip_reasons.get(cat, [])))})")
            log(f"  {mark} {cat:<{width}}  {okay:>6,} / {exp:<6,} {'  ' + ', '.join(extra) if extra else ''}")
        if self.errors:
            log(f"\n{len(self.errors)} error(s) – first {min(20, len(self.errors))}:")
            for cat, item, err in self.errors[:20]:
                log(f"  [{cat}] {item}: {err}")
            log("Run the script again to retry – it only redoes what is missing.")


# =============================================================================
# Seeder – one method per phase
# =============================================================================

class Seeder:
    def __init__(self, client: Client, meta: dict, *, admin_id: str, workers: int, link_password: str,
                 only: set[str] | None = None):
        self.c, self.meta = client, meta
        self.admin_id, self.workers, self.link_password = admin_id, workers, link_password
        self.groups = {g["id"]: g["members"] for g in meta["groups"]}
        self.spaces = [s for s in meta["spaces"] if not only or s["id"] in only]
        self.personal = [p for p in meta["personal"] if not only or f"personal:{p['user']}" in only]
        self.by_space: dict[str, list] = {}
        for f in meta["files"]:
            self.by_space.setdefault(f["space"], []).append(f)
        self.report = Report()
        self.uids: dict[str, str] = {}
        self.gids: dict[str, str] = {}
        self.drives: dict[str, dict] = {}
        self.drive_of: dict[str, str] = {}    # space key -> drive id (project and personal)
        self.owner_of: dict[str, str] = {}    # personal space key -> user id
        self.files: dict[str, dict] = {}      # file key -> {fileid, tags, fresh}
        self._lock = threading.Lock()

    # ------------------------------------------------------------- plumbing
    def bump(self, counter: dict, key: str, n: int = 1):
        with self._lock:
            counter[key] = counter.get(key, 0) + n

    def each(self, cat, items, fn, label=None, workers=None):
        """Run fn(item) for all items in parallel; an exception fails only that item."""
        items = list(items)
        progress = Progress(label or cat, len(items))

        def run(item):
            try:
                return fn(item)
            except Exception as e:  # noqa: BLE001 – one bad item must not stop the run
                self.report.fail(cat, self.describe(item), e)
            finally:
                progress.tick()

        try:
            with ThreadPoolExecutor(workers or self.workers) as ex:
                return list(ex.map(run, items))
        finally:
            progress.close()

    @staticmethod
    def describe(item):
        if isinstance(item, dict):
            return item.get("file") or item.get("name") or item.get("id") or item.get("user") or str(item)[:60]
        return str(item)[:80]

    def recipient(self, member):
        if member.startswith("group:"):
            return {"objectId": self.gids[member[6:]], "@libre.graph.recipient.type": "group"}
        return {"objectId": self.uids[member], "@libre.graph.recipient.type": "user"}

    def phase(self, title):
        log(title)
        return time.time()

    def done(self, started, text):
        log(f"  {text} ({fmt_duration(time.time() - started)})")

    # ------------------------------------------------------------- users & groups
    def seed_users(self):
        t = self.phase("Users")
        users = self.meta["users"]
        self.report.expect("users", len(users))
        existing = {u["onPremisesSamAccountName"]: u for u in
                    self.c.paged("/graph/v1.0/users?$select=id,onPremisesSamAccountName,accountEnabled")}
        self.uids = {k: v["id"] for k, v in existing.items()}

        def create(u):
            name = u["name"].replace("Prof. ", "").replace("Dr. ", "")
            given, _, sur = name.partition(" ")
            d = self.c.json("POST", "/graph/v1.0/users", body={
                "onPremisesSamAccountName": u["id"], "displayName": u["name"], "mail": u["email"],
                "givenName": given, "surname": sur or given, "accountEnabled": u["enabled"],
                "passwordProfile": {"password": self.c.user_password}})
            self.uids[u["id"]] = d["id"]

        todo = [u for u in users if u["id"] not in existing]
        self.each("users", todo, create, "creating users")
        out_of_sync = [u for u in users if u["id"] in existing
                       and existing[u["id"]].get("accountEnabled", u["enabled"]) != u["enabled"]]
        self.each("users", out_of_sync, lambda u: self.c.req(
            "PATCH", f"/graph/v1.0/users/{self.uids[u['id']]}", body={"accountEnabled": u["enabled"]}))
        self.done(t, f"{len(todo) - self.report.failed.get('users', 0):,} created, {len(users) - len(todo):,} already present")

    def seed_groups(self):
        t = self.phase("Groups")
        groups = self.meta["groups"]
        self.report.expect("groups", len(groups))
        self.report.expect("group memberships", sum(len(g["members"]) for g in groups))
        existing = {g["displayName"]: g["id"] for g in self.c.paged("/graph/v1.0/groups")}

        def create(g):
            if g["id"] in existing:
                self.gids[g["id"]] = existing[g["id"]]
            else:
                self.gids[g["id"]] = self.c.json("POST", "/graph/v1.0/groups",
                                                 body={"displayName": g["id"], "description": g["description"]})["id"]
        self.each("groups", groups, create)
        added = {"n": 0}

        def fill(g):
            if g["id"] not in self.gids:
                self.report.skip("group memberships", "group missing", len(g["members"]))
                return
            gid = self.gids[g["id"]]
            mem = self.c.json("GET", f"/graph/v1.0/groups/{gid}/members")
            current = {m["id"] for m in (mem.get("value", []) if isinstance(mem, dict) else mem)}
            unknown = [m for m in g["members"] if m not in self.uids]
            if unknown:
                self.report.skip("group memberships", "user missing", len(unknown))
            missing = [self.uids[m] for m in g["members"] if m in self.uids and self.uids[m] not in current]
            for i in range(0, len(missing), 20):
                chunk = missing[i:i + 20]
                try:
                    self.c.req("PATCH", f"/graph/v1.0/groups/{gid}",
                               body={"members@odata.bind": [f"{self.c.base}/graph/v1.0/users/{x}" for x in chunk]})
                    self.bump(added, "n", len(chunk))
                except HTTPError:      # older servers: add one by one
                    for x in chunk:
                        try:
                            self.c.req("POST", f"/graph/v1.0/groups/{gid}/members/$ref",
                                       body={"@odata.id": f"{self.c.base}/graph/v1.0/users/{x}"})
                            self.bump(added, "n")
                        except HTTPError as e:
                            self.report.fail("group memberships", f"{g['id']} ← {x}", e)
        self.each("groups", groups, fill, "group members")
        self.done(t, f"{len(groups):,} groups, {added['n']:,} memberships added")

    def seed_photos(self):
        t = self.phase("Profile pictures")
        todo = [u for u in self.meta["users"] if u.get("photo") and u["id"] in self.uids]
        with_photo = [u for u in self.meta["users"] if u.get("photo")]
        self.report.expect("profile pictures", len(with_photo))
        if len(with_photo) > len(todo):
            self.report.skip("profile pictures", "user missing", len(with_photo) - len(todo))
        changed = {"n": 0}

        def one(u):
            data = (DATA / u["photo"]).read_bytes()
            uid = self.uids[u["id"]]
            if not u["enabled"]:       # disabled accounts cannot log in – enable briefly
                self.c.req("PATCH", f"/graph/v1.0/users/{uid}", body={"accountEnabled": True})
            try:
                if not self.c.can_login(u["id"]):
                    self.report.skip("profile pictures", "cannot log in as user (password differs?)")
                    return
                try:
                    _, _, current = self.c.req("GET", "/graph/v1.0/me/photo/$value", user=u["id"])
                except HTTPError as e:
                    if e.status != 404:
                        raise
                    current = b""
                if current != data:
                    self.c.req("PUT", "/graph/v1.0/me/photo/$value", user=u["id"], body=data,
                               headers={"Content-Type": "image/jpeg"})
                    self.bump(changed, "n")
            finally:
                if not u["enabled"]:
                    self.c.req("PATCH", f"/graph/v1.0/users/{uid}", body={"accountEnabled": False})
        self.each("profile pictures", todo, one)
        self.done(t, f"{changed['n']:,} set, {len(todo) - changed['n']:,} unchanged or skipped")

    # ------------------------------------------------------------- spaces
    def seed_spaces(self):
        t = self.phase("Spaces and members")
        self.report.expect("spaces", len(self.spaces))
        self.report.expect("space members", sum(len(s["members"]) for s in self.spaces))
        existing = {d["name"]: d for d in self.c.paged("/graph/v1.0/drives?$filter=driveType eq 'project'")}
        st = {"created": 0, "added": 0, "changed": 0, "removed": 0}

        def one(sp):
            d = existing.get(sp["name"])
            if not d:
                d = self.c.json("POST", "/graph/v1.0/drives", body={
                    "name": sp["name"], "description": sp["description"], "driveType": "project",
                    "quota": {"total": 50 * 1024 ** 3}})
                self.bump(st, "created")
            elif d.get("description") != sp["description"]:
                self.c.req("PATCH", f"/graph/v1.0/drives/{d['id']}", body={"description": sp["description"]})
            self.drives[sp["id"]] = d
            self.drive_of[sp["id"]] = d["id"]
            self.reconcile_members(sp, d["id"], st)
        self.each("spaces", self.spaces, one, workers=4)
        self.done(t, f"{st['created']} created; members: {st['added']} added, {st['changed']} role changes, "
                     f"{st['removed']} removed")

    def reconcile_members(self, sp, drive_id, st):
        base = f"/graph/v1beta1/drives/{drive_id}/root/permissions"
        have = {}
        for p in self.c.json("GET", base).get("value", []):
            for oid in granted_ids([p]):
                have[oid] = p
        want = {}
        for m in sp["members"]:
            try:
                rec = self.recipient(m["member"])
            except KeyError:
                self.report.skip("space members", "user or group missing")
                continue
            want[rec["objectId"]] = (rec, SPACE_ROLE[m["role"]], m["member"])
        for oid, (rec, role, label) in want.items():
            p = have.get(oid)
            try:
                if not p:
                    self.c.req("POST", f"/graph/v1beta1/drives/{drive_id}/root/invite",
                               body={"recipients": [rec], "roles": [role]})
                    self.bump(st, "added")
                elif role not in p.get("roles", []):
                    self.c.req("PATCH", f"{base}/{quote(p['id'], safe='')}", body={"roles": [role]})
                    self.bump(st, "changed")
            except HTTPError as e:
                self.report.fail("space members", f"{sp['name']} ← {label}", e)
        for oid, p in have.items():
            if oid not in want and oid != self.admin_id:
                self.c.req("DELETE", f"{base}/{quote(p['id'], safe='')}", ok=(204, 404))
                self.bump(st, "removed")

    # ------------------------------------------------------------- files
    def listing(self, drive_id, folders, user=None) -> dict[str, dict]:
        """path -> {size, mtime, fileid, tags, dir} for the given folders (depth 1 each)."""
        prefix = unquote(dav(drive_id))

        def one(folder):
            try:
                _, _, data = self.c.req("PROPFIND", dav(drive_id, folder), user=user, body=PROPFIND_BODY,
                                        headers={"Depth": "1", "Content-Type": "application/xml"})
            except HTTPError as e:
                if e.status == 404:
                    return {}
                raise
            res = {}
            for r in ET.fromstring(data).findall("d:response", NS):
                rel = unquote(r.findtext("d:href", "", NS)).split(prefix, 1)[-1].strip("/")
                p = r.find("d:propstat/d:prop", NS)
                if p is None:
                    continue
                lm = p.findtext("d:getlastmodified", None, NS)
                res[rel] = {"dir": p.find("d:resourcetype/d:collection", NS) is not None,
                            "size": int(p.findtext("d:getcontentlength", "0", NS) or 0),
                            "mtime": int(parsedate_to_datetime(lm).timestamp()) if lm else 0,
                            "fileid": p.findtext("oc:fileid", None, NS),
                            "tags": [x for x in (p.findtext("oc:tags", "", NS) or "").split(",") if x]}
            return res

        out = {}
        with ThreadPoolExecutor(6) as ex:
            for r in ex.map(one, sorted(folders)):
                out.update(r)
        return out

    def put(self, drive_id, rel, src: Path, mtime_iso, user=None) -> str:
        _, headers, _ = self.c.req("PUT", dav(drive_id, rel), user=user, body=src.read_bytes(),
                                   headers={"X-OC-Mtime": str(ts(mtime_iso)), "Content-Type": "application/octet-stream"})
        return headers.get("OC-FileId") or headers.get("Oc-Fileid")

    def make_folders(self, drive_id, folders, have, user=None):
        """Create missing folders level by level (a level in parallel)."""
        levels: dict[int, list] = {}
        for f in folders:
            if f not in have:
                levels.setdefault(f.count("/"), []).append(f)
        for depth in sorted(levels):
            with ThreadPoolExecutor(6) as ex:
                list(ex.map(lambda f: self.c.req("MKCOL", dav(drive_id, f), user=user, ok=(201, 405)), levels[depth]))

    def sync_files(self, drive_id, files, *, can_edit=frozenset(), personal_user=None, workers=None, progress=None):
        """Upload missing or changed files of one space. Returns {'new': n, 'changed': n, 'same': n}."""
        folders = folders_of(f["path"] for f in files)
        have = self.listing(drive_id, {""} | set(folders), user=personal_user)
        self.make_folders(drive_id, folders, have, user=personal_user)
        stats = {"new": 0, "changed": 0, "same": 0}

        def uploader(author):
            if personal_user:
                return personal_user
            return author if author in can_edit and self.c.can_login(author) else None

        def one(f):
            try:
                src = DATA / f["file"]
                cur = have.get(f["path"])
                state = upload_state(cur, src.stat().st_size, ts(f["modified"]))
                if state == "same":
                    self.files[f["file"]] = {"fileid": cur["fileid"], "tags": cur["tags"], "fresh": False}
                elif state == "changed" and not f["versions"]:
                    # overwrite in place: keeps file id, shares and tags; old content becomes a version
                    fid = self.put(drive_id, f["path"], src, f["modified"], user=uploader(f["author"]))
                    self.files[f["file"]] = {"fileid": fid or cur["fileid"], "tags": cur["tags"], "fresh": False}
                else:
                    # new file, or a changed file whose version history must be rebuilt exactly
                    if state == "changed":
                        self.c.req("DELETE", dav(drive_id, f["path"]), user=personal_user, ok=(204, 404))
                    for v in f["versions"]:
                        self.put(drive_id, f["path"], DATA / v["file"], v["modified"], user=uploader(v["author"]))
                    fid = self.put(drive_id, f["path"], src, f["modified"], user=uploader(f["author"]))
                    self.files[f["file"]] = {"fileid": fid, "tags": [], "fresh": True}
                self.bump(stats, state)
            except Exception as e:  # noqa: BLE001
                self.report.fail("files", f["file"], e)
                if f["versions"]:
                    self.report.fail("older versions", f["file"], "its file failed", len(f["versions"]))
            finally:
                if progress:
                    progress.tick()

        with ThreadPoolExecutor(workers or self.workers) as ex:
            list(ex.map(one, files))
        return stats

    def seed_space_files(self):
        t = self.phase("Files in spaces")
        files = [f for sp in self.spaces for f in self.by_space.get(sp["id"], [])]
        self.report.expect("files", len(files))
        self.report.expect("older versions", sum(len(f["versions"]) for f in files))
        self.report.expect("folder shares", sum(len(sp.get("folder_shares", [])) for sp in self.spaces))
        progress = Progress("files in spaces", len(files))
        totals = {"new": 0, "changed": 0, "same": 0}
        lock = threading.Lock()

        def one(sp):
            if sp["id"] not in self.drives:
                n = len(self.by_space.get(sp["id"], []))
                self.report.skip("files", "space missing", n)
                if sp.get("folder_shares"):
                    self.report.skip("folder shares", "space missing", len(sp["folder_shares"]))
                progress.tick(n)
                return
            drive = self.drives[sp["id"]]
            self.space_specials(sp, drive)
            can_edit = expand([m["member"] for m in sp["members"] if m["role"] in ("manager", "editor")], self.groups)
            stats = self.sync_files(drive["id"], self.by_space.get(sp["id"], []), can_edit=can_edit, progress=progress)
            with lock:
                for k in totals:
                    totals[k] += stats[k]
            self.folder_shares(sp, drive["id"])

        try:
            self.each("spaces", self.spaces, one, workers=3)
        finally:
            progress.close()
        self.done(t, f"{totals['new']:,} uploaded, {totals['changed']:,} updated, {totals['same']:,} unchanged")

    def space_specials(self, sp, drive):
        folder = DATA / sp["folder"] / ".space"
        if not folder.exists():
            return
        have = self.listing(drive["id"], {"", ".space"})
        if ".space" not in have:
            self.c.req("MKCOL", dav(drive["id"], ".space"), ok=(201, 405))
        mtime = self.meta["reference_date"] + "T09:00:00"
        special = []
        for name, fn in (("readme", "readme.md"), ("image", "image.jpg")):
            src = folder / fn
            if not src.exists():
                continue
            cur = have.get(f".space/{fn}")
            fid = cur["fileid"] if cur and cur["size"] == src.stat().st_size else \
                self.put(drive["id"], f".space/{fn}", src, mtime)
            special.append({"specialFolder": {"name": name}, "id": fid})
        current = {s.get("specialFolder", {}).get("name"): s.get("id") for s in drive.get("special", [])}
        if special and any(current.get(s["specialFolder"]["name"]) != s["id"] for s in special):
            self.c.req("PATCH", f"/graph/v1.0/drives/{drive['id']}", body={"special": special})

    def folder_shares(self, sp, drive_id):
        """Share single folders of a space (e.g. with external partner groups)."""
        for fs in sp.get("folder_shares", []):
            label = f"{sp['name']}/{fs['folder']} → {fs['member']}"
            try:
                try:
                    _, _, data = self.c.req("PROPFIND", dav(drive_id, fs["folder"]), body=PROPFIND_BODY,
                                            headers={"Depth": "0", "Content-Type": "application/xml"})
                except HTTPError as e:
                    if e.status == 404:
                        self.report.skip("folder shares", "folder does not exist")
                        continue
                    raise
                fid = ET.fromstring(data).findtext("d:response/d:propstat/d:prop/oc:fileid", None, NS)
                if not fid:
                    self.report.skip("folder shares", "folder does not exist")
                    continue
                rec = self.recipient(fs["member"])
                perms = self.c.json("GET", item_path(drive_id, fid)).get("value", [])
                if rec["objectId"] in granted_ids(perms):
                    continue
                role = ROLE["folder_editor"] if fs["role"] == "editor" else ROLE["file_viewer"]
                self.c.req("POST", item_path(drive_id, fid, "invite"), body={"recipients": [rec], "roles": [role]})
            except Exception as e:  # noqa: BLE001
                self.report.fail("folder shares", label, e)

    def seed_personal_files(self):
        t = self.phase("Personal files")
        files = [f for p in self.personal for f in self.by_space.get(f"personal:{p['user']}", [])]
        self.report.expect("files", len(files))
        self.report.expect("older versions", sum(len(f["versions"]) for f in files))
        progress = Progress("personal files", len(files))
        totals = {"new": 0, "changed": 0, "same": 0}
        lock = threading.Lock()

        def one(pf):
            user, key = pf["user"], f"personal:{pf['user']}"
            own = self.by_space.get(key, [])
            if not self.c.can_login(user):
                self.report.skip("files", "cannot log in as owner (password differs?)", len(own))
                progress.tick(len(own))
                return
            drives = self.c.paged("/graph/v1.0/me/drives?$filter=driveType eq 'personal'", user=user)
            if not drives:
                self.report.skip("files", "owner has no personal space", len(own))
                progress.tick(len(own))
                return
            self.drive_of[key], self.owner_of[key] = drives[0]["id"], user
            stats = self.sync_files(drives[0]["id"], own, personal_user=user, workers=2, progress=progress)
            with lock:
                for k in totals:
                    totals[k] += stats[k]

        try:
            self.each("personal spaces", self.personal, one)
        finally:
            progress.close()
        self.done(t, f"{len(self.personal):,} users: {totals['new']:,} uploaded, {totals['changed']:,} updated, "
                     f"{totals['same']:,} unchanged")

    # ------------------------------------------------------------- tags, shares, links, favourites
    def seed_metadata(self, favorites_enabled: bool):
        t = self.phase("Tags, shares, links and favourites")
        files = [f for f in self.meta["files"] if f["space"] in self.drive_of]
        ref = self.meta["reference_date"]
        self.report.expect("tagged files", sum(1 for f in files if f["tags"]))
        self.report.expect("shares", sum(len(f["shares"]) for f in files))
        self.report.expect("public links", sum(len(f["links"]) for f in files))
        self.report.expect("favourites", sum(1 for f in files if f["favorite"]))
        spaces = {s["id"]: s for s in self.meta["spaces"]}

        def one(f):
            info = self.files.get(f["file"])
            if not info or not info.get("fileid"):
                self.skip_metadata(f, "file missing")
                return
            fid, drive_id, actor = info["fileid"], self.drive_of[f["space"]], self.owner_of.get(f["space"])
            missing_tags = [x for x in f["tags"] if x not in info["tags"]]
            if missing_tags:
                try:
                    self.c.req("PUT", "/graph/v1.0/extensions/org.libregraph/tags", user=actor,
                               body={"resourceId": fid, "tags": missing_tags})
                except HTTPError as e:
                    self.report.fail("tagged files", f["file"], e)
            if f["shares"] or f["links"]:
                # a file uploaded in this run cannot have permissions yet – skip the lookup
                perms = [] if info["fresh"] else self.c.json("GET", item_path(drive_id, fid), user=actor).get("value", [])
                self.apply_shares(f, drive_id, fid, actor, granted_ids(perms))
                self.apply_links(f, drive_id, fid, actor, link_names(perms), ref)
            if f["favorite"]:
                self.apply_favorite(f, drive_id, actor, favorites_enabled, spaces)

        self.each("metadata", files, one, "tags, shares, links")
        self.done(t, "done")

    def skip_metadata(self, f, reason):
        if f["tags"]:
            self.report.skip("tagged files", reason)
        if f["shares"]:
            self.report.skip("shares", reason, len(f["shares"]))
        if f["links"]:
            self.report.skip("public links", reason, len(f["links"]))
        if f["favorite"]:
            self.report.skip("favourites", reason)

    def apply_shares(self, f, drive_id, fid, actor, have):
        for s in f["shares"]:
            try:
                rec = self.recipient(s["to"])
                if rec["objectId"] in have:
                    continue
                body = {"recipients": [rec], "roles": [ROLE["file_editor" if s["role"] == "editor" else "file_viewer"]]}
                if s.get("expires"):
                    body["expirationDateTime"] = s["expires"] + "T23:59:59Z"
                self.c.req("POST", item_path(drive_id, fid, "invite"), user=actor, body=body)
            except Exception as e:  # noqa: BLE001
                self.report.fail("shares", f"{f['file']} → {s['to']}", e)

    def apply_links(self, f, drive_id, fid, actor, have, reference_date):
        for link in f["links"]:
            if link["name"] in have:
                continue
            if link.get("expires") and link["expires"] < reference_date:
                self.report.skip("public links", "already expired at the dataset date")
                continue
            body = {"type": "edit" if link["role"] == "editor" else "view", "displayName": link["name"],
                    "password": self.link_password}
            if link.get("expires"):
                body["expirationDateTime"] = link["expires"] + "T23:59:59Z"
            try:
                self.c.req("POST", item_path(drive_id, fid, "createLink"), user=actor, body=body)
            except HTTPError as e:
                self.report.fail("public links", f["file"], e)

    def apply_favorite(self, f, drive_id, actor, enabled, spaces):
        if not enabled:
            self.report.skip("favourites", "disabled on this server")
            return
        who = actor or f["author"]
        if not actor and who not in expand([m["member"] for m in spaces[f["space"]]["members"]], self.groups):
            self.report.skip("favourites", "author has no access")
            return
        if not self.c.can_login(who):
            self.report.skip("favourites", "cannot log in as user")
            return
        try:
            self.c.req("PROPPATCH", dav(drive_id, f["path"]), user=who, body=FAVORITE_BODY,
                       headers={"Content-Type": "application/xml"})
        except HTTPError as e:
            self.report.fail("favourites", f["file"], e)

    # ------------------------------------------------------------- reset
    def reset(self):
        t = self.phase("Deleting the dataset")
        names = {s["name"] for s in self.meta["spaces"]}
        drives = [d for d in self.c.paged("/graph/v1.0/drives?$filter=driveType eq 'project'") if d["name"] in names]
        self.report.expect("spaces deleted", len(drives))

        def delete_drive(d):
            self.c.req("DELETE", f"/graph/v1.0/drives/{d['id']}", ok=(204, 404))                       # disable
            self.c.req("DELETE", f"/graph/v1.0/drives/{d['id']}", headers={"Purge": "T"}, ok=(204, 404))  # purge
        self.each("spaces deleted", drives, delete_drive, workers=4)
        ids = {u["id"] for u in self.meta["users"]}
        users = [u for u in self.c.paged("/graph/v1.0/users?$select=id,onPremisesSamAccountName")
                 if u["onPremisesSamAccountName"] in ids]
        self.report.expect("users deleted", len(users))
        self.each("users deleted", users, lambda u: self.c.req("DELETE", f"/graph/v1.0/users/{u['id']}", ok=(204, 404)))
        gnames = {g["id"] for g in self.meta["groups"]}
        groups = [g for g in self.c.paged("/graph/v1.0/groups") if g["displayName"] in gnames]
        self.report.expect("groups deleted", len(groups))
        self.each("groups deleted", groups, lambda g: self.c.req("DELETE", f"/graph/v1.0/groups/{g['id']}", ok=(204, 404)))
        self.done(t, f"{len(drives)} spaces, {len(users)} users, {len(groups)} groups")

    def use_existing_directory(self):
        """--skip-users: look up the ids of users and groups that already exist."""
        self.uids = {u["onPremisesSamAccountName"]: u["id"]
                     for u in self.c.paged("/graph/v1.0/users?$select=id,onPremisesSamAccountName")}
        self.gids = {g["displayName"]: g["id"] for g in self.c.paged("/graph/v1.0/groups")}


# =============================================================================
# Command line
# =============================================================================

def ask(prompt, default, secret=False):
    import getpass
    shown = "hidden – Enter keeps it" if secret and default else default
    try:
        val = (getpass.getpass if secret else input)(f"  {prompt} [{shown}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(1)
    return val or default


def connect(url, user, password, demo_pw, verify):
    c = Client(url, (user, password), demo_pw, verify)
    try:
        me = c.json("GET", "/graph/v1.0/me")
    except HTTPError as e:
        if e.status == 0:
            hint = " – try https://localhost:9200" if "host.docker.internal" in url else ""
            sys.exit(f"\n✗ Cannot reach {url}{hint}\n  ({e})")
        if e.status == 401:
            sys.exit("\n✗ Login failed (401). Check user/password – and that basic auth is enabled on the server\n"
                     "  (OpenCloud: PROXY_ENABLE_BASIC_AUTH=true).")
        sys.exit(f"\n✗ Unexpected answer from {url}: {e}")
    return c, me


def load_metadata() -> dict:
    path = DATA / "metadata.json"
    if not path.exists():
        sys.exit(f"✗ {path} not found. Run populate.py from the repository folder (it needs the data/ folder).")
    meta = json.loads(path.read_text(encoding="utf-8"))
    check_metadata(meta)
    return meta


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", help=f"OpenCloud URL (default: {SETTINGS['url']})")
    ap.add_argument("--admin", help="admin credentials as user:password (or user:app-token)")
    ap.add_argument("--yes", "-y", action="store_true", help="do not ask anything, use SETTINGS / environment")
    ap.add_argument("--user-password", help="password for all demo users (default: demo)")
    ap.add_argument("--link-password", help="password for public links")
    ap.add_argument("--only", help="comma separated space ids, e.g. harrow-bay,personal:elena.marquez")
    ap.add_argument("--skip-users", action="store_true", help="do not create users/groups (they must exist)")
    ap.add_argument("--skip-files", action="store_true", help="only users, groups and spaces")
    ap.add_argument("--skip-photos", action="store_true", help="do not set profile pictures")
    ap.add_argument("--workers", type=int, default=6, help="parallel requests (default: 6)")
    ap.add_argument("--verify-tls", action="store_true", help="verify TLS certificates")
    ap.add_argument("--reset", action="store_true", help="delete all spaces, users and groups of this dataset")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    env = os.environ
    url = args.url or env.get("OC_URL") or SETTINGS["url"]
    if args.admin:
        user, _, pw = args.admin.partition(":")
    else:
        user = env.get("OC_ADMIN_USER") or SETTINGS["admin_user"]
        pw = env.get("OC_ADMIN_PASSWORD") or SETTINGS["admin_password"]
    demo_pw = args.user_password or env.get("OC_USER_PASSWORD") or SETTINGS["demo_user_password"]
    link_pw = args.link_password or env.get("OC_LINK_PASSWORD") or SETTINGS["link_password"]
    verify = args.verify_tls or SETTINGS["verify_tls"]

    log("\nWeyland Energy – OpenCloud demo data\n")
    meta = load_metadata()
    interactive = sys.stdin.isatty() and not args.yes
    if interactive and not (args.url and args.admin):
        log("Press Enter to accept the value in [brackets].")
        url = ask("OpenCloud URL", url).rstrip("/")
        user = ask("Admin user", user)
        pw = ask("Admin password", pw, secret=True)
    client, me = connect(url, user, pw, demo_pw, verify)
    log(f"\n✓ Connected to {url} as {me.get('displayName', user)}")

    only = set(args.only.split(",")) if args.only else None
    seeder = Seeder(client, meta, admin_id=me["id"], workers=max(1, args.workers), link_password=link_pw, only=only)
    summary = meta["summary"]

    if args.reset:
        log(f"\nThis DELETES the {len(meta['spaces'])} spaces, {summary['users']} users and {summary['groups']} "
            "groups of the dataset.")
        if not args.yes and ask("Type 'delete' to confirm", "") != "delete":
            sys.exit("Aborted.")
        seeder.reset()
        seeder.report.print()
        return 1 if seeder.report.has_errors else 0

    log(f"\nThis will create/update: {summary['users']} users (with profile pictures), {summary['groups']} groups, "
        f"{summary['spaces']} spaces and {summary['files']:,} files\n(+ versions, tags, shares, public links). "
        f"Demo users get the password '{demo_pw}'.")
    if interactive and ask("Start now? (y/n)", "y").lower() not in ("y", "yes", "j", "ja"):
        sys.exit("Aborted.")
    log()

    caps = client.json("GET", "/ocs/v1.php/cloud/capabilities?format=json")["ocs"]["data"]["capabilities"]
    favorites = bool(caps.get("files", {}).get("favorites"))
    started = time.time()
    if args.skip_users:
        seeder.use_existing_directory()
    else:
        seeder.seed_users()
        seeder.seed_groups()
        if not args.skip_photos:
            seeder.seed_photos()
    seeder.seed_spaces()
    if not args.skip_files:
        seeder.seed_space_files()
        seeder.seed_personal_files()
        seeder.seed_metadata(favorites)
    seeder.report.print()
    log(f"\nDone in {fmt_duration(time.time() - started)}. Demo users log in with their username "
        f"(e.g. elena.marquez) and the password '{demo_pw}'.")
    return 1 if seeder.report.has_errors else 0


if __name__ == "__main__":
    sys.exit(main())
