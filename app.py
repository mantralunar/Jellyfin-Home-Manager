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

    def libraries(self) -> list[dict]:
        folders = self.request("GET", "/Library/VirtualFolders") or []
        result = []
        for folder in folders:
            item_id = folder.get("ItemId")
            if item_id:
                result.append({"id": item_id, "name": folder.get("Name") or item_id, "collectionType": folder.get("CollectionType") or ""})
        return result

    def accessible_views(self, user_id: str) -> set[str]:
        payload = self.request("GET", f"/Users/{urllib.parse.quote(user_id)}/Views") or {}
        return {x.get("Id") for x in payload.get("Items", []) if x.get("Id")}

    def user(self, user_id: str) -> dict:
        return self.request("GET", f"/Users/{urllib.parse.quote(user_id)}") or {}

    def update_configuration(self, user_id: str, config: dict) -> None:
        self.request("POST", f"/Users/{urllib.parse.quote(user_id)}/Configuration", config)


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


def desired_config(current: dict, master: dict, accessible: set[str]) -> tuple[dict, list[dict]]:
    config = dict(current.get("Configuration") or {})
    rows = [row for row in master["libraries"] if row["id"] in accessible]
    config.update(master["settings"])
    config["OrderedViews"] = [row["id"] for row in rows]
    config["MyMediaExcludes"] = [row["id"] for row in rows if not row["myMedia"]]
    config["LatestItemsExcludes"] = [row["id"] for row in rows if not row["latest"]]
    return config, rows


def relevant(config: dict) -> dict:
    return {k: config.get(k) for k in (*CONFIG_KEYS, "OrderedViews", "MyMediaExcludes", "LatestItemsExcludes") if k in config}


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
            local = load_local(); users = jf.users(); libraries = jf.libraries()
            return self.send_json(200, {**local, "users": [{"id": u["Id"], "name": u.get("Name", u["Id"]), "disabled": bool((u.get("Policy") or {}).get("IsDisabled"))} for u in users], "libraries": libraries})
        if self.command == "POST" and self.path == "/api/refresh":
            local = load_local(); old = {x["id"]: x for x in local.get("master", {}).get("libraries", [])}; discovered = jf.libraries()
            ids = {x["id"] for x in discovered}; live = {x["id"]: x for x in discovered}; changes = {"new": [], "renamed": [], "removed": []}; merged = []
            # Retain the operator's saved order, update names in place, then append new libraries.
            for prev in local.get("master", {}).get("libraries", []):
                if prev["id"] not in live: continue
                lib = live[prev["id"]]
                if prev.get("name") != lib["name"]: changes["renamed"].append({"id": lib["id"], "from": prev.get("name"), "to": lib["name"]})
                merged.append({**prev, "name": lib["name"]})
            for lib in discovered:
                if lib["id"] not in old:
                    changes["new"].append(lib); merged.append({**lib, "myMedia": True, "latest": True})
            changes["removed"] = [x for x in old.values() if x["id"] not in ids]
            local["master"] = {"libraries": merged, "settings": local.get("master", {}).get("settings", {})}; local["lastDiscovery"] = utcnow(); atomic_json(CONFIG_FILE, local)
            return self.send_json(200, {"master": local["master"], "libraries": discovered, "changes": changes, "lastDiscovery": local["lastDiscovery"]})
        if self.command == "PUT" and self.path == "/api/master":
            master = validate_master(self.body().get("master")); local = load_local(); local["master"] = master; atomic_json(CONFIG_FILE, local); return self.send_json(200, {"master": master})
        if self.command == "POST" and self.path == "/api/preview":
            master = validate_master(self.body().get("master")); previews = []
            for user in jf.users():
                if (user.get("Policy") or {}).get("IsDisabled"): continue
                uid = user["Id"]; current = user if "Configuration" in user else jf.user(uid); desired, rows = desired_config(current, master, jf.accessible_views(uid))
                previews.append({"id": uid, "name": user.get("Name", uid), "libraries": rows, "before": relevant(current.get("Configuration") or {}), "after": relevant(desired)})
            return self.send_json(200, {"users": previews})
        if self.command == "POST" and self.path == "/api/apply":
            master = validate_master(self.body().get("master")); local = load_local(); local["master"] = master; atomic_json(CONFIG_FILE, local)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"); backup = {"createdAt": utcnow(), "server": jf.base, "users": []}; plan = []
            for user in jf.users():
                if (user.get("Policy") or {}).get("IsDisabled"): continue
                uid = user["Id"]; full = jf.user(uid); backup["users"].append(full); desired, _ = desired_config(full, master, jf.accessible_views(uid)); plan.append((uid, user.get("Name", uid), desired))
            backup_path = BACKUP_DIR / f"users-{stamp}-{secrets.token_hex(3)}.json"; atomic_json(backup_path, backup)
            results = []
            for uid, name, desired in plan:
                try:
                    jf.update_configuration(uid, desired); actual = (jf.user(uid).get("Configuration") or {}); expected_rel = relevant(desired); actual_rel = relevant(actual)
                    results.append({"id": uid, "name": name, "ok": actual_rel == expected_rel, "expected": expected_rel, "actual": actual_rel})
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
