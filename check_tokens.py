#!/usr/bin/env python3
"""
check_tokens.py — Diagnostic & Health Check Utility for AudiophileArchTagger & AudiophileCoverFetcher

Validates all API keys, OAuth tokens, and session cookies against live service endpoints.
Supports terminal color formatting and JSON output. It does not read browser cookies or refresh private web-player sessions.

Usage:
    python3 check_tokens.py              # Standard colorized report
    python3 check_tokens.py --json       # Output structured JSON
    python3 check_tokens.py --verbose    # Detailed diagnostics & API responses
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from provider_cache import redact_secrets

# ANSI Colors
CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


def supports_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("TERM", "") != "dumb"


def c(text: str, color: str) -> str:
    return f"{color}{text}{RESET}" if supports_color() else text


@dataclass
class TokenCheckResult:
    service: str
    var_name: str
    status: str  # "OK", "WARN", "FAIL", "SKIP"
    message: str
    details: dict[str, Any] | None = None
    recommendation: str | None = None


def load_env_file(env_path: Path | None = None) -> dict[str, str]:
    """Load key=value pairs from .env file into a dictionary and os.environ."""
    candidates = (
        [env_path]
        if env_path
        else [
            Path.cwd() / ".env",
            Path(__file__).resolve().parent / ".env",
            Path.home() / "Music" / ".env",
        ]
    )
    env_dict: dict[str, str] = {}
    for p in candidates:
        if p and p.is_file():
            try:
                for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("'\"")
                    if k:
                        env_dict[k] = v
                        if k not in os.environ:
                            os.environ[k] = v
                break
            except Exception:
                pass
    # Match the pipeline: exported values take precedence over .env defaults.
    return {**env_dict, **os.environ}


def update_env_var(key: str, value: str, env_path: Path | None = None) -> bool:
    """Update or add an environment variable in the .env file."""
    target = env_path or (Path(__file__).resolve().parent / ".env")
    if not target.is_file():
        target = Path.cwd() / ".env"
    if not target.is_file():
        return False
    try:
        content = target.read_text(encoding="utf-8")
        if re.search(rf"^{re.escape(key)}=.*$", content, flags=re.MULTILINE):
            new_content = re.sub(
                rf"^{re.escape(key)}=.*$",
                f"{key}={value}",
                content,
                flags=re.MULTILINE,
            )
        else:
            new_content = content + f"\n{key}={value}\n"
        target.write_text(new_content, encoding="utf-8")
        os.environ[key] = value
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Provider Check Functions
# ---------------------------------------------------------------------------


def check_discogs(token: str) -> TokenCheckResult:
    if not token:
        return TokenCheckResult(
            service="Discogs API",
            var_name="DISCOGS_TOKEN",
            status="WARN",
            message="Отсутствует токен (поиск и сканы обложек в высоком разрешении будут ограничены)",
            recommendation="Получите личный токен на https://www.discogs.com/settings/developers",
        )
    url = "https://api.discogs.com/oauth/identity"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "AudiophileArchTagger/6.0 (+https://github.com/)",
            "Authorization": f"Discogs token={token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read(1_000_001).decode("utf-8"))
            user = data.get("username", "Unknown")
            return TokenCheckResult(
                service="Discogs API",
                var_name="DISCOGS_TOKEN",
                status="OK",
                message=f"Активен (аккаунт: {user})",
                details=data,
            )
    except urllib.error.HTTPError as e:
        return TokenCheckResult(
            service="Discogs API",
            var_name="DISCOGS_TOKEN",
            status="FAIL",
            message=f"Ошибка авторизации (HTTP {e.code}: {e.reason})",
            recommendation="Обновите токен в .env на странице https://www.discogs.com/settings/developers",
        )
    except Exception as e:
        return TokenCheckResult(
            service="Discogs API",
            var_name="DISCOGS_TOKEN",
            status="WARN",
            message=f"Сетевой сбой при проверке: {redact_secrets(str(e))}",
        )


def check_acoustid(key: str) -> TokenCheckResult:
    if not key:
        return TokenCheckResult(
            service="AcoustID (Chromaprint)",
            var_name="ACOUSTID_API_KEY",
            status="WARN",
            message="Ключ не указан (акустический отпечаток треков отключен)",
            recommendation="Зарегистрируйте ключ на https://acoustid.org/api-key",
        )
    url = f"https://api.acoustid.org/v2/lookup?client={key}&duration=100&fingerprint=AQAAAA"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "AudiophileArchTagger/6.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read(1_000_001).decode("utf-8"))
            if data.get("status") == "ok":
                return TokenCheckResult("AcoustID (Chromaprint)", "ACOUSTID_API_KEY", "OK", "API key accepted")
            error = data.get("error") or {}
            if error.get("code") == 4:
                return TokenCheckResult("AcoustID (Chromaprint)", "ACOUSTID_API_KEY", "FAIL", "Invalid API key")
            return TokenCheckResult(
                "AcoustID (Chromaprint)",
                "ACOUSTID_API_KEY",
                "WARN",
                "Lookup rejected; this diagnostic fingerprint does not establish key validity",
            )
    except urllib.error.HTTPError as e:
        body = e.read(1_000_001).decode(errors="replace")
        if "invalid api key" in body.lower():
            return TokenCheckResult(
                service="AcoustID (Chromaprint)",
                var_name="ACOUSTID_API_KEY",
                status="FAIL",
                message="Недействительный ключ (сервер вернул 'invalid API key')",
                recommendation="Проверьте и обновите ключ на https://acoustid.org/api-key",
            )
        return TokenCheckResult(
            service="AcoustID (Chromaprint)",
            var_name="ACOUSTID_API_KEY",
            status="WARN",
            message=f"HTTP {e.code}: key validity could not be established",
        )
    except Exception as e:
        return TokenCheckResult(
            service="AcoustID (Chromaprint)",
            var_name="ACOUSTID_API_KEY",
            status="WARN",
            message=f"Ошибка сети: {redact_secrets(str(e))}",
        )


def check_musicbrainz(contact: str) -> TokenCheckResult:
    if not contact:
        return TokenCheckResult(
            service="MusicBrainz Contact",
            var_name="MB_CONTACT",
            status="WARN",
            message="Не задан email для User-Agent (используется fallback)",
            recommendation="Укажите ваш email в MB_CONTACT в файле .env",
        )
    if "@" in contact and "." in contact.split("@")[-1]:
        return TokenCheckResult(
            service="MusicBrainz Contact",
            var_name="MB_CONTACT",
            status="OK",
            message=f"Корректный email ({contact})",
        )
    return TokenCheckResult(
        service="MusicBrainz Contact",
        var_name="MB_CONTACT",
        status="WARN",
        message=f"Некорректный формат email: '{contact}'",
    )


def check_fanart(key: str) -> TokenCheckResult:
    if not key:
        return TokenCheckResult(
            service="Fanart.tv API",
            var_name="FANART_API_KEY",
            status="SKIP",
            message="Не настроен (опциональный провайдер обложек и накаток CD)",
            recommendation="Получите персональный API-ключ на https://fanart.tv/get-an-api-key/",
        )
    # Test with Nirvana MBID (5b11f4ce-a62d-471e-81fc-a69a8278c7da)
    url = f"https://webservice.fanart.tv/v3/music/5b11f4ce-a62d-471e-81fc-a69a8278c7da?api_key={key}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read(1_000_001).decode("utf-8"))
            albums = len(data.get("albums", {}))
            return TokenCheckResult(
                service="Fanart.tv API",
                var_name="FANART_API_KEY",
                status="OK",
                message=f"Активен (проверен live-запросом, альбомов в базе: {albums})",
            )
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return TokenCheckResult(
                service="Fanart.tv API",
                var_name="FANART_API_KEY",
                status="FAIL",
                message="Недействительный ключ (HTTP 401 Unauthorized)",
                recommendation="Проверьте правильность ключа на https://fanart.tv/",
            )
        return TokenCheckResult(
            service="Fanart.tv API",
            var_name="FANART_API_KEY",
            status="WARN",
            message=f"HTTP {e.code}: {e.reason}",
        )
    except Exception as e:
        return TokenCheckResult(
            service="Fanart.tv API",
            var_name="FANART_API_KEY",
            status="WARN",
            message=f"Ошибка сети: {redact_secrets(str(e))}",
        )


def check_spotify(sp_dc: str = "", access_token: str = "", do_refresh: bool = False) -> list[TokenCheckResult]:
    del sp_dc, access_token, do_refresh
    return [
        TokenCheckResult(
            service="Spotify lyrics",
            var_name="SPOTIFY_ACCESS_TOKEN",
            status="SKIP",
            message="Lyrics are unavailable through Spotify's documented Web API; private Web Player routes are disabled.",
        )
    ]


def check_deezer(arl: str = "") -> TokenCheckResult:
    del arl
    return TokenCheckResult(
        service="Deezer lyrics",
        var_name="DEEZER_ARL",
        status="SKIP",
        message="No documented public lyrics endpoint is configured; private gateway routes are disabled.",
    )


def check_yandex(token: str, proxy: str = "") -> TokenCheckResult:
    del proxy
    if not token:
        return TokenCheckResult(
            service="Яндекс Музыка",
            var_name="YANDEX_MUSIC_TOKEN",
            status="WARN",
            message="Токен Яндекс Музыки не указан",
            recommendation="Получите OAuth токен через Яндекс Музыку и укажите в .env",
        )
    url = "https://api.music.yandex.net/account/status"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"OAuth {token}",
            "User-Agent": "YandexMusic/2024",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read(1_000_001).decode("utf-8"))
            acc = data.get("result", {}).get("account", {})
            has_plus = data.get("result", {}).get("plus", {}).get("hasPlus", False)
            login = acc.get("login", "Unknown")
            uid = acc.get("uid", "")
            sub_info = "Подписка Плюс: активна" if has_plus else "Без Плюса (требуется РФ/СНГ прокси вне РФ)"
            return TokenCheckResult(
                service="Яндекс Музыка",
                var_name="YANDEX_MUSIC_TOKEN",
                status="OK",
                message=f"Активен (UID: {uid}, логин: {login}, {sub_info})",
            )
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return TokenCheckResult(
                service="Яндекс Музыка",
                var_name="YANDEX_MUSIC_TOKEN",
                status="FAIL",
                message="Токен недействителен или отозван (HTTP 401)",
                recommendation="Обновите OAuth токен Яндекс Музыки в .env",
            )
        elif e.code == 451:
            return TokenCheckResult(
                service="Яндекс Музыка",
                var_name="YANDEX_MUSIC_TOKEN",
                status="WARN",
                message="Токен валиден, но доступ заблокирован по гео-IP (HTTP 451)",
                recommendation="Доступность зависит от провайдера и региона; no proxy or access-control workaround is attempted.",
            )
        return TokenCheckResult(
            service="Яндекс Музыка",
            var_name="YANDEX_MUSIC_TOKEN",
            status="WARN",
            message=f"HTTP {e.code}: {e.reason}",
        )
    except Exception as e:
        return TokenCheckResult(
            service="Яндекс Музыка",
            var_name="YANDEX_MUSIC_TOKEN",
            status="WARN",
            message=f"Ошибка сети: {redact_secrets(str(e))}",
        )


def check_musixmatch(api_key: str) -> TokenCheckResult:
    if not api_key:
        return TokenCheckResult(
            service="Musixmatch API",
            var_name="MUSIXMATCH_API_KEY",
            status="SKIP",
            message="No registered Musixmatch API key is configured.",
            recommendation="Configure MUSIXMATCH_API_KEY or ~/.config/musixmatch_api_key if you have authorized API access.",
        )
    return TokenCheckResult(
        service="Musixmatch API",
        var_name="MUSIXMATCH_API_KEY",
        status="WARN",
        message="An API key is configured; validity and account limits are checked by documented API responses during lookup.",
    )


def check_usdb(cookie: str) -> TokenCheckResult:
    if not cookie:
        return TokenCheckResult(
            service="UltraStar DB",
            var_name="USDB_COOKIE",
            status="SKIP",
            message="Кука сессии USDB не указана (пословное караоке)",
            recommendation="Скопируйте куку PHPSESSID из браузера на usdb.animux.de в .env",
        )
    url = "https://usdb.animux.de/?link=profile"
    clean_cookie = cookie if "PHPSESSID=" in cookie else f"PHPSESSID={cookie}"
    req = urllib.request.Request(url, headers={"Cookie": clean_cookie, "User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            html = resp.read(1_000_001).decode(errors="replace")
            if "Logout" in html or "profile" in html or "Log out" in html:
                return TokenCheckResult(
                    service="UltraStar DB",
                    var_name="USDB_COOKIE",
                    status="OK",
                    message="Активен (авторизованная веб-сессия для пословных TTML)",
                )
            return TokenCheckResult(
                service="UltraStar DB",
                var_name="USDB_COOKIE",
                status="WARN",
                message="Сессия не авторизована (гостевой доступ)",
                recommendation="Обновите куку PHPSESSID в ~/.config/usdb_cookie или .env",
            )
    except Exception as e:
        return TokenCheckResult(
            service="UltraStar DB",
            var_name="USDB_COOKIE",
            status="WARN",
            message=f"Ошибка сети: {redact_secrets(str(e))}",
        )


def check_vk(token: str, cookie: str) -> list[TokenCheckResult]:
    results: list[TokenCheckResult] = []

    # 1. VK API Token
    if token:
        url = f"https://api.vk.com/method/users.get?v=5.199&access_token={token}"
        req = urllib.request.Request(url, headers={"User-Agent": "VKAndroidApp/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read(1_000_001).decode("utf-8"))
                if "response" in data:
                    user_info = data["response"][0] if data["response"] else {}
                    uid = user_info.get("id")
                    name = f"{user_info.get('first_name', '')} {user_info.get('last_name', '')}".strip()
                    results.append(
                        TokenCheckResult(
                            service="ВКонтакте API",
                            var_name="VK_TOKEN",
                            status="OK",
                            message=f"Активен (ID: {uid}, {name})",
                        )
                    )
                else:
                    err = data.get("error", {})
                    code = err.get("error_code")
                    msg = err.get("error_msg", "")
                    if code == 9:
                        results.append(
                            TokenCheckResult(
                                service="ВКонтакте API",
                                var_name="VK_TOKEN",
                                status="WARN",
                                message="Валиден, но ограничен сервером по IP (Flood control)",
                                recommendation="Используйте прокси (~/.config/vk_proxy) или веб-сессию remixsid",
                            )
                        )
                    else:
                        results.append(
                            TokenCheckResult(
                                service="ВКонтакте API",
                                var_name="VK_TOKEN",
                                status="FAIL",
                                message=f"Ошибка {code}: {msg}",
                                recommendation="Обновите токен VK в .env",
                            )
                        )
        except Exception as e:
            results.append(
                TokenCheckResult(
                    service="ВКонтакте API",
                    var_name="VK_TOKEN",
                    status="WARN",
                    message=f"Ошибка сети: {redact_secrets(str(e))}",
                )
            )
    else:
        results.append(
            TokenCheckResult(
                service="ВКонтакте API",
                var_name="VK_TOKEN",
                status="SKIP",
                message="Токен не указан",
            )
        )

    # 2. VK Cookie (remixsid)
    if cookie:
        clean_cookie = cookie if "remixsid=" in cookie else f"remixsid={cookie}"

        class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
            def http_error_302(self, req, fp, code, msg, headers):
                return fp

            def http_error_301(self, req, fp, code, msg, headers):
                return fp

            def http_error_303(self, req, fp, code, msg, headers):
                return fp

            def http_error_307(self, req, fp, code, msg, headers):
                return fp

        c_opener = urllib.request.build_opener(NoRedirectHandler)
        req_c = urllib.request.Request(
            "https://vk.com/feed",
            headers={"Cookie": clean_cookie, "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"},
        )
        try:
            with c_opener.open(req_c, timeout=8) as resp_c:
                loc = resp_c.headers.get("Location", "")
                if "login.vk.com" in loc:
                    results.append(
                        TokenCheckResult(
                            service="ВКонтакте Web (remixsid)",
                            var_name="VK_COOKIE",
                            status="WARN",
                            message="Кука remixsid устарела (перенаправление на login.vk.com)",
                            recommendation="Обновите remixsid из браузера в ~/.config/vk_cookie",
                        )
                    )
                else:
                    results.append(
                        TokenCheckResult(
                            service="ВКонтакте Web (remixsid)",
                            var_name="VK_COOKIE",
                            status="OK",
                            message="Активна (веб-сессия валидна)",
                        )
                    )
        except Exception as e:
            results.append(
                TokenCheckResult(
                    service="ВКонтакте Web (remixsid)",
                    var_name="VK_COOKIE",
                    status="WARN",
                    message=f"Ошибка проверки: {redact_secrets(str(e))}",
                )
            )
    else:
        results.append(
            TokenCheckResult(
                service="ВКонтакте Web (remixsid)",
                var_name="VK_COOKIE",
                status="SKIP",
                message="Кука remixsid не указана",
            )
        )

    return results


def check_tidal_builtin() -> TokenCheckResult:
    url = "https://api.tidal.com/v1/search/albums?query=Queen&limit=1&countryCode=US"
    req = urllib.request.Request(
        url,
        headers={"x-tidal-token": "CzET4vdadNUFQ5JU", "User-Agent": "Mozilla/5.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read(1_000_001).decode("utf-8"))
            cnt = len(data.get("items", []))
            return TokenCheckResult(
                service="Tidal High-Res CDN",
                var_name="TIDAL (Builtin)",
                status="OK",
                message=f"Активен (встроенный клиентский ключ валиден, найдено релизов: {cnt})",
            )
    except Exception as e:
        return TokenCheckResult(
            service="Tidal High-Res CDN",
            var_name="TIDAL (Builtin)",
            status="WARN",
            message=f"Ошибка: {redact_secrets(str(e))}",
        )


# ---------------------------------------------------------------------------
# CLI Runner
# ---------------------------------------------------------------------------


def run_all_checks(env_path: Path | None = None) -> list[TokenCheckResult]:
    env = load_env_file(env_path)

    results: list[TokenCheckResult] = []

    # 1. Metadata Core
    results.append(check_discogs(env.get("DISCOGS_TOKEN", "")))
    results.append(check_acoustid(env.get("ACOUSTID_API_KEY", "")))
    results.append(check_musicbrainz(env.get("MB_CONTACT", "")))

    # 2. Covers
    results.append(check_fanart(env.get("FANART_API_KEY", "")))
    results.append(check_tidal_builtin())

    # 3. Streaming & Lyrics
    results.extend(check_spotify())
    results.append(check_deezer(env.get("DEEZER_ARL", "")))
    results.append(
        check_yandex(
            env.get("YANDEX_MUSIC_TOKEN", ""),
            env.get("YANDEX_MUSIC_PROXY", "") or env.get("YANDEX_PROXY", ""),
        )
    )
    results.append(check_musixmatch(env.get("MUSIXMATCH_API_KEY", "")))
    results.append(check_usdb(env.get("USDB_COOKIE", "")))
    results.extend(check_vk(env.get("VK_TOKEN", ""), env.get("VK_COOKIE", "")))

    return results


def main() -> int:
    parser = argparse.ArgumentParser(
        description="check_tokens.py — Diagnostic health check for AudiophileArchTagger API keys & sessions."
    )
    parser.add_argument("--env", type=Path, default=None, help="Path to custom .env file")
    parser.add_argument("--json", action="store_true", help="Output results in JSON format")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print detailed diagnostic info")
    args = parser.parse_args()

    results = run_all_checks(env_path=args.env)

    if args.json:
        out = [
            {
                "service": r.service,
                "var_name": r.var_name,
                "status": r.status,
                "message": r.message,
                "recommendation": r.recommendation,
            }
            for r in results
        ]
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    # Human-readable table
    print()
    print(c("=" * 86, BOLD))
    print(c("       AUDIOPHILE ARCH TAGGER — ДИАГНОСТИКА ТОКЕНОВ И СЕССИЙ", BOLD))
    print(c("=" * 86, BOLD))
    print()

    has_fail = False
    for r in results:
        if r.status == "OK":
            badge = c("[  OK  ]", GREEN)
        elif r.status == "WARN":
            badge = c("[ WARN ]", YELLOW)
        elif r.status == "SKIP":
            badge = c("[ SKIP ]", DIM)
        else:
            badge = c("[ FAIL ]", RED)
            has_fail = True

        var_label = f"({r.var_name})" if r.var_name else ""
        print(f" {badge} {c(r.service, BOLD):<28} {c(var_label, DIM):<24} {r.message}")
        if r.recommendation and (r.status in ("FAIL", "WARN") or args.verbose):
            print(f"          ↳ {c('Совет:', YELLOW)} {r.recommendation}")

    print()
    print(c("=" * 86, BOLD))

    # Summary
    ok_count = sum(1 for r in results if r.status == "OK")
    warn_count = sum(1 for r in results if r.status == "WARN")
    fail_count = sum(1 for r in results if r.status == "FAIL")
    skip_count = sum(1 for r in results if r.status == "SKIP")

    print(
        f" Всего проверено: {len(results)} | "
        f"{c(f'Активно: {ok_count}', GREEN)} | "
        f"{c(f'Предупреждений: {warn_count}', YELLOW)} | "
        f"{c(f'Ошибок: {fail_count}', RED)} | "
        f"{c(f'Пропущено: {skip_count}', DIM)}"
    )
    print()

    return 1 if has_fail else 0


if __name__ == "__main__":
    sys.exit(main())
