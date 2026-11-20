"""Vercel entrypoint for the FastAPI server.

When FLARE_DATA_API_URL is configured, this module proxies every HTTP request
to the authoritative Windows-hosted server. This avoids a Vercel database and
SQLite/databse connectivity assumption; the remote server owns the data.
"""

import asyncio
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from fastapi import FastAPI, Request as FastAPIRequest, Response

DATA_API_URL = os.environ.get("FLARE_DATA_API_URL", "").rstrip("/")

app = FastAPI(title="Flare Telemetry")

if DATA_API_URL:
    app.title = "Flare Telemetry Proxy"

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    async def proxy(path: str, request: FastAPIRequest):
        target = urljoin(DATA_API_URL + "/", path)
        if request.url.query:
            target += "?" + request.url.query

        body = await request.body()
        headers = {
            name: value
            for name, value in request.headers.items()
            if name.lower() not in {
                "host",
                "content-length",
                "transfer-encoding",
                "connection",
            }
        }

        def forward():
            req = Request(target, data=body or None, headers=headers,
                          method=request.method)
            try:
                with urlopen(req, timeout=30) as upstream:
                    data = upstream.read()
                    return upstream.status, upstream.headers, data
            except HTTPError as exc:
                return exc.code, exc.headers, exc.read()
            except URLError as exc:
                raise RuntimeError(f"data API unreachable: {exc.reason}") from exc

        try:
            status, upstream_headers, data = await asyncio.to_thread(forward)
        except Exception as exc:
            return Response(
                content=str(exc),
                status_code=502,
                media_type="text/plain",
                headers={"Cache-Control": "no-store"},
            )

        headers_out = {}
        for name in ("Content-Type", "Content-Disposition", "Cache-Control",
                     "Set-Cookie"):
            values = upstream_headers.get_all(name)
            if values:
                headers_out[name] = values[0]

        return Response(
            content=data,
            status_code=status,
            headers=headers_out,
        )
else:
    from pathlib import Path
    import sys

    ROOT = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(ROOT / "server"))

    from main import app  # noqa: E402

