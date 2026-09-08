#!/usr/bin/env python3
"""zotero-project-papers v0.2.1

Stdlib-only helper for an Agent Skill that binds a local project to a Zotero 10
collection and materializes Zotero-managed PDFs into the project reference dir.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import shutil
import socket
import stat
import sys
import tempfile
import time
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
SCHEMA_VERSION = 2
DEFAULT_COLLECTION_ROOT = "Projects"
DEFAULT_REFERENCE_DIR = "papers/reference"
DEFAULT_BIB_FILE = "papers/references.bib"
MANIFEST_NAME = "papers.json"
REFERENCE_CANDIDATES = ("reference", "references", "refs", "literature", "literatures", "papers/reference", "papers/references")
AUTH_TIMEOUT = int(os.environ.get("ZPP_AUTH_TIMEOUT", "300"))

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


def auth_path() -> Path:
    return user_config_dir() / "auth.json"


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
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"[^\w\s]", "", value, flags=re.UNICODE)
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
    out.setdefault("fallback", "copy")
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


@dataclass
class HTTPResult:
    status: int
    headers: dict[str, str]
    body: bytes

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
                return HTTPResult(resp.status, dict(resp.headers.items()), resp.read())
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
                hint="Make sure Zotero 10+ is running and local application communication is enabled.",
            ) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise ZPPError(
                f"Timed out waiting for Zotero after {timeout} seconds.",
                code="timeout",
                hint="If Zotero is showing an authorization dialog, approve it there and retry.",
            ) from exc

    def bootstrap(self) -> None:
        result = self._raw_request("GET", "")
        self.server_id = result.headers.get("Zotero-Server-ID")
        self.api_version = result.headers.get("Zotero-API-Version")
        if not self.server_id:
            raise ZPPError("Running Zotero does not expose Zotero-Server-ID. Zotero 10+ is required for this skill.")
        store = load_auth_store()
        entry = store.get("servers", {}).get(self.server_id, {})
        self.api_key = entry.get("key")

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
            raise ZPPError("Zotero did not return a local write key.")
        self.api_key = key
        if remembered:
            store = load_auth_store()
            store.setdefault("servers", {})[self.server_id or ""] = {
                "key": key,
                "remember": True,
                "savedAt": utc_now(),
            }
            save_auth_store(store)
        if require_remembered and not remembered:
            raise ZPPError("This operation needs reusable authorization. Run `authorize` again and choose 'Always Allow' in Zotero.")
        return {"authorized": True, "remembered": remembered, "serverID": self.server_id}

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
        ctype = result.headers.get("Content-Type", "")
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
        "publicationTitle": d.get("publicationTitle", ""),
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
    params: dict[str, Any] = {"limit": 100}
    if query:
        params["q"] = query
        params["qmode"] = "everything" if fulltext else "titleCreatorYear"
    rows = client.read(f"users/0/collections/{collection_key}/items", params) or []
    out = []
    for obj in rows:
        d = item_data(obj)
        if d.get("itemType") not in {"attachment", "note", "annotation"}:
            out.append(obj)
    return out


def library_search(client: ZoteroClient, query: str, fulltext: bool = False, limit: int = 30) -> list[dict[str, Any]]:
    rows = client.read(
        "users/0/items",
        {"q": query, "qmode": "everything" if fulltext else "titleCreatorYear", "limit": min(limit, 100)},
    ) or []
    out = []
    for obj in rows:
        d = item_data(obj)
        if d.get("itemType") not in {"attachment", "note", "annotation"}:
            out.append(obj)
    return out


def find_duplicates(client: ZoteroClient, metadata: dict[str, Any]) -> list[dict[str, Any]]:
    matches: dict[str, dict[str, Any]] = {}
    doi = normalize_doi(str(metadata.get("DOI") or ""))
    if doi:
        for obj in library_search(client, doi, fulltext=True, limit=100):
            d = item_data(obj)
            if normalize_doi(str(d.get("DOI") or "")) == doi:
                key = str(item_summary(obj, brief=True).get("key") or "")
                if key:
                    matches[key] = obj
    title = str(metadata.get("title") or "").strip()
    if title:
        target = normalize_title(title)
        for obj in library_search(client, title, fulltext=False, limit=100):
            if normalize_title(str(item_data(obj).get("title") or "")) == target:
                key = str(item_summary(obj, brief=True).get("key") or "")
                if key:
                    matches[key] = obj
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
    return {
        "projectRoot": str(root),
        "collection": config["collectionPath"],
        "paperCount": len(papers),
        "withPdf": sum(1 for p in papers if p.get("projectPath")),
        "manifest": config["manifestFile"],
        "bib": config["bibFile"],
        "pruned": pruned,
        "pruneSkippedModified": prune_skipped_modified,
        "consistency": consistency,
        "papers": papers,
    }


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
    checks: list[dict[str, Any]] = []
    client = ZoteroClient()
    try:
        client.bootstrap()
        checks.append({"name": "zotero-local-api", "ok": True, "serverID": client.server_id, "apiVersion": client.api_version})
        checks.append({
            "name": "write-authorization",
            "ok": bool(client.api_key),
            "status": "authorized" if client.api_key else "not-authorized",
            "hint": None if client.api_key else "Run `authorize` when a write operation is needed, then choose Always Allow in Zotero.",
        })
        try:
            fields = client.read("itemTypeFields", {"itemType": "journalArticle"}) or []
            checks.append({
                "name": "item-schema",
                "ok": bool(fields),
                "strategy": "itemTypeFields" if fields else "builtin-fallback",
                "hint": None if fields else "Dynamic item fields were unavailable; common imports will use the built-in conservative schema.",
            })
        except ZPPError as schema_exc:
            checks.append({
                "name": "item-schema",
                "ok": True,
                "strategy": "builtin-fallback",
                "warning": schema_exc.as_dict(),
                "hint": "The skill can still import common paper types using its built-in conservative schema.",
            })
        try:
            client.read("items/new", {"itemType": "journalArticle"})
            checks.append({"name": "items-new-template", "ok": True, "supported": True, "required": False})
        except ZPPError as template_exc:
            checks.append({
                "name": "items-new-template",
                "ok": True,
                "supported": False,
                "required": False,
                "note": "This Zotero Local API does not expose /items/new. v0.2.1 does not depend on it.",
                "detail": template_exc.as_dict(),
            })
    except ZPPError as exc:
        checks.append({"name": "zotero-local-api", "ok": False, "error": exc.as_dict()})

    config_path = root / CONFIG_NAME
    project_check = {
        "name": "project-root",
        "ok": True,
        "path": str(root),
        "initialized": config_path.exists(),
        "hint": None if config_path.exists() else "Run `init` from the intended project root before project-scoped operations.",
    }
    if not config_path.exists():
        project_check["referenceCandidates"] = detect_reference_dirs(root)
    checks.append(project_check)
    executable = find_zotero_executable()
    checks.append({
        "name": "zotero-executable",
        "ok": bool(executable),
        "path": executable,
        "hint": None if executable else "Zotero executable was not found in common locations; this is informational if Zotero is already running.",
    })
    return {
        "ok": all(c.get("ok") for c in checks if c["name"] in {"zotero-local-api", "project-root"}),
        "projectRoot": str(root),
        "checks": checks,
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
        "projectRoot": str(root),
        "projectInitialized": bool(config),
        "project": config,
    }


def cmd_check(args: argparse.Namespace) -> dict[str, Any]:
    root, config = load_project(requested_project_root(args))
    assert config is not None
    client = ZoteroClient()
    client.bootstrap()
    snapshot = consistency_snapshot(client, root, config)
    snapshot = persist_consistency_state(
        root,
        config,
        snapshot,
        acknowledge_continue=args.ack_continue,
        reset_policy=args.reset_policy,
    )
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
        "sync": default_sync_config(),
    }
    (root / reference_dir).mkdir(parents=True, exist_ok=True)
    save_project_config(root, config)

    merged: list[dict[str, str]] = []
    if onboarding == "merge" and existing_dir:
        merged = flatten_reference_pdfs(root / existing_dir, root / reference_dir, args.fallback)

    result = sync_project(client, root, config, prune=False)
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
    root, config = load_project(requested_project_root(args), required=args.scope == "project")
    client = ZoteroClient()
    client.bootstrap()

    def run(fulltext: bool) -> list[dict[str, Any]]:
        if args.scope == "project":
            assert config is not None
            return collection_items(client, config["collectionKey"], args.query, fulltext)
        return library_search(client, args.query, fulltext, args.limit)

    used_fulltext = bool(args.fulltext)
    rows = run(used_fulltext)
    fallback_used = False
    if not rows and not used_fulltext and not args.no_fulltext_fallback:
        rows = run(True)
        used_fulltext = True
        fallback_used = True

    selected = rows[: args.limit]
    summaries = [item_summary(x, brief=not args.verbose) for x in selected]
    return {
        "scope": args.scope,
        "query": args.query,
        "count": len(summaries),
        "searchMode": "everything" if used_fulltext else "titleCreatorYear",
        "fulltextFallbackUsed": fallback_used,
        "items": summaries,
        "warnings": duplicate_warnings(selected),
    }


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
    return sync_project(client, root, config, prune=args.prune)


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


def cmd_import_pdf(args: argparse.Namespace) -> dict[str, Any]:
    root, config = load_project(requested_project_root(args))
    assert config is not None
    pdf = Path(args.pdf).expanduser().resolve()
    if not pdf.exists() or not pdf.is_file():
        raise ZPPError(f"PDF not found: {pdf}")
    if pdf.suffix.casefold() != ".pdf":
        raise ZPPError("import-pdf accepts .pdf files only")
    metadata_path = Path(args.metadata).expanduser().resolve()
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ZPPError(f"Cannot read metadata JSON {metadata_path}: {exc}") from exc
    if not isinstance(metadata, dict) or not str(metadata.get("title") or "").strip():
        raise ZPPError("Metadata JSON must be an object containing at least `title`")

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
    p.add_argument("--version", action="version", version="zotero-project-papers 0.2.1")
    p.add_argument("--project-root", help="Explicit project root; otherwise discover from the current working directory")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("doctor", help="Preflight Zotero, authorization, and project-root checks")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("status", help="Check Zotero Local API and project binding")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("check", help="Cheap consistency check for Zotero collection, manifest, and reference PDFs")
    s.add_argument("--ack-continue", action="store_true", help="Remember the current drift fingerprint and continue without prompting until it changes")
    s.add_argument("--reset-policy", action="store_true", help="Forget any remembered continue decision and prompt again while drift remains")
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
    s.add_argument("--verbose", action="store_true", help="Include abstract, tags, collections, and detailed creator metadata")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_search)

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
