"""Gateway HTML error pages are logged as their title (compact_html_pages).

On 2026-11-07 llm.apache.org answered a gemma4-26b outage with httpd's 503
page; each failed call logged the page in the message and four more times in
the chained traceback.
"""

from unittest.mock import patch

from services.observability_service import ObservabilityService, compact_html_pages

PAGE = (
    '<!DOCTYPE HTML PUBLIC "-//IETF//DTD HTML 2.0//EN">\n<html><head>\n'
    "<title>503 Service Unavailable</title>\n</head><body>\n<h1>Service Unavailable</h1>\n"
    "<p>The server is temporarily unable to service your\nrequest due to maintenance "
    "downtime or capacity\nproblems. Please try again later.</p>\n</body></html>"
)


def test_a_page_becomes_its_title():
    msg = f"litellm.ServiceUnavailableError: OpenAIException - {PAGE}"
    assert compact_html_pages(msg) == (
        "litellm.ServiceUnavailableError: OpenAIException - "
        "[HTML error page: 503 Service Unavailable]")


def test_every_page_in_a_traceback_is_compacted():
    tb = "\n".join(f"Traceback {i}\nopenai.InternalServerError: {PAGE}\n" for i in range(4))
    out = compact_html_pages(tb)
    assert out.count("[HTML error page: 503 Service Unavailable]") == 4
    assert "<html" not in out and "maintenance" not in out


def test_text_without_a_page_is_unchanged():
    for text in ("", "plain error", "x < y and <b>bold</b> but no page", None):
        assert compact_html_pages(text) == text


def test_a_page_without_a_title():
    assert compact_html_pages("<html><body>oops</body></html>") == "[HTML error page: no title]"


def test_log_exception_compacts_message_and_traceback():
    svc = ObservabilityService.__new__(ObservabilityService)
    with patch.object(ObservabilityService, "log") as log:
        try:
            raise RuntimeError(PAGE)
        except RuntimeError as e:
            svc.log_exception(e, metadata={"model": "gemma4-26b"})
    kw = log.call_args.kwargs
    assert kw["message"] == "[HTML error page: 503 Service Unavailable]"
    assert "<html" not in kw["metadata"]["traceback"]
    assert kw["metadata"]["model"] == "gemma4-26b"
