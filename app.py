#!/usr/bin/env python3
"""Jellyfin Home Manager - dependency-free web service."""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
DATA_DIR = Path(os.environ.get("JHM_DATA_DIR", APP_DIR / "data"))
CONFIG_FILE = DATA_DIR / "config.json"
BACKUP_DIR = DATA_DIR / "backups"
MAX_BODY = 1_000_000
SPECIAL_PLAYLISTS_ID = "special:playlists"
CONFIG_KEYS = (
    "AudioLanguagePreference", "PlayDefaultAudioTrack", "SubtitleLanguagePreference",
    "DisplayMissingEpisodes", "GroupedFolders", "SubtitleMode", "DisplayCollectionsView",
    "EnableLocalPassword", "HidePlayedInLatest", "RememberAudioSelections",
    "RememberSubtitleSelections", "EnableNextEpisodeAutoPlay",
)


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def load_local() -> dict:
    default = {"master": {"libraries": [], "settings": {}}, "lastDiscovery": None}
    if not CONFIG_FILE.exists():
        return default
    try:
        value = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else default
    except (OSError, json.JSONDecodeError):
        return default


class Jellyfin:
    def __init__(self):
        self.base = os.environ.get("JELLYFIN_URL", "").rstrip("/")
        self.token = os.environ.get("JELLYFIN_API_KEY", "").strip()
        self.timeout = float(os.environ.get("JELLYFIN_TIMEOUT", "15"))
        self.verify_tls = os.environ.get("JELLYFIN_VERIFY_TLS", "true").lower() not in ("0", "false", "no")
        if not self.base or not self.token:
            raise ApiError(503, "JELLYFIN_URL and JELLYFIN_API_KEY must be configured")

    def request(self, method: str, path: str, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={
                "Authorization": self.authorization_header(),
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        context = None if self.verify_tls else ssl._create_unverified_context()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=context) as res:
                raw = res.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            detail = exc.read(2048).decode(errors="replace")
            raise ApiError(502, f"Jellyfin returned HTTP {exc.code}: {detail or exc.reason}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ApiError(502, f"Cannot reach Jellyfin: {exc.reason if hasattr(exc, 'reason') else exc}") from exc

    def authorization_header(self) -> str:
        # Jellyfin 12 rejects the legacy X-Emby-Token header by default.
        safe_token = self.token.replace("\\", "\\\\").replace('"', '\\"')
        return (
            'MediaBrowser Client="Jellyfin Home Manager", Device="Server", '
            'DeviceId="jellyfin-home-manager", Version="1.0.0", '
            f'Token="{safe_token}"'
        )

    def users(self) -> list[dict]:
        return self.request("GET", "/Users") or []

    def libraries(self, users: list[dict] | None = None) -> list[dict]:
        folders = self.request("GET", "/Library/VirtualFolders") or []
        result = []
        seen = set()
        for folder in folders:
            item_id = folder.get("ItemId")
            if item_id:
                result.append({"id": item_id, "name": folder.get("Name") or item_id, "collectionType": folder.get("CollectionType") or ""})
                seen.add(item_id)
        # Playlists is a system-created view, not a virtual folder. Its raw ID is
        # user-specific, so expose one logical template entry and translate it
        # back to each user's real ID when generating their configuration.
        playlists_name = None
        for user in users if users is not None else self.users():
            if (user.get("Policy") or {}).get("IsDisabled") or not user.get("Id"):
                continue
            for view in self.user_views(user["Id"]):
                collection_type = str(view.get("CollectionType") or "").lower()
                if view.get("Id") and collection_type == "playlists" and playlists_name is None:
                    playlists_name = view.get("Name") or "Playlists"
        if playlists_name:
            result.append({"id": SPECIAL_PLAYLISTS_ID, "name": playlists_name, "collectionType": "playlists"})
        return result

    def user_views(self, user_id: str) -> list[dict]:
        # includeHidden is essential for managing a view after it has been
        # excluded from the user's Home screen. Jellyfin Web uses the same flag.
        payload = self.request("GET", f"/Users/{urllib.parse.quote(user_id)}/Views?includeHidden=true") or {}
        return payload.get("Items", [])

    def accessible_views(self, user_id: str) -> set[str]:
        return {x.get("Id") for x in self.user_views(user_id) if x.get("Id")}

    def view_id_map(self, user_id: str) -> dict[str, str]:
        mapping = {}
        for view in self.user_views(user_id):
            item_id = view.get("Id")
            if not item_id:
                continue
            mapping[item_id] = item_id
            if str(view.get("CollectionType") or "").lower() == "playlists":
                mapping[SPECIAL_PLAYLISTS_ID] = item_id
        return mapping

    def user(self, user_id: str) -> dict:
        return self.request("GET", f"/Users/{urllib.parse.quote(user_id)}") or {}

    def update_configuration(self, user_id: str, config: dict) -> None:
        self.request("POST", f"/Users/{urllib.parse.quote(user_id)}/Configuration", config)

    def update_policy(self, user_id: str, policy: dict) -> None:
        self.request("POST", f"/Users/{urllib.parse.quote(user_id)}/Policy", policy)


def validate_master(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ApiError(400, "master must be an object")
    libraries = value.get("libraries", [])
    settings = value.get("settings", {})
    if not isinstance(libraries, list) or not isinstance(settings, dict):
        raise ApiError(400, "libraries must be a list and settings must be an object")
    seen, cleaned = set(), []
    for row in libraries:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"] or row["id"] in seen:
            raise ApiError(400, "Each library needs a unique id")
        seen.add(row["id"])
        cleaned.append({"id": row["id"], "name": str(row.get("name", row["id"])), "myMedia": bool(row.get("myMedia", True)), "latest": bool(row.get("latest", True))})
    return {"libraries": cleaned, "settings": {k: settings[k] for k in CONFIG_KEYS if k in settings}}


def desired_config(current: dict, master: dict, accessible: set[str] | dict[str, str]) -> tuple[dict, list[dict]]:
    config = dict(current.get("Configuration") or {})
    view_ids = accessible if isinstance(accessible, dict) else {item_id: item_id for item_id in accessible}
    rows = [row for row in master["libraries"] if row["id"] in view_ids]
    actual_ids = {row["id"]: view_ids[row["id"]] for row in rows}
    config.update(master["settings"])
    config["OrderedViews"] = [actual_ids[row["id"]] for row in rows]
    config["MyMediaExcludes"] = [actual_ids[row["id"]] for row in rows if not row["myMedia"]]
    config["LatestItemsExcludes"] = [actual_ids[row["id"]] for row in rows if not row["latest"]]
    return config, rows


def relevant(config: dict) -> dict:
    return {k: config.get(k) for k in (*CONFIG_KEYS, "OrderedViews", "MyMediaExcludes", "LatestItemsExcludes") if k in config}


def current_access(policy: dict, library_ids: list[str]) -> tuple[str, list[str]]:
    if policy.get("EnableAllFolders", True):
        return "all", list(library_ids)
    enabled = set(policy.get("EnabledFolders") or [])
    return "selected", [item_id for item_id in library_ids if item_id in enabled]


def users_payload(users: list[dict], library_ids: list[str]) -> list[dict]:
    result = []
    for user in users:
        policy = user.get("Policy") or {}
        mode, selected = current_access(policy, library_ids)
        result.append({
            "id": user["Id"], "name": user.get("Name", user["Id"]),
            "disabled": bool(policy.get("IsDisabled")),
            "administrator": bool(policy.get("IsAdministrator")),
            "accessMode": mode, "libraryIds": selected,
        })
    return result


def validate_access(value, users: list[dict], library_ids: list[str]) -> dict[str, dict]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ApiError(400, "access must be an object")
    valid_users, valid_libraries = {u.get("Id") for u in users}, set(library_ids)
    cleaned = {}
    for user_id, spec in value.items():
        if user_id not in valid_users or not isinstance(spec, dict):
            raise ApiError(400, "Access contains an unknown user")
        mode = spec.get("mode")
        if mode not in ("all", "selected"):
            raise ApiError(400, "Access mode must be all or selected")
        selected = spec.get("libraryIds", [])
        if not isinstance(selected, list) or any(x not in valid_libraries for x in selected):
            raise ApiError(400, "Access contains an unknown library")
        cleaned[user_id] = {"mode": mode, "libraryIds": [x for x in library_ids if x in set(selected)]}
    return cleaned


def desired_policy(current: dict, spec: dict | None, library_ids: list[str]) -> tuple[dict, list[str]]:
    policy = dict(current.get("Policy") or {})
    if not spec:
        _, effective = current_access(policy, library_ids)
        return policy, effective
    if spec["mode"] == "all":
        policy["EnableAllFolders"] = True
        policy["EnabledFolders"] = []
        return policy, list(library_ids)
    policy["EnableAllFolders"] = False
    policy["EnabledFolders"] = list(spec["libraryIds"])
    return policy, list(spec["libraryIds"])


def relevant_policy(policy: dict) -> dict:
    return {
        "EnableAllFolders": bool(policy.get("EnableAllFolders", True)),
        "EnabledFolders": sorted(policy.get("EnabledFolders") or []),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "JellyfinHomeManager/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

    def send_json(self, status: int, value) -> None:
        raw = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers(); self.wfile.write(raw)

    def body(self):
        try: length = int(self.headers.get("Content-Length", "0"))
        except ValueError: raise ApiError(400, "Invalid Content-Length")
        if length > MAX_BODY: raise ApiError(413, "Request too large")
        try: return json.loads(self.rfile.read(length)) if length else {}
        except json.JSONDecodeError: raise ApiError(400, "Invalid JSON")

    def route(self):
        if not self.path.startswith("/api/"):
            name = "index.html" if self.path in ("/", "/index.html") else self.path.lstrip("/")
            if name not in ("index.html", "app.js", "style.css"):
                raise ApiError(404, "Not found")
            raw = (STATIC_DIR / name).read_bytes()
            ctype = {"html": "text/html", "js": "application/javascript", "css": "text/css"}[name.rsplit(".", 1)[1]]
            self.send_response(200); self.send_header("Content-Type", ctype + "; charset=utf-8"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw); return
        jf = Jellyfin()
        if self.command == "GET" and self.path == "/api/state":
            local = load_local(); users = jf.users(); libraries = jf.libraries(users)
            library_ids = [x["id"] for x in libraries if x["id"] != SPECIAL_PLAYLISTS_ID]
            return self.send_json(200, {**local, "users": users_payload(users, library_ids), "libraries": libraries})
        if self.command == "POST" and self.path == "/api/refresh":
            local = load_local(); users = jf.users(); old = {x["id"]: x for x in local.get("master", {}).get("libraries", [])}; discovered = jf.libraries(users)
            ids = {x["id"] for x in discovered}; live = {x["id"]: x for x in discovered}; changes = {"new": [], "renamed": [], "removed": []}; merged = []
            # Retain the operator's saved order, update names in place, then append new libraries.
            for prev in local.get("master", {}).get("libraries", []):
                if prev["id"] not in live: continue
                lib = live[prev["id"]]
                if prev.get("name") != lib["name"]: changes["renamed"].append({"id": lib["id"], "from": prev.get("name"), "to": lib["name"]})
                merged.append({**prev, "name": lib["name"]})
            for lib in discovered:
                if lib["id"] not in old:
                    changes["new"].append(lib); merged.append({**lib, "myMedia": True, "latest": lib["id"] != SPECIAL_PLAYLISTS_ID})
            changes["removed"] = [x for x in old.values() if x["id"] not in ids]
            local["master"] = {"libraries": merged, "settings": local.get("master", {}).get("settings", {})}; local["lastDiscovery"] = utcnow(); atomic_json(CONFIG_FILE, local)
            library_ids = [x["id"] for x in discovered if x["id"] != SPECIAL_PLAYLISTS_ID]
            return self.send_json(200, {"master": local["master"], "libraries": discovered, "users": users_payload(users, library_ids), "changes": changes, "lastDiscovery": local["lastDiscovery"]})
        if self.command == "PUT" and self.path == "/api/master":
            master = validate_master(self.body().get("master")); local = load_local(); local["master"] = master; atomic_json(CONFIG_FILE, local); return self.send_json(200, {"master": master})
        if self.command == "POST" and self.path == "/api/preview":
            body = self.body(); master = validate_master(body.get("master")); previews = []; users = jf.users()
            library_ids = [x["id"] for x in jf.libraries(users) if x["id"] != SPECIAL_PLAYLISTS_ID]
            access = validate_access(body.get("access"), users, library_ids)
            library_names = {x["id"]: x["name"] for x in master["libraries"]}
            for user in users:
                if (user.get("Policy") or {}).get("IsDisabled"): continue
                uid = user["Id"]; current = user if "Configuration" in user else jf.user(uid)
                before_ids = set(current_access(current.get("Policy") or {}, library_ids)[1])
                policy, effective_ids = desired_policy(current, access.get(uid), library_ids)
                view_map = jf.view_id_map(uid); view_map.update({x: x for x in effective_ids})
                desired, rows = desired_config(current, master, view_map)
                after_ids = set(effective_ids); access_changes = []
                for item_id in library_ids:
                    if (item_id in before_ids) != (item_id in after_ids):
                        access_changes.append({"id": item_id, "name": library_names.get(item_id, item_id), "before": item_id in before_ids, "after": item_id in after_ids})
                previews.append({"id": uid, "name": user.get("Name", uid), "libraries": rows, "accessChanges": access_changes, "before": relevant(current.get("Configuration") or {}), "after": relevant(desired), "policyAfter": relevant_policy(policy)})
            return self.send_json(200, {"users": previews})
        if self.command == "POST" and self.path == "/api/apply":
            body = self.body(); master = validate_master(body.get("master")); local = load_local(); local["master"] = master; atomic_json(CONFIG_FILE, local)
            users = jf.users(); library_ids = [x["id"] for x in jf.libraries(users) if x["id"] != SPECIAL_PLAYLISTS_ID]
            access = validate_access(body.get("access"), users, library_ids)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"); backup = {"createdAt": utcnow(), "server": jf.base, "users": []}; plan = []
            for user in users:
                if (user.get("Policy") or {}).get("IsDisabled"): continue
                uid = user["Id"]; full = jf.user(uid); backup["users"].append(full)
                policy, effective_ids = desired_policy(full, access.get(uid), library_ids)
                plan.append((uid, user.get("Name", uid), policy, effective_ids, full))
            backup_path = BACKUP_DIR / f"users-{stamp}-{secrets.token_hex(3)}.json"; atomic_json(backup_path, backup)
            results = []
            for uid, name, policy, effective_ids, full in plan:
                try:
                    if uid in access: jf.update_policy(uid, policy)
                    policy_actual = (jf.user(uid).get("Policy") or {})
                    policy_expected_rel = relevant_policy(policy); policy_actual_rel = relevant_policy(policy_actual)
                    view_map = jf.view_id_map(uid); view_map.update({x: x for x in effective_ids})
                    desired, _ = desired_config(full, master, view_map)
                    jf.update_configuration(uid, desired); actual = (jf.user(uid).get("Configuration") or {}); expected_rel = relevant(desired); actual_rel = relevant(actual)
                    policy_ok = uid not in access or policy_actual_rel == policy_expected_rel
                    results.append({"id": uid, "name": name, "ok": policy_ok and actual_rel == expected_rel, "expected": expected_rel, "actual": actual_rel, "policyExpected": policy_expected_rel, "policyActual": policy_actual_rel})
                except ApiError as exc: results.append({"id": uid, "name": name, "ok": False, "error": exc.message})
            ok = all(x["ok"] for x in results)
            return self.send_json(200 if ok else 207, {"ok": ok, "backup": backup_path.name, "results": results})
        raise ApiError(404, "Not found")

    def do_GET(self): self._handle()
    def do_POST(self): self._handle()
    def do_PUT(self): self._handle()
    def _handle(self):
        try: self.route()
        except ApiError as exc: self.send_json(exc.status, {"error": exc.message})
        except Exception as exc:
            self.log_error("Unhandled: %r", exc); self.send_json(500, {"error": "Internal server error"})


if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True); BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    host = os.environ.get("JHM_HOST", "0.0.0.0"); port = int(os.environ.get("JHM_PORT", "8080"))
    print(f"Jellyfin Home Manager listening on http://{host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
