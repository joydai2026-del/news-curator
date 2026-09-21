"""Dependency-free ASGI boundary for the Modal ranking container."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback

from .service import (AuthenticationError, ProviderConsentRequiredError,
                      RankingInProgressError, StaleRankingError)
from .supabase_http import SupabaseHTTPError


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
        except RankingInProgressError:
            # Retryable, and distinct from staleness: the answer is being bought
            # right now and buying a second one is the bug this reports.
            await self._reply(send, 409, {"error": "ranking_in_progress"})
        except ProviderConsentRequiredError as exc:
            # Distinct from staleness on purpose. A reader that only sees "409"
            # shows a dead feed; this one can show a re-consent line and the
            # control that fixes it.
            await self._reply(send, 409, {"error": "provider_consent_required",
                                          "provider_policy_id": exc.provider_policy_id})
        except StaleRankingError as exc:
            await self._reply(send, 409, {"error": str(exc)})
        except (ValueError, json.JSONDecodeError) as exc:
            frame = traceback.extract_tb(exc.__traceback__)[-1]
            print(json.dumps({"event": "ranker_invalid_request",
                "exception_class": type(exc).__name__,
                "source_basename": os.path.basename(frame.filename),
                "source_line": frame.lineno}, separators=(",", ":")), file=sys.stderr, flush=True)
            await self._reply(send, 400, {"error": "invalid_request"})
        except SupabaseHTTPError as exc:
            # Which call failed, and whether the database answered at all. The
            # transport already printed the timing line; this is the part an
            # operator reads off a curl. Nothing else is added: no body, no
            # headers, and the path is already query-stripped at the transport.
            await self._reply(send, 503, {"error": str(exc), "path": exc.path,
                                          "status_code": exc.status_code})
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
