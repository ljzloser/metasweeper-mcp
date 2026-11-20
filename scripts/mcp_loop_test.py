#!/usr/bin/env python3
"""Run continuous end-to-end games through the local Metasweeper MCP service.

The script uses only MCP HTTP tools. It starts games, reads the visible board,
flags cells proved to be mines, opens cells proved safe, and guesses when the
visible information is insufficient. It never reads hidden board data.
"""
from __future__ import annotations

import argparse
import http.client
import json
import random
import time
from typing import Any

PROTOCOL_VERSION = "2025-11-25"
TERMINAL = {"win", "jowin", "lose", "fail", "jofail", "finished"}
ACTIVE = {"playing", "joking"}
READY = {"ready"}


class McpClient:
    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.path = "/mcp"
        self.session: str | None = None
        self.request_id = 0

    def _post(self, message: dict, notification: bool = False) -> dict | None:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Origin": f"http://{self.host}:{self.port}",
        }
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        body = json.dumps(message, separators=(",", ":")).encode()
        for attempt in range(8):
            connection = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
            try:
                connection.request("POST", self.path, body, headers)
                response = connection.getresponse()
                raw = response.read()
                if response.status == 429 and attempt < 7:
                    retry_after = response.getheader("Retry-After", "1")
                    try:
                        delay = max(0.1, min(5.0, float(retry_after)))
                    except ValueError:
                        delay = 1.0
                    time.sleep(delay)
                    continue
                if response.status not in (200, 202):
                    raise RuntimeError(f"HTTP {response.status}: {raw.decode(errors='replace')}")
                if notification or response.status == 202 or not raw:
                    return None
                text = raw.decode("utf-8")
                if text.startswith("data:"):
                    text = "\n".join(line[5:].strip() for line in text.splitlines()
                                        if line.startswith("data:"))
                return json.loads(text)
            finally:
                connection.close()
        raise RuntimeError("MCP request retry limit exceeded")

    def connect(self) -> None:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "Origin": f"http://{self.host}:{self.port}"}
        self.request_id += 1
        message = {"jsonrpc": "2.0", "id": self.request_id, "method": "initialize",
                   "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                              "clientInfo": {"name": "mcp-loop-test", "version": "1.0"}}}
        for attempt in range(8):
            connection = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
            try:
                connection.request("POST", self.path, json.dumps(message).encode(), headers)
                response = connection.getresponse()
                raw = response.read()
                if response.status == 429 and attempt < 7:
                    time.sleep(1.0)
                    continue
                if response.status != 200:
                    raise RuntimeError(f"initialize HTTP {response.status}: {raw.decode(errors='replace')}")
                self.session = response.getheader("Mcp-Session-Id")
                payload = json.loads(raw.decode())
                if "result" not in payload or not self.session:
                    raise RuntimeError(f"initialize failed: {payload}")
                break
            finally:
                connection.close()
        else:
            raise RuntimeError("MCP initialize retry limit exceeded")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, notification=True)

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict:
        self.request_id += 1
        payload = self._post({"jsonrpc": "2.0", "id": self.request_id, "method": "tools/call",
                              "params": {"name": name, "arguments": arguments or {}}})
        if not payload or "result" not in payload:
            raise RuntimeError(f"{name} failed: {payload}")
        result = payload["result"].get("structuredContent")
        if not isinstance(result, dict):
            raise RuntimeError(f"{name} returned no structuredContent: {payload}")
        return result


def neighbors(row: int, col: int, rows: int, cols: int):
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            if dr or dc:
                nr, nc = row + dr, col + dc
                if 0 <= nr < rows and 0 <= nc < cols:
                    yield nr, nc


def deductions(cells: list[list[Any]]) -> tuple[set[tuple[int, int]], set[tuple[int, int]]]:
    rows, cols = len(cells), len(cells[0])
    safe: set[tuple[int, int]] = set()
    mines: set[tuple[int, int]] = set()
    for row in range(rows):
        for col in range(cols):
            value = cells[row][col]
            if type(value) is not int or not 0 <= value <= 8:
                continue
            around = list(neighbors(row, col, rows, cols))
            hidden = [(r, c) for r, c in around if cells[r][c] == -1]
            flagged = sum(cells[r][c] == "F" for r, c in around)
            remaining = value - flagged
            if hidden and remaining == 0:
                safe.update(hidden)
            elif hidden and remaining == len(hidden):
                mines.update(hidden)
    safe.difference_update(mines)
    return safe, mines


def board_status(snapshot: dict) -> str:
    status = snapshot.get("game_status")
    return status if isinstance(status, str) else "unknown"


def transient_batch_failure(result: dict) -> bool:
    error = result.get("error", "")
    return (result.get("outcome") == "not_executed"
            and any(text in error for text in (
                "No actionable visible board", "fresh live visible board",
                "Board changed", "returned to ready")))


def read_live_board(client: McpClient, retries: int = 20) -> dict:
    """Retry safe read-only snapshots while the board is changing."""
    last_result: dict = {}
    for attempt in range(retries):
        last_result = client.call("get_board_state")
        if (last_result.get("success")
                and last_result.get("source") == "live"
                and isinstance(last_result.get("cells"), list)):
            return last_result
        if attempt + 1 < retries:
            time.sleep(min(0.1 * (attempt + 1), 0.5))
    raise RuntimeError(f"could not get a stable live board after {retries} reads: {last_result}")


def run_game(client: McpClient, difficulty: str, rng: random.Random, max_steps: int) -> str:
    current = client.call("get_game_status")
    current_status = board_status(current)
    first_click = False
    if current_status in READY or current_status in TERMINAL or current_status == "unknown":
        started = client.call("start_new_game", {"difficulty": difficulty})
        if not started.get("success"):
            raise RuntimeError(f"start_new_game failed: {started}")
        print("started", difficulty, flush=True)
        first_click = True
    else:
        print("continuing", current_status, flush=True)
    for step in range(1, max_steps + 1):
        snapshot = read_live_board(client)
        status = board_status(snapshot)
        cells = snapshot.get("cells")
        if not isinstance(cells, list) or not cells or not isinstance(cells[0], list):
            raise RuntimeError(f"invalid board snapshot: {snapshot}")
        if status in TERMINAL:
            print("finished", status, "steps", step - 1, flush=True)
            return status
        if status not in ACTIVE and not first_click:
            time.sleep(0.15)
            continue
        rows, cols = len(cells), len(cells[0])
        if first_click:
            target = (rows // 2, cols // 2)
            result = client.call("click_cell", {"clicks": [{"row": target[0], "col": target[1]}], "button": "left"})
            if not result.get("success"):
                if transient_batch_failure(result):
                    time.sleep(0.2)
                    continue
                raise RuntimeError(f"initial click failed: {result}")
            first_click = False
            continue
        safe, mines = deductions(cells)
        unknown = [(r, c) for r in range(rows) for c in range(cols) if cells[r][c] == -1]
        if mines:
            result = client.call("click_cell", {"clicks": [{"row": r, "col": c} for r, c in sorted(mines)], "button": "right"})
            if not result.get("success"):
                if transient_batch_failure(result):
                    time.sleep(0.2)
                    continue
                raise RuntimeError(f"flag batch failed: {result}")
            continue
        if safe:
            result = client.call("click_cell", {"clicks": [{"row": r, "col": c} for r, c in sorted(safe)], "button": "left"})
            if not result.get("success"):
                if transient_batch_failure(result):
                    time.sleep(0.2)
                    continue
                raise RuntimeError(f"safe batch failed: {result}")
            continue
        if not unknown:
            time.sleep(0.15)
            continue
        target = rng.choice(unknown)
        print("guess", target, "step", step, flush=True)
        result = client.call("click_cell", {"clicks": [{"row": target[0], "col": target[1]}], "button": "left"})
        if not result.get("success"):
            if transient_batch_failure(result):
                time.sleep(0.2)
                continue
            raise RuntimeError(f"guess failed: {result}")
    raise RuntimeError(f"step limit exceeded ({max_steps})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--games", type=int, default=0, help="0 means continue until Ctrl+C")
    parser.add_argument("--difficulty", choices=("easy", "medium", "hard"), default="easy")
    parser.add_argument("--delay", type=float, default=0.05)
    parser.add_argument("--max-steps", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()
    client = McpClient(args.host, args.port, 8.0)
    client.connect()
    rng = random.Random(args.seed)
    completed = 0
    try:
        while args.games == 0 or completed < args.games:
            run_game(client, args.difficulty, rng, args.max_steps)
            completed += 1
            time.sleep(args.delay)
    except KeyboardInterrupt:
        print("stopped by user; games", completed, flush=True)
    print("games completed", completed, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
