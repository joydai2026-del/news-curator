"""Dependency-free ASGI boundary for the Modal ranking container."""

from __future__ import annotations

import asyncio
import json

from .service import AuthenticationError, StaleRankingError


class RankingASGI:
    def __init__(self, *, service, reader_origin: str, maximum_body_bytes: int = 32768) -> None:
        if not reader_origin.startswith("https://") or maximum_body_bytes < 1:
            raise ValueError("invalid ASGI configuration")
        self._service, self._origin, self._maximum = service, reader_origin, maximum_body_bytes

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return
        headers = {key.decode().lower(): value.decode() for key, value in scope.get("headers", ())}
        origin = headers.get("origin")
        if origin is not None and origin != self._origin:
            return await self._reply(send, 403, {"error": "origin_denied"})
        method, path = scope.get("method"), scope.get("path")
        if method == "OPTIONS":
            return await self._reply(send, 204, None)
        try:
            if method == "GET" and path == "/config":
                result = {"schema_version": 1, "enabled": self._service._policy.enabled,
                    "policy_version": self._service._policy.policy_version, "model_version": self._service._policy.model_version}
            elif method == "POST" and path == "/rank":
                body = await self._body(receive)
                result = await asyncio.to_thread(self._service.rank, authorization=headers.get("authorization", ""), body=body)
            elif method == "GET" and path == "/page":
                from urllib.parse import parse_qs
                cursor = parse_qs(scope.get("query_string", b"").decode()).get("cursor", [""])[0]
                result = await asyncio.to_thread(self._service.page, authorization=headers.get("authorization", ""), cursor=cursor)
            else:
                return await self._reply(send, 404, {"error": "not_found"})
            await self._reply(send, 200, result)
        except AuthenticationError:
            await self._reply(send, 401, {"error": "authentication_required"})
        except StaleRankingError as exc:
            await self._reply(send, 409, {"error": str(exc)})
        except (ValueError, json.JSONDecodeError):
            await self._reply(send, 400, {"error": "invalid_request"})
        except RuntimeError as exc:
            await self._reply(send, 503, {"error": str(exc)})

    async def _body(self, receive):
        chunks, size = [], 0
        while True:
            event = await receive()
            chunk = event.get("body", b""); size += len(chunk)
            if size > self._maximum: raise ValueError("body_too_large")
            chunks.append(chunk)
            if not event.get("more_body", False): break
        value = json.loads(b"".join(chunks) or b"{}")
        if not isinstance(value, dict): raise ValueError("body must be an object")
        return value

    async def _reply(self, send, status, value):
        body = b"" if value is None else json.dumps(value, separators=(",", ":")).encode()
        headers = [(b"content-type", b"application/json"), (b"access-control-allow-origin", self._origin.encode()),
            (b"access-control-allow-headers", b"authorization,content-type"), (b"access-control-allow-methods", b"GET,POST,OPTIONS")]
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})
