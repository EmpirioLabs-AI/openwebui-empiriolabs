"""Offline tests for the EmpirioLabs AI Media tool. The API is mocked; nothing reaches the network."""

import asyncio
import json
import sys
import types
from pathlib import Path

import httpx
import pytest
from fastapi.responses import HTMLResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import empiriolabs_media as tool  # noqa: E402

KEY = "sk-empiriolabs-test-key-0123456789"
MEDIA = "https://media.empiriolabs.ai/worker-outputs/2026/10/19/aa/file"


def _param(name, **extra):
    return {"name": name, **extra}


CATALOG = [
    {
        "id": "flux-2-klein-4b",
        "display_name": "FLUX.2 Klein 4B",
        "is_featured": True,
        "status": "available",
        "input_modalities": ["text", "image"],
        "supported_endpoints": ["POST /v1/images/generations"],
        "supported_parameters": [
            _param("prompt", required=True),
            _param("aspect_ratio", options=["1:1", "16:9", "9:16"]),
            _param("response_format", options=["url", "b64_json"]),
        ],
    },
    {
        "id": "needs-reference",
        "status": "available",
        "supported_endpoints": ["POST /v1/images/generations"],
        "supported_parameters": [_param("prompt", required=True), _param("image", required=True)],
    },
    {
        "id": "hunyuan-video-1-5",
        "display_name": "Hunyuan Video 1.5",
        "aliases": ["tencent/hunyuan-video-1.5"],
        "is_featured": True,
        "status": "available",
        "input_modalities": ["text", "image"],
        "supported_endpoints": ["POST /v1/videos/generations"],
        "supported_parameters": [
            _param("prompt", required=True),
            _param("duration", min=1, max=10, default=5),
            _param("resolution", options=["480p", "720p", "1080p"], default="480p"),
            _param("aspect_ratio", options=["16:9", "9:16", "1:1"]),
            _param("image"),
        ],
    },
    {
        "id": "pixverse-v6",
        "display_name": "Pixverse v6",
        "status": "available",
        "input_modalities": ["text", "image", "video"],
        "supported_endpoints": ["POST /v1/videos/generations"],
        "supported_parameters": [
            _param("prompt", required=True),
            _param("duration", min=1, max=15, default=5),
            _param("resolution", options=["360p", "540p", "720p", "1080p"], default="720p"),
            _param("generate_audio_switch", default=False),
            _param("image"),
        ],
    },
    {
        "id": "options-video",
        "status": "available",
        "input_modalities": ["text"],
        "supported_endpoints": ["POST /v1/videos/generations"],
        "supported_parameters": [_param("prompt", required=True), _param("duration", options=["5s", "8s"])],
    },
    {
        "id": "long-video",
        "status": "available",
        "input_modalities": ["text"],
        "supported_endpoints": ["POST /v1/videos/generations"],
        "supported_parameters": [_param("prompt"), _param("duration", min=18, max=120, default=18)],
    },
    {
        "id": "motion-control",
        "status": "available",
        "input_modalities": ["text", "image", "video"],
        "supported_endpoints": ["POST /v1/videos/generations"],
        "supported_parameters": [_param("prompt", required=True), _param("reference_video", required=True)],
    },
    {
        "id": "video-editor",
        "status": "available",
        "input_modalities": ["text", "video"],
        "features": ["video_editing", "styles"],
        "supported_endpoints": ["POST /v1/videos/generations"],
        "supported_parameters": [_param("prompt"), _param("video")],
    },
    {
        "id": "lip-sync",
        "status": "available",
        "input_modalities": ["text", "image", "audio"],
        "features": ["lipsync", "audio_in", "image_to_video"],
        "supported_endpoints": ["POST /v1/videos/generations"],
        "supported_parameters": [_param("prompt"), _param("image"), _param("audio")],
    },
    {
        "id": "no-prompt-video",
        "status": "available",
        "input_modalities": ["text", "image"],
        "supported_endpoints": ["POST /v1/videos/generations"],
        "supported_parameters": [_param("duration", min=18, max=120)],
    },
    {
        "id": "retired-video",
        "status": "deprecated",
        "input_modalities": ["text"],
        "supported_endpoints": ["POST /v1/videos/generations"],
        "supported_parameters": [_param("prompt", required=True)],
    },
    {
        "id": "tts-2-flash",
        "display_name": "TTS 2 Flash",
        "is_featured": True,
        "status": "available",
        "input_modalities": ["text"],
        "supported_endpoints": ["POST /v1/audio/speech", "GET /v1/voices"],
        "supported_parameters": [
            _param("input", required=True, max=2000),
            _param("voice", options=["Sarah", "Mark"], default="Sarah"),
            _param("voice_id"),
            _param("output_format", options=["MP3", "WAV"], default="WAV"),
        ],
    },
    {
        "id": "plain-tts",
        "status": "available",
        "supported_endpoints": ["POST /v1/audio/speech"],
        "supported_parameters": [_param("input", required=True), _param("voice", options=["Alpha", "Beta"])],
    },
    {
        "id": "music-model",
        "status": "available",
        "supported_endpoints": ["POST /v1/audio/speech"],
        "supported_parameters": [_param("prompt", required=True)],
    },
]


class FakeApi:
    """Routes httpx requests to canned responses and records what was sent."""

    def __init__(self):
        self.requests = []
        self.job_polls = {}
        self.job_plan = {}
        self.responses = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append({"method": request.method, "url": str(request.url), "headers": dict(request.headers), "json": body})
        url = str(request.url)
        if url == tool.API_BASE + "/models":
            return httpx.Response(200, json={"data": CATALOG})
        if url.startswith(MEDIA):
            kind = url.rsplit(".", 1)[-1].split("?")[0]
            content_type = {"mp4": "video/mp4", "webp": "image/webp", "mp3": "audio/mpeg"}.get(kind, "application/octet-stream")
            return httpx.Response(200, content=b"bytes-" + kind.encode(), headers={"content-type": content_type})
        key = (request.method, url.replace(tool.API_BASE, ""))
        if key in self.responses:
            status, payload = self.responses[key]
            return httpx.Response(status, json=payload)
        if request.method == "GET" and url.startswith(tool.API_BASE + "/jobs/"):
            job_id = url.rsplit("/", 1)[-1]
            plan = self.job_plan.get(job_id, [])
            count = self.job_polls.get(job_id, 0)
            self.job_polls[job_id] = count + 1
            status, payload = plan[min(count, len(plan) - 1)]
            return httpx.Response(status, json=payload)
        return httpx.Response(404, json={"error": {"message": "not found"}})

    def sent(self, method, path):
        return [r for r in self.requests if r["method"] == method and r["url"] == tool.API_BASE + path]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


@pytest.fixture()
def api(monkeypatch):
    fake = FakeApi()
    clock = Clock()
    monkeypatch.setattr(tool, "_TRANSPORT", httpx.MockTransport(fake.handler))
    monkeypatch.setattr(tool, "_now", clock)
    monkeypatch.setattr(tool, "_sleep", clock.sleep)
    monkeypatch.setattr(tool, "_catalog_rows", None)
    monkeypatch.setattr(tool, "_catalog_at", 0.0)
    monkeypatch.setattr(tool, "_catalog_failed_at", 0.0)
    fake.clock = clock
    return fake


class Events:
    def __init__(self):
        self.items = []

    async def __call__(self, event):
        self.items.append(event)

    def statuses(self):
        return [e["data"]["description"] for e in self.items if e["type"] == "status"]


def user(key=KEY, **valves):
    values = {"api_key": key, "image_model": "", "video_model": "", "speech_model": "", "voice": "", **valves}
    return {"id": "user-1", "valves": types.SimpleNamespace(**values)}


def run(coro):
    return asyncio.run(coro)


# -- catalog --------------------------------------------------------------------


def test_catalog_kinds():
    ids = lambda kind: [row["id"] for row in CATALOG if tool.KINDS[kind](row)]  # noqa: E731
    assert ids("image") == ["flux-2-klein-4b"]
    assert ids("video") == ["hunyuan-video-1-5", "pixverse-v6", "options-video", "long-video"]
    assert ids("speech") == ["tts-2-flash", "plain-tts"]


def test_valve_options_come_from_the_catalog(api):
    tool._remember_catalog(CATALOG)
    admin = tool.Tools.Valves.video_model_options()
    personal = tool.Tools.UserValves.video_model_options()
    assert [o["value"] for o in admin] == ["hunyuan-video-1-5", "long-video", "options-video", "pixverse-v6"]
    assert personal[0] == {"value": "", "label": "Default"}
    assert {"value": "pixverse-v6", "label": "Pixverse v6 (pixverse-v6)"} in admin


def test_duration_choice():
    rows = {row["id"]: row for row in CATALOG}
    assert tool._pick_duration(rows["pixverse-v6"], 1, 10) == (1, "")
    assert tool._pick_duration(rows["pixverse-v6"], 30, 10) == (10, "")
    assert tool._pick_duration(rows["pixverse-v6"], None, 3) == (3, "")
    assert tool._pick_duration(rows["options-video"], 6, 10) == ("5s", "")
    assert tool._pick_duration(rows["options-video"], 9, 10) == ("8s", "")
    value, problem = tool._pick_duration(rows["long-video"], None, 10)
    assert value is None and "18 seconds" in problem


def test_resolution_choice():
    row = next(r for r in CATALOG if r["id"] == "pixverse-v6")
    assert tool._pick_resolution(row, "720P") == "720p"
    assert tool._pick_resolution(row, "480p") == "360p"
    assert tool._pick_resolution(row, "240p") == "360p"
    assert tool._pick_resolution(row, "4k") == "1080p"
    assert tool._pick_resolution(row, "") is None


def test_download_targets():
    assert tool._download_target_ok("https://media.empiriolabs.ai/a.mp4")
    for bad in ("http://media.empiriolabs.ai/a.mp4", "https://localhost/a", "https://127.0.0.1/a", "https://10.0.0.5/a", "ftp://x/a", "/api/v1/files/1/content"):
        assert not tool._download_target_ok(bad), bad


# -- video ------------------------------------------------------------------------


def _video_plan(api, job_id, url=MEDIA + ".mp4"):
    api.responses[("POST", "/videos/generations")] = (202, {"job_id": job_id, "status": "processing", "poll_url": f"/v1/jobs/{job_id}"})
    api.job_plan[job_id] = [
        (200, {"job_id": job_id, "status": "queued"}),
        (200, {"job_id": job_id, "status": "processing"}),
        (200, {"job_id": job_id, "status": "processing"}),
        (200, {"job_id": job_id, "status": "processing"}),
        (200, {"job_id": job_id, "status": "completed", "result": {"data": [{"url": url, "duration": 1}]}}),
    ]


def test_text_to_video(api):
    _video_plan(api, "job-1")
    tools = tool.Tools()
    tools.valves.video_model = "pixverse-v6"
    events = Events()
    result = run(tools.create_video("a paper boat", duration=1, resolution="360p", aspect_ratio="16:9", audio=False, __user__=user(), __event_emitter__=events))
    page, context = result
    assert isinstance(page, HTMLResponse) and page.headers["content-disposition"] == "inline"
    body = page.body.decode()
    assert "<video" in body and "iframe:height" in body and "&amp;" not in MEDIA
    assert context["status"] == "success" and context["model"] == "pixverse-v6" and context["url"] == MEDIA + ".mp4"
    sent = api.sent("POST", "/videos/generations")[0]
    assert sent["json"] == {"model": "pixverse-v6", "prompt": "a paper boat", "duration": 1, "resolution": "360p", "generate_audio_switch": False}
    assert sent["headers"]["authorization"] == f"Bearer {KEY}"
    statuses = events.statuses()
    assert statuses[0] == "Creating video with Pixverse v6" and statuses[-1] == "Video ready"
    assert any("in progress" in s for s in statuses)
    assert events.items[-1]["data"]["done"] is True


def test_image_to_video_uses_the_image_field(api):
    _video_plan(api, "job-2")
    result = run(tool.Tools().create_video("waves", image_url="https://example.com/start.png", __user__=user()))
    assert isinstance(result, tuple)
    sent = api.sent("POST", "/videos/generations")[0]["json"]
    assert sent["image"] == "https://example.com/start.png" and sent["model"] == "hunyuan-video-1-5"
    assert sent["duration"] == 5


def test_image_to_video_rejects_unsafe_links_and_text_only_models(api):
    tools = tool.Tools()
    bad = run(tools.create_video("x", image_url="http://example.com/a.png", __user__=user()))
    assert bad["status"] == "error" and "https" in bad["message"]
    no_image = run(tools.create_video("x", image_url="https://example.com/a.png", model="options-video", __user__=user()))
    assert no_image["status"] == "error" and "start image" in no_image["message"]
    assert not api.sent("POST", "/videos/generations")


def test_unknown_model_is_an_error_not_a_fallback(api):
    result = run(tool.Tools().create_video("x", model="no-such-model", __user__=user()))
    assert result["status"] == "error" and "no-such-model" in result["message"] and "pixverse-v6" in result["message"]
    assert not api.sent("POST", "/videos/generations")


def test_alias_resolves_to_the_catalog_id(api):
    _video_plan(api, "job-3")
    run(tool.Tools().create_video("x", model="tencent/hunyuan-video-1.5", __user__=user()))
    assert api.sent("POST", "/videos/generations")[0]["json"]["model"] == "hunyuan-video-1-5"


def test_video_over_the_wait_limit_hands_back_the_job(api):
    api.responses[("POST", "/videos/generations")] = (202, {"job_id": "slow", "status": "processing"})
    api.job_plan["slow"] = [(200, {"job_id": "slow", "status": "processing"})]
    tools = tool.Tools()
    tools.valves.video_wait_minutes = 1
    result = run(tools.create_video("x", __user__=user()))
    assert result["status"] == "processing" and result["job_id"] == "slow"
    api.job_plan["slow"] = [(200, {"job_id": "slow", "status": "completed", "model": "hunyuan-video-1-5", "result": {"data": [{"url": MEDIA + ".mp4"}]}})]
    api.job_polls["slow"] = 0
    later = run(tools.check_video("slow", __user__=user()))
    assert isinstance(later, tuple) and later[1]["model"] == "hunyuan-video-1-5"


def test_failed_job_reports_the_api_message(api):
    api.responses[("POST", "/videos/generations")] = (202, {"job_id": "bad", "status": "processing"})
    api.job_plan["bad"] = [(200, {"job_id": "bad", "status": "failed", "error": {"message": "This request violates our content policy."}})]
    result = run(tool.Tools().create_video("x", __user__=user()))
    assert result == {"status": "error", "message": "This request violates our content policy.", "job_id": "bad"}


def test_poll_retries_transient_errors(api):
    api.responses[("POST", "/videos/generations")] = (202, {"job_id": "flaky", "status": "processing"})
    api.job_plan["flaky"] = [
        (503, {"error": {"message": "busy"}}),
        (200, {"job_id": "flaky", "status": "completed", "result": {"data": [{"url": MEDIA + ".mp4"}]}}),
    ]
    assert isinstance(run(tool.Tools().create_video("x", __user__=user())), tuple)


# -- keys ---------------------------------------------------------------------------


def test_missing_key_explains_where_to_add_it(api):
    result = run(tool.Tools().create_image("x", __user__=user(key="")))
    assert result["status"] == "error" and "Valves" in result["message"] and tool.KEYS_URL in result["message"]
    assert not api.requests


def test_shared_key_is_used_when_the_user_has_none(api):
    api.responses[("POST", "/images/generations")] = (200, {"data": [{"url": MEDIA + ".webp"}]})
    tools = tool.Tools()
    tools.valves.api_key = "sk-empiriolabs-shared"
    run(tools.create_image("x", __user__=user(key="")))
    assert api.sent("POST", "/images/generations")[0]["headers"]["authorization"] == "Bearer sk-empiriolabs-shared"


def test_key_is_never_echoed(api):
    api.responses[("POST", "/images/generations")] = (401, {"error": {"message": f"Invalid API key {KEY}"}})
    result = run(tool.Tools().create_image("x", __user__=user()))
    assert KEY not in json.dumps(result) and "Valves" in result["message"]


def test_downloads_never_carry_the_key(api, monkeypatch):
    _install_open_webui(monkeypatch)
    api.responses[("POST", "/images/generations")] = (200, {"data": [{"url": MEDIA + ".webp"}]})
    run(tool.Tools().create_image("x", __user__=user(), __request__=FakeRequest(), __chat_id__="chat-1", __message_id__="msg-1"))
    downloads = [r for r in api.requests if r["url"].startswith(MEDIA)]
    assert downloads and all("authorization" not in r["headers"] for r in downloads)


# -- images -------------------------------------------------------------------------


class FakeRequest:
    class app:  # noqa: N801
        @staticmethod
        def url_path_for(name, id):
            return f"/api/v1/files/{id}/content"


def _install_open_webui(monkeypatch, fail_upload=False):
    stored = {"uploads": [], "message_files": []}

    class Users:
        @staticmethod
        async def get_user_by_id(user_id):
            return types.SimpleNamespace(id=user_id)

    async def upload_file_handler(request, file, metadata, process, user):
        if fail_upload:
            raise RuntimeError("storage down")
        stored["uploads"].append({"name": file.filename, "type": file.content_type, "metadata": metadata, "data": file.file.read()})
        return types.SimpleNamespace(id=f"file-{len(stored['uploads'])}")

    class Chats:
        @staticmethod
        async def insert_chat_files(chat_id, message_id, file_ids, user_id):
            return None

        @staticmethod
        async def add_message_files_by_id_and_message_id(chat_id, message_id, files):
            stored["message_files"].extend(files)
            return list(stored["message_files"])

    class Files:
        @staticmethod
        async def get_file_by_id(file_id):
            return stored.get("files", {}).get(file_id)

    modules = {
        "open_webui": types.ModuleType("open_webui"),
        "open_webui.models": types.ModuleType("open_webui.models"),
        "open_webui.models.users": types.SimpleNamespace(Users=Users),
        "open_webui.models.chats": types.SimpleNamespace(Chats=Chats),
        "open_webui.models.files": types.SimpleNamespace(Files=Files),
        "open_webui.routers": types.ModuleType("open_webui.routers"),
        "open_webui.routers.files": types.SimpleNamespace(upload_file_handler=upload_file_handler),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    return stored


def test_image_is_stored_and_attached_like_open_webui_images(api, monkeypatch):
    stored = _install_open_webui(monkeypatch)
    api.responses[("POST", "/images/generations")] = (200, {"created": 1, "data": [{"url": MEDIA + ".webp"}]})
    events = Events()
    result = run(tool.Tools().create_image("a lighthouse", aspect_ratio="1:1", __user__=user(), __event_emitter__=events, __request__=FakeRequest(), __chat_id__="chat-1", __message_id__="msg-1"))
    assert result["status"] == "success" and result["source_url"] == MEDIA + ".webp"
    sent = api.sent("POST", "/images/generations")[0]["json"]
    assert sent == {"model": "flux-2-klein-4b", "prompt": "a lighthouse", "sync": True, "response_format": "url", "aspect_ratio": "1:1"}
    assert stored["uploads"][0]["data"] == b"bytes-webp"
    assert stored["uploads"][0]["metadata"] == {
        "chat_id": "chat-1", "message_id": "msg-1",
        "empiriolabs_source_url": MEDIA + ".webp", "empiriolabs_model": "flux-2-klein-4b",
    }
    assert result["image_id"] == "file-1"
    files_event = next(e for e in events.items if e["type"] == "chat:message:files")
    assert files_event["data"]["files"][0]["url"] == "/api/v1/files/file-1/content"
    assert files_event["data"]["files"][0]["type"] == "image"


def test_image_falls_back_to_the_link_when_storage_fails(api, monkeypatch):
    _install_open_webui(monkeypatch, fail_upload=True)
    api.responses[("POST", "/images/generations")] = (200, {"data": [{"url": MEDIA + ".webp"}]})
    events = Events()
    run(tool.Tools().create_image("x", __user__=user(), __event_emitter__=events, __request__=FakeRequest(), __chat_id__="chat-1", __message_id__="msg-1"))
    files = next(e for e in events.items if e["type"] in ("files", "chat:message:files"))["data"]["files"]
    assert files[-1] == {"type": "image", "url": MEDIA + ".webp"} or files[0] == {"type": "image", "url": MEDIA + ".webp"}


def test_image_follows_the_job_when_the_wait_runs_out(api):
    api.responses[("POST", "/images/generations")] = (200, {"job_id": "img-job", "status": "processing", "poll_url": "/v1/jobs/img-job"})
    api.job_plan["img-job"] = [(200, {"job_id": "img-job", "status": "completed", "result": {"data": [{"url": MEDIA + ".webp"}]}})]
    result = run(tool.Tools().create_image("x", __user__=user()))
    assert result["status"] == "success" and result["source_url"] == MEDIA + ".webp"


def test_unsupported_aspect_ratio_is_noted(api):
    api.responses[("POST", "/images/generations")] = (200, {"data": [{"url": MEDIA + ".webp"}]})
    result = run(tool.Tools().create_image("x", aspect_ratio="21:9", __user__=user()))
    assert "aspect_ratio" not in api.sent("POST", "/images/generations")[0]["json"]
    assert "21:9" in result["notes"][0]


# -- speech --------------------------------------------------------------------------


def test_speech(api):
    api.responses[("POST", "/audio/speech")] = (200, {"data": [{"url": MEDIA + ".mp3", "format": "mp3", "duration_seconds": 2.9}]})
    page, context = run(tool.Tools().create_speech("Hello there.", voice="mark", __user__=user()))
    assert "<audio" in page.body.decode() and context["url"] == MEDIA + ".mp3"
    sent = api.sent("POST", "/audio/speech")[0]["json"]
    assert sent == {"model": "tts-2-flash", "input": "Hello there.", "voice": "Mark", "output_format": "MP3"}


def test_speech_voice_rules(api):
    api.responses[("POST", "/audio/speech")] = (200, {"data": [{"url": MEDIA + ".mp3"}]})
    tools = tool.Tools()
    run(tools.create_speech("Hi", voice="Ashley", __user__=user()))
    assert api.sent("POST", "/audio/speech")[0]["json"]["voice_id"] == "Ashley"
    missing = run(tools.create_speech("Hi", voice="Gamma", model="plain-tts", __user__=user()))
    assert missing["status"] == "error" and "Alpha, Beta" in missing["message"]
    personal = run(tools.create_speech("Hi", __user__=user(voice="sarah")))
    assert api.sent("POST", "/audio/speech")[-1]["json"]["voice"] == "Sarah" and isinstance(personal, tuple)


def test_speech_length_limit(api):
    result = run(tool.Tools().create_speech("x" * 2001, __user__=user()))
    assert result["status"] == "error" and "2,000" in result["message"]


# -- listing ---------------------------------------------------------------------------


def test_list_media_models(api):
    video = run(tool.Tools().list_media_models("video"))
    assert video["count"] == 4 and video["models"][0]["id"] == "hunyuan-video-1-5"
    assert video["models"][0]["start_image"] is True and video["models"][0]["resolutions"] == ["480p", "720p", "1080p"]
    speech = run(tool.Tools().list_media_models("tts"))
    assert speech["models"][0]["voices"] == ["Sarah", "Mark"] and "more_voices" in speech["models"][0]
    assert run(tool.Tools().list_media_models("3d"))["status"] == "error"


def test_media_page_escapes_the_link():
    page = tool._media_page("video", 'https://media.empiriolabs.ai/a.mp4?token=x"><script>&expires=1793046501', "Model")
    assert '"><script>' not in page and "&quot;&gt;&lt;script&gt;" in page
    assert "Link available until" in page


IMAGE_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"


def test_video_from_a_created_image_id(api, monkeypatch):
    stored = _install_open_webui(monkeypatch)
    stored["files"] = {
        IMAGE_ID: types.SimpleNamespace(user_id="user-1", meta={"data": {"empiriolabs_source_url": MEDIA + ".webp"}}),
    }
    _video_plan(api, "job-ref")
    for reference in (IMAGE_ID, f"/api/v1/files/{IMAGE_ID}/content"):
        assert isinstance(run(tool.Tools().create_video("waves", image_url=reference, __user__=user())), tuple)
    assert [r["json"]["image"] for r in api.sent("POST", "/videos/generations")] == [MEDIA + ".webp"] * 2


def test_another_users_image_id_is_refused(api, monkeypatch):
    stored = _install_open_webui(monkeypatch)
    stored["files"] = {IMAGE_ID: types.SimpleNamespace(user_id="someone-else", meta={"data": {"empiriolabs_source_url": MEDIA + ".webp"}})}
    result = run(tool.Tools().create_video("waves", image_url=IMAGE_ID, __user__=user()))
    assert result["status"] == "error" and not api.sent("POST", "/videos/generations")


def test_typographic_dashes_in_a_copied_link_are_repaired(api):
    _video_plan(api, "job-dash")
    run(tool.Tools().create_video("waves", image_url="https://media.empiriolabs.ai/a\u2014b.webp?token=x%E2%80%93y", __user__=user()))
    assert api.sent("POST", "/videos/generations")[0]["json"]["image"] == "https://media.empiriolabs.ai/a--b.webp?token=x--y"


def test_unreadable_start_image_explains_what_to_pass(api):
    api.responses[("POST", "/videos/generations")] = (400, {"error": {"message": "A supplied media URL could not be downloaded"}})
    result = run(tool.Tools().create_video("waves", image_url="https://example.com/missing.png", __user__=user()))
    assert result["status"] == "error" and "image_id" in result["message"] and "do not retry" in result["message"]


def test_a_retired_default_falls_back_to_a_featured_model(api):
    _video_plan(api, "job-fallback")
    tools = tool.Tools()
    tools.valves.video_model = "retired-video"
    run(tools.create_video("x", __user__=user()))
    assert api.sent("POST", "/videos/generations")[0]["json"]["model"] == "hunyuan-video-1-5"


def test_a_personal_model_choice_wins_over_the_admin_default(api):
    _video_plan(api, "job-personal")
    tools = tool.Tools()
    tools.valves.video_model = "hunyuan-video-1-5"
    run(tools.create_video("x", __user__=user(video_model="pixverse-v6")))
    assert api.sent("POST", "/videos/generations")[0]["json"]["model"] == "pixverse-v6"


def test_catalog_outage_still_sends_the_configured_model(api, monkeypatch):
    def offline(request):
        if request.url.path.endswith("/models"):
            raise httpx.ConnectError("offline")
        return api.handler(request)

    monkeypatch.setattr(tool, "_TRANSPORT", httpx.MockTransport(offline))
    _video_plan(api, "job-offline")
    result = run(tool.Tools().create_video("x", duration=30, __user__=user()))
    assert isinstance(result, tuple)
    assert api.sent("POST", "/videos/generations")[0]["json"] == {"model": "hunyuan-video-1-5", "prompt": "x", "duration": 10}
