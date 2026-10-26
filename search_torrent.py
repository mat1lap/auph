"""Validated v1 downloads and conservative, journaled qBittorrent submission."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit

from pipeline_config import SearchPolicy
from process_lock import file_lock
from search_models import Candidate, SearchError
from search_providers import HTTP, Download, HTTPError, endpoint


class Bencode:
    def __init__(self, data: bytes) -> None:
        self.data, self.position, self.nodes = data, 0, 0
        self.info_bytes = b""

    def read(self, depth: int = 0) -> Any:
        self.nodes += 1
        if depth > 20 or self.nodes > 100_000 or self.position >= len(self.data):
            raise ValueError("bencode bound")
        start = self.position
        token = self.data[start : start + 1]
        self.position += 1
        if token == b"i":
            end = self.data.index(b"e", self.position)
            number = self.data[self.position : end]
            if not re.fullmatch(rb"0|-?[1-9][0-9]{0,18}", number):
                raise ValueError("integer")
            self.position = end + 1
            return int(number)
        if token in {b"l", b"d"}:
            values: Any = [] if token == b"l" else {}
            previous = b""
            while self.data[self.position : self.position + 1] != b"e":
                key = self.read(depth + 1)
                if token == b"l":
                    values.append(key)
                    continue
                if not isinstance(key, bytes) or key in values or (values and key <= previous):
                    raise ValueError("dictionary keys")
                previous = key
                value_start = self.position
                values[key] = self.read(depth + 1)
                if depth == 0 and key == b"info":
                    self.info_bytes = self.data[value_start : self.position]
            self.position += 1
            return values
        self.position = start
        colon = self.data.index(b":", start)
        length = self.data[start:colon]
        if not re.fullmatch(rb"0|[1-9][0-9]{0,7}", length):
            raise ValueError("string length")
        self.position = colon + 1 + int(length)
        if self.position > len(self.data):
            raise ValueError("short string")
        return self.data[colon + 1 : self.position]


def component(value: object) -> bytes:
    if not isinstance(value, bytes) or not value or value in {b".", b".."} or re.search(rb"[/\\:\x00-\x1f\x7f]", value):
        raise ValueError("unsafe torrent path")
    return value


def validate_torrent(raw: bytes) -> Download:
    try:
        if not 1 <= len(raw) <= 8_000_000:
            raise ValueError("payload size")
        parser = Bencode(raw)
        root = parser.read()
        if parser.position != len(raw) or not isinstance(root, dict):
            raise ValueError("root")
        info = root[b"info"]
        if not isinstance(info, dict) or b"meta version" in info:
            raise ValueError("only v1 torrents supported")
        component(info[b"name"])
        if b"name.utf-8" in info:
            component(info[b"name.utf-8"])
        files = info.get(b"files")
        lengths = []
        paths: set[tuple[bytes, ...]] = set()
        directories: set[tuple[bytes, ...]] = set()
        if files is not None:
            if b"length" in info or not isinstance(files, list) or not 1 <= len(files) <= 10_000:
                raise ValueError("files")
            for file in files:
                if not isinstance(file, dict) or b"symlink path" in file or b"l" in file.get(b"attr", b""):
                    raise ValueError("links forbidden")
                path = file[b"path"]
                if not isinstance(path, list) or not path or len(path) > 20:
                    raise ValueError("path")
                normalized = tuple(component(part).lower() for part in path)
                if (
                    normalized in paths
                    or normalized in directories
                    or any(normalized[:length] in paths for length in range(1, len(normalized)))
                ):
                    raise ValueError("path collision")
                paths.add(normalized)
                directories.update(normalized[:length] for length in range(1, len(normalized)))
                if b"path.utf-8" in file:
                    # Avoid alternate names bypassing the same collision/safety checks.
                    if file[b"path.utf-8"] != path:
                        raise ValueError("ambiguous alternate path")
                lengths.append(file[b"length"])
        else:
            lengths.append(info[b"length"])
        if any(type(length) is not int or length < 0 for length in lengths) or sum(lengths) <= 0:
            raise ValueError("length")
        piece_length, pieces = info[b"piece length"], info[b"pieces"]
        if (
            type(piece_length) is not int
            or piece_length <= 0
            or not isinstance(pieces, bytes)
            or len(pieces) != 20 * ((sum(lengths) + piece_length - 1) // piece_length)
        ):
            raise ValueError("piece data")
        # Explicitly reject symlinks and alternate external payload sources.
        if (
            b"symlink path" in info
            or b"l" in info.get(b"attr", b"")
            or any(k in root for k in (b"url-list", b"httpseeds"))
        ):
            raise ValueError("unsupported external payload source")
        return Download(hashlib.sha1(parser.info_bytes).hexdigest(), torrent=raw)
    except (KeyError, ValueError, TypeError, IndexError, OverflowError) as exc:
        raise SearchError("INVALID_DOWNLOAD: malformed, unsafe or unsupported v1 torrent") from exc


def validate_magnet(value: str) -> Download:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "magnet"
            or parsed.netloc
            or parsed.path
            or parsed.fragment
            or len(value) > 16_000
            or re.search(r"[\x00-\x20\x7f]", value)
        ):
            raise ValueError("magnet syntax")
        pairs = parse_qsl(parsed.query, strict_parsing=True)
        if any(key not in {"xt", "dn", "tr"} for key, _ in pairs):
            raise ValueError("unsupported external sources")
        xt = [v for k, v in pairs if k == "xt"]
        if len(xt) != 1 or not xt[0].startswith("urn:btih:"):
            raise ValueError("v1 hash required")
        hash_value = xt[0][9:]
        if re.fullmatch(r"[0-9a-fA-F]{40}", hash_value):
            digest = hash_value.lower()
        elif re.fullmatch(r"[A-Z2-7a-z]{32}", hash_value):
            digest = base64.b32decode(hash_value.upper()).hex()
        else:
            raise ValueError("hash")
        trackers = []
        for key, tracker in pairs:
            if key != "tr":
                continue
            url = urlsplit(tracker)
            if (
                url.scheme not in {"http", "https", "udp"}
                or not url.hostname
                or url.username
                or url.password
                or re.search(r"[\x00-\x20\x7f]", tracker)
            ):
                raise ValueError("tracker")
            trackers.append(("tr", tracker))
        # Drop untrusted display filenames. Never pass unsupported xs/as/ws URLs.
        magnet = "magnet:?" + urlencode([("xt", "urn:btih:" + digest), *trackers])
        return Download(digest, magnet=magnet)
    except (ValueError, TypeError) as exc:
        raise SearchError("INVALID_DOWNLOAD: unsupported or malformed v1 magnet") from exc


class TorrentClient(Protocol):
    def submit(self, candidate: Candidate, download: Download) -> str: ...


def client_settings(policy: SearchPolicy) -> tuple[str, str]:
    if not policy.qbittorrent_url:
        raise SearchError("NOT_CONFIGURED: set search.qbittorrent_url")
    base = endpoint(policy.qbittorrent_url)
    if (
        not policy.save_path
        or not (policy.save_path.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", policy.save_path))
        or re.search(r"[\x00-\x1f\x7f]", policy.save_path)
    ):
        raise SearchError("Configure search.save_path as an absolute destination on the client host")
    return base, policy.save_path


def write_receipt(path: Path, candidate: Candidate, download: Download, status: str) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                {"candidate_id": candidate.candidate_id, "info_hash": download.info_hash, "status": status}, handle
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class Qbittorrent:
    def __init__(self, policy: SearchPolicy, http: HTTP | None = None) -> None:
        self.base, self.destination = client_settings(policy)
        self.policy, self.http = policy, http or HTTP(policy.timeout)
        parsed = urlsplit(self.base)
        authority = parsed.scheme + "://" + parsed.netloc
        self.headers = {"Origin": authority, "Referer": self.base + "/"}

    def login(self) -> None:
        username = os.environ.get(self.policy.qbittorrent_user_env, "")
        password = os.environ.get(self.policy.qbittorrent_password_env, "")
        if not username or not password:
            raise SearchError("NOT_CONFIGURED: set qBittorrent username/password environment variables")
        response = self.http.request(
            "POST",
            self.base + "/api/v2/auth/login",
            retry=False,
            headers=self.headers,
            data={"username": username, "password": password},
        )
        if response.strip() != b"Ok.":
            raise SearchError("AUTHENTICATION_ERROR: qBittorrent login rejected")

    def present(self, digest: str) -> bool:
        response = self.http.json(
            "GET", self.base + "/api/v2/torrents/info", headers=self.headers, params={"hashes": digest}
        )
        if not isinstance(response, list) or any(
            not isinstance(row, dict) or not isinstance(row.get("hash"), str) for row in response
        ):
            raise SearchError("PARSER_ERROR: invalid qBittorrent torrent list")
        return any(row["hash"].lower() == digest for row in response)

    def submit(self, candidate: Candidate, download: Download) -> str:
        # Validate again at the replaceable client boundary; never trust caller hashes.
        checked = (
            validate_torrent(download.torrent)
            if download.torrent is not None
            else validate_magnet(download.magnet or "")
        )
        if checked.info_hash != download.info_hash:
            raise SearchError("INVALID_DOWNLOAD: mismatched info hash")
        default = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "auph/search-submissions"
        directory = Path(self.policy.state_directory) if self.policy.state_directory else default
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise SearchError("Submission state directory must be a private, owned, nonsymlinked directory")
        key = hashlib.sha256((self.base + "\n" + checked.info_hash).encode()).hexdigest()
        receipt = directory / (key + ".json")
        with file_lock(receipt):
            self.login()
            if self.present(checked.info_hash):
                write_receipt(receipt, candidate, checked, "confirmed")
                return "already present; no submission"
            if receipt.exists() or receipt.is_symlink():
                raise SearchError(
                    "Submission already recorded or outcome uncertain; inspect client and receipt manually before any retry"
                )
            # Persist BEFORE the only mutation request. Crashes leave a conservative pending gate.
            write_receipt(receipt, candidate, checked, "pending")
            data = {"savepath": self.destination, "autoTMM": "false", "skip_checking": "false"}
            kwargs: dict[str, Any] = {"data": data, "headers": self.headers, "retry": False}
            if checked.torrent is not None:
                kwargs["files"] = {"torrents": ("release.torrent", checked.torrent, "application/x-bittorrent")}
            else:
                data["urls"] = checked.magnet or ""
            try:
                response = self.http.request("POST", self.base + "/api/v2/torrents/add", **kwargs)
                if response.strip() != b"Ok." or not self.present(checked.info_hash):
                    raise SearchError("Submission outcome uncertain; receipt retained, no automatic retry")
            except HTTPError as exc:
                if exc.status == 415:
                    write_receipt(receipt, candidate, checked, "rejected")
                    raise SearchError("Client rejected torrent; receipt retained for review") from exc
                raise SearchError("Submission outcome uncertain; receipt retained, no automatic retry") from exc
            except SearchError as exc:
                raise SearchError("Submission outcome uncertain; receipt retained, no automatic retry") from exc
            write_receipt(receipt, candidate, checked, "confirmed")
            return "submitted and visible in client"
