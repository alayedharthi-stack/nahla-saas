#!/usr/bin/env python3
"""Minimal exact-target TCP relay for a disposable public PostgreSQL proxy."""
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


class ExactDbRelay:
    def __init__(
        self, host: str, port: int, expected_ips: set[str], evidence: Path
    ) -> None:
        self.host = host.lower()
        self.port = port
        self.expected_ips = expected_ips
        self.evidence = evidence

    def resolve_verified(self) -> list[str]:
        live = sorted(
            {
                item[4][0]
                for item in socket.getaddrinfo(
                    self.host,
                    self.port,
                    family=socket.AF_INET,
                    type=socket.SOCK_STREAM,
                )
            }
        )
        if not live or set(live) != self.expected_ips:
            raise RuntimeError("db_live_dns_mismatch")
        return live

    async def open_verified_upstream(
        self,
    ) -> tuple[str, asyncio.StreamReader, asyncio.StreamWriter]:
        """Resolve once, then connect to the selected numeric address."""
        selected_ip = self.resolve_verified()[0]
        reader, writer = await asyncio.open_connection(selected_ip, self.port)
        return selected_ip, reader, writer

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        connected = False
        selected_upstream_ip = None
        try:
            (
                selected_upstream_ip,
                upstream_reader,
                upstream_writer,
            ) = await self.open_verified_upstream()
            connected = True
            await asyncio.gather(
                _pipe(reader, upstream_writer), _pipe(upstream_reader, writer)
            )
        except OSError:
            writer.close()
        finally:
            event = {
                "at_utc": datetime.now(timezone.utc).isoformat(),
                "target_host": self.host,
                "target_port": self.port,
                "connected": connected,
                "selected_upstream_ip": selected_upstream_ip,
            }
            with self.evidence.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, sort_keys=True) + "\n")


async def main_async(args: argparse.Namespace) -> None:
    relay = ExactDbRelay(
        args.target_host,
        args.target_port,
        set(args.expected_ip),
        Path(args.evidence),
    )
    resolved = relay.resolve_verified()
    with relay.evidence.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "at_utc": datetime.now(timezone.utc).isoformat(),
                    "event": "startup_dns_verified",
                    "hostname": relay.host,
                    "port": relay.port,
                    "resolved_ips": resolved,
                },
                sort_keys=True,
            )
            + "\n"
        )
    server = await asyncio.start_server(relay.handle, "0.0.0.0", args.listen_port)
    async with server:
        await server.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-host", required=True)
    parser.add_argument("--target-port", type=int, required=True)
    parser.add_argument("--expected-ip", action="append", required=True)
    parser.add_argument("--listen-port", type=int, default=5432)
    parser.add_argument("--evidence", default="/evidence/db-relay.jsonl")
    asyncio.run(main_async(parser.parse_args()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
