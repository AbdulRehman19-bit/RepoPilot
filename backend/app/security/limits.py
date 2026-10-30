"""Request body size limits that also hold for chunked uploads.

Checking `Content-Length` is not enough: a client can omit it and stream forever. This middleware
counts the bytes as they arrive and, once the cap is passed, tells the application the client
disconnected and answers 413 itself, whatever the application would have said.
"""

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import settings

WEBHOOK_PATH = "/github/webhook"
TOO_LARGE = json.dumps({"detail": "request body too large"}).encode()


def limit_for(path: str) -> int:
    """Webhooks carry whole issue bodies; everything else is small JSON."""
    return settings.max_webhook_bytes if path == WEBHOOK_PATH else settings.max_request_bytes


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = limit_for(scope["path"])
        headers = dict(scope["headers"])
        declared = headers.get(b"content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > limit):
            await self._refuse(send)
            return

        received = 0
        exceeded = False
        replaced = False

        async def counted_receive() -> Message:
            nonlocal received, exceeded
            if exceeded:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal replaced
            if not exceeded:
                await send(message)
                return
            if not replaced:  # swap the application's answer for ours, once
                replaced = True
                await self._refuse(send)

        await self.app(scope, counted_receive, guarded_send)
        if exceeded and not replaced:  # the application said nothing at all
            await self._refuse(send)

    @staticmethod
    async def _refuse(send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(TOO_LARGE)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": TOO_LARGE})
