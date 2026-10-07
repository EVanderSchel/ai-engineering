"""The HTTP API with a fake Claude client: every response it can give, no API calls, no downloads."""

import io

import pymupdf
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from test_checks import gold_answers
from test_extract import FakeClient, reply

import api
import extract
import rate_limit


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for name in ("IRS990_API_KEY", "IRS990_RATE_LIMIT_PER_MINUTE"):
        monkeypatch.delenv(name, raising=False)
    rate_limit.reset()
    yield
    rate_limit.reset()


@pytest.fixture
def good():
    return gold_answers()[0]


@pytest.fixture
def http():
    return TestClient(api.app)


def pdf_bytes(pages: int = 2) -> bytes:
    with pymupdf.open() as doc:
        for number in range(1, pages + 1):
            doc.new_page(width=612, height=792).insert_text((72, 72), f"Form 990 page {number}")
        return doc.tobytes()


def png_bytes(size=(3000, 4000)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, "PNG")
    return buffer.getvalue()


def upload(data: bytes, name: str = "return.pdf", content_type: str = "application/pdf"):
    return {"file": (name, data, content_type)}


def use(monkeypatch, *replies) -> FakeClient:
    fake = FakeClient(*replies)
    monkeypatch.setattr(api, "client", fake)
    return fake


def test_health_says_whether_extraction_is_configured(http, monkeypatch):
    monkeypatch.setattr(api, "client", None)
    body = http.get("/health").json()
    assert body["status"] == "ok" and body["extraction_enabled"] is False and body["auth_required"] is False


def test_a_pdf_comes_back_as_part_i_with_no_review_needed(http, monkeypatch, good):
    fake = use(monkeypatch, reply(good))
    response = http.post("/extract", files=upload(pdf_bytes()))
    assert response.status_code == 200
    body = response.json()
    assert body["fields"] == good and body["problems"] == [] and body["review"] == {"needed": False, "reasons": []}
    assert body["model"] == extract.MODEL and body["attempts"] == 1 and body["cost_usd"] > 0
    image = fake.requests[0]["messages"][0]["content"][0]
    assert image["type"] == "image" and image["source"]["media_type"] == "image/png"


def test_an_image_upload_is_scaled_to_the_usual_size(http, monkeypatch, good):
    fake = use(monkeypatch, reply(good))
    assert http.post("/extract", files=upload(png_bytes(), "page1.png", "image/png")).status_code == 200
    data = fake.requests[0]["messages"][0]["content"][0]["source"]["data"]
    sent = pymupdf.Pixmap(extract.base64.standard_b64decode(data))
    assert max(sent.width, sent.height) == api.LONG_EDGE_PX


def test_answers_that_need_a_person_say_so(http, monkeypatch, good):
    misread = good | {"total_revenue_current_year": (good["total_revenue_current_year"] or 0) + 1}
    use(monkeypatch, reply(misread), reply(misread))  # wrong twice: the retry doesn't fix it
    body = http.post("/extract", files=upload(pdf_bytes())).json()
    assert body["problems"] and body["attempts"] == 2
    assert body["review"]["needed"] and "the form's arithmetic doesn't add up" in body["review"]["reasons"]


def test_a_long_think_flags_the_page_as_hard(http, monkeypatch, good):
    use(monkeypatch, reply(good, output_tokens=5000))
    body = http.post("/extract", files=upload(pdf_bytes())).json()
    assert body["review"]["needed"] and "hard page" in body["review"]["reasons"][0]


@pytest.mark.parametrize(
    ("data", "params", "status"),
    [
        (b"hello, not a return", {}, 415),
        (b"%PDF-1.7 but then nothing a PDF needs", {}, 422),
        (pdf_bytes(2), {"page": 3}, 422),
        (png_bytes(), {"page": 2}, 422),
    ],
    ids=["not-a-return", "broken-pdf", "page-missing", "image-page-2"],
)
def test_unusable_uploads_are_rejected_before_any_claude_call(http, monkeypatch, good, data, params, status):
    fake = use(monkeypatch, reply(good))
    response = http.post("/extract", files=upload(data), params=params)
    assert response.status_code == status and fake.requests == []


def test_an_upload_over_the_size_limit_is_rejected(http, monkeypatch, good):
    fake = use(monkeypatch, reply(good))
    monkeypatch.setattr(api, "MAX_UPLOAD_BYTES", 1000)
    assert http.post("/extract", files=upload(pdf_bytes())).status_code == 413 and fake.requests == []


def test_no_anthropic_key_on_the_server_is_a_503(http, monkeypatch):
    monkeypatch.setattr(api, "client", None)
    assert http.post("/extract", files=upload(pdf_bytes())).status_code == 503


def test_the_api_key_is_required_when_configured(http, monkeypatch, good):
    monkeypatch.setenv("IRS990_API_KEY", "s3cret")
    use(monkeypatch, reply(good))
    assert http.post("/extract", files=upload(pdf_bytes())).status_code == 401
    assert http.post("/extract", files=upload(pdf_bytes()), headers={"X-API-Key": "wrong"}).status_code == 401
    ok = http.post("/extract", files=upload(pdf_bytes()), headers={"X-API-Key": "s3cret"})
    assert ok.status_code == 200 and http.get("/health").json()["auth_required"] is True


def test_callers_over_the_limit_get_429_before_any_claude_call(http, monkeypatch, good):
    monkeypatch.setenv("IRS990_RATE_LIMIT_PER_MINUTE", "1")
    fake = use(monkeypatch, reply(good))
    assert http.post("/extract", files=upload(pdf_bytes())).status_code == 200
    second = http.post("/extract", files=upload(pdf_bytes()))
    assert second.status_code == 429 and int(second.headers["Retry-After"]) >= 1 and len(fake.requests) == 1


def test_a_failed_claude_call_is_a_502(http, monkeypatch):
    class Down(FakeClient):
        def parse(self, **request):
            raise extract.anthropic.APIConnectionError(message="unreachable", request=None)

    monkeypatch.setattr(api, "client", Down())
    response = http.post("/extract", files=upload(pdf_bytes()))
    assert response.status_code == 502 and "APIConnectionError" in response.json()["detail"]


def test_no_answer_from_claude_is_a_422(http, monkeypatch, good):
    use(monkeypatch, reply(good, stop_reason="refusal"))
    response = http.post("/extract", files=upload(pdf_bytes()))
    assert response.status_code == 422 and "refusal" in response.json()["detail"]
