"""Tests for Cloudflare Access headers on the ASR endpoint connector."""

import importlib.util
import io
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, rel: str, deps: dict | None = None):
    """Load a module file without executing package __init__ side effects."""
    path = ROOT / rel
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    if deps:
        for k, v in deps.items():
            sys.modules[k] = v
    spec.loader.exec_module(mod)
    return mod


# Minimal package shells so relative imports inside connectors resolve.
sys.modules.setdefault("src", types.ModuleType("src"))
sys.modules.setdefault("src.config", types.ModuleType("src.config"))
app_config = types.ModuleType("src.config.app_config")
app_config.ASR_ENABLE_CHUNKING = False
app_config.ASR_MAX_DURATION_SECONDS = 7200
sys.modules["src.config.app_config"] = app_config

sys.modules.setdefault("src.services", types.ModuleType("src.services"))
sys.modules.setdefault(
    "src.services.transcription", types.ModuleType("src.services.transcription")
)
exc = _load(
    "src.services.transcription.exceptions",
    "src/services/transcription/exceptions.py",
)
base = _load(
    "src.services.transcription.base",
    "src/services/transcription/base.py",
    deps={"src.services.transcription.exceptions": exc},
)
asr = _load(
    "src.services.transcription.connectors.asr_endpoint",
    "src/services/transcription/connectors/asr_endpoint.py",
    deps={
        "src.services.transcription.base": base,
        "src.services.transcription.exceptions": exc,
        "src.config.app_config": app_config,
    },
)

ASREndpointConnector = asr.ASREndpointConnector
TranscriptionRequest = base.TranscriptionRequest


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {
            "text": "hi",
            "segments": [{"text": "hi", "start": 0, "end": 1, "speaker": "SPEAKER_00"}],
        }
        self.text = text or (
            '{"text":"hi","segments":[{"text":"hi","start":0,"end":1,"speaker":"SPEAKER_00"}]}'
        )

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", "http://asr.test/asr")
            response = httpx.Response(self.status_code, request=request, text=self.text)
            raise httpx.HTTPStatusError(self.text, request=request, response=response)


class _Client:
    def __init__(self, response=None):
        self.response = response or _Response()
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def _request():
    return TranscriptionRequest(
        audio_file=io.BytesIO(b"RIFFxxxx"),
        filename="a.wav",
        mime_type="audio/wav",
        diarize=False,
    )


def test_asr_sends_cf_access_headers_when_env_set(monkeypatch):
    monkeypatch.setenv("CF_ACCESS_CLIENT_ID", "client-id.access")
    monkeypatch.setenv("CF_ACCESS_CLIENT_SECRET", "client-secret")
    client = _Client()
    connector = ASREndpointConnector({"base_url": "http://asr.test", "diarize": False})
    with patch.object(asr.httpx, "Client", return_value=client):
        connector.transcribe(_request())
    assert client.calls, "expected ASR POST"
    headers = client.calls[0][1].get("headers") or {}
    assert headers["CF-Access-Client-Id"] == "client-id.access"
    assert headers["CF-Access-Client-Secret"] == "client-secret"


def test_asr_prefers_asr_specific_cf_env(monkeypatch):
    monkeypatch.setenv("CF_ACCESS_CLIENT_ID", "shared-id")
    monkeypatch.setenv("CF_ACCESS_CLIENT_SECRET", "shared-secret")
    monkeypatch.setenv("ASR_CF_ACCESS_CLIENT_ID", "asr-id")
    monkeypatch.setenv("ASR_CF_ACCESS_CLIENT_SECRET", "asr-secret")
    client = _Client()
    connector = ASREndpointConnector({"base_url": "http://asr.test", "diarize": False})
    with patch.object(asr.httpx, "Client", return_value=client):
        connector.transcribe(_request())
    headers = client.calls[0][1].get("headers") or {}
    assert headers["CF-Access-Client-Id"] == "asr-id"
    assert headers["CF-Access-Client-Secret"] == "asr-secret"


def test_asr_omits_cf_access_headers_when_env_unset(monkeypatch):
    monkeypatch.delenv("CF_ACCESS_CLIENT_ID", raising=False)
    monkeypatch.delenv("CF_ACCESS_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("ASR_CF_ACCESS_CLIENT_ID", raising=False)
    monkeypatch.delenv("ASR_CF_ACCESS_CLIENT_SECRET", raising=False)
    client = _Client()
    connector = ASREndpointConnector({"base_url": "http://asr.test", "diarize": False})
    with patch.object(asr.httpx, "Client", return_value=client):
        connector.transcribe(_request())
    headers = client.calls[0][1].get("headers")
    assert not headers
