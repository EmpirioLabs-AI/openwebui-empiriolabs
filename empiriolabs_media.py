"""
title: EmpirioLabs AI Media
author: EmpirioLabs AI
author_url: https://empiriolabs.ai
git_url: https://github.com/EmpirioLabs-AI/openwebui-empiriolabs
description: Create videos, images and speech in the chat with models from the EmpirioLabs AI catalog. Text-to-video, image-to-video from a link, image generation and text-to-speech.
required_open_webui_version: 0.12.0
version: 1.0.0
license: MIT
"""

import asyncio
import html
import inspect
import io
import ipaddress
import json
import logging
import re
import threading
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import quote, urljoin, urlsplit, parse_qs

import httpx
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

VERSION = "1.0.0"
DISPLAY_NAME = "EmpirioLabs AI"
API_BASE = "https://api.empiriolabs.ai/v1"
KEYS_URL = "https://platform.empiriolabs.ai/dashboard/api-keys"
MODELS_URL = "https://empiriolabs.ai/models"
USER_AGENT = f"openwebui-empiriolabs/{VERSION}"

# Release defaults. Each is used only while it is in the live catalog; otherwise the
# first featured model of the same kind is used.
DEFAULT_IMAGE_MODEL = "flux-2-klein-4b"
DEFAULT_VIDEO_MODEL = "hunyuan-video-1-5"
DEFAULT_SPEECH_MODEL = "tts-2-flash"

IMAGE_ENDPOINT = "POST /v1/images/generations"
VIDEO_ENDPOINT = "POST /v1/videos/generations"
SPEECH_ENDPOINT = "POST /v1/audio/speech"

CATALOG_TTL_SECONDS = 3600.0
CATALOG_RETRY_SECONDS = 60.0
CATALOG_TIMEOUT_SECONDS = 6.0
MAX_JSON_BYTES = 32 * 1024 * 1024
MAX_IMAGE_BYTES = 50 * 1024 * 1024
MAX_AUDIO_BYTES = 100 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 300.0
POLL_SECONDS = 5.0
STATUS_EVERY_SECONDS = 15.0
MAX_POLL_RETRIES = 5

JOB_RUNNING = {"queued", "pending", "processing", "running", "in_progress", "submitted", "starting"}
JOB_DONE = {"completed", "succeeded"}
JOB_FAILED = {"failed", "error", "cancelled", "canceled", "expired"}
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

# Test seam: tests replace these with an httpx.MockTransport and a fake clock.
_TRANSPORT: Optional[httpx.AsyncBaseTransport] = None
_sleep = asyncio.sleep
_now = time.monotonic

EventEmitter = Optional[Callable[[dict], Awaitable[Any]]]


class ApiError(Exception):
    """A failed API call. `status` is the HTTP status, 0 when no response arrived."""

    def __init__(self, message: str, status: int = 0, job_id: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.job_id = job_id


class JobStillRunning(ApiError):
    """The job had not finished when the wait limit passed. It keeps running."""


# ---------------------------------------------------------------------------
# Live model catalog (public, no key needed)
# ---------------------------------------------------------------------------

_catalog_lock = threading.Lock()
_catalog_rows: Optional[list] = None
_catalog_at = 0.0
_catalog_failed_at = 0.0


def _keep_rows(payload: Any) -> Optional[list]:
    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return None
    return [row for row in items if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"]]


def _cached_catalog(allow_stale: bool) -> tuple[Optional[list], bool]:
    """(rows, fresh). Rows may be stale when `allow_stale` is set."""
    with _catalog_lock:
        now = _now()
        fresh = _catalog_rows is not None and now - _catalog_at < CATALOG_TTL_SECONDS
        recently_failed = now - _catalog_failed_at < CATALOG_RETRY_SECONDS
        if fresh or (allow_stale and recently_failed):
            return _catalog_rows, True
        return (_catalog_rows if allow_stale else None), False


def _remember_catalog(rows: Optional[list]) -> None:
    global _catalog_rows, _catalog_at, _catalog_failed_at
    with _catalog_lock:
        if rows is None:
            _catalog_failed_at = _now()
        else:
            _catalog_rows, _catalog_at = rows, _now()


def _catalog_sync() -> Optional[list]:
    """Blocking catalog read for the Valves dropdowns (short timeout, cached for an hour)."""
    rows, fresh = _cached_catalog(allow_stale=True)
    if fresh:
        return rows
    try:
        request = urllib.request.Request(
            API_BASE + "/models", headers={"Accept": "application/json", "User-Agent": USER_AGENT}
        )
        with urllib.request.urlopen(request, timeout=CATALOG_TIMEOUT_SECONDS) as response:
            fetched = _keep_rows(json.loads(response.read(MAX_JSON_BYTES)))
    except Exception as exc:  # network or parse failure: keep the last good copy
        log.debug("%s catalog read failed: %s", DISPLAY_NAME, type(exc).__name__)
        fetched = None
    _remember_catalog(fetched)
    return fetched if fetched is not None else rows


async def _catalog() -> Optional[list]:
    rows, fresh = _cached_catalog(allow_stale=True)
    if fresh:
        return rows
    try:
        async with _client(CATALOG_TIMEOUT_SECONDS) as client:
            response = await client.get(API_BASE + "/models", headers={"Accept": "application/json"})
        fetched = _keep_rows(response.json()) if response.status_code == 200 else None
    except Exception as exc:
        log.debug("%s catalog read failed: %s", DISPLAY_NAME, type(exc).__name__)
        fetched = None
    _remember_catalog(fetched)
    return fetched if fetched is not None else rows


def _strings(value: Any) -> list[str]:
    return [str(item) for item in value if isinstance(item, (str, int, float))] if isinstance(value, list) else []


def _param(row: Optional[dict], name: str) -> Optional[dict]:
    for spec in (row or {}).get("supported_parameters") or []:
        if isinstance(spec, dict) and spec.get("name") == name:
            return spec
    return None


def _options(row: Optional[dict], name: str) -> list[str]:
    return [option for option in _strings((_param(row, name) or {}).get("options")) if option.strip()]


def _required(row: dict) -> set[str]:
    return {
        str(spec.get("name"))
        for spec in row.get("supported_parameters") or []
        if isinstance(spec, dict) and spec.get("required")
    }


def _available(row: dict, endpoint: str) -> bool:
    return (
        endpoint in _strings(row.get("supported_endpoints"))
        and str(row.get("status") or "available").lower() == "available"
        and not row.get("deprecated")
    )


def _is_image_model(row: dict) -> bool:
    return _available(row, IMAGE_ENDPOINT) and _param(row, "prompt") is not None and _required(row) <= {"prompt"}


def _is_video_model(row: dict) -> bool:
    """A model that makes a video from a prompt (and optionally a start image).

    Models that only edit, dub or lip-sync an existing video or recording are left out.
    """
    inputs = _strings(row.get("input_modalities")) or ["text"]
    if not (
        _available(row, VIDEO_ENDPOINT)
        and "text" in inputs
        and _param(row, "prompt") is not None
        and _required(row) <= {"prompt"}
    ):
        return False
    features = set(_strings(row.get("features")))
    generates = bool(features & {"text_to_video", "image_to_video", "video_generation"})
    if "video_to_video" in features or {"lipsync", "audio_in"} <= features:
        return False
    return generates or "video_editing" not in features


def _is_speech_model(row: dict) -> bool:
    return _available(row, SPEECH_ENDPOINT) and _param(row, "input") is not None and _required(row) <= {"input"}


def _takes_start_image(row: Optional[dict]) -> bool:
    if row is None:
        return True
    inputs = _strings(row.get("input_modalities"))
    return "image" in inputs and (_param(row, "image") is not None or _param(row, "image_url") is not None)


KINDS: dict[str, Callable[[dict], bool]] = {
    "image": _is_image_model,
    "video": _is_video_model,
    "speech": _is_speech_model,
}
DEFAULTS = {"image": DEFAULT_IMAGE_MODEL, "video": DEFAULT_VIDEO_MODEL, "speech": DEFAULT_SPEECH_MODEL}


def _label(row: dict) -> str:
    name = str(row.get("display_name") or row["id"])
    return name if name == row["id"] else f"{name} ({row['id']})"


def _find(rows: list, wanted: str, eligible: Callable[[dict], bool]) -> Optional[dict]:
    wanted = (wanted or "").strip()
    if not wanted:
        return None
    for row in rows:
        if row["id"] == wanted and eligible(row):
            return row
    lowered = wanted.lower()
    for row in rows:
        if eligible(row) and (row["id"].lower() == lowered or lowered in (a.lower() for a in _strings(row.get("aliases")))):
            return row
    return None


def _model_options(kind: str, with_default: bool) -> list[dict]:
    eligible = KINDS[kind]
    rows = _catalog_sync() or []
    options = sorted(
        ({"value": row["id"], "label": _label(row)} for row in rows if eligible(row)),
        key=lambda option: option["label"].lower(),
    )
    if not options:
        options = [{"value": DEFAULTS[kind], "label": DEFAULTS[kind]}]
    if with_default:
        options.insert(0, {"value": "", "label": "Default"})
    return options


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _client(timeout: float) -> httpx.AsyncClient:
    kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(timeout, connect=15.0),
        "follow_redirects": False,
        "headers": {"User-Agent": USER_AGENT},
    }
    if _TRANSPORT is not None:
        kwargs["transport"] = _TRANSPORT
    return httpx.AsyncClient(**kwargs)


def _error_text(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        body = None
    message = ""
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or error.get("detail") or "")
        elif isinstance(error, str):
            message = error
        message = message or str(body.get("detail") or body.get("message") or "")
    return message.strip()[:500]


def _download_target_ok(url: str) -> bool:
    """Generated files are fetched only over https and never from a local or private address."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host or host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return address.is_global


# ---------------------------------------------------------------------------
# Request building helpers
# ---------------------------------------------------------------------------

_FILE_REF_RE = re.compile(
    r"^(?:/api/v1/files/)?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:/content)?/?$", re.IGNORECASE
)
_SECONDS_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*s?\s*$", re.IGNORECASE)
_HEIGHT_RE = re.compile(r"(\d{3,4})\s*p\b", re.IGNORECASE)
_SIZE_RE = re.compile(r"^\s*(\d{2,5})\s*[x*]\s*(\d{2,5})\s*$", re.IGNORECASE)


def _seconds(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    match = _SECONDS_RE.match(str(value or ""))
    return float(match.group(1)) if match else None


def _height(value: str) -> Optional[int]:
    text = str(value or "").strip().lower()
    if text in ("2k", "4k"):
        return 1440 if text == "2k" else 2160
    size = _SIZE_RE.match(text)
    if size:
        return min(int(size.group(1)), int(size.group(2)))
    match = _HEIGHT_RE.search(text)
    return int(match.group(1)) if match else None


def _pick_duration(row: Optional[dict], requested: Optional[float], cap: int) -> tuple[Any, str]:
    """(value to send or None, error). Never exceeds `cap` seconds."""
    spec = _param(row, "duration")
    if row is None:
        if requested:
            return min(int(round(requested)), cap), ""
        return None, ""
    if spec is None:
        return None, ""
    want = requested if requested else _seconds(spec.get("default"))
    options = [option for option in _options(row, "duration") if _seconds(option) is not None]
    if options:
        allowed = [option for option in options if _seconds(option) <= cap]
        if not allowed:
            shortest = min(_seconds(option) for option in options)
            return None, f"The shortest video this model makes is {shortest:g} seconds, above the {cap}-second limit set for this tool."
        if want is None:
            return None, ""
        return min(allowed, key=lambda option: (abs(_seconds(option) - want), _seconds(option))), ""
    low = spec.get("min") if isinstance(spec.get("min"), (int, float)) else None
    high = spec.get("max") if isinstance(spec.get("max"), (int, float)) else None
    if low is not None and low > cap:
        return None, f"The shortest video this model makes is {low:g} seconds, above the {cap}-second limit set for this tool."
    if want is None:
        return None, ""
    value = float(want)
    if low is not None:
        value = max(value, float(low))
    value = min(value, float(high) if high is not None else value, float(cap))
    return (int(value) if float(value).is_integer() else round(value, 1)), ""


def _pick_resolution(row: Optional[dict], requested: str) -> Optional[str]:
    requested = (requested or "").strip()
    if not requested:
        return None
    options = _options(row, "resolution")
    if not options:
        return requested if row is None else None
    for option in options:
        if option.lower() == requested.lower():
            return option
    wanted = _height(requested)
    sized = [(height, option) for option in options if (height := _height(option))]
    if wanted is None or not sized:
        return None
    below = [item for item in sized if item[0] <= wanted]
    return max(below)[1] if below else min(sized)[1]


def _pick_choice(row: Optional[dict], name: str, requested: str) -> Optional[str]:
    requested = (requested or "").strip()
    if not requested:
        return None
    options = _options(row, name)
    if row is None:
        return requested
    for option in options:
        if option.lower() == requested.lower():
            return option
    return None


def _first_output(payload: dict) -> tuple[Optional[str], dict]:
    for container in (payload, payload.get("result")):
        items = container.get("data") if isinstance(container, dict) else None
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and isinstance(item.get("url"), str) and item["url"]:
                    return item["url"], item
    return None, {}


def _all_outputs(payload: dict) -> list[str]:
    urls: list[str] = []
    for container in (payload, payload.get("result")):
        items = container.get("data") if isinstance(container, dict) else None
        if isinstance(items, list):
            urls.extend(item["url"] for item in items if isinstance(item, dict) and isinstance(item.get("url"), str) and item["url"])
        if urls:
            break
    return urls


def _expiry_text(url: str) -> str:
    try:
        expires = int(parse_qs(urlsplit(url).query).get("expires", [""])[0])
    except (ValueError, TypeError):
        return ""
    return datetime.fromtimestamp(expires, tz=timezone.utc).strftime("%b %d, %Y").replace(" 0", " ")


def _extension(content_type: str, url: str, fallback: str) -> str:
    known = {
        "image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif",
        "video/mp4": "mp4", "video/webm": "webm", "video/quicktime": "mov",
        "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/wav": "wav", "audio/x-wav": "wav",
        "audio/ogg": "ogg", "audio/flac": "flac", "audio/aac": "aac", "audio/mp4": "m4a",
    }
    ext = known.get((content_type or "").split(";")[0].strip().lower())
    if ext:
        return ext
    path_ext = urlsplit(url).path.rsplit(".", 1)
    if len(path_ext) == 2 and 1 <= len(path_ext[1]) <= 5 and path_ext[1].isalnum():
        return path_ext[1].lower()
    return fallback


def _media_page(kind: str, url: str, caption: str) -> str:
    """A small self-contained page for the chat's embedded result view."""
    src = html.escape(url, quote=True)
    player = (
        f'<video controls playsinline preload="metadata" src="{src}"></video>'
        if kind == "video"
        else f'<audio controls preload="metadata" src="{src}"></audio>'
    )
    expires = _expiry_text(url)
    link_label = "Open video" if kind == "video" else "Open audio"
    details = [html.escape(caption)] if caption else []
    details.append(f'<a href="{src}" target="_blank" rel="noopener noreferrer">{link_label}</a>')
    if expires:
        details.append(f"Link available until {html.escape(expires)}")
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\"><style>"
        "body{margin:0;padding:2px 0;font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;"
        "color:#6b7280;background:transparent}"
        "video{display:block;width:100%;height:auto;max-height:540px;border-radius:12px;background:#000}"
        "audio{display:block;width:100%}"
        ".meta{margin:6px 4px 0;font-size:12px;line-height:1.5}"
        ".meta a{color:inherit}"
        "</style></head><body>"
        f"{player}<div class=\"meta\">{' &middot; '.join(details)}</div>"
        "<script>function h(){parent.postMessage({type:'iframe:height',height:Math.ceil(document.body.getBoundingClientRect().height)+6},'*')}"
        "window.addEventListener('load',h);new ResizeObserver(h).observe(document.body);"
        "document.querySelectorAll('video,audio').forEach(function(m){m.addEventListener('loadedmetadata',h)});</script>"
        "</body></html>"
    )


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


class Tools:
    class Valves(BaseModel):
        api_key: str = Field(
            default="",
            title="API Key",
            description="Optional shared EmpirioLabs AI API key, used only for people who have not added their own key in the tool's user Valves. Requests made with it use this key's credits.",
            json_schema_extra={"input": {"type": "password"}},
        )
        image_model: str = Field(
            default=DEFAULT_IMAGE_MODEL,
            title="Image Model",
            description="Default image model.",
            json_schema_extra={"input": {"type": "select", "options": "image_model_options"}},
        )
        video_model: str = Field(
            default=DEFAULT_VIDEO_MODEL,
            title="Video Model",
            description="Default video model.",
            json_schema_extra={"input": {"type": "select", "options": "video_model_options"}},
        )
        speech_model: str = Field(
            default=DEFAULT_SPEECH_MODEL,
            title="Speech Model",
            description="Default text-to-speech model.",
            json_schema_extra={"input": {"type": "select", "options": "speech_model_options"}},
        )
        max_video_seconds: int = Field(
            default=10,
            ge=1,
            le=60,
            title="Longest Video (seconds)",
            description="Longest video the tool requests, in seconds.",
        )
        video_wait_minutes: int = Field(
            default=20,
            ge=1,
            le=120,
            title="Video Wait (minutes)",
            description="How long to wait for a video before handing back its job ID. The job keeps running and check_video can pick up the result later.",
        )
        keep_copies: bool = Field(
            default=True,
            title="Keep Copies",
            description="Keep a copy of each generated video and audio file in Open WebUI, attached to the chat message, so it stays available after the download link expires. Images are always kept.",
        )
        max_copy_mb: int = Field(
            default=100,
            ge=1,
            le=2048,
            title="Largest Copy (MB)",
            description="Largest file to keep a copy of, in MB.",
        )

        @classmethod
        def image_model_options(cls, __user__: Optional[dict] = None) -> list:
            return _model_options("image", with_default=False)

        @classmethod
        def video_model_options(cls, __user__: Optional[dict] = None) -> list:
            return _model_options("video", with_default=False)

        @classmethod
        def speech_model_options(cls, __user__: Optional[dict] = None) -> list:
            return _model_options("speech", with_default=False)

    class UserValves(BaseModel):
        api_key: str = Field(
            default="",
            title="API Key",
            description=f"Your EmpirioLabs AI API key. Create one at {KEYS_URL}.",
            json_schema_extra={"input": {"type": "password"}},
        )
        image_model: str = Field(
            default="",
            title="Image Model",
            description="Image model for your requests. Default uses the model your admin set.",
            json_schema_extra={"input": {"type": "select", "options": "image_model_options"}},
        )
        video_model: str = Field(
            default="",
            title="Video Model",
            description="Video model for your requests. Default uses the model your admin set.",
            json_schema_extra={"input": {"type": "select", "options": "video_model_options"}},
        )
        speech_model: str = Field(
            default="",
            title="Speech Model",
            description="Text-to-speech model for your requests. Default uses the model your admin set.",
            json_schema_extra={"input": {"type": "select", "options": "speech_model_options"}},
        )
        voice: str = Field(
            default="",
            title="Voice",
            description="Voice for text to speech. Leave empty for the model's default voice. list_media_models shows each model's voices.",
        )

        @classmethod
        def image_model_options(cls, __user__: Optional[dict] = None) -> list:
            return _model_options("image", with_default=True)

        @classmethod
        def video_model_options(cls, __user__: Optional[dict] = None) -> list:
            return _model_options("video", with_default=True)

        @classmethod
        def speech_model_options(cls, __user__: Optional[dict] = None) -> list:
            return _model_options("speech", with_default=True)

    def __init__(self) -> None:
        self.valves = self.Valves()

    # -- public tools ---------------------------------------------------------

    async def create_video(
        self,
        prompt: str,
        image_url: str = "",
        duration: Optional[float] = None,
        resolution: str = "",
        aspect_ratio: str = "",
        audio: Optional[bool] = None,
        model: str = "",
        __user__: Optional[dict] = None,
        __event_emitter__: EventEmitter = None,
        __request__: Any = None,
        __chat_id__: Optional[str] = None,
        __message_id__: Optional[str] = None,
    ) -> Any:
        """
        Create a video with EmpirioLabs AI from a text prompt, or animate a start image (image-to-video).
        Videos take from under a minute to several minutes. The finished video is shown in the chat.

        :param prompt: Description of the video: subject, action, camera and style.
        :param image_url: Optional start image to animate: the image_id returned by create_image, or a public https link copied exactly as given.
        :param duration: Optional length in seconds. Leave empty for the model's default.
        :param resolution: Optional resolution such as 480p, 720p or 1080p. Leave empty for the model's default.
        :param aspect_ratio: Optional aspect ratio such as 16:9, 9:16 or 1:1. Image-to-video usually follows the start image.
        :param audio: Optional. True to add a sound track on models that can, False for silent video.
        :param model: Optional video model ID. Leave empty to use the configured model. list_media_models shows the choices.
        """
        context = _Context(self, __user__, __event_emitter__, __request__, __chat_id__, __message_id__)
        try:
            key = context.api_key()
            prompt = (prompt or "").strip()
            if not prompt:
                return context.error("A prompt is required.")
            rows = await _catalog()
            model_id, row = context.choose_model("video", model, rows)
            if image_url and not _takes_start_image(row):
                return context.error(f"{model_id} does not animate a start image. Choose a model with image input, or leave out image_url.")
            payload: dict[str, Any] = {"model": model_id, "prompt": prompt}
            notes: list[str] = []
            if image_url:
                start = await context.start_image(image_url)
                if not start:
                    return context.error(
                        "image_url must be the image_id returned by create_image, or a public https:// link to the start image."
                    )
                field = "image" if row is None or _param(row, "image") is not None else "image_url"
                payload[field] = start
            seconds, problem = _pick_duration(row, _seconds(duration) if duration not in (None, "") else None, self.valves.max_video_seconds)
            if problem:
                return context.error(problem)
            if seconds is not None:
                payload["duration"] = seconds
            chosen = _pick_resolution(row, resolution)
            if chosen:
                payload["resolution"] = chosen
            elif resolution:
                notes.append(f"Resolution {resolution} is not offered by {model_id}; the model's default was used.")
            ratio = _pick_choice(row, "aspect_ratio", aspect_ratio)
            if ratio:
                payload["aspect_ratio"] = ratio
            elif aspect_ratio and _param(row, "aspect_ratio") is not None:
                notes.append(f"Aspect ratio {aspect_ratio} is not offered by {model_id}; the model's default was used.")
            if audio is not None:
                switch = next((name for name in ("generate_audio", "generate_audio_switch") if _param(row, name) is not None), None)
                if switch:
                    payload[switch] = bool(audio)
                elif audio:
                    notes.append(f"{model_id} does not add a sound track.")

            label = _display(row, model_id)
            await context.status(f"Creating video with {label}")
            submitted = await context.api("POST", "/videos/generations", key, payload)
            job_id = str(submitted.get("job_id") or "")
            if _first_output(submitted)[0]:
                job = submitted
            elif job_id:
                job = await context.wait_for_job(key, job_id, f"Creating video with {label}", self.valves.video_wait_minutes * 60)
            else:
                return context.error("The video request was accepted without a job ID. Try again.")
            return await context.show_video(job, model_id, label, job_id, notes)
        except JobStillRunning as exc:
            return await context.still_running(exc.job_id, "video")
        except ApiError as exc:
            message = exc.message
            if image_url and "url" in message.lower() and (exc.status in (400, 422) or exc.job_id):
                message += (
                    " The start image could not be read. Pass the image_id from create_image, or the public link "
                    "exactly as it was given; do not retry with a changed link."
                )
            return context.error(message, exc.job_id)

    async def check_video(
        self,
        job_id: str,
        __user__: Optional[dict] = None,
        __event_emitter__: EventEmitter = None,
        __request__: Any = None,
        __chat_id__: Optional[str] = None,
        __message_id__: Optional[str] = None,
    ) -> Any:
        """
        Check a video that was still being created and show it in the chat when it is ready.

        :param job_id: The job ID returned by create_video.
        """
        context = _Context(self, __user__, __event_emitter__, __request__, __chat_id__, __message_id__)
        try:
            key = context.api_key()
            job_id = (job_id or "").strip()
            if not job_id:
                return context.error("A job ID is required.")
            job = await context.api("GET", f"/jobs/{quote(job_id, safe='')}", key)
            status = str(job.get("status") or "").lower()
            if status in JOB_FAILED:
                return context.error(_job_error(job, status), job_id)
            if status not in JOB_DONE:
                return {"status": "processing", "job_id": job_id, "message": "The video is still being created. Check again in a minute."}
            model_id = str(job.get("model") or "")
            rows = await _catalog()
            row = _find(rows or [], model_id, _is_video_model) if model_id else None
            return await context.show_video(job, model_id, _display(row, model_id or "video"), job_id, [])
        except ApiError as exc:
            return context.error(exc.message, exc.job_id)

    async def create_image(
        self,
        prompt: str,
        aspect_ratio: str = "",
        model: str = "",
        __user__: Optional[dict] = None,
        __event_emitter__: EventEmitter = None,
        __request__: Any = None,
        __chat_id__: Optional[str] = None,
        __message_id__: Optional[str] = None,
    ) -> Any:
        """
        Create an image with EmpirioLabs AI from a text prompt. The image is shown in the chat.

        :param prompt: Description of the image: subject, setting, composition and style.
        :param aspect_ratio: Optional aspect ratio such as 1:1, 16:9 or 9:16. Leave empty for the model's default.
        :param model: Optional image model ID. Leave empty to use the configured model. list_media_models shows the choices.
        """
        context = _Context(self, __user__, __event_emitter__, __request__, __chat_id__, __message_id__)
        try:
            key = context.api_key()
            prompt = (prompt or "").strip()
            if not prompt:
                return context.error("A prompt is required.")
            rows = await _catalog()
            model_id, row = context.choose_model("image", model, rows)
            payload: dict[str, Any] = {"model": model_id, "prompt": prompt, "sync": True}
            if row is None or _param(row, "response_format") is not None:
                payload["response_format"] = "url"
            notes: list[str] = []
            ratio = _pick_choice(row, "aspect_ratio", aspect_ratio)
            if ratio:
                payload["aspect_ratio"] = ratio
            elif aspect_ratio:
                notes.append(f"Aspect ratio {aspect_ratio} is not offered by {model_id}; the model's default was used.")
            label = _display(row, model_id)
            await context.status(f"Creating image with {label}")
            result = await context.api("POST", "/images/generations", key, payload, timeout=REQUEST_TIMEOUT_SECONDS)
            urls = _all_outputs(result)
            job_id = str(result.get("job_id") or result.get("id") or "")
            if not urls and result.get("job_id"):
                result = await context.wait_for_job(key, str(result["job_id"]), f"Creating image with {label}", 600)
                urls = _all_outputs(result)
            if not urls:
                return context.error("The image request finished without an image. Try again.", job_id)
            shown = []
            for url in urls[:4]:
                entry = await context.keep_file(url, "image", model_id, MAX_IMAGE_BYTES, always=True)
                shown.append(entry or {"type": "image", "url": url})
            image_ids = [entry["id"] for entry in shown if entry.get("id")]
            await context.attach(shown)
            await context.status("Image ready", done=True)
            reply: dict[str, Any] = {
                "status": "success",
                "model": model_id,
                "source_url": urls[0],
                "message": "The image is already shown to the user in the chat; do not repeat it. To animate it, call create_video with image_url set to image_id (or source_url).",
            }
            if image_ids:
                reply["image_id"] = image_ids[0]
            if len(urls) > 1:
                reply["source_urls"] = urls[:4]
            if notes:
                reply["notes"] = notes
            return reply
        except JobStillRunning as exc:
            return await context.still_running(exc.job_id, "image")
        except ApiError as exc:
            return context.error(exc.message, exc.job_id)

    async def create_speech(
        self,
        text: str,
        voice: str = "",
        model: str = "",
        __user__: Optional[dict] = None,
        __event_emitter__: EventEmitter = None,
        __request__: Any = None,
        __chat_id__: Optional[str] = None,
        __message_id__: Optional[str] = None,
    ) -> Any:
        """
        Turn text into natural speech with EmpirioLabs AI. An audio player is shown in the chat.

        :param text: The exact text to speak.
        :param voice: Optional voice name. Leave empty for the configured or default voice. list_media_models shows each model's voices.
        :param model: Optional text-to-speech model ID. Leave empty to use the configured model.
        """
        context = _Context(self, __user__, __event_emitter__, __request__, __chat_id__, __message_id__)
        try:
            key = context.api_key()
            text = (text or "").strip()
            if not text:
                return context.error("Text is required.")
            rows = await _catalog()
            model_id, row = context.choose_model("speech", model, rows)
            limit = (_param(row, "input") or {}).get("max")
            if isinstance(limit, (int, float)) and len(text) > limit:
                return context.error(f"{model_id} reads up to {int(limit):,} characters per request. Shorten the text or split it.")
            payload: dict[str, Any] = {"model": model_id, "input": text}
            wanted_voice = (voice or "").strip() or context.user_value("voice")
            if wanted_voice:
                voices = _options(row, "voice")
                match = next((option for option in voices if option.lower() == wanted_voice.lower()), None)
                if match or row is None or (not voices and _param(row, "voice") is not None):
                    payload["voice"] = match or wanted_voice
                elif _param(row, "voice_id") is not None:
                    payload["voice_id"] = wanted_voice
                else:
                    listed = ", ".join(voices[:40])
                    return context.error(f"{model_id} has no voice named {wanted_voice}. Voices: {listed}.")
            mp3 = next((option for option in _options(row, "output_format") if option.lower() == "mp3"), None)
            if mp3:
                payload["output_format"] = mp3
            label = _display(row, model_id)
            await context.status(f"Creating speech with {label}")
            result = await context.api("POST", "/audio/speech", key, payload, timeout=REQUEST_TIMEOUT_SECONDS)
            url, item = _first_output(result)
            if not url and result.get("job_id"):
                result = await context.wait_for_job(key, str(result["job_id"]), f"Creating speech with {label}", 600)
                url, item = _first_output(result)
            if not url:
                return context.error("The speech request finished without audio. Try again.")
            seconds = _seconds(item.get("duration_seconds") or item.get("duration"))
            caption = f"{label} · {seconds:.1f} s" if seconds else label
            if self.valves.keep_copies:
                entry = await context.keep_file(url, "audio", model_id, min(MAX_AUDIO_BYTES, self.valves.max_copy_mb * 1024 * 1024))
                if entry:
                    await context.attach([entry])
            await context.status("Speech ready", done=True)
            return (
                HTMLResponse(_media_page("audio", url, caption), headers={"Content-Disposition": "inline"}),
                {
                    "status": "success",
                    "model": model_id,
                    "duration_seconds": seconds,
                    "url": url,
                    "message": "An audio player with the speech is already shown to the user in the chat; do not repeat the link unless asked.",
                },
            )
        except JobStillRunning as exc:
            return await context.still_running(exc.job_id, "speech")
        except ApiError as exc:
            return context.error(exc.message, exc.job_id)

    async def list_media_models(self, kind: str = "video") -> dict:
        """
        List the EmpirioLabs AI models this tool can use, from the live catalog, with their main options.

        :param kind: video, image or speech.
        """
        kind = (kind or "video").strip().lower()
        kind = {"tts": "speech", "audio": "speech", "voice": "speech", "images": "image", "videos": "video"}.get(kind, kind)
        if kind not in KINDS:
            return {"status": "error", "message": "kind must be video, image or speech."}
        rows = await _catalog()
        if rows is None:
            return {"status": "error", "message": f"The model catalog could not be read right now. See {MODELS_URL}."}
        models = []
        for row in rows:
            if not KINDS[kind](row):
                continue
            entry: dict[str, Any] = {"id": row["id"], "name": str(row.get("display_name") or row["id"])}
            if kind == "video":
                entry["start_image"] = _takes_start_image(row)
                duration = _param(row, "duration") or {}
                if _options(row, "duration"):
                    entry["durations"] = _options(row, "duration")
                elif duration:
                    entry["duration_seconds"] = {"min": duration.get("min"), "max": duration.get("max")}
            if kind in ("video", "image"):
                for name in ("resolution", "aspect_ratio"):
                    if _options(row, name):
                        entry[name + "s"] = _options(row, name)
            if kind == "speech":
                voices = _options(row, "voice")
                if voices:
                    entry["voices"] = voices[:60]
                if _param(row, "voice_id") is not None:
                    entry["more_voices"] = "Any voice ID from GET /v1/voices also works."
            models.append(entry)
        return {"kind": kind, "count": len(models), "models": models, "prices": MODELS_URL}


def _display(row: Optional[dict], model_id: str) -> str:
    return str((row or {}).get("display_name") or model_id)


def _job_error(job: dict, status: str) -> str:
    error = job.get("error")
    if isinstance(error, dict):
        message = str(error.get("message") or error.get("detail") or "").strip()
        if message:
            return message
    elif isinstance(error, str) and error.strip():
        return error.strip()
    return f"The job ended with status {status}."


class _Context:
    """Per-call state: the user's key, the chat to attach files to and the event emitter."""

    def __init__(self, tools: Tools, user: Optional[dict], emitter: EventEmitter, request: Any, chat_id: Optional[str], message_id: Optional[str]) -> None:
        self.tools = tools
        self.user = user or {}
        self.emitter = emitter
        self.request = request
        self.chat_id = chat_id or ""
        self.message_id = message_id or ""
        self._key = ""

    # -- settings ---------------------------------------------------------------

    def user_value(self, name: str) -> str:
        valves = self.user.get("valves")
        value = getattr(valves, name, None) if valves is not None else None
        if value is None and isinstance(valves, dict):
            value = valves.get(name)
        return str(value or "").strip()

    def api_key(self) -> str:
        self._key = self.user_value("api_key") or str(self.tools.valves.api_key or "").strip()
        if not self._key:
            raise ApiError(
                "Add your EmpirioLabs AI API key first. In the chat, open Integrations, then Tools, select the Valves "
                f"button next to EmpirioLabs AI Media and paste the key into API Key. Create a key at {KEYS_URL}."
            )
        return self._key

    def choose_model(self, kind: str, requested: str, rows: Optional[list]) -> tuple[str, Optional[dict]]:
        eligible = KINDS[kind]
        requested = (requested or "").strip()
        configured = [self.user_value(f"{kind}_model"), str(getattr(self.tools.valves, f"{kind}_model", "") or "").strip()]
        if rows is None:
            return next((value for value in (requested, *configured) if value), DEFAULTS[kind]), None
        if requested:
            row = _find(rows, requested, eligible)
            if row is None:
                choices = ", ".join(r["id"] for r in rows if eligible(r))[:1500]
                raise ApiError(f"{requested} is not an available {kind} model. Available: {choices}.")
            return row["id"], row
        for candidate in (*configured, DEFAULTS[kind]):
            row = _find(rows, candidate, eligible)
            if row is not None:
                return row["id"], row
        pool = [r for r in rows if eligible(r)]
        if not pool:
            raise ApiError(f"No {kind} model is available right now.")
        row = next((r for r in pool if r.get("is_featured")), pool[0])
        return row["id"], row

    async def start_image(self, value: str) -> Optional[str]:
        """A start image link for the API: an image this tool created (by its ID) or a public https link."""
        value = (value or "").strip()
        reference = _FILE_REF_RE.match(value)
        if reference:
            try:
                from open_webui.models.files import Files

                found = Files.get_file_by_id(reference.group(1))
                found = await found if inspect.isawaitable(found) else found
            except Exception:
                return None
            if found is None or getattr(found, "user_id", None) != self.user.get("id"):
                return None
            data = ((getattr(found, "meta", None) or {}).get("data") or {})
            link = data.get("empiriolabs_source_url") if isinstance(data, dict) else None
            return link if isinstance(link, str) and _download_target_ok(link) else None
        # Chat models sometimes turn a copied "--" into a typographic dash.
        for dash in ("\u2014", "\u2013", "%E2%80%94", "%E2%80%93", "%e2%80%94", "%e2%80%93"):
            value = value.replace(dash, "--")
        return value if _download_target_ok(value) else None

    # -- output to the user -------------------------------------------------------

    def scrub(self, text: str) -> str:
        return text.replace(self._key, "[key]") if self._key and isinstance(text, str) else text

    def error(self, message: str, job_id: str = "") -> dict:
        reply: dict[str, Any] = {"status": "error", "message": self.scrub(message)}
        if job_id:
            reply["job_id"] = job_id
        return reply

    async def status(self, description: str, done: bool = False) -> None:
        if self.emitter:
            try:
                await self.emitter({"type": "status", "data": {"description": self.scrub(description), "done": done, "hidden": False}})
            except Exception:
                log.debug("status event failed")

    async def still_running(self, job_id: str, kind: str) -> dict:
        await self.status("Still in progress", done=True)
        follow_up = (
            "Call check_video with this job ID in a few minutes to show it."
            if kind == "video"
            else "Try again in a few minutes."
        )
        return {
            "status": "processing",
            "job_id": job_id,
            "message": f"The {kind} is still being created and keeps running. {follow_up}",
        }

    async def show_video(self, job: dict, model_id: str, label: str, job_id: str, notes: list[str]) -> Any:
        url, item = _first_output(job)
        if not url:
            raise ApiError("The video finished without a video file. Try again.", job_id=job_id)
        seconds = _seconds(item.get("duration") or item.get("duration_seconds"))
        caption = " · ".join(part for part in (label, f"{seconds:g} s" if seconds else "") if part)
        if self.tools.valves.keep_copies:
            entry = await self.keep_file(url, "video", model_id, self.tools.valves.max_copy_mb * 1024 * 1024)
            if entry:
                await self.attach([entry])
        await self.status("Video ready", done=True)
        context: dict[str, Any] = {
            "status": "success",
            "model": model_id,
            "job_id": job_id,
            "url": url,
            "message": "The video is already playing in the chat; do not repeat the link unless asked.",
        }
        if notes:
            context["notes"] = notes
        return HTMLResponse(_media_page("video", url, caption), headers={"Content-Disposition": "inline"}), context

    # -- API calls --------------------------------------------------------------

    async def api(self, method: str, path: str, key: str, payload: Optional[dict] = None, timeout: float = 120.0) -> dict:
        headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
        try:
            async with _client(timeout) as client:
                response = await client.request(method, API_BASE + path, json=payload, headers=headers)
        except httpx.TimeoutException:
            raise ApiError(f"{DISPLAY_NAME} did not answer in time. Try again.") from None
        except httpx.HTTPError as exc:
            raise ApiError(f"Could not reach {DISPLAY_NAME} ({type(exc).__name__}). Try again.") from None
        if response.status_code >= 400:
            detail = self.scrub(_error_text(response))
            if response.status_code in (401, 403):
                message = "EmpirioLabs AI did not accept the API key. Check the key in the tool's Valves."
                if detail:
                    message = f"{message} ({detail})"
            elif response.status_code == 429:
                message = detail or "Too many requests right now. Try again in a moment."
            else:
                message = detail or f"The request failed with HTTP {response.status_code}."
            raise ApiError(message, status=response.status_code)
        try:
            data = response.json()
        except ValueError:
            raise ApiError(f"{DISPLAY_NAME} returned an unexpected response.", status=response.status_code) from None
        if not isinstance(data, dict):
            raise ApiError(f"{DISPLAY_NAME} returned an unexpected response.", status=response.status_code)
        return data

    async def wait_for_job(self, key: str, job_id: str, label: str, limit_seconds: float) -> dict:
        started = _now()
        last_status_at = started
        last_text = ""
        retries = 0
        while True:
            try:
                job = await self.api("GET", f"/jobs/{quote(job_id, safe='')}", key, timeout=60.0)
                retries = 0
            except ApiError as exc:
                if (exc.status in RETRYABLE_STATUS or exc.status == 0) and retries < MAX_POLL_RETRIES:
                    retries += 1
                    await _sleep(POLL_SECONDS)
                    continue
                exc.job_id = job_id
                raise
            state = str(job.get("status") or "").strip().lower()
            if state in JOB_DONE:
                return job
            if state in JOB_FAILED:
                raise ApiError(_job_error(job, state), job_id=job_id)
            if state not in JOB_RUNNING:
                raise ApiError(f"The job returned an unexpected status: {state or 'none'}.", job_id=job_id)
            elapsed = _now() - started
            if elapsed >= limit_seconds:
                raise JobStillRunning("still running", job_id=job_id)
            progress = job.get("progress")
            if isinstance(progress, (int, float)) and not isinstance(progress, bool) and progress > 0:
                percent = progress * 100 if progress <= 1 else progress
                text = f"{label}: {min(int(percent), 99)}%"
            else:
                minutes, seconds = divmod(int(elapsed), 60)
                text = f"{label}: {'queued' if state in ('queued', 'pending') else 'in progress'}, {minutes}:{seconds:02d}"
            now = _now()
            if text != last_text and now - last_status_at >= STATUS_EVERY_SECONDS:
                await self.status(text)
                last_text, last_status_at = text, now
            await _sleep(POLL_SECONDS)

    async def download(self, url: str, max_bytes: int) -> tuple[bytes, str]:
        """Fetch a generated file from a link the API returned. The API key is never sent."""
        target = url
        for _ in range(4):
            if not _download_target_ok(target):
                raise ApiError("A generated file link was not a public https link.")
            async with _client(120.0) as client:
                async with client.stream("GET", target) as response:
                    if response.status_code in (301, 302, 303, 307, 308) and response.headers.get("location"):
                        target = urljoin(target, response.headers["location"])
                        continue
                    if response.status_code != 200:
                        raise ApiError(f"The generated file could not be downloaded (HTTP {response.status_code}).")
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > max_bytes:
                        raise ApiError("The generated file is larger than the copy limit.")
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise ApiError("The generated file is larger than the copy limit.")
                        chunks.append(chunk)
                    return b"".join(chunks), response.headers.get("content-type", "")
        raise ApiError("The generated file link redirected too many times.")

    # -- Open WebUI storage ------------------------------------------------------

    async def keep_file(self, url: str, kind: str, model_id: str, max_bytes: int, always: bool = False) -> Optional[dict]:
        """Store a copy of a generated file in Open WebUI. Returns the message file entry, or None."""
        if not (always or self.tools.valves.keep_copies) or self.request is None or not self.user.get("id"):
            return None
        try:
            data, content_type = await self.download(url, max_bytes)
        except ApiError as exc:
            log.info("%s copy skipped: %s", DISPLAY_NAME, exc.message)
            return None
        content_type = (content_type or "").split(";")[0].strip().lower()
        if not content_type.startswith(f"{kind}/"):
            content_type = {"image": "image/png", "video": "video/mp4", "audio": "audio/mpeg"}[kind]
        safe_model = re.sub(r"[^A-Za-z0-9._-]+", "-", model_id or kind)[:60]
        filename = f"empiriolabs-{safe_model}-{int(time.time())}.{_extension(content_type, url, {'image': 'png', 'video': 'mp4', 'audio': 'mp3'}[kind])}"
        try:
            from fastapi import UploadFile
            from open_webui.models.users import Users
            from open_webui.routers.files import upload_file_handler

            user = Users.get_user_by_id(self.user["id"])
            user = await user if inspect.isawaitable(user) else user
            if user is None:
                return None
            saved_chat = bool(self.chat_id) and not self.chat_id.startswith("local:")
            metadata = {"chat_id": self.chat_id, "message_id": self.message_id} if saved_chat and self.message_id else {}
            metadata.update({"empiriolabs_source_url": url, "empiriolabs_model": model_id})
            upload = UploadFile(file=io.BytesIO(data), filename=filename, headers={"content-type": content_type})
            item = upload_file_handler(self.request, file=upload, metadata=metadata, process=False, user=user)
            item = await item if inspect.isawaitable(item) else item
            file_id = getattr(item, "id", None)
            if not file_id:
                return None
            if saved_chat and self.message_id:
                try:
                    from open_webui.models.chats import Chats

                    linked = Chats.insert_chat_files(chat_id=self.chat_id, message_id=self.message_id, file_ids=[file_id], user_id=user.id)
                    if inspect.isawaitable(linked):
                        await linked
                except Exception:
                    log.debug("could not link the copy to the chat")
            file_url = str(self.request.app.url_path_for("get_file_content_by_id", id=file_id))
        except Exception as exc:
            log.info("%s copy not stored: %s", DISPLAY_NAME, type(exc).__name__)
            return None
        entry: dict[str, Any] = {
            "type": "image" if kind == "image" else "file",
            "id": file_id,
            "url": file_url,
            "name": filename,
            "content_type": content_type,
            "size": len(data),
        }
        if kind != "image":
            entry["meta"] = {"name": filename, "content_type": content_type, "size": len(data)}
        return entry

    async def attach(self, entries: list[dict]) -> None:
        """Add files to the assistant message, the way Open WebUI's own image tool does."""
        if not entries:
            return
        merged = None
        if self.chat_id and self.message_id and not self.chat_id.startswith("local:"):
            try:
                from open_webui.models.chats import Chats

                result = Chats.add_message_files_by_id_and_message_id(self.chat_id, self.message_id, entries)
                merged = await result if inspect.isawaitable(result) else result
            except Exception:
                merged = None
        if not self.emitter:
            return
        try:
            if isinstance(merged, list):
                await self.emitter({"type": "chat:message:files", "data": {"files": merged}})
            else:
                await self.emitter({"type": "files", "data": {"files": entries}})
        except Exception:
            log.debug("files event failed")
