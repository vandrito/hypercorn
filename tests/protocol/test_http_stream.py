from typing import Any
from unittest.mock import call, Mock

import pytest

from asynctest.mock import CoroutineMock
from hypercorn.config import Config
from hypercorn.protocol.events import Body, EndBody, Request, Response, StreamClosed
from hypercorn.protocol.http_stream import ASGIHTTPState, HTTPStream
from hypercorn.utils import UnexpectedMessage


@pytest.fixture(name="stream")
async def _stream() -> HTTPStream:
    stream = HTTPStream(Config(), False, None, None, CoroutineMock(), CoroutineMock(), 1)
    stream.app_put = CoroutineMock()
    stream.config._log = Mock()
    return stream


@pytest.mark.asyncio
async def test_handle_request(stream: HTTPStream) -> None:
    await stream.handle(
        Request(stream_id=1, http_version="1.1", headers=[], raw_path=b"/?a=b", method="GET")
    )
    stream.spawn_app.assert_called()
    scope = stream.spawn_app.call_args[0][0]
    assert scope == {
        "type": "http",
        "http_version": "1.1",
        "asgi": {"spec_version": "2.1"},
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"a=b",
        "root_path": stream.config.root_path,
        "headers": [],
        "client": None,
        "server": None,
    }


@pytest.mark.asyncio
async def test_handle_body(stream: HTTPStream) -> None:
    await stream.handle(Body(stream_id=1, data=b"data"))
    stream.app_put.assert_called()
    assert stream.app_put.call_args_list == [
        call({"type": "http.request", "body": b"data", "more_body": True})
    ]


@pytest.mark.asyncio
async def test_handle_end_body(stream: HTTPStream) -> None:
    stream.app_put = CoroutineMock()
    await stream.handle(EndBody(stream_id=1))
    stream.app_put.assert_called()
    assert stream.app_put.call_args_list == [
        call({"type": "http.request", "body": b"", "more_body": False})
    ]


@pytest.mark.asyncio
async def test_handle_closed(stream: HTTPStream) -> None:
    await stream.handle(StreamClosed(stream_id=1))
    stream.app_put.assert_called()
    assert stream.app_put.call_args_list == [call({"type": "http.disconnect"})]


@pytest.mark.asyncio
async def test_send_response(stream: HTTPStream) -> None:
    await stream.handle(
        Request(stream_id=1, http_version="1.1", headers=[], raw_path=b"/?a=b", method="GET")
    )
    await stream.app_send({"type": "http.response.start", "status": 200, "headers": []})
    assert stream.state == ASGIHTTPState.REQUEST
    # Must wait for response before sending anything
    stream.send.assert_not_called()
    await stream.app_send({"type": "http.response.body", "body": b"Body"})
    assert stream.state == ASGIHTTPState.CLOSED
    stream.send.assert_called()
    assert stream.send.call_args_list == [
        call(Response(stream_id=1, headers=[], status_code=200)),
        call(Body(stream_id=1, data=b"Body")),
        call(EndBody(stream_id=1)),
    ]
    stream.config._log.access.assert_called()


@pytest.mark.asyncio
async def test_send_app_error(stream: HTTPStream) -> None:
    await stream.handle(
        Request(stream_id=1, http_version="1.1", headers=[], raw_path=b"/?a=b", method="GET")
    )
    await stream.app_send(None)
    stream.send.assert_called()
    assert stream.send.call_args_list == [
        call(
            Response(
                stream_id=1,
                headers=[(b"content-length", b"0"), (b"connection", b"close")],
                status_code=500,
            )
        ),
        call(EndBody(stream_id=1)),
        call(StreamClosed(stream_id=1)),
    ]
    stream.config._log.access.assert_called()


@pytest.mark.parametrize(
    "state, message_type",
    [
        (ASGIHTTPState.REQUEST, "not_a_real_type"),
        (ASGIHTTPState.RESPONSE, "http.response.start"),
        (ASGIHTTPState.CLOSED, "http.response.start"),
        (ASGIHTTPState.CLOSED, "http.response.body"),
    ],
)
@pytest.mark.asyncio
async def test_send_invalid_message_given_state(
    stream: HTTPStream, state: ASGIHTTPState, message_type: str
) -> None:
    stream.state = state
    with pytest.raises(UnexpectedMessage):
        await stream.app_send({"type": message_type})


@pytest.mark.parametrize(
    "status, headers, body",
    [
        ("201 NO CONTENT", [], b""),  # Status should be int
        (200, [("X-Foo", "foo")], b""),  # Headers should be bytes
        (200, [], "Body"),  # Body should be bytes
    ],
)
@pytest.mark.asyncio
async def test_send_invalid_message(
    stream: HTTPStream, status: Any, headers: Any, body: Any
) -> None:
    stream.scope = {"method": "GET"}
    stream.state = ASGIHTTPState.REQUEST
    with pytest.raises((TypeError, ValueError)):
        await stream.app_send({"type": "http.response.start", "headers": headers, "status": status})
        await stream.app_send({"type": "http.response.body", "body": body})
