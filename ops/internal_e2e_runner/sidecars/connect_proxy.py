#!/usr/bin/env python3
"""Minimal exact-host HTTPS CONNECT proxy with no credential awareness."""
from __future__ import annotations

import argparse
import asyncio
import json
import socket
from datetime import datetime, timezone
from pathlib import Path


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()


class ExactConnectProxy:
    def __init__(self, host: str, expected_ips: set[str], evidence: Path) -> None:
        self.host = host.lower()
        self.expected_ips = expected_ips
        self.evidence = evidence

    def resolve_verified(self) -> list[str]:
        live = sorted(
            {
                item[4][0]
                for item in socket.getaddrinfo(self.host, 443, type=socket.SOCK_STREAM)
            }
        )
        if not live or set(live) != self.expected_ips:
            raise RuntimeError("llm_live_dns_mismatch")
        return live

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        accepted = False
        target = ""
        try:
            request = await asyncio.wait_for(reader.readline(), timeout=5)
            parts = request.decode("ascii", "replace").strip().split()
            target = parts[1].lower() if len(parts) == 3 else ""
            while await reader.readline() not in (b"\r\n", b"\n", b""):
                pass
            if parts[:1] != ["CONNECT"] or target != f"{self.host}:443":
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            self.resolve_verified()
            upstream_reader, upstream_writer = await asyncio.open_connection(
                self.host, 443
            )
            accepted = True
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await asyncio.gather(
                _pipe(reader, upstream_writer), _pipe(upstream_reader, writer)
            )
        except (OSError, asyncio.TimeoutError):
            writer.close()
        finally:
            event = {
                "at_utc": datetime.now(timezone.utc).isoformat(),
                "target": target,
                "accepted": accepted,
            }
            with self.evidence.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, sort_keys=True) + "\n")


async def main_async(args: argparse.Namespace) -> None:
    proxy = ExactConnectProxy(
        args.allowed_host,
        set(args.expected_ip),
        Path(args.evidence),
    )
    resolved = proxy.resolve_verified()
    with proxy.evidence.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "at_utc": datetime.now(timezone.utc).isoformat(),
                    "event": "startup_dns_verified",
                    "hostname": proxy.host,
                    "resolved_ips": resolved,
                },
                sort_keys=True,
            )
            + "\n"
        )
    server = await asyncio.start_server(proxy.handle, "0.0.0.0", args.listen_port)
    async with server:
        await server.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--allowed-host", required=True)
    parser.add_argument("--expected-ip", action="append", required=True)
    parser.add_argument("--listen-port", type=int, default=3128)
    parser.add_argument("--evidence", default="/evidence/connect-proxy.jsonl")
    asyncio.run(main_async(parser.parse_args()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
