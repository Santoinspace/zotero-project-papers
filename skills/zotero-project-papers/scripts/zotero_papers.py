#!/usr/bin/env python3
"""zotero-project-papers v0.5.2

Stdlib-only helper for an Agent Skill that binds a local project to a Zotero 10
collection and materializes Zotero-managed PDFs into the project reference dir.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import socket
import stat
import sys
import tempfile
import time
from time import perf_counter
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

APP_NAME = "Zotero Project Papers"
BASE_URL = os.environ.get("ZOTERO_LOCAL_API", "http://127.0.0.1:23119/api/").rstrip("/") + "/"
CONFIG_NAME = ".zotero-project.json"
SCHEMA_VERSION = 5
DEFAULT_COLLECTION_ROOT = "Projects"
DEFAULT_REFERENCE_DIR = "papers/reference"
DEFAULT_BIB_FILE = "papers/references.bib"
MANIFEST_NAME = "papers.json"
CLAIMS_FILE = "papers/claims.json"
REFERENCE_CANDIDATES = ("reference", "references", "refs", "literature", "literatures", "papers/reference", "papers/references")
AUTH_TIMEOUT = int(os.environ.get("ZPP_AUTH_TIMEOUT", "300"))
CACHE_TTL_SECONDS = int(os.environ.get("ZPP_CACHE_TTL", "60"))
DEFAULT_SEARCH_LIMIT = 10
DEFAULT_MATCH_THRESHOLD = float(os.environ.get("ZPP_MATCH_THRESHOLD", "0.45"))
CROSSREF_BASE_URL = os.environ.get("ZPP_CROSSREF_API", "https://api.crossref.org").rstrip("/")
TRUSTED_PAPER_HOSTS = {
    "arxiv.org", "export.arxiv.org", "openreview.net", "proceedings.neurips.cc",
    "openaccess.thecvf.com", "papers.miccai.org", "aclanthology.org", "jmlr.org",
    "proceedings.mlr.press", "dl.acm.org", "ieeexplore.ieee.org", "link.springer.com",
    "nature.com", "www.nature.com", "science.org", "www.science.org",
}

# Creating items does not require /items/new. Zotero Local API implementations may
# omit that Web-API convenience endpoint, so creation uses itemTypeFields when
# available and falls back to conservative local field sets.
COMMON_FALLBACK_FIELDS = {
    "title", "abstractNote", "date", "shortTitle", "url", "accessDate",
    "language", "rights", "extra", "DOI", "ISBN", "ISSN",
}
ITEM_TYPE_FALLBACK_FIELDS: dict[str, set[str]] = {
    "journalArticle": COMMON_FALLBACK_FIELDS | {
        "publicationTitle", "volume", "issue", "pages", "series",
        "seriesTitle", "journalAbbreviation", "archive", "archiveLocation",
        "libraryCatalog", "callNumber",
    },
    "conferencePaper": COMMON_FALLBACK_FIELDS | {
        "proceedingsTitle", "conferenceName", "place", "publisher", "volume",
        "pages", "series", "archive", "archiveLocation", "libraryCatalog",
        "callNumber",
    },
    "preprint": COMMON_FALLBACK_FIELDS | {
        "repository", "archiveID", "place", "type", "number",
    },
    "bookSection": COMMON_FALLBACK_FIELDS | {
        "bookTitle", "series", "seriesNumber", "volume", "numberOfVolumes",
        "edition", "place", "publisher", "pages",
    },
    "report": COMMON_FALLBACK_FIELDS | {
        "reportNumber", "reportType", "institution", "place", "seriesTitle",
    },
    "thesis": COMMON_FALLBACK_FIELDS | {
        "thesisType", "university", "place",
    },
    "webpage": COMMON_FALLBACK_FIELDS | {
        "websiteTitle", "websiteType",
    },
}


class ZPPError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "zpp_error",
        hint: str | None = None,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.hint = hint
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"type": self.code, "message": str(self)}
        if self.hint:
            data["hint"] = self.hint
        if self.details:
            data["details"] = self.details
        return data


def configure_stdio_utf8() -> None:
    """Make CLI output safe on Windows consoles using legacy encodings such as GBK."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def json_dump(data: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def user_config_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "zotero-project-papers"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return Path(xdg) / "zotero-project-papers" if xdg else Path.home() / ".config" / "zotero-project-papers"


def user_cache_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "zotero-project-papers" / "cache"
    xdg = os.environ.get("XDG_CACHE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".cache") / "zotero-project-papers"


def cache_db_path(server_id: str) -> Path:
    # Server IDs are opaque. Hash them so cache paths stay portable and filesystem-safe.
    partition = hashlib.sha256(server_id.encode("utf-8")).hexdigest()[:20]
    return user_cache_dir() / partition / "library.sqlite3"


def auth_path() -> Path:
    override = os.environ.get("ZPP_AUTH_STORE")
    if override:
        return Path(override).expanduser().resolve()
    return user_config_dir() / "auth.json"


def auth_store_preflight(*, create: bool = False) -> dict[str, Any]:
    """Check whether the authorization store can be persisted before prompting Zotero."""
    path = auth_path()
    parent = path.parent
    try:
        if create:
            parent.mkdir(parents=True, exist_ok=True)
        if not parent.exists():
            return {"path": str(path), "writable": False, "reason": "parent-directory-missing"}
        probe = parent / f".{path.name}.write-test-{uuid.uuid4().hex}"
        probe.write_bytes(b"")
        probe.unlink()
        return {"path": str(path), "writable": True, "reason": None}
    except Exception as exc:
        return {"path": str(path), "writable": False, "reason": str(exc)}


def ensure_auth_store_writable() -> dict[str, Any]:
    status = auth_store_preflight(create=True)
    if not status["writable"]:
        raise ZPPError(
            f"Authorization can be granted by Zotero, but the key cannot be persisted at {status['path']}.",
            code="auth_store_unwritable",
            hint="Set ZPP_AUTH_STORE to a writable private path, then retry authorization. The skill will not open the Zotero authorization dialog until persistence is writable.",
            details=status,
        )
    return status


def load_auth_store() -> dict[str, Any]:
    path = auth_path()
    if not path.exists():
        return {"schemaVersion": 1, "servers": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ZPPError(f"Cannot read auth store {path}: {exc}") from exc
    if not isinstance(data, dict):
        return {"schemaVersion": 1, "servers": {}}
    data.setdefault("schemaVersion", 1)
    data.setdefault("servers", {})
    return data


def save_auth_store(data: dict[str, Any]) -> None:
    path = auth_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    json_dump(data, path)
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def normalize_title(value: str) -> str:
    value = value.casefold().strip()
    # Treat punctuation/hyphens as token boundaries rather than deleting them, so
    # "cross-scale" and "cross scale" normalize identically.
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def normalize_doi(value: str) -> str:
    value = value.strip().casefold()
    value = re.sub(r"^https?://(dx\.)?doi\.org/", "", value)
    value = re.sub(r"^doi:\s*", "", value)
    return value.strip()


def safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(".")
    if not name:
        name = "paper.pdf"
    stem, suffix = os.path.splitext(name)
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if stem.upper() in reserved:
        stem = "_" + stem
    if len(stem) > 180:
        stem = stem[:180].rstrip()
    return stem + suffix


def parse_file_url(url: str) -> Path:
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme != "file":
        raise ZPPError(f"Zotero returned a non-file attachment URL: {url}")
    path = urllib.request.url2pathname(urllib.parse.unquote(parsed.path))
    if os.name == "nt":
        if parsed.netloc:
            path = "\\\\" + parsed.netloc + path.replace("/", "\\")
        elif re.match(r"^[\\/]?[A-Za-z]:", path):
            path = path.lstrip("/\\")
    elif parsed.netloc:
        path = f"//{parsed.netloc}{path}"
    return Path(path)


def project_root(start: Path | None = None) -> Path:
    cur = (start or Path.cwd()).resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / CONFIG_NAME).exists():
            return candidate
    for candidate in [cur, *cur.parents]:
        if (candidate / ".git").exists():
            return candidate
    return cur


def default_sync_config() -> dict[str, Any]:
    return {
        "state": "clean",
        "driftPolicy": "prompt",
        "acknowledgedFingerprint": None,
        "acknowledgedAt": None,
        # v0.5 quick preflight markers. These avoid a Zotero round-trip when neither
        # the project reference view nor the cached Zotero library version changed.
        "quickFingerprint": None,
        "lastFullClean": None,
    }


def normalize_project_config(config: dict[str, Any]) -> dict[str, Any]:
    """Upgrade older project configs in memory without breaking existing users."""
    version = int(config.get("schemaVersion") or 1)
    if version > SCHEMA_VERSION:
        raise ZPPError(f"Unsupported {CONFIG_NAME} schemaVersion: {version}")
    out = dict(config)
    out["schemaVersion"] = SCHEMA_VERSION
    out.setdefault("referenceDir", DEFAULT_REFERENCE_DIR)
    out.setdefault("manifestFile", (Path(out["referenceDir"]) / MANIFEST_NAME).as_posix())
    out.setdefault("bibFile", DEFAULT_BIB_FILE)
    out.setdefault("claimsFile", CLAIMS_FILE)
    out.setdefault("fallback", "copy")
    out.setdefault("zoteroServerID", None)
    sync = default_sync_config()
    existing_sync = out.get("sync")
    if isinstance(existing_sync, dict):
        sync.update(existing_sync)
    out["sync"] = sync
    return out


def save_project_config(root: Path, config: dict[str, Any]) -> None:
    config = normalize_project_config(config)
    json_dump(config, root / CONFIG_NAME)


def load_project(root: Path | None = None, required: bool = True) -> tuple[Path, dict[str, Any] | None]:
    root = project_root(root)
    path = root / CONFIG_NAME
    if not path.exists():
        if required:
            raise ZPPError(f"Project is not initialized. Run `zotero_papers.py init` from {root}")
        return root, None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ZPPError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ZPPError(f"{path} must contain a JSON object")
    config = normalize_project_config(raw)
    if raw != config:
        save_project_config(root, config)
    return root, config


def detect_reference_dirs(root: Path) -> list[dict[str, Any]]:
    """Find likely pre-existing literature folders without scanning the whole repository."""
    found: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for rel in REFERENCE_CANDIDATES:
        path = (root / rel).resolve()
        if path in seen or not path.is_dir():
            continue
        seen.add(path)
        pdfs = [x for x in path.rglob("*.pdf") if x.is_file()]
        if not pdfs and rel not in {"reference", "references", "refs", "literature", "literatures"}:
            continue
        subdirs = [x for x in path.iterdir() if x.is_dir()]
        found.append({
            "path": rel,
            "pdfCount": len(pdfs),
            "hasSubdirectories": bool(subdirs),
        })
    papers = (root / "papers").resolve()
    if papers.is_dir() and papers not in seen:
        direct_pdfs = [x for x in papers.glob("*.pdf") if x.is_file()]
        if direct_pdfs:
            found.append({"path": "papers", "pdfCount": len(direct_pdfs), "hasSubdirectories": any(x.is_dir() for x in papers.iterdir())})
    return found


def possible_legacy_reference_dirs(root: Path, active_reference_dir: str) -> list[dict[str, Any]]:
    """Report plausible old reference directories without treating them as active state."""
    active = Path(active_reference_dir).as_posix().rstrip("/")
    rows = []
    for row in detect_reference_dirs(root):
        rel = Path(str(row.get("path") or "")).as_posix().rstrip("/")
        if not rel or rel == active:
            continue
        # Prefer singular/plural and common historical aliases, but include other detected
        # literature folders so migrations remain visible rather than looking like data loss.
        candidate = dict(row)
        candidate["reason"] = "possible-legacy-reference-directory"
        rows.append(candidate)
    return rows


def unique_flatten_destination(target: Path, source: Path) -> Path:
    name = safe_filename(source.name)
    dest = target / name
    if not dest.exists():
        return dest
    try:
        if os.path.samefile(source, dest):
            return dest
    except OSError:
        pass
    stem, suffix = os.path.splitext(name)
    i = 2
    while True:
        candidate = target / f"{stem} [{i}]{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def flatten_reference_pdfs(source_dir: Path, target_dir: Path, fallback: str = "copy") -> list[dict[str, str]]:
    source_dir = source_dir.resolve()
    target_dir = target_dir.resolve()
    if source_dir == target_dir:
        return []
    pdfs = [p.resolve() for p in source_dir.rglob("*.pdf") if p.is_file()]
    target_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, str]] = []
    target_inside_source = is_within(target_dir, source_dir)
    for source in pdfs:
        if target_inside_source and is_within(source, target_dir):
            continue
        dest = unique_flatten_destination(target_dir, source)
        if dest.exists():
            try:
                if os.path.samefile(source, dest):
                    mode = "existing-hardlink-or-samefile"
                else:
                    mode = materialize_pdf(source, dest, fallback)
            except OSError:
                mode = materialize_pdf(source, dest, fallback)
        else:
            mode = materialize_pdf(source, dest, fallback)
        results.append({"source": str(source), "destination": str(dest), "mode": mode})
    return results


def normalize_http_headers(headers: Iterable[tuple[str, str]] | dict[str, str]) -> dict[str, str]:
    items = headers.items() if isinstance(headers, dict) else headers
    return {str(k).casefold(): str(v) for k, v in items}


@dataclass
class HTTPResult:
    status: int
    headers: dict[str, str]
    body: bytes

    def __post_init__(self) -> None:
        self.headers = normalize_http_headers(self.headers)

    def header(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name.casefold(), default)

    def json(self) -> Any:
        if not self.body:
            return None
        return json.loads(self.body.decode("utf-8"))

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class ZoteroClient:
    def __init__(self, base_url: str = BASE_URL):
        self.base_url = base_url.rstrip("/") + "/"
        self.server_id: str | None = None
        self.api_version: str | None = None
        self.api_key: str | None = None

    def _url(self, path: str) -> str:
        return urllib.parse.urljoin(self.base_url, path.lstrip("/"))

    def _raw_request(
        self,
        method: str,
        path_or_url: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: int = 30,
        absolute: bool = False,
    ) -> HTTPResult:
        url = path_or_url if absolute else self._url(path_or_url)
        req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return HTTPResult(resp.status, normalize_http_headers(resp.headers.items()), resp.read())
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            detail = payload.decode("utf-8", errors="replace").strip()
            hint: str | None = None
            code = f"zotero_http_{exc.code}"
            if exc.code == 403 and "local/authorize" not in url:
                hint = "Enable Zotero Settings → Advanced → Allow other applications on this computer to communicate with Zotero."
            elif exc.code == 401:
                hint = "Local write authorization is missing or expired; run `authorize`."
            elif exc.code == 412:
                hint = "Zotero state changed; rerun the command to refresh local versions."
            raise ZPPError(
                f"Zotero HTTP {exc.code} for {method} {url}: {detail or exc.reason}.",
                code=code,
                hint=hint,
                details={
                    "status": exc.code,
                    "url": url,
                    "headers": normalize_http_headers(exc.headers.items()) if exc.headers else {},
                    "body": detail[:500],
                    "userAction": (
                        {"type": "enable_local_api", "message": "In Zotero, enable Settings → Advanced → Allow other applications on this computer to communicate with Zotero."}
                        if exc.code == 403 and "local/authorize" not in url else
                        {"type": "approve_write_authorization", "message": "A Zotero write authorization is required. Keep Zotero open and approve the dialog; choose Always Allow to avoid repeated prompts."}
                        if exc.code == 401 else None
                    ),
                },
            ) from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise ZPPError(
                    f"Timed out waiting for Zotero after {timeout} seconds.",
                    code="timeout",
                    hint="If Zotero is showing an authorization dialog, approve it there and retry.",
                ) from exc
            raise ZPPError(
                f"Cannot reach Zotero Local API at {self.base_url}: {reason}.",
                code="zotero_unreachable",
                hint="Open Zotero and retry. If Zotero is already open, verify Settings → Advanced → Allow other applications on this computer to communicate with Zotero.",
                details={"userAction": {"type": "start_zotero", "message": "Please open Zotero, keep it running, and retry the blocked Zotero operation."}},
            ) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise ZPPError(
                f"Timed out waiting for Zotero after {timeout} seconds.",
                code="timeout",
                hint="If Zotero is showing an authorization dialog, approve it there and retry.",
            ) from exc

    def bootstrap(self) -> None:
        result = self._raw_request("GET", "")
        self.server_id = result.header("Zotero-Server-ID")
        self.api_version = result.header("Zotero-API-Version")
        zotero_version = result.header("X-Zotero-Version")
        if not self.server_id:
            raise ZPPError(
                "Connected to the local HTTP service, but the response did not include Zotero-Server-ID.",
                code="zotero_server_id_missing",
                hint="Do not change Zotero settings automatically. Run `doctor --json` to distinguish a version/port/proxy response from the Zotero 10 Local API.",
                details={
                    "status": result.status,
                    "xZoteroVersion": zotero_version,
                    "apiVersion": self.api_version,
                    "headers": sorted(result.headers.keys()),
                },
            )
        try:
            store = load_auth_store()
            entry = store.get("servers", {}).get(self.server_id, {})
            self.api_key = entry.get("key")
        except ZPPError:
            # Read-only local Zotero access must not fail just because a sandbox blocks
            # the optional persisted write-key store. doctor/authorize report this separately.
            self.api_key = None

    def read(self, path: str, params: dict[str, Any] | None = None, *, raw: bool = False) -> Any:
        if self.server_id is None:
            self.bootstrap()
        if params:
            qs = urllib.parse.urlencode(params, doseq=True)
            path = path + ("&" if "?" in path else "?") + qs
        headers = {"Zotero-API-Version": "3", "Zotero-Server-ID": self.server_id or ""}
        result = self._raw_request("GET", path, headers=headers)
        return result if raw else result.json()

    def authorize(self, require_remembered: bool = True) -> dict[str, Any]:
        if self.server_id is None:
            self.bootstrap()
        persistence = auth_store_preflight(create=False)
        if require_remembered:
            persistence = ensure_auth_store_writable()
        payload = json.dumps({"appName": APP_NAME}).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Zotero-Server-ID": self.server_id or "",
            "Zotero-API-Version": "3",
        }
        print(
            f"Waiting for Zotero authorization (up to {AUTH_TIMEOUT}s). In Zotero, choose 'Always Allow' for persistent access.",
            file=sys.stderr,
            flush=True,
        )
        result = self._raw_request(
            "POST", "local/authorize", body=payload, headers=headers, timeout=AUTH_TIMEOUT
        )
        data = result.json()
        key = data.get("key") if isinstance(data, dict) else None
        remembered = bool(data.get("remember")) if isinstance(data, dict) else False
        if not key:
            raise ZPPError("Zotero did not return a local write key.", code="authorization_no_key")
        self.api_key = key
        token_persisted = False
        persistence_error: str | None = None
        if remembered:
            try:
                store = load_auth_store()
                store.setdefault("servers", {})[self.server_id or ""] = {
                    "key": key,
                    "remember": True,
                    "savedAt": utc_now(),
                }
                save_auth_store(store)
                token_persisted = True
            except Exception as exc:
                persistence_error = str(exc)
        if require_remembered and not remembered:
            raise ZPPError(
                "Zotero granted only one-shot authorization, but this operation needs a reusable key.",
                code="authorization_not_remembered",
                hint="Run `authorize` again and choose 'Always Allow' in Zotero.",
                details={"authorizationGranted": True, "remembered": False, "tokenPersisted": False},
            )
        return {
            "ok": not (remembered and not token_persisted),
            "authorizationGranted": True,
            "authorized": True,
            "remembered": remembered,
            "tokenPersisted": token_persisted if remembered else False,
            "authStore": str(auth_path()),
            "persistenceError": persistence_error,
            "serverID": self.server_id,
        }

    def ensure_write_auth(self, require_remembered: bool = True) -> None:
        if self.server_id is None:
            self.bootstrap()
        if self.api_key:
            return
        self.authorize(require_remembered=require_remembered)

    def write(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        form: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        require_remembered: bool = True,
        raw: bool = False,
    ) -> Any:
        self.ensure_write_auth(require_remembered=require_remembered)
        headers = {
            "Zotero-API-Version": "3",
            "Zotero-Server-ID": self.server_id or "",
            "Zotero-API-Key": self.api_key or "",
        }
        if extra_headers:
            headers.update(extra_headers)
        body: bytes | None = None
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif form is not None:
            body = urllib.parse.urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        result = self._raw_request(method, path, body=body, headers=headers, timeout=120)
        if raw:
            return result
        if not result.body:
            return None
        ctype = result.header("Content-Type", "") or ""
        return result.json() if "json" in ctype or result.body[:1] in (b"{", b"[") else result.text()

    def upload_bytes(self, url: str, file_path: Path, content_type: str) -> None:
        body = file_path.read_bytes()
        headers = {"Content-Type": content_type or "application/octet-stream"}
        self._raw_request("POST", url, body=body, headers=headers, timeout=300, absolute=True)

    @staticmethod
    def _created_key(response: Any) -> str:
        if not isinstance(response, dict):
            raise ZPPError(f"Unexpected Zotero create response: {response!r}")
        for field in ("successful", "success"):
            block = response.get(field)
            if not isinstance(block, dict):
                continue
            value = block.get("0")
            if isinstance(value, str):
                return value
            if isinstance(value, dict):
                if isinstance(value.get("key"), str):
                    return value["key"]
                data = value.get("data")
                if isinstance(data, dict) and isinstance(data.get("key"), str):
                    return data["key"]
        failed = response.get("failed")
        if failed:
            raise ZPPError(f"Zotero object creation failed: {failed}")
        raise ZPPError(f"Could not determine created Zotero key from response: {response}")

    def create_collection(self, name: str, parent: str | bool = False) -> str:
        obj: dict[str, Any] = {"name": name}
        if parent:
            obj["parentCollection"] = parent
        token = uuid.uuid4().hex
        response = self.write(
            "POST",
            "users/0/collections",
            json_body=[obj],
            extra_headers={"Zotero-Write-Token": token},
        )
        return self._created_key(response)

    def create_item(self, item: dict[str, Any]) -> str:
        token = uuid.uuid4().hex
        response = self.write(
            "POST",
            "users/0/items",
            json_body=[item],
            extra_headers={"Zotero-Write-Token": token},
        )
        return self._created_key(response)



# ---------- Fast local metadata cache ----------


def _cache_connect(server_id: str) -> sqlite3.Connection:
    path = cache_db_path(server_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS items (
            key TEXT PRIMARY KEY,
            version INTEGER NOT NULL DEFAULT 0,
            item_type TEXT,
            title TEXT,
            creators_json TEXT,
            creators_text TEXT,
            date TEXT,
            doi TEXT,
            publication_title TEXT,
            abstract_note TEXT,
            tags_text TEXT,
            collections_json TEXT,
            url TEXT,
            arxiv_id TEXT,
            extra TEXT,
            updated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_items_doi ON items(doi);
        CREATE INDEX IF NOT EXISTS idx_items_date ON items(date);
        CREATE INDEX IF NOT EXISTS idx_items_title ON items(title);
        """
    )
    existing_cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(items)").fetchall()}
    if "arxiv_id" not in existing_cols:
        conn.execute("ALTER TABLE items ADD COLUMN arxiv_id TEXT")
    if "extra" not in existing_cols:
        conn.execute("ALTER TABLE items ADD COLUMN extra TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_items_arxiv ON items(arxiv_id)")
    return conn


def _cache_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return str(row[0]) if row else default


def _cache_set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def cache_status(server_id: str) -> dict[str, Any]:
    path = cache_db_path(server_id)
    if not path.exists():
        return {"exists": False, "path": str(path), "libraryVersion": None, "ageSeconds": None, "itemCount": 0}
    conn = _cache_connect(server_id)
    try:
        synced = _cache_meta(conn, "last_synced_epoch")
        age = max(0.0, time.time() - float(synced)) if synced else None
        count = int(conn.execute("SELECT COUNT(*) FROM items").fetchone()[0])
        version = _cache_meta(conn, "library_version")
        return {
            "exists": True,
            "path": str(path),
            "libraryVersion": int(version) if version and version.isdigit() else None,
            "ageSeconds": round(age, 3) if age is not None else None,
            "itemCount": count,
        }
    finally:
        conn.close()


def _cache_upsert_item(conn: sqlite3.Connection, obj: dict[str, Any]) -> None:
    d = item_data(obj)
    key = str(d.get("key") or obj.get("key") or "")
    if not key:
        return
    item_type = str(d.get("itemType") or "")
    if item_type in {"attachment", "note", "annotation"} or d.get("deleted"):
        conn.execute("DELETE FROM items WHERE key=?", (key,))
        return
    creators = [c for c in (d.get("creators") or []) if isinstance(c, dict)]
    creator_names = [creator_name(c) for c in creators]
    tags = [str(t.get("tag")) for t in (d.get("tags") or []) if isinstance(t, dict) and t.get("tag")]
    collections = [str(x) for x in (d.get("collections") or []) if x]
    arxiv_id = record_arxiv_id(d)
    conn.execute(
        """
        INSERT INTO items(
            key,version,item_type,title,creators_json,creators_text,date,doi,
            publication_title,abstract_note,tags_text,collections_json,url,arxiv_id,extra,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(key) DO UPDATE SET
            version=excluded.version,item_type=excluded.item_type,title=excluded.title,
            creators_json=excluded.creators_json,creators_text=excluded.creators_text,
            date=excluded.date,doi=excluded.doi,publication_title=excluded.publication_title,
            abstract_note=excluded.abstract_note,tags_text=excluded.tags_text,
            collections_json=excluded.collections_json,url=excluded.url,arxiv_id=excluded.arxiv_id,
            extra=excluded.extra,updated_at=excluded.updated_at
        """,
        (
            key, int(d.get("version") or obj.get("version") or 0), item_type,
            str(d.get("title") or ""), json.dumps(creators, ensure_ascii=False), " | ".join(creator_names),
            str(d.get("date") or ""), normalize_doi(str(d.get("DOI") or "")),
            str(d.get("publicationTitle") or d.get("proceedingsTitle") or d.get("conferenceName") or ""),
            str(d.get("abstractNote") or ""), " | ".join(tags), json.dumps(collections, ensure_ascii=False),
            str(d.get("url") or ""), arxiv_id, str(d.get("extra") or ""), utc_now(),
        ),
    )


def _raw_json_page(client: "ZoteroClient", path: str, params: dict[str, Any]) -> tuple[list[dict[str, Any]], int | None]:
    result = client.read(path, params, raw=True)
    assert isinstance(result, HTTPResult)
    payload = result.json() if result.body else []
    rows = [x for x in payload if isinstance(x, dict)] if isinstance(payload, list) else []
    lmv = result.header("Last-Modified-Version")
    return rows, int(lmv) if lmv and str(lmv).isdigit() else None


def refresh_metadata_cache(client: "ZoteroClient", *, force: bool = False, max_age: int = CACHE_TTL_SECONDS) -> dict[str, Any]:
    """Refresh a compact top-level-item cache, using Zotero local versions after first build."""
    if client.server_id is None:
        client.bootstrap()
    assert client.server_id is not None
    conn = _cache_connect(client.server_id)
    try:
        last_epoch = _cache_meta(conn, "last_synced_epoch")
        current_version = _cache_meta(conn, "library_version")
        age = time.time() - float(last_epoch) if last_epoch else None
        if not force and age is not None and age < max_age:
            count = int(conn.execute("SELECT COUNT(*) FROM items").fetchone()[0])
            return {
                "refreshed": False,
                "strategy": "fresh-cache",
                "libraryVersion": int(current_version) if current_version and current_version.isdigit() else None,
                "itemCount": count,
                "ageSeconds": round(max(0.0, age), 3),
            }

        since = int(current_version) if current_version and current_version.isdigit() else None
        strategy = "incremental" if since is not None else "full"
        start = 0
        page_size = 100
        latest_version = since
        changed = 0
        while True:
            params: dict[str, Any] = {"limit": page_size, "start": start, "includeTrashed": 1}
            if since is not None:
                params["since"] = since
            rows, lmv = _raw_json_page(client, "users/0/items/top", params)
            if lmv is not None:
                latest_version = max(latest_version or 0, lmv)
            for obj in rows:
                _cache_upsert_item(conn, obj)
                changed += 1
            if len(rows) < page_size:
                break
            start += page_size

        deleted_count = 0
        if since is not None:
            result = client.read("users/0/deleted", {"since": since}, raw=True)
            assert isinstance(result, HTTPResult)
            payload = result.json() if result.body else {}
            if isinstance(payload, dict):
                for key in payload.get("items", []) or []:
                    conn.execute("DELETE FROM items WHERE key=?", (str(key),))
                    deleted_count += 1
            lmv = result.header("Last-Modified-Version")
            if lmv and str(lmv).isdigit():
                latest_version = max(latest_version or 0, int(lmv))

        _cache_set_meta(conn, "server_id", client.server_id)
        if latest_version is not None:
            _cache_set_meta(conn, "library_version", latest_version)
        _cache_set_meta(conn, "last_synced_epoch", time.time())
        _cache_set_meta(conn, "last_synced_at", utc_now())
        conn.commit()
        count = int(conn.execute("SELECT COUNT(*) FROM items").fetchone()[0])
        return {
            "refreshed": True,
            "strategy": strategy,
            "libraryVersion": latest_version,
            "changedItems": changed,
            "deletedItems": deleted_count,
            "itemCount": count,
            "ageSeconds": 0.0,
        }
    finally:
        conn.close()


def _query_tokens(query: str) -> list[str]:
    return [x for x in re.findall(r"[\w.-]+", query.casefold(), flags=re.UNICODE) if len(x) > 1][:16]


def extract_year(value: Any) -> str:
    m = re.search(r"(?:19|20)\d{2}", str(value or ""))
    return m.group(0) if m else ""


def extract_arxiv_id(value: Any) -> str:
    text = str(value or "")
    # Modern IDs (YYMM.NNNNN) and legacy archive/category IDs.
    m = re.search(r"(?i)(?:arxiv\s*:\s*|arxiv\.org/(?:abs|pdf)/)?((?:\d{4}\.\d{4,5})(?:v\d+)?|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:\.pdf)?", text)
    if not m:
        return ""
    return re.sub(r"v\d+$", "", m.group(1), flags=re.I).casefold()


def record_arxiv_id(record: dict[str, Any]) -> str:
    for field in ("arxiv", "arxivID", "archiveID", "extra", "url", "DOI"):
        value = record.get(field)
        found = extract_arxiv_id(value)
        if found:
            return found
    return ""


def _first_creator_last_name(record: dict[str, Any]) -> str:
    creators = record.get("creators") or []
    if creators and isinstance(creators[0], dict):
        c = creators[0]
        return str(c.get("lastName") or c.get("name") or "").casefold().strip()
    if creators:
        text = str(creators[0]).strip().casefold()
        return text.split()[-1] if text else ""
    return ""


def classify_match(record: dict[str, Any], query: str, *, allow_abstract: bool = True) -> dict[str, Any]:
    """Rank matches conservatively and explain why a candidate matched.

    Scores are normalized to 0..1. Exact identifiers/titles outrank phrase/token matches;
    abstract-only matches are deliberately weak so they do not masquerade as "already have
    this paper" when the user supplied a specific title.
    """
    q = query.strip()
    qnorm = normalize_title(q)
    qdoi = normalize_doi(q) if re.search(r"10\.\d{4,9}/", q, re.I) else ""
    qarxiv = extract_arxiv_id(q)
    title = str(record.get("title") or "")
    title_norm = normalize_title(title)
    doi = normalize_doi(str(record.get("DOI") or record.get("doi") or ""))
    arxiv = record_arxiv_id(record)
    publication = str(record.get("publicationTitle") or record.get("publication_title") or "")
    creators_text = " ".join(
        creator_name(x) if isinstance(x, dict) else str(x)
        for x in (record.get("creators") or [])
    )
    date = str(record.get("date") or "")
    tags = str(record.get("tagsText") or record.get("tags_text") or "")
    abstract = str(record.get("abstractNote") or record.get("abstract_note") or "")

    if qdoi and doi and qdoi == doi:
        return {"score": 1.0, "matchType": "doi-exact", "matchedFields": ["DOI"]}
    if qarxiv and arxiv and qarxiv == arxiv:
        return {"score": 0.995, "matchType": "arxiv-exact", "matchedFields": ["arXiv"]}
    if qnorm and title_norm and qnorm == title_norm:
        return {"score": 0.98, "matchType": "normalized-title-exact", "matchedFields": ["title"]}
    if qnorm and len(qnorm) >= 12 and qnorm in title_norm:
        return {"score": 0.93, "matchType": "title-phrase", "matchedFields": ["title"]}

    tokens = _query_tokens(q)
    if not tokens:
        return {"score": 0.0, "matchType": "none", "matchedFields": []}
    title_tokens = set(_query_tokens(title_norm))
    query_tokens = set(tokens)
    title_hits = query_tokens & title_tokens
    coverage = len(title_hits) / max(1, len(query_tokens))
    fields: list[str] = []
    score = 0.0
    match_type = "weak"
    if title_hits:
        fields.append("title")
        score = 0.48 + 0.42 * coverage
        match_type = "title-token"
    pub_hits = [t for t in tokens if t in publication.casefold()]
    if pub_hits:
        fields.append("publicationTitle")
        score = max(score, 0.48 + 0.08 * min(3, len(pub_hits)))
        if match_type == "weak": match_type = "venue-token"
    creator_hits = [t for t in tokens if t in creators_text.casefold()]
    year_q = extract_year(q)
    if creator_hits and year_q and year_q == extract_year(date):
        fields.extend(x for x in ("creators", "date") if x not in fields)
        score = max(score, 0.78)
        match_type = "author-year"
    tag_hits = [t for t in tokens if t in tags.casefold()]
    if tag_hits:
        fields.append("tags")
        score = max(score, 0.50 + 0.04 * min(3, len(tag_hits)))
        if match_type == "weak": match_type = "tag-token"
    if allow_abstract:
        abstract_hits = [t for t in tokens if t in abstract.casefold()]
        if abstract_hits:
            fields.append("abstractNote")
            # Abstract-only hits are useful for topical discovery, not duplicate certainty.
            abstract_coverage = len(set(abstract_hits)) / max(1, len(query_tokens))
            if score == 0:
                score = 0.28 + 0.22 * abstract_coverage
                match_type = "abstract-token"
            else:
                score = min(0.94, score + 0.04 * abstract_coverage)
    return {"score": round(min(1.0, score), 4), "matchType": match_type if score else "none", "matchedFields": sorted(set(fields))}


def _score_text_record(record: dict[str, Any], query: str) -> float:
    return float(classify_match(record, query).get("score") or 0.0)


def search_manifest(root: Path, config: dict[str, Any], query: str, limit: int) -> list[dict[str, Any]]:
    manifest = load_old_manifest(root, config)
    rows: list[tuple[float, dict[str, Any]]] = []
    for p in manifest.get("papers", []) or []:
        if not isinstance(p, dict):
            continue
        match = classify_match(p, query, allow_abstract=True)
        score = float(match["score"])
        if score < DEFAULT_MATCH_THRESHOLD:
            continue
        summary = {
            "key": p.get("itemKey"), "itemType": p.get("itemType"), "title": p.get("title", ""),
            "creators": p.get("creators", []) or [], "date": p.get("date", ""), "DOI": p.get("DOI", ""),
            "publicationTitle": p.get("publicationTitle", ""), "projectPath": p.get("projectPath"),
            **match,
        }
        rows.append((score, summary))
    rows.sort(key=lambda x: (-x[0], str(x[1].get("date") or ""), str(x[1].get("title") or "").casefold()))
    return [x[1] for x in rows[:limit]]


def search_metadata_cache(server_id: str, query: str, limit: int, *, collection_key: str | None = None, verbose: bool = False) -> list[dict[str, Any]]:
    path = cache_db_path(server_id)
    if not path.exists():
        return []
    conn = _cache_connect(server_id)
    try:
        tokens = _query_tokens(query)
        qdoi = normalize_doi(query) if re.search(r"10\.\d{4,9}/", query, re.I) else ""
        qarxiv = extract_arxiv_id(query)
        params: list[Any] = []
        clauses: list[str] = []
        if qdoi:
            clauses.append("doi = ?")
            params.append(qdoi)
        if qarxiv:
            clauses.append("arxiv_id = ?")
            params.append(qarxiv)
        for token in tokens:
            for col in ("title", "creators_text", "date", "doi", "publication_title", "abstract_note", "tags_text", "arxiv_id", "extra"):
                clauses.append(f"lower({col}) LIKE ?")
                params.append(f"%{token}%")
        if not clauses:
            return []
        where = "(" + " OR ".join(clauses) + ")"
        if collection_key:
            where += " AND collections_json LIKE ?"
            params.append(f'%"{collection_key}"%')
        candidates = conn.execute(f"SELECT * FROM items WHERE {where} LIMIT 300", params).fetchall()
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in candidates:
            creators_detailed = json.loads(row["creators_json"] or "[]")
            creators = [creator_name(c) for c in creators_detailed if isinstance(c, dict)]
            record = {
                "key": row["key"], "itemType": row["item_type"], "title": row["title"] or "",
                "creators": creators, "date": row["date"] or "", "DOI": row["doi"] or "",
                "publicationTitle": row["publication_title"] or "", "abstractNote": row["abstract_note"] or "",
                "tagsText": row["tags_text"] or "", "collections": json.loads(row["collections_json"] or "[]"),
                "url": row["url"] or "", "arxivID": row["arxiv_id"] or "", "extra": row["extra"] or "",
            }
            match = classify_match(record, query, allow_abstract=True)
            score = float(match["score"])
            if score < DEFAULT_MATCH_THRESHOLD:
                continue
            out = {
                "key": record["key"], "itemType": record["itemType"], "title": record["title"],
                "creators": record["creators"], "date": record["date"], "DOI": record["DOI"],
                "publicationTitle": record["publicationTitle"], "arxivID": record["arxivID"], **match,
            }
            if verbose:
                out.update({
                    "abstractNote": record["abstractNote"],
                    "tags": [x.strip() for x in str(record["tagsText"]).split("|") if x.strip()],
                    "collections": record["collections"], "url": record["url"], "extra": record["extra"],
                })
            scored.append((score, out))
        scored.sort(key=lambda x: (-x[0], str(x[1].get("date") or ""), str(x[1].get("title") or "").casefold()))
        return [x[1] for x in scored[:limit]]
    finally:
        conn.close()


def quick_project_marker(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Cheap, no-Zotero marker for reference-view changes plus cached library version."""
    ref_dir = root / config["referenceDir"]
    pdfs: list[tuple[str, int, int]] = []
    if ref_dir.exists():
        for p in ref_dir.rglob("*.pdf"):
            if not p.is_file():
                continue
            try:
                st = p.stat()
                pdfs.append((relpath_for_manifest(p, root), int(st.st_size), int(st.st_mtime_ns)))
            except OSError:
                continue
    manifest = root / config["manifestFile"]
    manifest_stat: tuple[int, int] | None = None
    if manifest.exists():
        try:
            st = manifest.stat()
            manifest_stat = (int(st.st_size), int(st.st_mtime_ns))
        except OSError:
            pass
    server_id = config.get("zoteroServerID")
    cstat = cache_status(str(server_id)) if server_id else {"libraryVersion": None}
    payload = {
        "manifest": manifest_stat,
        "pdfs": sorted(pdfs),
        "cachedLibraryVersion": cstat.get("libraryVersion"),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {"fingerprint": hashlib.sha256(canonical.encode("utf-8")).hexdigest(), **payload}


# ---------- Zotero/project helpers ----------


def item_data(obj: dict[str, Any]) -> dict[str, Any]:
    data = obj.get("data") if isinstance(obj, dict) else None
    return data if isinstance(data, dict) else obj


def creator_name(c: dict[str, Any]) -> str:
    if c.get("name"):
        return str(c["name"])
    return " ".join(x for x in [str(c.get("firstName", "")), str(c.get("lastName", ""))] if x).strip()


def metadata_warnings(obj: dict[str, Any]) -> list[str]:
    d = item_data(obj)
    creators = [c for c in (d.get("creators") or []) if isinstance(c, dict)]
    warnings: list[str] = []
    author_positions = [i for i, c in enumerate(creators) if c.get("creatorType") == "author"]
    if creators and author_positions and author_positions[0] > 0:
        warnings.append("Non-author creators precede the first author; verify creator roles before relying on exported BibTeX.")
    if creators and not author_positions and d.get("itemType") not in {"book", "encyclopediaArticle", "dictionaryEntry"}:
        warnings.append("No creator is marked as an author; verify Zotero metadata before citation export.")
    return warnings


def item_summary(obj: dict[str, Any], *, brief: bool = False) -> dict[str, Any]:
    d = item_data(obj)
    creators_raw = [c for c in (d.get("creators") or []) if isinstance(c, dict)]
    result: dict[str, Any] = {
        "key": d.get("key") or obj.get("key"),
        "itemType": d.get("itemType"),
        "title": d.get("title", ""),
        "creators": [creator_name(c) for c in creators_raw],
        "date": d.get("date", ""),
        "DOI": d.get("DOI", ""),
        "arxivID": record_arxiv_id(d),
        "publicationTitle": d.get("publicationTitle", "") or d.get("proceedingsTitle", "") or d.get("conferenceName", ""),
    }
    if brief:
        return result
    result.update(
        {
            "creatorsDetailed": creators_raw,
            "url": d.get("url", ""),
            "abstractNote": d.get("abstractNote", ""),
            "tags": [t.get("tag") for t in d.get("tags", []) if isinstance(t, dict) and t.get("tag")],
            "collections": d.get("collections", []) or [],
            "metadataWarnings": metadata_warnings(obj),
        }
    )
    return result


def duplicate_warnings(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_doi: dict[str, list[str]] = {}
    by_title: dict[str, list[str]] = {}
    for obj in rows:
        s = item_summary(obj, brief=True)
        key = str(s.get("key") or "")
        doi = normalize_doi(str(s.get("DOI") or ""))
        title = normalize_title(str(s.get("title") or ""))
        if doi:
            by_doi.setdefault(doi, []).append(key)
        elif title:
            by_title.setdefault(title, []).append(key)
    warnings: list[dict[str, Any]] = []
    for doi, keys in by_doi.items():
        if len(keys) > 1:
            warnings.append({"type": "duplicate-doi", "DOI": doi, "itemKeys": keys})
    for title, keys in by_title.items():
        if len(keys) > 1:
            warnings.append({"type": "duplicate-title", "normalizedTitle": title, "itemKeys": keys})
    return warnings


def list_collections(client: ZoteroClient) -> list[dict[str, Any]]:
    rows = client.read("users/0/collections") or []
    return [item_data(x) for x in rows if isinstance(x, dict)]


def find_collection(client: ZoteroClient, name: str, parent: str | bool = False) -> dict[str, Any] | None:
    for c in list_collections(client):
        p = c.get("parentCollection") or False
        if c.get("name") == name and p == parent:
            return c
    return None


def ensure_collection_path(client: ZoteroClient, parts: list[str]) -> str:
    parent: str | bool = False
    for name in parts:
        found = find_collection(client, name, parent)
        if found:
            parent = found.get("key")
        else:
            parent = client.create_collection(name, parent)
    assert isinstance(parent, str)
    return parent


def get_item(client: ZoteroClient, key: str) -> dict[str, Any]:
    obj = client.read(f"users/0/items/{key}")
    if not isinstance(obj, dict):
        raise ZPPError(f"Zotero item {key} not found")
    return obj


def get_children(client: ZoteroClient, key: str) -> list[dict[str, Any]]:
    rows = client.read(f"users/0/items/{key}/children") or []
    return [x for x in rows if isinstance(x, dict)]


def attachment_path(client: ZoteroClient, attachment_key: str) -> Path:
    result = client.read(f"users/0/items/{attachment_key}/file/view/url", raw=True)
    assert isinstance(result, HTTPResult)
    return parse_file_url(result.text().strip())


def pdf_attachments(client: ZoteroClient, item_key: str) -> list[dict[str, Any]]:
    out = []
    for child in get_children(client, item_key):
        d = item_data(child)
        ctype = (d.get("contentType") or "").casefold()
        filename = d.get("filename") or ""
        if ctype == "application/pdf" or filename.casefold().endswith(".pdf"):
            row = {
                "key": d.get("key") or child.get("key"),
                "title": d.get("title", ""),
                "filename": filename,
                "contentType": d.get("contentType", ""),
                "linkMode": d.get("linkMode", ""),
            }
            try:
                path = attachment_path(client, row["key"])
                row["path"] = str(path)
            except ZPPError as exc:
                row["pathError"] = str(exc)
            out.append(row)
    return out


def collection_items(client: ZoteroClient, collection_key: str, query: str | None = None, fulltext: bool = False) -> list[dict[str, Any]]:
    # Search top-level bibliographic items directly. Filtering child attachments only
    # after applying a small API limit can produce a false zero-result set.
    params: dict[str, Any] = {"limit": 100}
    if query:
        params["q"] = query
        params["qmode"] = "everything" if fulltext else "titleCreatorYear"
    rows = client.read(f"users/0/collections/{collection_key}/items/top", params) or []
    return [obj for obj in rows if item_data(obj).get("itemType") not in {"attachment", "note", "annotation"}]


def _promote_search_rows_to_top_level(client: ZoteroClient, rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Promote attachment/note full-text hits to their parent bibliographic items.

    This is only a compatibility fallback for Local API builds where `items/top`
    does not surface a child attachment's indexed-text match.
    """
    direct: list[dict[str, Any]] = []
    parent_keys: list[str] = []
    seen: set[str] = set()
    for obj in rows:
        d = item_data(obj)
        typ = d.get("itemType")
        key = str(d.get("key") or obj.get("key") or "")
        if typ not in {"attachment", "note", "annotation"}:
            if key and key not in seen:
                direct.append(obj)
                seen.add(key)
            continue
        parent = str(d.get("parentItem") or "")
        if parent and parent not in seen and parent not in parent_keys:
            parent_keys.append(parent)
    for start in range(0, len(parent_keys), 50):
        batch = parent_keys[start:start + 50]
        if not batch:
            continue
        parents = client.read("users/0/items/top", {"itemKey": ",".join(batch), "limit": len(batch)}) or []
        for obj in parents:
            d = item_data(obj)
            key = str(d.get("key") or obj.get("key") or "")
            if key and key not in seen:
                direct.append(obj)
                seen.add(key)
                if len(direct) >= limit:
                    return direct[:limit]
    return direct[:limit]


def library_search(client: ZoteroClient, query: str, fulltext: bool = False, limit: int = 30) -> list[dict[str, Any]]:
    # `/items` includes child attachments and notes. Applying `limit` before filtering
    # them can make a healthy library look empty. `/items/top` is the correct endpoint
    # for bibliographic-paper search.
    params = {"q": query, "qmode": "everything" if fulltext else "titleCreatorYear", "limit": min(limit, 100)}
    rows = client.read("users/0/items/top", params) or []
    out = [obj for obj in rows if item_data(obj).get("itemType") not in {"attachment", "note", "annotation"}]
    if out or not fulltext:
        return out[:limit]

    # Compatibility fallback: some Local API quicksearch implementations can expose
    # the matching child attachment rather than its parent when searching indexed text.
    # Query a bounded all-items set and promote child hits to their parent papers.
    raw = client.read(
        "users/0/items",
        {"q": query, "qmode": "everything", "limit": min(max(limit * 10, 50), 100)},
    ) or []
    return _promote_search_rows_to_top_level(client, raw, limit)


def rank_zotero_rows(rows: list[dict[str, Any]], query: str, *, fulltext: bool, limit: int, verbose: bool = False) -> list[dict[str, Any]]:
    ranked: list[tuple[float, dict[str, Any]]] = []
    for obj in rows:
        summary_full = item_summary(obj, brief=False)
        match = classify_match(summary_full, query, allow_abstract=True)
        score = float(match["score"])
        if fulltext and score < DEFAULT_MATCH_THRESHOLD:
            # Zotero itself returned the item from indexed full text. Keep it as a weak,
            # explicitly-labelled candidate rather than pretending metadata matched.
            match = {"score": 0.40, "matchType": "zotero-fulltext", "matchedFields": ["fulltext"]}
            score = 0.40
        if not fulltext and score < DEFAULT_MATCH_THRESHOLD:
            continue
        out = item_summary(obj, brief=not verbose)
        out.update(match)
        ranked.append((score, out))
    ranked.sort(key=lambda x: (-x[0], str(x[1].get("date") or ""), str(x[1].get("title") or "").casefold()))
    return [x[1] for x in ranked[:limit]]


def find_duplicates(client: ZoteroClient, metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """Conservative duplicate cascade: DOI -> arXiv -> normalized title -> title+author+year."""
    matches: dict[str, dict[str, Any]] = {}
    doi = normalize_doi(str(metadata.get("DOI") or ""))
    arxiv = record_arxiv_id(metadata)
    title = str(metadata.get("title") or "").strip()
    target_title = normalize_title(title)
    first_author = _first_creator_last_name(metadata)
    year = extract_year(metadata.get("date"))

    def remember(obj: dict[str, Any]) -> None:
        key = str(item_summary(obj, brief=True).get("key") or "")
        if key:
            matches[key] = obj

    if doi:
        for obj in library_search(client, doi, fulltext=True, limit=100):
            if normalize_doi(str(item_data(obj).get("DOI") or "")) == doi:
                remember(obj)
        if matches:
            return list(matches.values())

    if arxiv:
        for obj in library_search(client, arxiv, fulltext=True, limit=100):
            if record_arxiv_id(item_data(obj)) == arxiv:
                remember(obj)
        if matches:
            return list(matches.values())

    if title:
        for obj in library_search(client, title, fulltext=False, limit=100):
            d = item_data(obj)
            if normalize_title(str(d.get("title") or "")) == target_title:
                remember(obj)
        if matches:
            return list(matches.values())

    # Last-resort identity heuristic is intentionally strict: title must be highly similar
    # and first author + year must agree.
    if title and first_author and year:
        for obj in library_search(client, title, fulltext=False, limit=100):
            d = item_data(obj)
            candidate = {
                "title": d.get("title", ""), "creators": d.get("creators", []), "date": d.get("date", "")
            }
            title_match = classify_match(candidate, title, allow_abstract=False)
            if float(title_match["score"]) < 0.90:
                continue
            if _first_creator_last_name(candidate) == first_author and extract_year(candidate.get("date")) == year:
                remember(obj)
    return list(matches.values())


def find_duplicate(client: ZoteroClient, metadata: dict[str, Any]) -> dict[str, Any] | None:
    matches = find_duplicates(client, metadata)
    return matches[0] if matches else None


def add_item_to_collection(client: ZoteroClient, item_key: str, collection_key: str) -> bool:
    obj = get_item(client, item_key)
    d = item_data(obj)
    if d.get("itemType") in {"attachment", "note", "annotation"}:
        raise ZPPError(f"{item_key} is not a top-level bibliographic item")
    collections = list(d.get("collections") or [])
    if collection_key in collections:
        return False
    collections.append(collection_key)
    headers = {"If-Unmodified-Since-Version": str(d.get("version", 0))}
    client.write("PATCH", f"users/0/items/{item_key}", json_body={"collections": collections}, extra_headers=headers)
    return True


def file_matches_source(source: Path, dest: Path) -> bool:
    if not source.exists() or not dest.exists() or not dest.is_file():
        return False
    try:
        if os.path.samefile(source, dest):
            return True
    except OSError:
        pass
    try:
        a, b = source.stat(), dest.stat()
        # copy2 preserves mtime; this is a cheap guard for cross-volume copy fallback.
        return a.st_size == b.st_size and a.st_mtime_ns == b.st_mtime_ns
    except OSError:
        return False


def choose_reference_filename(source: Path, item_key: str, reference_dir: Path, prior: str | None = None) -> Path:
    if prior:
        p = Path(prior)
        p = p if p.is_absolute() else reference_dir.parent.parent / p
        if not p.exists() or file_matches_source(source, p):
            return p
        # The user changed/replaced a formerly managed path. Preserve it and materialize
        # the Zotero-owned PDF under a collision-safe new name instead of overwriting.
    name = safe_filename(source.name if source.suffix.casefold() == ".pdf" else source.name + ".pdf")
    dest = reference_dir / name
    if not dest.exists():
        return dest
    try:
        if os.path.samefile(source, dest):
            return dest
    except OSError:
        pass
    stem, suffix = os.path.splitext(name)
    return reference_dir / f"{stem} [{item_key}]{suffix}"


def materialize_pdf(source: Path, dest: Path, fallback: str = "copy") -> str:
    if not source.exists():
        raise ZPPError(f"Zotero attachment file does not exist: {source}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        if file_matches_source(source, dest):
            try:
                if os.path.samefile(source, dest):
                    return "existing-hardlink-or-samefile"
            except OSError:
                pass
            return "existing-copy"
        # The destination here is either a fresh collision-safe path or an explicitly
        # managed target. User-modified prior paths are filtered by choose_reference_filename.
        dest.unlink()
    try:
        os.link(source, dest)
        return "hardlink"
    except OSError as link_error:
        if fallback == "symlink":
            try:
                os.symlink(source, dest)
                return "symlink"
            except OSError as exc:
                raise ZPPError(f"Hard link failed ({link_error}); symlink fallback also failed ({exc})") from exc
        if fallback == "copy":
            shutil.copy2(source, dest)
            return "copy"
        raise ZPPError(f"Hard link failed and fallback={fallback!r}: {link_error}") from link_error


def relpath_for_manifest(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return path.as_posix()


def load_old_manifest(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    path = root / config["manifestFile"]
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}



def claims_path(root: Path, config: dict[str, Any]) -> Path:
    return root / str(config.get("claimsFile") or CLAIMS_FILE)


def load_claims(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    path = claims_path(root, config)
    if not path.exists():
        return {
            "schemaVersion": 1,
            "managedBy": "zotero-project-papers",
            "collectionPath": config.get("collectionPath"),
            "collectionKey": config.get("collectionKey"),
            "claims": [],
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ZPPError(
            f"Cannot read claim ledger {path}: {exc}",
            code="claims_invalid",
            hint="Fix or remove the invalid claims.json before recording or auditing claims.",
        ) from exc
    if not isinstance(data, dict):
        raise ZPPError(f"{path} must contain a JSON object", code="claims_invalid")
    claims = data.get("claims")
    if claims is None:
        data["claims"] = []
    elif not isinstance(claims, list):
        raise ZPPError(f"{path}: claims must be an array", code="claims_invalid")
    data.setdefault("schemaVersion", 1)
    data.setdefault("managedBy", "zotero-project-papers")
    data.pop("projectRoot", None)
    data.setdefault("collectionPath", config.get("collectionPath"))
    data.setdefault("collectionKey", config.get("collectionKey"))
    return data


def save_claims(root: Path, config: dict[str, Any], ledger: dict[str, Any]) -> Path:
    ledger = dict(ledger)
    ledger["schemaVersion"] = 1
    ledger["managedBy"] = "zotero-project-papers"
    ledger.pop("projectRoot", None)
    ledger["collectionPath"] = config.get("collectionPath")
    ledger["collectionKey"] = config.get("collectionKey")
    ledger["updatedAt"] = utc_now()
    path = claims_path(root, config)
    json_dump(ledger, path)
    return path


def project_paper_index(root: Path, config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    manifest = load_old_manifest(root, config)
    return {
        str(row.get("itemKey")): row
        for row in (manifest.get("papers") or [])
        if isinstance(row, dict) and row.get("itemKey")
    }


def claim_default_source_layer(claim_type: str) -> str:
    return {
        "primary": "original-paper",
        "summary": "ai-summary",
        "inference": "agent-inference",
        "hypothesis": "agent-inference",
    }[claim_type]


def claim_locator_from_args(args: argparse.Namespace) -> dict[str, str]:
    locator: dict[str, str] = {}
    for attr, key in (("page", "page"), ("section", "section"), ("table", "table"), ("figure", "figure")):
        value = getattr(args, attr, None)
        if value not in (None, ""):
            locator[key] = str(value)
    return locator


def validate_claim_record(claim_type: str, source_layer: str, papers: list[str], locator: dict[str, str]) -> list[str]:
    warnings: list[str] = []
    if claim_type in {"primary", "summary"} and not papers:
        raise ZPPError(
            f"{claim_type} claims require at least one --paper ITEM_KEY.",
            code="claim_missing_paper",
        )
    if claim_type == "inference" and not papers:
        raise ZPPError(
            "Inference claims require at least one source paper. Use hypothesis for a proposal not directly based on a paper.",
            code="claim_missing_paper",
        )
    if claim_type == "primary" and source_layer != "original-paper":
        raise ZPPError(
            "A primary claim must use source layer original-paper.",
            code="claim_source_mismatch",
            hint="Use --type summary for an AI-generated summary or --type inference for an Agent synthesis.",
        )
    if claim_type == "primary" and not locator:
        warnings.append("Primary claim has no page/section/table/figure locator; add one when possible so a reviewer can reproduce the citation check.")
    if source_layer == "metadata" and claim_type == "primary":
        warnings.append("Metadata cannot establish a primary paper claim; verify against the original paper before citing it as evidence.")
    return warnings


def _claim_dedupe_key(claim: str, claim_type: str, papers: list[str]) -> tuple[str, str, tuple[str, ...]]:
    return (normalize_title(claim), claim_type, tuple(sorted(set(papers))))


def record_claim_local(
    root: Path,
    config: dict[str, Any],
    *,
    claim: str,
    claim_type: str,
    papers: list[str],
    source_layer: str | None = None,
    locator: dict[str, str] | None = None,
    source_ref: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    claim = claim.strip()
    if not claim:
        raise ZPPError("Claim text cannot be empty.", code="claim_empty")
    papers = [str(x).strip() for x in papers if str(x).strip()]
    papers = list(dict.fromkeys(papers))
    source_layer = source_layer or claim_default_source_layer(claim_type)
    locator = dict(locator or {})
    warnings = validate_claim_record(claim_type, source_layer, papers, locator)

    paper_index = project_paper_index(root, config)
    missing = [key for key in papers if key not in paper_index]
    if missing:
        raise ZPPError(
            "Claim references paper(s) that are not in the current project manifest.",
            code="claim_untracked_paper",
            hint="Add/sync the paper into the current project first, then record the claim.",
            details={"itemKeys": missing},
        )

    ledger = load_claims(root, config)
    target_key = _claim_dedupe_key(claim, claim_type, papers)
    for existing in ledger.get("claims", []):
        if not isinstance(existing, dict):
            continue
        if _claim_dedupe_key(str(existing.get("claim") or ""), str(existing.get("type") or ""), [str(x) for x in (existing.get("papers") or [])]) == target_key:
            return {
                "recorded": False,
                "duplicate": True,
                "claim": existing,
                "claimsFile": relpath_for_manifest(claims_path(root, config), root),
            }

    row: dict[str, Any] = {
        "id": f"claim-{uuid.uuid4().hex[:12]}",
        "claim": claim,
        "type": claim_type,
        "sourceLayer": source_layer,
        "papers": papers,
        "locator": locator,
        "createdAt": utc_now(),
    }
    if source_ref:
        ref_path = Path(source_ref).expanduser()
        if ref_path.is_absolute():
            try:
                source_ref = ref_path.resolve().relative_to(root.resolve()).as_posix()
            except ValueError as exc:
                raise ZPPError(
                    "--source-ref must be project-relative (or an absolute path inside the project).",
                    code="claim_source_ref_outside_project",
                    hint="Use a project-local note/summary reference so claims.json does not persist private absolute paths.",
                ) from exc
        row["sourceRef"] = Path(source_ref).as_posix()
    if note:
        row["note"] = note
    if warnings:
        row["warnings"] = warnings
    ledger.setdefault("claims", []).append(row)
    path = save_claims(root, config, ledger)
    return {
        "recorded": True,
        "duplicate": False,
        "claim": row,
        "claimsFile": relpath_for_manifest(path, root),
    }


def claims_for_paper(root: Path, config: dict[str, Any], item_key: str) -> list[dict[str, Any]]:
    ledger = load_claims(root, config)
    out: list[dict[str, Any]] = []
    for row in ledger.get("claims", []) or []:
        if isinstance(row, dict) and item_key in [str(x) for x in (row.get("papers") or [])]:
            out.append({
                "id": row.get("id"),
                "claim": row.get("claim"),
                "type": row.get("type"),
                "sourceLayer": row.get("sourceLayer"),
                "locator": row.get("locator") or {},
            })
    return out


def audit_claims_local(root: Path, config: dict[str, Any], *, include_claims: bool = False) -> dict[str, Any]:
    ledger = load_claims(root, config)
    paper_index = project_paper_index(root, config)
    rows = [x for x in (ledger.get("claims") or []) if isinstance(x, dict)]
    by_type: dict[str, int] = {}
    by_layer: dict[str, int] = {}
    broken: list[dict[str, Any]] = []
    needs_locator: list[str] = []
    original_claims = 0
    summary_claims = 0
    inference_claims = 0
    hypothesis_claims = 0
    metadata_only_claims = 0
    pdf_missing_claims: list[str] = []

    assessed: list[dict[str, Any]] = []
    for row in rows:
        cid = str(row.get("id") or "")
        ctype = str(row.get("type") or "unknown")
        layer = str(row.get("sourceLayer") or "unknown")
        papers = [str(x) for x in (row.get("papers") or [])]
        locator = row.get("locator") if isinstance(row.get("locator"), dict) else {}
        by_type[ctype] = by_type.get(ctype, 0) + 1
        by_layer[layer] = by_layer.get(layer, 0) + 1

        missing_papers = [key for key in papers if key not in paper_index]
        if missing_papers:
            broken.append({"claimId": cid, "missingItemKeys": missing_papers})
        if ctype == "primary":
            original_claims += 1
            if not locator:
                needs_locator.append(cid)
        elif ctype == "summary":
            summary_claims += 1
        elif ctype == "inference":
            inference_claims += 1
        elif ctype == "hypothesis":
            hypothesis_claims += 1
        if layer == "metadata":
            metadata_only_claims += 1
        if layer == "original-paper":
            if any(paper_index.get(key, {}).get("pdfStatus") != "available" for key in papers if key in paper_index):
                pdf_missing_claims.append(cid)

        if missing_papers:
            traceability = "broken-paper-reference"
        elif ctype == "hypothesis":
            traceability = "hypothesis-not-paper-claim"
        elif layer == "agent-inference":
            traceability = "agent-inference"
        elif layer == "ai-summary":
            traceability = "ai-summary-derived"
        elif layer == "metadata":
            traceability = "metadata-only"
        elif layer == "original-paper" and not locator:
            traceability = "original-source-needs-locator"
        else:
            traceability = "source-traceable"
        assessed.append({
            "id": cid,
            "type": ctype,
            "sourceLayer": layer,
            "traceability": traceability,
            "papers": papers,
            "locator": locator,
            "claim": row.get("claim") if include_claims else None,
        })

    verdict = "clean"
    if broken:
        verdict = "broken"
    elif needs_locator or pdf_missing_claims or metadata_only_claims:
        verdict = "needs-review"

    out: dict[str, Any] = {
        "claimsFile": relpath_for_manifest(claims_path(root, config), root),
        "claimCount": len(rows),
        "byType": by_type,
        "bySourceLayer": by_layer,
        "originalPaperClaims": original_claims,
        "aiSummaryClaims": summary_claims,
        "inferenceClaims": inference_claims,
        "hypothesisClaims": hypothesis_claims,
        "metadataOnlyClaims": metadata_only_claims,
        "needsLocatorClaimIds": needs_locator,
        "missingOriginalPdfClaimIds": sorted(set(pdf_missing_claims)),
        "brokenPaperReferences": broken,
        "verdict": verdict,
        "semanticVerificationPerformed": False,
        "note": "This is a provenance/traceability audit. It does not prove that a cited paper semantically supports the claim.",
    }
    if include_claims:
        out["claims"] = assessed
    return out

def manifest_prior_paths(old_manifest: dict[str, Any]) -> dict[str, str]:
    out = {}
    for p in old_manifest.get("papers", []) or []:
        if isinstance(p, dict) and p.get("itemKey") and p.get("projectPath"):
            out[str(p["itemKey"])] = str(p["projectPath"])
    return out


def reference_pdf_files(root: Path, config: dict[str, Any]) -> list[Path]:
    ref_dir = (root / config["referenceDir"]).resolve()
    if not ref_dir.exists():
        return []
    return sorted((p.resolve() for p in ref_dir.rglob("*.pdf") if p.is_file()), key=lambda p: str(p).casefold())


def consistency_snapshot(client: ZoteroClient, root: Path, config: dict[str, Any]) -> dict[str, Any]:
    rows = collection_items(client, config["collectionKey"])
    zotero_keys = {str(item_summary(x, brief=True).get("key") or "") for x in rows}
    zotero_keys.discard("")
    manifest = load_old_manifest(root, config)
    manifest_rows = [x for x in (manifest.get("papers") or []) if isinstance(x, dict)]
    manifest_keys = {str(x.get("itemKey") or "") for x in manifest_rows}
    manifest_keys.discard("")

    managed_paths: set[str] = set()
    missing_managed: list[str] = []
    modified_managed: list[str] = []
    for row in manifest_rows:
        rel = row.get("projectPath")
        if not rel:
            continue
        rel = str(rel)
        rel_norm = Path(rel).as_posix()
        managed_paths.add(rel_norm)
        local_path = root / rel
        if not local_path.exists():
            missing_managed.append(rel_norm)
            continue
        zotero_path = row.get("zoteroPath")
        if zotero_path:
            source = Path(str(zotero_path))
            if source.exists() and not file_matches_source(source, local_path):
                modified_managed.append(rel_norm)

    local_pdfs = reference_pdf_files(root, config)
    local_rel = {relpath_for_manifest(x, root) for x in local_pdfs}
    local_only = sorted(local_rel - managed_paths)
    zotero_only = sorted(zotero_keys - manifest_keys)
    manifest_only = sorted(manifest_keys - zotero_keys)
    missing_managed = sorted(set(missing_managed))
    modified_managed = sorted(set(modified_managed))

    drift = {
        "zoteroOnlyItemKeys": zotero_only,
        "manifestOnlyItemKeys": manifest_only,
        "missingManagedFiles": missing_managed,
        "modifiedManagedFiles": modified_managed,
        "localOnlyFiles": local_only,
    }
    canonical = json.dumps(drift, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    clean = not any(drift.values())
    sync_cfg = config.get("sync") if isinstance(config.get("sync"), dict) else default_sync_config()
    ack = sync_cfg.get("acknowledgedFingerprint")
    policy = sync_cfg.get("driftPolicy", "prompt")
    needs_prompt = (not clean) and not (policy == "continue" and ack == fingerprint)
    return {
        "clean": clean,
        "fingerprint": fingerprint,
        "needsPrompt": needs_prompt,
        "driftPolicy": policy,
        "zoteroCount": len(zotero_keys),
        "manifestCount": len(manifest_keys),
        "localPdfCount": len(local_rel),
        "drift": drift,
    }


def persist_consistency_state(
    root: Path,
    config: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    acknowledge_continue: bool = False,
    reset_policy: bool = False,
) -> dict[str, Any]:
    sync_cfg = default_sync_config()
    if isinstance(config.get("sync"), dict):
        sync_cfg.update(config["sync"])
    if snapshot["clean"]:
        sync_cfg.update(default_sync_config())
        sync_cfg["lastCheckedAt"] = utc_now()
    else:
        sync_cfg["state"] = "diverged"
        sync_cfg["lastCheckedAt"] = utc_now()
        if reset_policy:
            sync_cfg["driftPolicy"] = "prompt"
            sync_cfg["acknowledgedFingerprint"] = None
            sync_cfg["acknowledgedAt"] = None
        if acknowledge_continue:
            sync_cfg["driftPolicy"] = "continue"
            sync_cfg["acknowledgedFingerprint"] = snapshot["fingerprint"]
            sync_cfg["acknowledgedAt"] = utc_now()
    config["sync"] = sync_cfg
    save_project_config(root, config)
    updated = dict(snapshot)
    updated["driftPolicy"] = sync_cfg.get("driftPolicy")
    updated["needsPrompt"] = (not updated["clean"]) and not (
        sync_cfg.get("driftPolicy") == "continue"
        and sync_cfg.get("acknowledgedFingerprint") == updated["fingerprint"]
    )
    return updated


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def project_record(client: ZoteroClient, obj: dict[str, Any], root: Path, config: dict[str, Any], prior_path: str | None = None) -> dict[str, Any]:
    s = item_summary(obj)
    attachments = pdf_attachments(client, s["key"])
    chosen = next((a for a in attachments if a.get("path")), None)
    record = dict(s)
    record["itemKey"] = record.pop("key")
    record["attachments"] = attachments
    record["projectPath"] = None
    record["materialization"] = None
    record["pdfStatus"] = "missing"
    if chosen:
        source = Path(chosen["path"])
        ref_dir = root / config["referenceDir"]
        prior_abs = str(root / prior_path) if prior_path else None
        dest = choose_reference_filename(source, record["itemKey"], ref_dir, prior_abs)
        mode = materialize_pdf(source, dest, config.get("fallback", "copy"))
        record["projectPath"] = relpath_for_manifest(dest, root)
        record["materialization"] = mode
        record["attachmentKey"] = chosen.get("key")
        record["zoteroPath"] = str(source)
        record["pdfStatus"] = "available"
    return record


def write_bib(client: ZoteroClient, root: Path, config: dict[str, Any]) -> None:
    result = client.read(
        f"users/0/collections/{config['collectionKey']}/items",
        {"format": "bibtex", "itemType": "-attachment", "limit": 100},
        raw=True,
    )
    assert isinstance(result, HTTPResult)
    path = root / config["bibFile"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result.text(), encoding="utf-8")


def sync_project(client: ZoteroClient, root: Path, config: dict[str, Any], prune: bool = False) -> dict[str, Any]:
    old = load_old_manifest(root, config)
    priors = manifest_prior_paths(old)
    rows = collection_items(client, config["collectionKey"])
    papers = []
    for obj in rows:
        key = item_summary(obj)["key"]
        papers.append(project_record(client, obj, root, config, priors.get(key)))
    papers.sort(key=lambda x: ((x.get("date") or ""), (x.get("title") or "").casefold()), reverse=True)
    manifest = {
        "schemaVersion": 1,
        "managedBy": "zotero-project-papers",
        "generatedAt": utc_now(),
        "projectRoot": str(root),
        "collectionPath": config["collectionPath"],
        "collectionKey": config["collectionKey"],
        "papers": papers,
    }
    manifest_path = root / config["manifestFile"]
    json_dump(manifest, manifest_path)
    write_bib(client, root, config)

    pruned: list[str] = []
    prune_skipped_modified: list[str] = []
    if prune:
        desired = {p.get("projectPath") for p in papers if p.get("projectPath")}
        ref_dir = (root / config["referenceDir"]).resolve()
        for p in old.get("papers", []) or []:
            old_path = p.get("projectPath") if isinstance(p, dict) else None
            if not old_path or old_path in desired:
                continue
            candidate = (root / old_path).resolve()
            try:
                candidate.relative_to(ref_dir)
            except ValueError:
                continue
            if candidate.exists() and candidate.is_file():
                zotero_path = p.get("zoteroPath") if isinstance(p, dict) else None
                if zotero_path and Path(str(zotero_path)).exists() and not file_matches_source(Path(str(zotero_path)), candidate):
                    prune_skipped_modified.append(relpath_for_manifest(candidate, root))
                    continue
                candidate.unlink()
                pruned.append(relpath_for_manifest(candidate, root))
    snapshot = consistency_snapshot(client, root, config)
    consistency = persist_consistency_state(root, config, snapshot)
    old_rows = {str(p.get("itemKey")): p for p in old.get("papers", []) if isinstance(p, dict) and p.get("itemKey")}
    new_rows = {str(p.get("itemKey")): p for p in papers if p.get("itemKey")}
    old_keys = set(old_rows)
    new_keys = set(new_rows)
    sig_fields = ("title", "date", "DOI", "publicationTitle", "attachmentKey", "projectPath", "pdfStatus")
    updated_keys = sorted(
        key for key in (old_keys & new_keys)
        if any(old_rows[key].get(f) != new_rows[key].get(f) for f in sig_fields)
    )
    return {
        "projectRoot": str(root),
        "collection": config["collectionPath"],
        "paperCount": len(papers),
        "withPdf": sum(1 for p in papers if p.get("projectPath")),
        "withoutPdf": sum(1 for p in papers if not p.get("projectPath")),
        "manifest": config["manifestFile"],
        "bib": config["bibFile"],
        "added": len(new_keys - old_keys),
        "updated": len(updated_keys),
        "removed": len(old_keys - new_keys),
        "addedItemKeys": sorted(new_keys - old_keys),
        "updatedItemKeys": updated_keys,
        "removedItemKeys": sorted(old_keys - new_keys),
        "pruned": pruned,
        "pruneSkippedModified": prune_skipped_modified,
        "consistency": consistency,
        "papers": papers,
    }


def compact_sync_result(result: dict[str, Any], *, include_papers: bool = False) -> dict[str, Any]:
    out = dict(result)
    if not include_papers:
        out.pop("papers", None)
    return out


def item_type_fields(client: ZoteroClient, item_type: str) -> tuple[set[str], str]:
    """Return valid Zotero fields without depending on GET /items/new.

    The local API documents item type/field endpoints, but older or partial
    implementations may still omit them. In that case use a conservative
    built-in set so common imports remain available.
    """
    try:
        rows = client.read("itemTypeFields", {"itemType": item_type})
        fields = {str(x.get("field")) for x in (rows or []) if isinstance(x, dict) and x.get("field")}
        if fields:
            return fields, "itemTypeFields"
    except ZPPError:
        pass
    return set(ITEM_TYPE_FALLBACK_FIELDS.get(item_type, COMMON_FALLBACK_FIELDS)), "builtin-fallback"


def build_item_from_metadata(client: ZoteroClient, metadata: dict[str, Any], collection_key: str) -> dict[str, Any]:
    item_type = str(metadata.get("itemType") or "journalArticle")
    allowed, _strategy = item_type_fields(client, item_type)
    item: dict[str, Any] = {
        "itemType": item_type,
        "tags": metadata.get("tags") if isinstance(metadata.get("tags"), list) else [],
        "collections": [collection_key],
        "relations": metadata.get("relations") if isinstance(metadata.get("relations"), dict) else {},
    }
    if isinstance(metadata.get("creators"), list):
        item["creators"] = metadata["creators"]
    reserved = {"key", "version", "dateAdded", "dateModified", "parentItem", "linkMode", "filename", "contentType"}
    for key, value in metadata.items():
        if key in allowed and key not in reserved:
            item[key] = value
    return item


def canonical_pdf_filename(metadata: dict[str, Any], fallback_stem: str = "paper") -> str:
    creators = [c for c in (metadata.get("creators") or []) if isinstance(c, dict)]
    authors = [c for c in creators if c.get("creatorType") == "author"] or creators
    author = "Unknown"
    if authors:
        c = authors[0]
        author = str(c.get("lastName") or c.get("name") or c.get("firstName") or "Unknown").strip() or "Unknown"
        if len(authors) > 1:
            author += " et al."
    date = str(metadata.get("date") or "")
    m = re.search(r"(?:19|20)\d{2}", date)
    year = m.group(0) if m else "n.d."
    title = str(metadata.get("title") or fallback_stem).strip() or fallback_stem
    return safe_filename(f"{author} - {year} - {title}.pdf")


def create_imported_attachment(
    client: ZoteroClient, parent_key: str, pdf: Path, metadata: dict[str, Any]
) -> tuple[str, str]:
    filename = canonical_pdf_filename(metadata, pdf.stem)
    item = {
        "itemType": "attachment",
        "parentItem": parent_key,
        "linkMode": "imported_file",
        "title": Path(filename).stem,
        "contentType": "application/pdf",
        "filename": filename,
        "tags": [],
        "relations": {},
    }
    return client.create_item(item), filename


def metadata_diff(existing_obj: dict[str, Any], incoming: dict[str, Any]) -> dict[str, dict[str, Any]]:
    existing = item_data(existing_obj)
    interesting = {
        "itemType", "title", "creators", "date", "DOI", "publicationTitle",
        "proceedingsTitle", "conferenceName", "volume", "issue", "pages",
        "publisher", "place", "ISBN", "ISSN", "url",
    }
    diff: dict[str, dict[str, Any]] = {}
    for key in interesting:
        if key not in incoming:
            continue
        new = incoming.get(key)
        if new in (None, "", [], {}):
            continue
        old = existing.get(key)
        if old != new:
            diff[key] = {"existing": old, "incoming": new}
    return diff


def update_existing_metadata(
    client: ZoteroClient, item_key: str, metadata: dict[str, Any]
) -> dict[str, Any]:
    obj = get_item(client, item_key)
    d = item_data(obj)
    old_type = str(d.get("itemType") or "")
    new_type = str(metadata.get("itemType") or old_type)
    if new_type != old_type:
        raise ZPPError(
            f"Existing item {item_key} is {old_type}, but incoming metadata is {new_type}.",
            code="item_type_conflict",
            hint="Change the item type in Zotero manually, or import as a new item after resolving the duplicate. The skill will not silently rewrite an existing item's type.",
        )
    allowed, _ = item_type_fields(client, old_type)
    patch: dict[str, Any] = {}
    for key, value in metadata.items():
        if key in allowed or key == "creators":
            if value not in (None, "", [], {}):
                patch[key] = value
    if not patch:
        return {"updated": False, "fields": []}
    headers = {"If-Unmodified-Since-Version": str(d.get("version", 0))}
    client.write("PATCH", f"users/0/items/{item_key}", json_body=patch, extra_headers=headers)
    return {"updated": True, "fields": sorted(patch.keys())}


def md5_file(path: Path) -> str:
    h = hashlib.md5()  # Zotero upload protocol requires MD5
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def upload_attachment_file(client: ZoteroClient, attachment_key: str, pdf: Path, filename: str | None = None) -> None:
    md5 = md5_file(pdf)
    stat_result = pdf.stat()
    form = {
        "md5": md5,
        "filename": safe_filename(filename or pdf.name),
        "filesize": stat_result.st_size,
        "mtime": int(stat_result.st_mtime * 1000),
    }
    stage1 = client.write(
        "POST",
        f"users/0/items/{attachment_key}/file",
        form=form,
        extra_headers={"If-None-Match": "*"},
    )
    if isinstance(stage1, dict) and stage1.get("exists"):
        return
    if not isinstance(stage1, dict) or not stage1.get("uploadKey") or not stage1.get("url"):
        raise ZPPError(f"Unexpected Zotero upload authorization response: {stage1}")
    upload_url = urllib.parse.urljoin(client.base_url, stage1["url"])
    ctype = stage1.get("contentType") or mimetypes.guess_type(pdf.name)[0] or "application/pdf"
    client.upload_bytes(upload_url, pdf, ctype)
    client.write(
        "POST",
        f"users/0/items/{attachment_key}/file",
        form={"upload": stage1["uploadKey"]},
        extra_headers={"If-None-Match": "*"},
    )


def load_metadata_file(path_value: str) -> dict[str, Any]:
    path = Path(path_value).expanduser().resolve()
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ZPPError(f"Cannot read metadata JSON {path}: {exc}", code="metadata_read_failed") from exc
    if not isinstance(metadata, dict) or not str(metadata.get("title") or "").strip():
        raise ZPPError("Metadata JSON must be an object containing at least `title`", code="metadata_invalid")
    return metadata


def strip_markup(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value or ""))).strip()


def crossref_metadata(doi_value: str) -> dict[str, Any]:
    doi = normalize_doi(doi_value)
    if not doi or not re.match(r"^10\.\d{4,9}/\S+$", doi):
        raise ZPPError(f"Invalid DOI: {doi_value}", code="invalid_doi")
    url = f"{CROSSREF_BASE_URL}/works/{urllib.parse.quote(doi, safe='')}"
    mailto = os.environ.get("ZPP_CROSSREF_MAILTO", "").strip()
    agent = "ZoteroProjectPapers/0.5.2"
    if mailto:
        agent += f" (mailto:{mailto})"
    req = urllib.request.Request(url, headers={"User-Agent": agent, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise ZPPError(f"Crossref metadata lookup failed for {doi}: {exc}", code="crossref_lookup_failed") from exc
    message = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(message, dict):
        raise ZPPError(f"Crossref returned no work metadata for {doi}", code="crossref_no_metadata")
    ctype = str(message.get("type") or "")
    item_type = {
        "journal-article": "journalArticle", "proceedings-article": "conferencePaper",
        "book-chapter": "bookSection", "posted-content": "preprint", "report": "report",
        "dissertation": "thesis",
    }.get(ctype, "journalArticle")
    title_list = message.get("title") or []
    title = strip_markup(str(title_list[0])) if title_list else ""
    creators = []
    for author in message.get("author") or []:
        if not isinstance(author, dict):
            continue
        creators.append({
            "creatorType": "author", "firstName": str(author.get("given") or ""),
            "lastName": str(author.get("family") or author.get("name") or ""),
        })
    date_parts = None
    for key in ("published-print", "published-online", "issued", "created"):
        block = message.get(key)
        if isinstance(block, dict) and block.get("date-parts"):
            date_parts = block.get("date-parts")
            break
    date = ""
    if isinstance(date_parts, list) and date_parts and isinstance(date_parts[0], list):
        parts = [str(x) for x in date_parts[0][:3]]
        if parts:
            date = "-".join(x.zfill(2) if i else x for i, x in enumerate(parts))
    container = message.get("container-title") or []
    venue = strip_markup(str(container[0])) if container else ""
    metadata: dict[str, Any] = {
        "itemType": item_type, "title": title or doi, "creators": creators, "date": date,
        "DOI": str(message.get("DOI") or doi), "url": str(message.get("URL") or f"https://doi.org/{doi}"),
        "abstractNote": strip_markup(str(message.get("abstract") or "")),
        "volume": str(message.get("volume") or ""), "issue": str(message.get("issue") or ""),
        "pages": str(message.get("page") or ""), "publisher": str(message.get("publisher") or ""),
        "ISSN": ", ".join(str(x) for x in (message.get("ISSN") or [])),
    }
    if item_type == "conferencePaper":
        metadata["proceedingsTitle"] = venue
        event = message.get("event")
        if isinstance(event, dict):
            metadata["conferenceName"] = str(event.get("name") or venue)
    elif item_type == "bookSection":
        metadata["bookTitle"] = venue
    else:
        metadata["publicationTitle"] = venue
    metadata = {k: v for k, v in metadata.items() if v not in (None, "", [], {})}
    links = []
    for link in message.get("link") or []:
        if isinstance(link, dict) and link.get("URL"):
            links.append({k: link.get(k) for k in ("URL", "content-type", "content-version", "intended-application") if link.get(k)})
    return {"metadata": metadata, "source": "crossref", "doi": doi, "links": links}


def source_trust(url: str) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").casefold()
    trusted = any(host == x or host.endswith("." + x) for x in TRUSTED_PAPER_HOSTS)
    return {
        "scheme": parsed.scheme.casefold(), "host": host,
        "knownAcademicHost": trusted,
        "accessRights": "not-verified",
    }


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_pdf_file(path: Path, *, content_type: str = "") -> dict[str, Any]:
    size = path.stat().st_size
    if size < 1024:
        raise ZPPError(f"Downloaded file is too small to be a paper PDF ({size} bytes).", code="invalid_pdf")
    with path.open("rb") as f:
        head = f.read(8)
        f.seek(max(0, size - 4096))
        tail = f.read()
    if not head.startswith(b"%PDF-"):
        raise ZPPError("Downloaded content does not start with a PDF header.", code="invalid_pdf")
    if b"%%EOF" not in tail:
        raise ZPPError("Downloaded PDF appears truncated (EOF marker missing near file end).", code="invalid_pdf")
    # Lightweight, dependency-free estimate; not used as a correctness guarantee.
    raw = path.read_bytes() if size <= 50 * 1024 * 1024 else b""
    page_estimate = len(re.findall(br"/Type\s*/Page\b", raw)) if raw else None
    return {
        "validPdf": True, "validationLevel": "stdlib-lightweight", "sizeBytes": size,
        "contentType": content_type, "sha256": sha256_file(path), "pageCountEstimate": page_estimate,
        "note": "Page count/title validation is intentionally lightweight without a PDF parsing dependency.",
    }


def download_pdf(url: str, output: Path, *, allow_untrusted: bool = False, max_bytes: int = 500 * 1024 * 1024) -> dict[str, Any]:
    trust = source_trust(url)
    if trust["scheme"] != "https":
        raise ZPPError("fetch accepts HTTPS paper URLs only.", code="unsafe_download_url")
    if not trust["knownAcademicHost"] and not allow_untrusted:
        raise ZPPError(
            f"Refusing an unrecognized paper host: {trust['host']}", code="untrusted_paper_host",
            hint="Prefer an official/open-access paper URL, or explicitly use --allow-untrusted after verifying the source.",
            details=trust,
        )
    req = urllib.request.Request(url, headers={"User-Agent": "ZoteroProjectPapers/0.5.2", "Accept": "application/pdf,*/*;q=0.5"})
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(output.suffix + ".part")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, tmp.open("wb") as f:
            final_url = resp.geturl()
            final_trust = source_trust(final_url)
            if not final_trust["knownAcademicHost"] and not allow_untrusted:
                raise ZPPError(f"Download redirected to an unrecognized host: {final_trust['host']}", code="untrusted_redirect", details=final_trust)
            content_type = str(resp.headers.get("Content-Type") or "").split(";", 1)[0].strip().casefold()
            total = 0
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ZPPError("Downloaded file exceeds the configured maximum size.", code="download_too_large")
                f.write(chunk)
        validation = validate_pdf_file(tmp, content_type=content_type)
        tmp.replace(output)
        return {"url": url, "finalUrl": final_url, "output": str(output), "source": final_trust, "validation": validation}
    except Exception:
        try:
            if tmp.exists(): tmp.unlink()
        except OSError:
            pass
        raise


def import_metadata_record(client: ZoteroClient, root: Path, config: dict[str, Any], metadata: dict[str, Any], *, update_existing: bool = False) -> dict[str, Any]:
    duplicates = find_duplicates(client, metadata)
    if duplicates:
        duplicate = duplicates[0]
        key = str(item_summary(duplicate, brief=True)["key"])
        diff = metadata_diff(duplicate, metadata)
        update = update_existing_metadata(client, key, metadata) if update_existing and diff else None
        changed = add_item_to_collection(client, key, config["collectionKey"])
        sync = sync_project(client, root, config, prune=False)
        record = next((p for p in sync["papers"] if p.get("itemKey") == key), None)
        return {
            "action": "reused-existing", "itemKey": key, "addedToCollection": changed,
            "metadataDiff": diff, "metadataUpdate": update, "paper": record,
            "pdfStatus": (record or {}).get("pdfStatus", "unknown"),
            "duplicateItemKeys": [item_summary(x, brief=True)["key"] for x in duplicates],
        }
    client.ensure_write_auth(require_remembered=True)
    item = build_item_from_metadata(client, metadata, config["collectionKey"])
    key = client.create_item(item)
    sync = sync_project(client, root, config, prune=False)
    record = next((p for p in sync["papers"] if p.get("itemKey") == key), None)
    return {"action": "imported-metadata", "itemKey": key, "paper": record, "pdfStatus": "missing"}


def attach_pdf_to_item(client: ZoteroClient, root: Path, config: dict[str, Any], item_key: str, pdf: Path, *, allow_multiple: bool = False) -> dict[str, Any]:
    obj = get_item(client, item_key)
    existing = [a for a in pdf_attachments(client, item_key) if a.get("path")]
    if existing and not allow_multiple:
        return {"action": "existing-pdf", "itemKey": item_key, "attachments": existing, "note": "Existing PDF preserved; use --allow-multiple only when a second PDF attachment is intentional."}
    client.ensure_write_auth(require_remembered=True)
    metadata = item_data(obj)
    attachment_key, filename = create_imported_attachment(client, item_key, pdf, metadata)
    upload_attachment_file(client, attachment_key, pdf, filename)
    reference_dir = (root / config["referenceDir"]).resolve()
    absorbed = False
    if is_within(pdf, reference_dir) and pdf.exists():
        pdf.unlink()
        absorbed = True
    sync = sync_project(client, root, config, prune=False)
    record = next((p for p in sync["papers"] if p.get("itemKey") == item_key), None)
    return {"action": "attached-pdf", "itemKey": item_key, "attachmentKey": attachment_key, "attachmentFilename": filename, "absorbedLocalReference": absorbed, "paper": record}


# ---------- Commands ----------


def requested_project_root(args: argparse.Namespace) -> Path | None:
    value = getattr(args, "project_root", None) or os.environ.get("ZPP_PROJECT_ROOT")
    return Path(value).expanduser().resolve() if value else None


def project_relative_path(value: str, *, label: str) -> str:
    path = Path(value)
    if path.is_absolute():
        raise ZPPError(f"{label} must be relative to the project root: {value}", code="invalid_project_path")
    normalized = Path(os.path.normpath(str(path)))
    if normalized == Path(".") or ".." in normalized.parts:
        raise ZPPError(f"{label} must stay inside the project root: {value}", code="invalid_project_path")
    return normalized.as_posix()


def find_zotero_executable() -> str | None:
    candidates: list[Path] = []
    if os.name == "nt":
        for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
            base = os.environ.get(env_name)
            if base:
                candidates.append(Path(base) / "Zotero" / "zotero.exe")
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.append(Path(local) / "Programs" / "Zotero" / "zotero.exe")
        try:
            import winreg  # type: ignore
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                for key_name in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\zotero.exe", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\zotero.exe"):
                    try:
                        with winreg.OpenKey(hive, key_name) as key:
                            value, _ = winreg.QueryValueEx(key, None)
                            if value:
                                candidates.insert(0, Path(value))
                    except OSError:
                        pass
        except ImportError:
            pass
    elif sys.platform == "darwin":
        candidates.append(Path("/Applications/Zotero.app/Contents/MacOS/zotero"))
    else:
        for path in ("/usr/bin/zotero", "/usr/local/bin/zotero"):
            candidates.append(Path(path))
        found = shutil.which("zotero")
        if found:
            candidates.insert(0, Path(found))
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return shutil.which("zotero")


def cmd_doctor(args: argparse.Namespace) -> dict[str, Any]:
    root = project_root(requested_project_root(args))
    diagnosis = "unknown"
    zotero_running = False
    local_api_enabled = False
    server_id_detected = False
    zotero_version: str | None = None
    api_version: str | None = None
    server_id: str | None = None
    http_status: int | None = None
    response_headers: list[str] = []
    write_authorized = False
    client = ZoteroClient()
    api_error: dict[str, Any] | None = None
    auth_store_read_error: dict[str, Any] | None = None

    try:
        result = client._raw_request("GET", "")
        http_status = result.status
        zotero_running = True
        zotero_version = result.header("X-Zotero-Version")
        api_version = result.header("Zotero-API-Version")
        server_id = result.header("Zotero-Server-ID")
        server_id_detected = bool(server_id)
        local_api_enabled = result.status == 200 and bool(server_id or api_version)
        response_headers = sorted(result.headers.keys())
        if server_id:
            client.server_id = server_id
            client.api_version = api_version
            try:
                store = load_auth_store()
                client.api_key = (store.get("servers", {}).get(server_id, {}) or {}).get("key")
            except ZPPError as auth_exc:
                client.api_key = None
                auth_store_read_error = auth_exc.as_dict()
            write_authorized = bool(client.api_key)
            diagnosis = "ready" if write_authorized else "authorization_required"
        else:
            diagnosis = "unexpected_local_api_response"
        if zotero_version:
            major_match = re.match(r"(\d+)", zotero_version)
            if major_match and int(major_match.group(1)) < 10:
                diagnosis = "zotero_version_unsupported"
    except ZPPError as exc:
        api_error = exc.as_dict()
        details = exc.details or {}
        headers = details.get("headers") if isinstance(details, dict) else {}
        if isinstance(headers, dict):
            zotero_version = headers.get("x-zotero-version")
            api_version = headers.get("zotero-api-version")
            response_headers = sorted(headers.keys())
        http_status = details.get("status") if isinstance(details, dict) else None
        if exc.code == "zotero_http_403":
            zotero_running = True
            local_api_enabled = False
            diagnosis = "local_api_disabled"
        elif exc.code == "zotero_unreachable":
            diagnosis = "zotero_unreachable"
        else:
            zotero_running = bool(zotero_version or http_status)
            diagnosis = "local_api_error"

    # Connector ping is informational and helps distinguish "Zotero process exists"
    # from a proxy/other service bound to the Local API port. It never changes settings.
    connector_ping: dict[str, Any] | None = None
    if not zotero_version:
        parsed_base = urllib.parse.urlsplit(client.base_url)
        ping_url = f"{parsed_base.scheme}://{parsed_base.netloc}/connector/ping"
        try:
            ping = client._raw_request("GET", ping_url, absolute=True, timeout=3)
            ping_version = ping.header("X-Zotero-Version")
            connector_ping = {"ok": ping.status == 200, "status": ping.status, "xZoteroVersion": ping_version}
            if ping_version:
                zotero_version = ping_version
                zotero_running = True
        except ZPPError as ping_exc:
            connector_ping = {"ok": False, "error": ping_exc.as_dict()}

    auth_store = auth_store_preflight(create=True)
    read_ready = bool(local_api_enabled and server_id_detected and diagnosis not in {"zotero_version_unsupported"})
    if read_ready and not auth_store.get("writable"):
        diagnosis = "read_ready_write_auth_store_unwritable"
    write_ready = bool(read_ready and write_authorized and auth_store.get("writable"))

    project_initialized = (root / CONFIG_NAME).exists()
    project_collection_exists: bool | None = None
    project_collection_key: str | None = None
    reference_dir: str | None = None
    legacy_dirs: list[dict[str, Any]] = []
    if project_initialized:
        try:
            _, cfg = load_project(root)
            assert cfg is not None
            project_collection_key = str(cfg.get("collectionKey") or "") or None
            reference_dir = str(cfg.get("referenceDir") or "") or None
            legacy_dirs = possible_legacy_reference_dirs(root, reference_dir or "")
            if server_id and project_collection_key:
                try:
                    obj = client.read(f"users/0/collections/{project_collection_key}")
                    project_collection_exists = isinstance(obj, dict)
                except ZPPError:
                    project_collection_exists = False
        except ZPPError:
            pass
    else:
        legacy_dirs = detect_reference_dirs(root)

    checks = [
        {"name": "zotero-process-or-http-service", "ok": zotero_running, "version": zotero_version},
        {"name": "local-api", "ok": local_api_enabled, "httpStatus": http_status},
        {"name": "server-id", "ok": server_id_detected, "serverID": server_id, "requiredForWrites": True},
        {"name": "auth-store", **auth_store},
        {"name": "write-authorization", "ok": write_authorized, "storedKeyPresent": write_authorized},
        {"name": "project-root", "ok": True, "path": str(root), "initialized": project_initialized},
        {"name": "project-collection", "ok": project_collection_exists if project_collection_exists is not None else True, "key": project_collection_key, "exists": project_collection_exists},
    ]

    return {
        "ok": read_ready,
        "readReady": read_ready,
        "writeReady": write_ready,
        "diagnosis": diagnosis,
        "zoteroRunning": zotero_running,
        "zoteroVersion": zotero_version,
        "localApiEnabled": local_api_enabled,
        "httpStatus": http_status,
        "apiVersion": api_version,
        "serverIdDetected": server_id_detected,
        "serverID": server_id,
        "writeAuthorized": write_authorized,
        "writeAuthorizationStatus": "stored-key-present" if write_authorized else "not-stored-or-not-authorized",
        "authStore": {**auth_store, "readError": auth_store_read_error},
        "projectRoot": str(root),
        "projectInitialized": project_initialized,
        "projectCollectionKey": project_collection_key,
        "projectCollectionExists": project_collection_exists,
        "activeReferenceDir": reference_dir,
        "possibleLegacyReferenceDirs": legacy_dirs,
        "responseHeaders": response_headers,
        "connectorPing": connector_ping,
        "error": api_error,
        "checks": checks,
        "nextAction": {
            "ready": "continue",
            "authorization_required": "read/search can continue; immediately before the first write, tell the user Zotero will show a permission dialog, then run authorize and ask them to choose Always Allow (recommended) or Allow for one-time access",
            "local_api_disabled": "ask the user to enable Zotero local application communication; do not use GUI automation unless explicitly requested",
            "zotero_unreachable": "ask the user to open Zotero, keep it running, and retry the blocked Zotero operation",
            "unexpected_local_api_response": "do not change Zotero settings automatically; inspect port/version/proxy response",
            "zotero_version_unsupported": "upgrade to Zotero 10+ before using write features",
            "read_ready_write_auth_store_unwritable": "read/search is available; set ZPP_AUTH_STORE to a writable private path before any write/authorization",
        }.get(diagnosis, "inspect the structured diagnostics; do not modify Zotero settings automatically"),
        "userInteraction": (
            {"required": True, "type": "start_zotero", "message": "Please open Zotero and keep it running, then retry."}
            if diagnosis == "zotero_unreachable" else
            {"required": False, "type": "write_authorization_later", "message": "No action is needed for read-only search. Before the first write, Zotero will show an authorization dialog; choose Always Allow to avoid repeated prompts."}
            if diagnosis == "authorization_required" else
            {"required": True, "type": "enable_local_api", "message": "Enable Zotero Settings → Advanced → Allow other applications on this computer to communicate with Zotero."}
            if diagnosis == "local_api_disabled" else
            {"required": False, "type": None, "message": None}
        ),
    }

def cmd_status(args: argparse.Namespace) -> dict[str, Any]:
    client = ZoteroClient()
    client.bootstrap()
    root, config = load_project(requested_project_root(args), required=False)
    return {
        "ok": True,
        "zoteroBase": client.base_url,
        "zoteroServerID": client.server_id,
        "zoteroApiVersion": client.api_version,
        "writeAuthorized": bool(client.api_key),
        "authStore": str(auth_path()),
        "projectRoot": str(root),
        "projectInitialized": bool(config),
        "collection": config.get("collectionPath") if config else None,
        "referenceDir": config.get("referenceDir") if config else None,
    }

def cmd_check(args: argparse.Namespace) -> dict[str, Any]:
    root, config = load_project(requested_project_root(args))
    assert config is not None

    # v0.5 fast path: when local files/manifest and the cached Zotero library version
    # are unchanged, reuse the last full consistency result without touching Zotero.
    quick = quick_project_marker(root, config)
    sync_cfg = config.get("sync") if isinstance(config.get("sync"), dict) else default_sync_config()
    previous_quick = sync_cfg.get("quickFingerprint")
    last_full_clean = sync_cfg.get("lastFullClean")
    quick_out = quick if getattr(args, "verbose", False) else {
        "fingerprint": quick["fingerprint"],
        "localPdfCount": len(quick.get("pdfs") or []),
        "cachedLibraryVersion": quick.get("cachedLibraryVersion"),
    }
    legacy_dirs = possible_legacy_reference_dirs(root, str(config.get("referenceDir") or ""))
    if not args.full and previous_quick == quick["fingerprint"] and last_full_clean is not None:
        if bool(last_full_clean):
            recommendation = "continue"
            status = "clean"
        elif sync_cfg.get("driftPolicy") == "continue":
            recommendation = "continue-with-acknowledged-drift"
            status = "diverged"
        else:
            recommendation = "ask-user"
            status = "diverged"
        return {
            "ok": True,
            "projectRoot": str(root),
            "collection": config["collectionPath"],
            "referenceDir": config["referenceDir"],
            "status": status,
            "recommendation": recommendation,
            "checkMode": "quick-cache-hit",
            "zoteroContacted": False,
            "quick": quick_out,
            "possibleLegacyReferenceDirs": legacy_dirs,
        }

    client = ZoteroClient()
    client.bootstrap()
    if not config.get("zoteroServerID"):
        config["zoteroServerID"] = client.server_id
    snapshot = consistency_snapshot(client, root, config)
    snapshot = persist_consistency_state(
        root,
        config,
        snapshot,
        acknowledge_continue=args.ack_continue,
        reset_policy=args.reset_policy,
    )
    config["sync"]["quickFingerprint"] = quick["fingerprint"]
    config["sync"]["lastFullClean"] = bool(snapshot["clean"])
    save_project_config(root, config)
    if snapshot["clean"]:
        recommendation = "continue"
    elif snapshot["needsPrompt"]:
        recommendation = "ask-user"
    else:
        recommendation = "continue-with-acknowledged-drift"
    return {
        "ok": True,
        "projectRoot": str(root),
        "collection": config["collectionPath"],
        "referenceDir": config["referenceDir"],
        "status": "clean" if snapshot["clean"] else "diverged",
        "recommendation": recommendation,
        "checkMode": "full" if args.full else "full-after-marker-change",
        "zoteroContacted": True,
        "quick": quick_out,
        "possibleLegacyReferenceDirs": legacy_dirs,
        "consistency": snapshot,
    }


def cmd_authorize(args: argparse.Namespace) -> dict[str, Any]:
    client = ZoteroClient()
    client.bootstrap()
    return client.authorize(require_remembered=not args.allow_once)


def cmd_set_reference_dir(args: argparse.Namespace) -> dict[str, Any]:
    root, config = load_project(requested_project_root(args))
    assert config is not None
    new_rel = project_relative_path(args.reference_dir, label="reference directory")
    old_rel = str(config["referenceDir"])
    if Path(new_rel) == Path(old_rel):
        return {"changed": False, "projectRoot": str(root), "referenceDir": new_rel, "config": config}

    new_dir = root / new_rel
    existing_pdfs = [p for p in new_dir.rglob("*.pdf") if p.is_file()] if new_dir.exists() else []
    if existing_pdfs and not args.allow_existing_target:
        raise ZPPError(
            f"Target reference directory already contains {len(existing_pdfs)} PDF(s): {new_rel}",
            code="reference_target_not_empty",
            hint="Ask the user whether to use/merge this directory, then rerun with --allow-existing-target if confirmed.",
            details={"referenceDir": new_rel, "pdfCount": len(existing_pdfs)},
        )

    old_manifest = load_old_manifest(root, config)
    old_manifest_path = root / config["manifestFile"]
    config["referenceDir"] = new_rel
    config["manifestFile"] = (Path(new_rel) / MANIFEST_NAME).as_posix()
    config["sync"] = default_sync_config()
    save_project_config(root, config)

    client = ZoteroClient()
    client.bootstrap()
    sync = sync_project(client, root, config, prune=False)

    removed: list[str] = []
    if not args.keep_old:
        old_ref = (root / old_rel).resolve()
        for row in old_manifest.get("papers", []) or []:
            if not isinstance(row, dict) or not row.get("projectPath"):
                continue
            candidate = (root / str(row["projectPath"])).resolve()
            if not is_within(candidate, old_ref):
                continue
            if candidate.exists() and candidate.is_file():
                candidate.unlink()
                removed.append(relpath_for_manifest(candidate, root))
        if old_manifest_path.exists() and is_within(old_manifest_path, old_ref):
            try:
                old_manifest_path.unlink()
            except OSError:
                pass

    return {
        "changed": True,
        "projectRoot": str(root),
        "oldReferenceDir": old_rel,
        "referenceDir": new_rel,
        "removedOldManagedFiles": removed,
        "sync": {k: v for k, v in sync.items() if k != "papers"},
    }


def cmd_reconcile(args: argparse.Namespace) -> dict[str, Any]:
    root, config = load_project(requested_project_root(args))
    assert config is not None
    client = ZoteroClient()
    client.bootstrap()

    before = consistency_snapshot(client, root, config)
    removed_local_only: list[str] = []
    if args.remove_local_only:
        # Explicitly destructive: only delete PDFs that are not represented by the current manifest.
        for rel in before["drift"]["localOnlyFiles"]:
            candidate = (root / rel).resolve()
            ref_dir = (root / config["referenceDir"]).resolve()
            if is_within(candidate, ref_dir) and candidate.exists() and candidate.is_file():
                candidate.unlink()
                removed_local_only.append(rel)

    sync = sync_project(client, root, config, prune=args.prune)
    after = consistency_snapshot(client, root, config)
    if args.remove_local_only and after["drift"]["localOnlyFiles"]:
        # A manually replaced managed path can become local-only only after canonical
        # rematerialization. Honor the explicit destructive option in the same command.
        ref_dir = (root / config["referenceDir"]).resolve()
        for rel in list(after["drift"]["localOnlyFiles"]):
            if rel in removed_local_only:
                continue
            candidate = (root / rel).resolve()
            if is_within(candidate, ref_dir) and candidate.exists() and candidate.is_file():
                candidate.unlink()
                removed_local_only.append(rel)
        sync = sync_project(client, root, config, prune=args.prune)
        after = consistency_snapshot(client, root, config)
    after = persist_consistency_state(root, config, after, reset_policy=True)
    unresolved = after["drift"]["localOnlyFiles"]
    return {
        "ok": True,
        "projectRoot": str(root),
        "before": before,
        "after": after,
        "removedLocalOnlyFiles": removed_local_only,
        "unresolvedLocalOnlyFiles": unresolved,
        "hint": (
            "Local-only PDFs remain. To fully unify, import each wanted PDF into Zotero with import-pdf, or explicitly remove files the user does not want."
            if unresolved
            else "Reference workspace is unified with the Zotero project collection."
        ),
        "sync": {k: v for k, v in sync.items() if k != "papers"},
    }


def cmd_init(args: argparse.Namespace) -> dict[str, Any]:
    root = project_root(requested_project_root(args))
    existing_path = root / CONFIG_NAME
    if existing_path.exists() and not args.force:
        _, config = load_project(root)
        assert config is not None
        return {"initialized": True, "existing": True, "projectRoot": str(root), "config": config}

    candidates = detect_reference_dirs(root)
    onboarding = args.onboarding
    existing_dir = args.existing_dir
    if existing_dir:
        existing_dir = project_relative_path(existing_dir, label="existing reference directory")

    # Existing literature should never be silently ignored or flattened. Ask the Agent/user
    # to choose an onboarding strategy unless the caller already supplied one explicitly.
    if candidates and not onboarding and not args.reference_dir:
        raise ZPPError(
            "Existing reference/literature directory detected; choose how Zotero Project Papers should onboard it.",
            code="onboarding_required",
            hint="Choose use-existing, separate, or merge. Merge flattens PDFs into the new reference directory and preserves the source directory.",
            details={
                "candidates": candidates,
                "options": ["use-existing", "separate", "merge"],
                "defaultNewDirectory": DEFAULT_REFERENCE_DIR,
            },
        )

    if onboarding in {"use-existing", "merge"}:
        if not existing_dir:
            if len(candidates) == 1:
                existing_dir = str(candidates[0]["path"])
            else:
                raise ZPPError(
                    "Specify --existing-dir when more than one existing reference directory is possible.",
                    code="existing_reference_ambiguous",
                    details={"candidates": candidates},
                )
        source = root / existing_dir
        if not source.is_dir():
            raise ZPPError(f"Existing reference directory not found: {existing_dir}", code="existing_reference_missing")

    if onboarding == "use-existing":
        assert existing_dir is not None
        reference_dir = existing_dir
    else:
        reference_dir = project_relative_path(args.reference_dir or DEFAULT_REFERENCE_DIR, label="reference directory")

    if onboarding == "separate" and existing_dir and Path(reference_dir) == Path(existing_dir):
        raise ZPPError("Separate onboarding requires a different --reference-dir from --existing-dir.", code="invalid_onboarding")
    if onboarding == "merge" and existing_dir and Path(reference_dir) == Path(existing_dir):
        raise ZPPError("Merge onboarding requires a new --reference-dir different from the existing directory.", code="invalid_onboarding")

    project_name = args.name or root.name
    client = ZoteroClient()
    client.bootstrap()
    collection_key = ensure_collection_path(client, [args.collection_root, project_name])
    manifest_file = (Path(reference_dir) / MANIFEST_NAME).as_posix()
    bib_file = project_relative_path(args.bib_file or DEFAULT_BIB_FILE, label="BibTeX output")
    config = {
        "schemaVersion": SCHEMA_VERSION,
        "collectionPath": f"{args.collection_root}/{project_name}",
        "collectionKey": collection_key,
        "referenceDir": Path(reference_dir).as_posix(),
        "manifestFile": manifest_file,
        "bibFile": bib_file,
        "fallback": args.fallback,
        "zoteroServerID": client.server_id,
        "sync": default_sync_config(),
    }
    (root / reference_dir).mkdir(parents=True, exist_ok=True)
    save_project_config(root, config)

    merged: list[dict[str, str]] = []
    if onboarding == "merge" and existing_dir:
        merged = flatten_reference_pdfs(root / existing_dir, root / reference_dir, args.fallback)

    result = compact_sync_result(sync_project(client, root, config, prune=False), include_papers=False)
    result.update({
        "initialized": True,
        "existing": False,
        "config": config,
        "onboarding": onboarding or "new",
        "detectedReferenceDirectories": candidates,
        "mergedLocalPdfs": merged,
    })
    return result


def cmd_search(args: argparse.Namespace) -> dict[str, Any]:
    started = perf_counter()
    root, config = load_project(requested_project_root(args), required=args.scope == "project")

    # Fastest path: current-project manifest. No Zotero process/API call at all.
    if args.scope == "project" and config is not None and not args.fulltext:
        local_hits = search_manifest(root, config, args.query, args.limit)
        if local_hits and float(local_hits[0].get("score") or 0) >= 0.55:
            return {
                "scope": args.scope,
                "query": args.query,
                "count": len(local_hits),
                "source": "project-manifest",
                "zoteroContacted": False,
                "webNeeded": False,
                "latencyMs": round((perf_counter() - started) * 1000, 2),
                "items": local_hits,
                "warnings": [],
            }

    client = ZoteroClient()
    client.bootstrap()
    if config is not None and not config.get("zoteroServerID"):
        config["zoteroServerID"] = client.server_id
        save_project_config(root, config)
    assert client.server_id is not None

    cache_info = cache_status(client.server_id)
    cache_error: str | None = None
    cache_available = bool(cache_info.get("exists"))

    # A prebuilt cache is refreshed incrementally. Do NOT build the entire library on the
    # first search: first-use latency must stay lower than a web search. Full cache warmup
    # is an explicit `cache --refresh` operation.
    if not args.fulltext and cache_available:
        try:
            refresh_info = refresh_metadata_cache(
                client,
                force=args.refresh_cache,
                max_age=0 if args.refresh_cache else CACHE_TTL_SECONDS,
            )
            cache_info = {**cache_status(client.server_id), **refresh_info}
            collection_key = config["collectionKey"] if args.scope == "project" and config else None
            cached = search_metadata_cache(
                client.server_id,
                args.query,
                args.limit,
                collection_key=collection_key,
                verbose=args.verbose,
            )
            if cached and float(cached[0].get("score") or 0) >= 0.55:
                return {
                    "scope": args.scope, "query": args.query, "count": len(cached),
                    "source": "zotero-metadata-cache", "searchMode": "cached-metadata",
                    "zoteroContacted": True, "webNeeded": False, "cache": cache_info,
                    "latencyMs": round((perf_counter() - started) * 1000, 2),
                    "items": cached, "warnings": [],
                }
        except ZPPError as exc:
            cache_error = str(exc)

    def run_zotero(fulltext: bool) -> list[dict[str, Any]]:
        if args.scope == "project":
            assert config is not None
            return collection_items(client, config["collectionKey"], args.query, fulltext)
        return library_search(client, args.query, fulltext, args.limit)

    # When there is no strong cache hit, direct Local API metadata search is the next
    # fast path. "Non-empty" is not enough: weak/irrelevant rows must not suppress the
    # indexed full-text fallback.
    if args.fulltext:
        rows = run_zotero(True)
        summaries = rank_zotero_rows(rows, args.query, fulltext=True, limit=args.limit, verbose=args.verbose)
        source = "zotero-fulltext"
        used_fulltext = True
    else:
        metadata_rows = run_zotero(False)
        metadata_summaries = rank_zotero_rows(metadata_rows, args.query, fulltext=False, limit=args.limit, verbose=args.verbose)
        metadata_strong = bool(metadata_summaries) and float(metadata_summaries[0].get("score") or 0) >= 0.55
        if metadata_strong or args.no_fulltext_fallback:
            rows = metadata_rows
            summaries = metadata_summaries
            source = "zotero-direct-metadata"
            used_fulltext = False
        else:
            rows = run_zotero(True)
            summaries = rank_zotero_rows(rows, args.query, fulltext=True, limit=args.limit, verbose=args.verbose)
            source = "zotero-fulltext"
            used_fulltext = True

    strong = bool(summaries) and float(summaries[0].get("score") or 0) >= 0.55
    result = {
        "scope": args.scope, "query": args.query, "count": len(summaries), "source": source,
        "searchMode": "everything" if used_fulltext else "titleCreatorYear",
        "zoteroContacted": True, "webNeeded": not strong, "cache": cache_info, "cacheError": cache_error,
        "latencyMs": round((perf_counter() - started) * 1000, 2), "items": summaries,
        "warnings": duplicate_warnings(rows),
    }
    if not summaries:
        result["zeroResultNote"] = (
            "No indexed local match was found. For topic/concept queries, this is not proof that the Zotero library contains no relevant paper; "
            "treat it as a local-search miss and continue the retrieval ladder. Exact DOI/arXiv/title checks are stronger absence signals."
        )
    return result

def cmd_cache(args: argparse.Namespace) -> dict[str, Any]:
    root, config = load_project(requested_project_root(args), required=False)
    server_id = config.get("zoteroServerID") if config else None
    if not args.refresh and server_id:
        return {"ok": True, "serverID": server_id, "cache": cache_status(str(server_id)), "zoteroContacted": False}
    client = ZoteroClient()
    client.bootstrap()
    assert client.server_id is not None
    if config is not None and config.get("zoteroServerID") != client.server_id:
        config["zoteroServerID"] = client.server_id
        save_project_config(root, config)
    info = refresh_metadata_cache(client, force=bool(args.refresh), max_age=0 if args.refresh else CACHE_TTL_SECONDS)
    return {"ok": True, "serverID": client.server_id, "cache": {**cache_status(client.server_id), **info}, "zoteroContacted": True}

def cmd_show(args: argparse.Namespace) -> dict[str, Any]:
    client = ZoteroClient()
    client.bootstrap()
    obj = get_item(client, args.item_key)
    return {"item": item_summary(obj, brief=False), "attachments": pdf_attachments(client, args.item_key)}


def cmd_resolve(args: argparse.Namespace) -> dict[str, Any]:
    root, config = load_project(requested_project_root(args), required=False)
    client = ZoteroClient()
    client.bootstrap()
    obj = get_item(client, args.item_key)
    summary = item_summary(obj)
    attachments = pdf_attachments(client, args.item_key)
    project_path = None
    if config:
        old = load_old_manifest(root, config)
        project_path = manifest_prior_paths(old).get(args.item_key)
    return {"item": summary, "attachments": attachments, "projectPath": project_path, "projectRoot": str(root)}


def cmd_add(args: argparse.Namespace) -> dict[str, Any]:
    root, config = load_project(requested_project_root(args))
    assert config is not None
    client = ZoteroClient()
    client.bootstrap()
    changed = add_item_to_collection(client, args.item_key, config["collectionKey"])
    sync = sync_project(client, root, config, prune=False)
    record = next((p for p in sync["papers"] if p.get("itemKey") == args.item_key), None)
    return {"addedToCollection": changed, "itemKey": args.item_key, "paper": record, "sync": {k: v for k, v in sync.items() if k != "papers"}}


def cmd_sync(args: argparse.Namespace) -> dict[str, Any]:
    root, config = load_project(requested_project_root(args))
    assert config is not None
    client = ZoteroClient()
    client.bootstrap()
    return compact_sync_result(sync_project(client, root, config, prune=args.prune), include_papers=bool(getattr(args, "include_papers", False)))


def cmd_fulltext(args: argparse.Namespace) -> dict[str, Any] | str:
    client = ZoteroClient()
    client.bootstrap()
    obj = get_item(client, args.item_key)
    d = item_data(obj)
    target_key = args.item_key
    if d.get("itemType") != "attachment":
        attachments = pdf_attachments(client, args.item_key)
        if not attachments:
            raise ZPPError(f"No PDF attachment found for {args.item_key}")
        target_key = attachments[0]["key"]
    full = client.read(f"users/0/items/{target_key}/fulltext")
    if not isinstance(full, dict):
        raise ZPPError(f"No indexed full text found for {target_key}")
    content = str(full.get("content") or "")
    if args.max_chars and len(content) > args.max_chars:
        content = content[: args.max_chars] + "\n...[truncated]"
    if args.json:
        return {
            "itemKey": args.item_key,
            "attachmentKey": target_key,
            "indexedPages": full.get("indexedPages"),
            "totalPages": full.get("totalPages"),
            "content": content,
        }
    return content



def paper_evidence_status(client: ZoteroClient, item_key: str) -> dict[str, Any]:
    obj = get_item(client, item_key)
    summary = item_summary(obj, brief=True)
    attachments = pdf_attachments(client, item_key)
    pdf_rows: list[dict[str, Any]] = []
    indexed_any = False
    for att in attachments:
        key = str(att.get("key") or "")
        path = str(att.get("path") or "")
        exists = bool(path and Path(path).exists())
        fulltext_info: dict[str, Any] | None = None
        if key:
            try:
                full = client.read(f"users/0/items/{key}/fulltext")
                if isinstance(full, dict):
                    indexed = bool(str(full.get("content") or "")) or bool(full.get("indexedPages"))
                    indexed_any = indexed_any or indexed
                    fulltext_info = {
                        "indexed": indexed,
                        "indexedPages": full.get("indexedPages"),
                        "totalPages": full.get("totalPages"),
                    }
            except ZPPError:
                fulltext_info = {"indexed": False}
        pdf_rows.append({
            "attachmentKey": key or None,
            "filename": att.get("filename"),
            "pathAvailable": exists,
            "linkMode": att.get("linkMode"),
            "fulltext": fulltext_info or {"indexed": False},
        })
    pdf_present = any(bool(x.get("pathAvailable")) for x in pdf_rows)
    if indexed_any:
        availability = "indexed-fulltext"
        recommended_layer = "original-paper"
    elif pdf_present:
        availability = "pdf-present-unindexed"
        recommended_layer = "original-paper"
    else:
        availability = "metadata-only"
        recommended_layer = "metadata"
    return {
        "itemKey": item_key,
        "item": summary,
        "sourceAvailability": availability,
        "recommendedSourceLayer": recommended_layer,
        "pdfPresent": pdf_present,
        "indexedFulltext": indexed_any,
        "attachments": pdf_rows,
    }


def cmd_evidence(args: argparse.Namespace) -> dict[str, Any]:
    root, config = load_project(requested_project_root(args), required=False)
    client = ZoteroClient()
    client.bootstrap()
    out = paper_evidence_status(client, args.item_key)
    out["projectRoot"] = str(root)
    if config:
        paper_index = project_paper_index(root, config)
        out["inCurrentProject"] = args.item_key in paper_index
        project_row = paper_index.get(args.item_key)
        out["projectPaper"] = ({
            "itemKey": project_row.get("itemKey"),
            "title": project_row.get("title"),
            "pdfStatus": project_row.get("pdfStatus"),
            "projectPath": project_row.get("projectPath"),
            "attachmentKey": project_row.get("attachmentKey"),
        } if project_row else None)
        out["claims"] = claims_for_paper(root, config, args.item_key)
        out["claimsFile"] = str(config.get("claimsFile") or CLAIMS_FILE)
    else:
        out["inCurrentProject"] = None
        out["claims"] = []
    return out


def cmd_record_claim(args: argparse.Namespace) -> dict[str, Any]:
    root, config = load_project(requested_project_root(args))
    assert config is not None
    return record_claim_local(
        root,
        config,
        claim=args.claim,
        claim_type=args.type,
        papers=list(args.paper or []),
        source_layer=args.source_layer,
        locator=claim_locator_from_args(args),
        source_ref=args.source_ref,
        note=args.note,
    )


def cmd_audit(args: argparse.Namespace) -> dict[str, Any]:
    root, config = load_project(requested_project_root(args))
    assert config is not None
    out = audit_claims_local(root, config, include_claims=bool(args.include_claims))
    out["projectRoot"] = str(root)
    if not args.check_sources:
        out["zoteroContacted"] = False
        return out

    ledger = load_claims(root, config)
    keys = sorted({
        str(key)
        for row in (ledger.get("claims") or [])
        if isinstance(row, dict)
        for key in (row.get("papers") or [])
        if str(key)
    })
    client = ZoteroClient()
    client.bootstrap()
    checks: dict[str, Any] = {}
    missing: list[str] = []
    for key in keys:
        try:
            checks[key] = paper_evidence_status(client, key)
        except ZPPError as exc:
            checks[key] = {"itemKey": key, "error": exc.as_dict()}
            missing.append(key)
    out["zoteroContacted"] = True
    out["sourceChecks"] = checks
    out["unavailableSourceItemKeys"] = missing
    original_source_unavailable: list[str] = []
    for row in (ledger.get("claims") or []):
        if not isinstance(row, dict) or row.get("sourceLayer") != "original-paper":
            continue
        papers = [str(x) for x in (row.get("papers") or [])]
        if any(
            isinstance(checks.get(key), dict)
            and checks[key].get("sourceAvailability") == "metadata-only"
            for key in papers
        ):
            original_source_unavailable.append(str(row.get("id") or ""))
    out["originalSourceUnavailableClaimIds"] = [x for x in original_source_unavailable if x]
    if missing:
        out["verdict"] = "broken"
    elif original_source_unavailable and out.get("verdict") == "clean":
        out["verdict"] = "needs-review"
    return out


def cmd_import_metadata(args: argparse.Namespace) -> dict[str, Any]:
    root, config = load_project(requested_project_root(args))
    assert config is not None
    metadata = load_metadata_file(args.metadata)
    client = ZoteroClient()
    client.bootstrap()
    result = import_metadata_record(
        client, root, config, metadata,
        update_existing=bool(getattr(args, "update_existing_metadata", False)),
    )
    result["metadataSource"] = "file"
    return result


def cmd_attach_pdf(args: argparse.Namespace) -> dict[str, Any]:
    root, config = load_project(requested_project_root(args))
    assert config is not None
    pdf = Path(args.pdf).expanduser().resolve()
    if not pdf.is_file() or pdf.suffix.casefold() != ".pdf":
        raise ZPPError(f"PDF not found or not a .pdf file: {pdf}", code="pdf_missing")
    validate_pdf_file(pdf)
    client = ZoteroClient()
    client.bootstrap()
    return attach_pdf_to_item(client, root, config, args.item_key, pdf, allow_multiple=bool(args.allow_multiple))


def cmd_import_doi(args: argparse.Namespace) -> dict[str, Any]:
    root, config = load_project(requested_project_root(args))
    assert config is not None
    looked_up = crossref_metadata(args.doi)
    client = ZoteroClient()
    client.bootstrap()
    result = import_metadata_record(
        client, root, config, looked_up["metadata"],
        update_existing=bool(getattr(args, "update_existing_metadata", False)),
    )
    result.update({"metadataSource": "crossref", "DOI": looked_up["doi"], "pdfCandidates": looked_up["links"][:5]})
    return result


def cmd_enrich(args: argparse.Namespace) -> dict[str, Any]:
    client = ZoteroClient()
    client.bootstrap()
    obj = get_item(client, args.item_key)
    existing = item_data(obj)
    doi = normalize_doi(str(existing.get("DOI") or ""))
    if not doi:
        raise ZPPError(
            f"Item {args.item_key} has no DOI; automatic Crossref enrichment is unavailable.",
            code="doi_missing",
            hint="Add/verify the DOI first, or update metadata manually from an authoritative source.",
        )
    looked_up = crossref_metadata(doi)
    incoming = looked_up["metadata"]
    diff = metadata_diff(obj, incoming)
    result: dict[str, Any] = {
        "itemKey": args.item_key, "DOI": doi, "source": "crossref", "metadataDiff": diff,
        "applied": False, "pdfCandidates": looked_up["links"][:5],
    }
    if args.apply and diff:
        update = update_existing_metadata(client, args.item_key, incoming)
        result["applied"] = bool(update.get("updated"))
        result["metadataUpdate"] = update
    return result


def cmd_fetch(args: argparse.Namespace) -> dict[str, Any]:
    if not args.output and not args.import_to_zotero:
        raise ZPPError("fetch requires --output unless --import is used.", code="fetch_output_required")
    metadata = load_metadata_file(args.metadata) if args.metadata else None
    if args.import_to_zotero and metadata is None:
        raise ZPPError("fetch --import requires --metadata.", code="metadata_required")
    cleanup = False
    if args.output:
        output = Path(args.output).expanduser().resolve()
    else:
        output = Path(tempfile.gettempdir()) / f"zpp-{uuid.uuid4().hex}.pdf"
        cleanup = True
    download = download_pdf(args.url, output, allow_untrusted=bool(args.allow_untrusted))
    result: dict[str, Any] = {"action": "fetched", **download}
    if args.import_to_zotero:
        ns = argparse.Namespace(
            project_root=getattr(args, "project_root", None), pdf=str(output), metadata=args.metadata,
            update_existing_metadata=bool(args.update_existing_metadata), json=True,
        )
        result["zotero"] = cmd_import_pdf(ns)
        result["action"] = "fetched-and-imported"
    if cleanup:
        try:
            if output.exists(): output.unlink()
        except OSError:
            pass
        result["temporaryFileRemoved"] = True
    return result


def cmd_tag(args: argparse.Namespace) -> dict[str, Any]:
    client = ZoteroClient()
    client.bootstrap()
    obj = get_item(client, args.item_key)
    d = item_data(obj)
    tags = [x for x in (d.get("tags") or []) if isinstance(x, dict) and x.get("tag")]
    existing = {str(x.get("tag")) for x in tags}
    changed = False
    if args.remove:
        new_tags = [x for x in tags if str(x.get("tag")) != args.tag]
        changed = len(new_tags) != len(tags)
    else:
        new_tags = list(tags)
        if args.tag not in existing:
            new_tags.append({"tag": args.tag})
            changed = True
    if changed:
        client.write(
            "PATCH", f"users/0/items/{args.item_key}", json_body={"tags": new_tags},
            extra_headers={"If-Unmodified-Since-Version": str(d.get("version", 0))},
        )
    return {"itemKey": args.item_key, "tag": args.tag, "removed": bool(args.remove), "changed": changed}


def cmd_organize(args: argparse.Namespace) -> dict[str, Any]:
    """Create project child collections from a small JSON topic map.

    Format: {"topics": {"Topic A": ["ITEMKEY1", "ITEMKEY2"]}}
    """
    root, config = load_project(requested_project_root(args))
    assert config is not None
    path = Path(args.map).expanduser().resolve()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ZPPError(f"Cannot read topic map {path}: {exc}", code="topic_map_invalid") from exc
    topics = data.get("topics") if isinstance(data, dict) else None
    if not isinstance(topics, dict):
        raise ZPPError('Topic map must be JSON like {"topics": {"Topic": ["ITEMKEY"]}}.', code="topic_map_invalid")
    client = ZoteroClient()
    client.bootstrap()
    created: dict[str, Any] = {}
    for topic, keys in topics.items():
        if not isinstance(topic, str) or not isinstance(keys, list):
            continue
        found = find_collection(client, topic, config["collectionKey"])
        child_key = str(found.get("key")) if found else client.create_collection(topic, config["collectionKey"])
        added = []
        for key in keys:
            key = str(key)
            # Keep every topic paper in the main project collection as well.
            add_item_to_collection(client, key, config["collectionKey"])
            if add_item_to_collection(client, key, child_key):
                added.append(key)
        created[topic] = {"collectionKey": child_key, "itemCount": len(keys), "newlyAdded": added}
    topic_out = root / "papers" / "topics.json"
    json_dump({"generatedAt": utc_now(), "topics": created}, topic_out)
    return {"ok": True, "projectRoot": str(root), "topicIndex": relpath_for_manifest(topic_out, root), "topics": created}


def cmd_import_pdf(args: argparse.Namespace) -> dict[str, Any]:
    root, config = load_project(requested_project_root(args))
    assert config is not None
    pdf = Path(args.pdf).expanduser().resolve()
    if not pdf.exists() or not pdf.is_file():
        raise ZPPError(f"PDF not found: {pdf}")
    if pdf.suffix.casefold() != ".pdf":
        raise ZPPError("import-pdf accepts .pdf files only")
    validate_pdf_file(pdf)
    metadata = load_metadata_file(args.metadata)

    client = ZoteroClient()
    client.bootstrap()
    reference_dir = (root / config["referenceDir"]).resolve()
    input_is_local_reference = is_within(pdf, reference_dir)
    duplicates = find_duplicates(client, metadata)
    if duplicates:
        duplicate = duplicates[0]
        key = item_summary(duplicate, brief=True)["key"]
        duplicate_metadata_diff = metadata_diff(duplicate, metadata)
        metadata_update = None
        if getattr(args, "update_existing_metadata", False) and duplicate_metadata_diff:
            metadata_update = update_existing_metadata(client, key, metadata)
        changed = add_item_to_collection(client, key, config["collectionKey"])
        absorbed = False
        available = [a for a in pdf_attachments(client, key) if a.get("path")]
        if input_is_local_reference and available and pdf.exists():
            # Zotero already owns a recoverable PDF. Remove the local-only copy/link so sync can
            # rematerialize the canonical attachment at the project path.
            pdf.unlink()
            absorbed = True
        sync = sync_project(client, root, config, prune=False)
        record = next((p for p in sync["papers"] if p.get("itemKey") == key), None)
        response = {
            "action": "reused-existing",
            "itemKey": key,
            "addedToCollection": changed,
            "absorbedLocalReference": absorbed,
            "paper": record,
            "metadataDiff": duplicate_metadata_diff,
            "metadataUpdate": metadata_update,
            "note": "A local duplicate was found; no new Zotero item or PDF was created.",
        }
        if duplicate_metadata_diff and not getattr(args, "update_existing_metadata", False):
            response.setdefault("warnings", []).append({
                "type": "existing-metadata-diff",
                "message": "The existing Zotero item differs from the incoming metadata. Review metadataDiff; rerun with --update-existing-metadata only if you want the skill to overwrite matching-type fields.",
            })
        if len(duplicates) > 1:
            response.setdefault("warnings", []).append({
                "type": "multiple-local-duplicates",
                "message": "Multiple Zotero items match this DOI/title. One existing item was reused; consider cleaning duplicates in Zotero.",
                "itemKeys": [item_summary(x, brief=True)["key"] for x in duplicates],
            })
        return response

    # Multi-write path: require a remembered key before mutating anything.
    client.ensure_write_auth(require_remembered=True)
    item = build_item_from_metadata(client, metadata, config["collectionKey"])
    item_key = client.create_item(item)
    attachment_key: str | None = None
    try:
        attachment_key, attachment_filename = create_imported_attachment(client, item_key, pdf, metadata)
        upload_attachment_file(client, attachment_key, pdf, attachment_filename)
    except Exception as exc:
        raise ZPPError(
            f"Partial import: Zotero bibliographic item {item_key}"
            + (f" and attachment {attachment_key}" if attachment_key else "")
            + f" were created, but PDF upload did not complete: {exc}. Search Zotero before retrying."
        ) from exc

    absorbed = False
    if input_is_local_reference and pdf.exists():
        # Upload is complete and Zotero now owns the canonical stored attachment. Remove the
        # pre-existing local-only PDF; sync will recreate it as a managed hardlink/copy view.
        pdf.unlink()
        absorbed = True
    sync = sync_project(client, root, config, prune=False)
    record = next((p for p in sync["papers"] if p.get("itemKey") == item_key), None)
    return {
        "action": "imported",
        "itemKey": item_key,
        "attachmentKey": attachment_key,
        "attachmentFilename": attachment_filename,
        "absorbedLocalReference": absorbed,
        "paper": record,
    }


def print_result(result: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif isinstance(result, str):
        print(result)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="zotero_papers.py", description="Project-oriented Zotero 10 helper for coding agents")
    p.add_argument("--version", action="version", version="zotero-project-papers 0.5.2")
    p.add_argument("--project-root", help="Explicit project root; otherwise discover from the current working directory")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("doctor", help="Preflight Zotero, authorization, and project-root checks")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("status", help="Check Zotero Local API and project binding")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("check", help="Cheap consistency preflight; contacts Zotero only when local/cache markers changed")
    s.add_argument("--ack-continue", action="store_true", help="Remember the current drift fingerprint and continue without prompting until it changes")
    s.add_argument("--reset-policy", action="store_true", help="Forget any remembered continue decision and prompt again while drift remains")
    s.add_argument("--full", action="store_true", help="Force a full Zotero/manifest/reference consistency comparison")
    s.add_argument("--verbose", action="store_true", help="Include file-level quick-marker details")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("authorize", help="Ask Zotero 10 for local write permission")
    s.add_argument("--allow-once", action="store_true", help="Accept one-shot authorization (not enough for PDF imports)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_authorize)

    s = sub.add_parser("init", help="Bind the current project to Projects/<repo-name> in Zotero")
    s.add_argument("--name", help="Project/collection name (default: repository directory name)")
    s.add_argument("--collection-root", default=DEFAULT_COLLECTION_ROOT)
    s.add_argument("--reference-dir", help=f"Project PDF working directory (default: {DEFAULT_REFERENCE_DIR})")
    s.add_argument("--existing-dir", help="Existing reference/literature directory selected during onboarding")
    s.add_argument("--onboarding", choices=["use-existing", "separate", "merge"], help="How to handle a detected existing literature directory")
    s.add_argument("--bib-file", help=f"BibTeX output path (default: {DEFAULT_BIB_FILE})")
    s.add_argument("--fallback", choices=["copy", "symlink", "none"], default="copy")
    s.add_argument("--force", action="store_true", help="Reinitialize project config")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("set-reference-dir", help="Change the project reference directory and rematerialize managed PDFs")
    s.add_argument("reference_dir")
    s.add_argument("--allow-existing-target", action="store_true", help="Allow a target directory that already contains PDFs")
    s.add_argument("--keep-old", action="store_true", help="Keep old helper-managed PDF links/copies instead of cleaning them")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_set_reference_dir)

    s = sub.add_parser("search", help="Search project collection or full local Zotero library")
    s.add_argument("query")
    s.add_argument("--scope", choices=["project", "library"], default="project")
    s.add_argument("--fulltext", action="store_true", help="Start directly with Zotero quicksearch 'everything' mode")
    s.add_argument("--no-fulltext-fallback", action="store_true", help="Do not retry an empty metadata search with full-text/everything search")
    s.add_argument("--verbose", action="store_true", help="Include abstract/tags for cached results; use show for one-paper details")
    s.add_argument("--refresh-cache", action="store_true", help="Force an incremental Zotero metadata-cache refresh before searching")
    s.add_argument("--limit", type=int, default=DEFAULT_SEARCH_LIMIT)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("cache", help="Inspect or refresh the local Zotero metadata cache")
    s.add_argument("--refresh", action="store_true", help="Refresh now; first refresh builds the cache, later refreshes use Zotero ?since= versions")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_cache)

    s = sub.add_parser("show", help="Show full metadata and attachments for one Zotero item")
    s.add_argument("item_key")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("resolve", help="Resolve a Zotero item to local PDF attachment path(s)")
    s.add_argument("item_key")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_resolve)

    s = sub.add_parser("add", help="Add an existing Zotero item to the current project and materialize its PDF")
    s.add_argument("item_key")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("sync", help="Materialize current project collection and regenerate manifest/BibTeX")
    s.add_argument("--prune", action="store_true", help="Remove stale helper-managed project files only")
    s.add_argument("--include-papers", action="store_true", help="Include the full paper array; default JSON is a compact sync summary")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("reconcile", help="Unify the project view toward the Zotero collection without deleting Zotero items")
    s.add_argument("--prune", action="store_true", help="Remove stale helper-managed project files")
    s.add_argument("--remove-local-only", action="store_true", help="Explicitly delete local-only PDFs instead of importing/keeping them")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_reconcile)

    s = sub.add_parser("fulltext", help="Return Zotero-indexed full text for an item/PDF attachment")
    s.add_argument("item_key")
    s.add_argument("--max-chars", type=int, default=100000)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_fulltext)

    s = sub.add_parser("evidence", help="Report whether a project paper is backed by Zotero metadata, a PDF, or indexed original full text")
    s.add_argument("item_key")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_evidence)

    s = sub.add_parser("record-claim", help="Record a project claim with explicit provenance in papers/claims.json")
    s.add_argument("--claim", required=True, help="The scientific/technical claim being used in the project")
    s.add_argument("--type", required=True, choices=["primary", "summary", "inference", "hypothesis"], help="Whether this is a direct paper claim, AI summary, cross-paper inference, or project hypothesis")
    s.add_argument("--paper", action="append", default=[], help="Supporting Zotero item key; repeat for multiple papers")
    s.add_argument("--source-layer", choices=["original-paper", "ai-summary", "agent-inference", "metadata"], help="Override the default provenance layer inferred from --type")
    s.add_argument("--page", help="Page or page range in the source paper")
    s.add_argument("--section", help="Section name/number in the source paper")
    s.add_argument("--table", help="Table identifier in the source paper")
    s.add_argument("--figure", help="Figure identifier in the source paper")
    s.add_argument("--source-ref", help="Optional project-local summary/note reference used to derive the claim")
    s.add_argument("--note", help="Optional audit note; do not use this as a substitute for a source locator")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_record_claim)

    s = sub.add_parser("audit", help="Audit claim provenance and citation traceability without pretending to semantically verify claims")
    s.add_argument("--check-sources", action="store_true", help="Also contact Zotero and verify that referenced items/PDF/fulltext are available")
    s.add_argument("--include-claims", action="store_true", help="Include individual claim text/details; default output is a compact summary")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_audit)

    s = sub.add_parser("import-metadata", help="Create/reuse a Zotero bibliographic item without requiring a PDF")
    s.add_argument("metadata", help="UTF-8 Zotero-style metadata JSON; title is required")
    s.add_argument("--update-existing-metadata", action="store_true", help="Update non-empty fields only when an existing duplicate has the same item type")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_import_metadata)

    s = sub.add_parser("attach-pdf", help="Attach a validated local PDF to an existing Zotero bibliographic item")
    s.add_argument("item_key")
    s.add_argument("pdf")
    s.add_argument("--allow-multiple", action="store_true", help="Allow another PDF even when the item already has a stored PDF")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_attach_pdf)

    s = sub.add_parser("import-doi", help="Fetch Crossref metadata for a DOI and create/reuse a metadata-only project item")
    s.add_argument("doi")
    s.add_argument("--update-existing-metadata", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_import_doi)

    s = sub.add_parser("enrich", help="Preview or apply DOI-based Crossref metadata enrichment for an existing item")
    s.add_argument("item_key")
    s.add_argument("--apply", action="store_true", help="Apply non-empty fields when item type is unchanged")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_enrich)

    s = sub.add_parser("fetch", help="Download and lightweight-validate a PDF from a known academic HTTPS source")
    s.add_argument("url")
    s.add_argument("--output", help="Destination PDF; optional when --import is used")
    s.add_argument("--metadata", help="Metadata JSON; required for --import")
    s.add_argument("--import", dest="import_to_zotero", action="store_true", help="After validation, import the PDF into Zotero/current project")
    s.add_argument("--allow-untrusted", action="store_true", help="Allow a host outside the built-in academic-host allowlist after explicit source verification")
    s.add_argument("--update-existing-metadata", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_fetch)

    s = sub.add_parser("tag", help="Add or remove one Zotero tag on a paper")
    s.add_argument("item_key")
    s.add_argument("tag")
    s.add_argument("--remove", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_tag)

    s = sub.add_parser("organize", help="Create project child collections from a JSON topic map")
    s.add_argument("map", help='JSON file: {"topics": {"Topic": ["ITEMKEY", ...]}}')
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_organize)

    s = sub.add_parser("import-pdf", help="Import a selected PDF + metadata JSON into Zotero and current project")
    s.add_argument("pdf")
    s.add_argument("--metadata", required=True, help="UTF-8 JSON with Zotero item metadata; title is required")
    s.add_argument("--update-existing-metadata", action="store_true", help="If a duplicate item is reused, overwrite non-empty incoming metadata fields only when the item type already matches")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_import_pdf)

    return p


def main(argv: list[str] | None = None) -> int:
    configure_stdio_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)
    as_json = bool(getattr(args, "json", False))
    try:
        result = args.func(args)
        print_result(result, as_json)
        return 0
    except KeyboardInterrupt:
        err = ZPPError("Interrupted by user.", code="interrupted")
        if as_json:
            print(json.dumps({"ok": False, "error": err.as_dict()}, ensure_ascii=False, indent=2))
        else:
            print(f"error: {err}", file=sys.stderr)
        return 130
    except Exception as exc:
        if isinstance(exc, ZPPError):
            err = exc
        elif isinstance(exc, (TimeoutError, socket.timeout)):
            err = ZPPError("Operation timed out.", code="timeout", hint="Check Zotero for a pending authorization dialog and retry.")
        else:
            err = ZPPError(
                str(exc) or exc.__class__.__name__,
                code="unexpected_error",
                hint="Run again with ZPP_DEBUG=1 if a developer traceback is needed.",
            )
        if os.environ.get("ZPP_DEBUG") == "1":
            import traceback
            traceback.print_exc(file=sys.stderr)
        if as_json:
            # Keep stdout machine-readable even on failure.
            print(json.dumps({"ok": False, "error": err.as_dict()}, ensure_ascii=False, indent=2))
        else:
            print(f"error: {err}", file=sys.stderr)
            if err.hint:
                print(f"hint: {err.hint}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
