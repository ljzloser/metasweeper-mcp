#!/usr/bin/env python3
"""Run continuous end-to-end games through the local Metasweeper MCP service.

The script uses only MCP HTTP tools. It starts games, reads the visible board,
tracks cells proved to be mines, and opens cells proved safe. Physical flags
are opt-in with --flag-mines. Standard mode may use a probability-guided guess
when the visible information is insufficient; no-guess modes never do so. It
never reads hidden board data.
"""
from __future__ import annotations

import argparse
import http.client
import json
import math
import random
import time
from typing import Any

PROTOCOL_VERSION = "2025-11-25"
TERMINAL = {"win", "jowin", "lose", "fail", "jofail", "finished"}
ACTIVE = {"playing", "joking"}
READY = {"ready"}
GAME_MODES = (
    "standard", "classic_no_guess", "strict_no_guess", "weak_no_guess",
    "blessing", "guessable_no_guess", "lucky", "chord_recursive",
    "flag_recursive", "chord_flag_recursive", "no_guess_chord_recursive",
    "no_guess_flag_recursive", "no_guess_chord_flag_recursive",
)
GAME_MODE_VALUES = {
    "standard": 0,
    "classic_no_guess": 5,
    "strict_no_guess": 6,
    "weak_no_guess": 7,
    "blessing": 8,
    "guessable_no_guess": 9,
    "lucky": 10,
    "chord_recursive": 11,
    "flag_recursive": 12,
    "chord_flag_recursive": 13,
    "no_guess_chord_recursive": 14,
    "no_guess_flag_recursive": 15,
    "no_guess_chord_flag_recursive": 16,
}
NO_GUESS_MODES = frozenset({
    "classic_no_guess", "strict_no_guess", "weak_no_guess", "blessing",
    "no_guess_chord_recursive",
    "no_guess_flag_recursive", "no_guess_chord_flag_recursive",
})


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


def _build_constraints(
        cells: list[list[Any]],
        known_mines: set[tuple[int, int]] | None = None,
) -> tuple[list[tuple[frozenset[tuple[int, int]], int]], set[tuple[int, int]]]:
    rows, cols = len(cells), len(cells[0])
    known_mines = known_mines or set()
    constraints: list[tuple[frozenset[tuple[int, int]], int]] = []
    unknown_cells = {
        (row, col)
        for row, values in enumerate(cells)
        for col, value in enumerate(values)
        if value == -1 and (row, col) not in known_mines
    }
    for row in range(rows):
        for col in range(cols):
            value = cells[row][col]
            if type(value) is not int or not 0 <= value <= 8:
                continue
            around = list(neighbors(row, col, rows, cols))
            hidden = frozenset(
                (r, c) for r, c in around
                if cells[r][c] == -1 and (r, c) not in known_mines)
            flagged = sum(
                cells[r][c] == "F" or (r, c) in known_mines
                for r, c in around)
            remaining = value - flagged
            if hidden and 0 <= remaining <= len(hidden):
                constraints.append((hidden, remaining))
    return constraints, unknown_cells


def deductions(cells: list[list[Any]], mines_remaining: int | None = None,
               known_mines: set[tuple[int, int]] | None = None
               ) -> tuple[set[tuple[int, int]], set[tuple[int, int]]]:
    """Find cells forced safe/mine by local and subset constraint rules."""
    safe: set[tuple[int, int]] = set()
    mines: set[tuple[int, int]] = set()
    known_mines = known_mines or set()
    constraints, unknown_cells = _build_constraints(cells, known_mines)

    # Repeatedly apply direct rules and A⊂B subtraction. This handles cases
    # where no single number has a complete mine/safe neighborhood but the
    # visible equations still force a result.
    changed = True
    while changed:
        changed = False
        for unknown, remaining in constraints:
            unresolved = unknown - safe - mines
            remaining -= len(unknown & mines)
            if not unresolved or remaining < 0 or remaining > len(unresolved):
                continue
            if remaining == 0:
                before = len(safe)
                safe.update(unresolved)
                changed |= len(safe) != before
            elif remaining == len(unresolved):
                before = len(mines)
                mines.update(unresolved)
                changed |= len(mines) != before
        for index, (left_cells, left_remaining) in enumerate(constraints):
            left_cells = left_cells - safe - mines
            left_remaining -= len(constraints[index][0] & mines)
            if not left_cells or left_remaining < 0:
                continue
            for right_original_cells, right_original_remaining in constraints[index + 1:]:
                right_cells = right_original_cells
                right_remaining = right_original_remaining
                right_cells = right_cells - safe - mines
                right_remaining -= len(right_original_cells & mines)
                if left_cells < right_cells:
                    subset, superset = left_cells, right_cells
                    difference = right_remaining - left_remaining
                elif right_cells < left_cells:
                    subset, superset = right_cells, left_cells
                    difference = left_remaining - right_remaining
                else:
                    continue
                new_cells = superset - subset
                if not new_cells or difference < 0 or difference > len(new_cells):
                    continue
                if difference == 0:
                    before = len(safe)
                    safe.update(new_cells)
                    changed |= len(safe) != before
                elif difference == len(new_cells):
                    before = len(mines)
                    mines.update(new_cells)
                    changed |= len(mines) != before

        # If subtraction still leaves an ambiguous frontier, enumerate each
        # small connected component. A no-guess board can require this global
        # constraint step even though every individual number is ambiguous.
        # Once a forced action exists, return it immediately and avoid paying
        # the enumeration cost on every ordinary board update.
        if not safe and not mines:
            forced_safe, forced_mines = _enumerated_forced_cells(
                constraints, safe, mines)
            before_safe, before_mines = len(safe), len(mines)
            safe.update(forced_safe)
            mines.update(forced_mines)
            changed |= len(safe) != before_safe or len(mines) != before_mines
    if isinstance(mines_remaining, int) and mines_remaining >= 0:
        global_safe, global_mines = _global_csp_forced_cells(
            constraints, safe, mines, mines_remaining,
            unknown_cells)
        safe.update(global_safe)
        mines.update(global_mines)
    safe.difference_update(mines)
    return safe, mines


def exact_probabilities(cells: list[list[Any]], mines_remaining: int,
                        known_mines: set[tuple[int, int]] | None = None
                        ) -> dict[tuple[int, int], float]:
    """Return exact frontier probabilities when the component search fits."""
    constraints, unknown_cells = _build_constraints(cells, known_mines)
    _, _, probabilities = _global_csp_forced_cells(
        constraints, set(), set(), mines_remaining, unknown_cells,
        with_probabilities=True)
    return probabilities


def _enumerated_forced_cells(
        constraints: list[tuple[frozenset[tuple[int, int]], int]],
        safe: set[tuple[int, int]],
        mines: set[tuple[int, int]],
        max_variables: int = 20,
) -> tuple[set[tuple[int, int]], set[tuple[int, int]]]:
    """Return cells fixed in every assignment satisfying each frontier.

    Components larger than ``max_variables`` are skipped to keep the tester
    bounded on large boards. They remain eligible for the subset rules above.
    """
    active: list[tuple[frozenset[tuple[int, int]], int]] = []
    for original_cells, original_remaining in constraints:
        unknown = original_cells - safe - mines
        remaining = original_remaining - len(original_cells & mines)
        if unknown and 0 <= remaining <= len(unknown):
            active.append((frozenset(unknown), remaining))
    if not active:
        return set(), set()

    variable_to_constraints: dict[tuple[int, int], set[int]] = {}
    for index, (unknown, _) in enumerate(active):
        for cell in unknown:
            variable_to_constraints.setdefault(cell, set()).add(index)

    safe_result: set[tuple[int, int]] = set()
    mines_result: set[tuple[int, int]] = set()
    visited: set[int] = set()
    for start in range(len(active)):
        if start in visited:
            continue
        component_constraints: set[int] = {start}
        queue = [start]
        visited.add(start)
        while queue:
            index = queue.pop()
            for cell in active[index][0]:
                for adjacent in variable_to_constraints[cell]:
                    if adjacent not in visited:
                        visited.add(adjacent)
                        component_constraints.add(adjacent)
                        queue.append(adjacent)
        variables = sorted({cell for index in component_constraints for cell in active[index][0]})
        if len(variables) > max_variables:
            continue
        positions = {cell: offset for offset, cell in enumerate(variables)}
        encoded = [
            ([positions[cell] for cell in unknown], remaining)
            for index in component_constraints
            for unknown, remaining in [active[index]]
        ]
        possible_masks: list[int] = []
        for mask in range(1 << len(variables)):
            if all(sum((mask >> position) & 1 for position in positions_list) == remaining
                   for positions_list, remaining in encoded):
                possible_masks.append(mask)
        if not possible_masks:
            continue
        for offset, cell in enumerate(variables):
            values = {(mask >> offset) & 1 for mask in possible_masks}
            if values == {0}:
                safe_result.add(cell)
            elif values == {1}:
                mines_result.add(cell)
    return safe_result, mines_result


def _enumerate_component(
        variables: list[tuple[int, int]],
        constraints: list[tuple[frozenset[tuple[int, int]], int]],
        max_nodes: int = 400_000,
) -> dict[int, list[int]] | None:
    """Enumerate valid masks for one independent constraint component.

    The search branches on high-degree cells first and prunes every partial
    assignment whose constraint can no longer reach its required mine count.
    ``None`` means the component exceeded the bounded search budget.
    """
    if len(variables) > 24:
        return None
    positions = {cell: index for index, cell in enumerate(variables)}
    encoded = [
        ([positions[cell] for cell in unknown], remaining)
        for unknown, remaining in constraints
    ]
    occurrences = [0] * len(variables)
    for positions_list, _ in encoded:
        for position in positions_list:
            occurrences[position] += 1
    order = sorted(range(len(variables)), key=lambda position: -occurrences[position])
    constraint_by_position: list[list[int]] = [[] for _ in variables]
    for index, (positions_list, _) in enumerate(encoded):
        for position in positions_list:
            constraint_by_position[position].append(index)
    assigned_mines = [0] * len(encoded)
    unassigned = [len(positions_list) for positions_list, _ in encoded]
    assignment = [0] * len(variables)
    result: dict[int, list[int]] = {}
    nodes = 0

    def search(depth: int) -> bool:
        nonlocal nodes
        nodes += 1
        if nodes > max_nodes:
            return False
        if depth == len(order):
            mask = sum(value << position for position, value in enumerate(assignment))
            result.setdefault(sum(assignment), []).append(mask)
            return True
        position = order[depth]
        for value in (0, 1):
            touched = constraint_by_position[position]
            valid = True
            assignment[position] = value
            for index in touched:
                assigned_mines[index] += value
                unassigned[index] -= 1
                _, required = encoded[index]
                if (assigned_mines[index] > required
                        or assigned_mines[index] + unassigned[index] < required):
                    valid = False
            if valid and not search(depth + 1):
                return False
            for index in touched:
                assigned_mines[index] -= value
                unassigned[index] += 1
            assignment[position] = 0
        return True

    if not search(0):
        return None
    return result


def _global_csp_forced_cells(
        constraints: list[tuple[frozenset[tuple[int, int]], int]],
        safe: set[tuple[int, int]],
        mines: set[tuple[int, int]],
        mines_remaining: int,
        unknown_cells: set[tuple[int, int]],
        with_probabilities: bool = False,
) -> tuple[set[tuple[int, int]], set[tuple[int, int]]] | tuple[
        set[tuple[int, int]], set[tuple[int, int]], dict[tuple[int, int], float]]:
    """Use exact component models plus the global remaining-mine count.

    This combines independent frontiers and unconstrained covered cells. A
    cell is returned only when every globally valid board assigns it the same
    value, so it is safe for no-guess play.
    """
    active: list[tuple[frozenset[tuple[int, int]], int]] = []
    for original_cells, original_remaining in constraints:
        unresolved = original_cells - safe - mines
        remaining = original_remaining - len(original_cells & mines)
        if unresolved and 0 <= remaining <= len(unresolved):
            active.append((frozenset(unresolved), remaining))
    if not active:
        if not unknown_cells or not 0 <= mines_remaining <= len(unknown_cells):
            return (set(), set(), {}) if with_probabilities else (set(), set())
        probability = mines_remaining / len(unknown_cells)
        safe_result = set(unknown_cells) if mines_remaining == 0 else set()
        mines_result = set(unknown_cells) if mines_remaining == len(unknown_cells) else set()
        probabilities = {cell: probability for cell in unknown_cells}
        if with_probabilities:
            return safe_result, mines_result, probabilities
        return safe_result, mines_result
    variable_to_constraints: dict[tuple[int, int], set[int]] = {}
    for index, (unknown, _) in enumerate(active):
        for cell in unknown:
            variable_to_constraints.setdefault(cell, set()).add(index)
    components: list[tuple[list[tuple[int, int]], dict[int, list[int]]]] = []
    visited: set[int] = set()
    frontier: set[tuple[int, int]] = set()
    for start in range(len(active)):
        if start in visited:
            continue
        component_constraints: set[int] = {start}
        queue = [start]
        visited.add(start)
        while queue:
            index = queue.pop()
            for cell in active[index][0]:
                frontier.add(cell)
                for adjacent in variable_to_constraints[cell]:
                    if adjacent not in visited:
                        visited.add(adjacent)
                        component_constraints.add(adjacent)
                        queue.append(adjacent)
        variables = sorted({cell for index in component_constraints for cell in active[index][0]})
        local_constraints = [active[index] for index in component_constraints]
        models = _enumerate_component(variables, local_constraints)
        if models is None or not models:
            return (set(), set(), {}) if with_probabilities else (set(), set())
        components.append((variables, models))

    # Prefix/suffix distributions let us condition each component on the
    # exact total mine count without enumerating Cartesian products.
    distributions = [{count: len(masks) for count, masks in models.items()}
                      for _, models in components]
    prefix: list[dict[int, int]] = [{0: 1}]
    for distribution in distributions:
        merged: dict[int, int] = {}
        for left_count, left_ways in prefix[-1].items():
            for right_count, right_ways in distribution.items():
                total = left_count + right_count
                merged[total] = merged.get(total, 0) + left_ways * right_ways
        prefix.append(merged)
    suffix: list[dict[int, int]] = [{} for _ in range(len(components) + 1)]
    suffix[-1] = {0: 1}
    for index in range(len(components) - 1, -1, -1):
        merged = {}
        for left_count, left_ways in distributions[index].items():
            for right_count, right_ways in suffix[index + 1].items():
                total = left_count + right_count
                merged[total] = merged.get(total, 0) + left_ways * right_ways
        suffix[index] = merged
    unconstrained_cells = unknown_cells - frontier - safe - mines
    unconstrained = len(unconstrained_cells)
    total_ways = 0
    for component_count, ways in prefix[-1].items():
        remaining = mines_remaining - component_count
        if 0 <= remaining <= unconstrained:
            total_ways += ways * math.comb(unconstrained, remaining)
    if total_ways == 0:
        return (set(), set(), {}) if with_probabilities else (set(), set())

    forced_safe: set[tuple[int, int]] = set()
    forced_mines: set[tuple[int, int]] = set()
    probabilities: dict[tuple[int, int], float] = {}
    for index, (variables, models) in enumerate(components):
        other_distribution: dict[int, int] = {}
        for left_count, left_ways in prefix[index].items():
            for right_count, right_ways in suffix[index + 1].items():
                total = left_count + right_count
                other_distribution[total] = other_distribution.get(total, 0) + left_ways * right_ways
        model_weight: dict[int, int] = {}
        for component_count in models:
            weight = 0
            for other_count, other_ways in other_distribution.items():
                remaining = mines_remaining - component_count - other_count
                if 0 <= remaining <= unconstrained:
                    weight += other_ways * math.comb(unconstrained, remaining)
            model_weight[component_count] = weight
        for position, cell in enumerate(variables):
            mine_ways = 0
            for component_count, masks in models.items():
                weight = model_weight[component_count]
                mine_ways += sum((mask >> position) & 1 for mask in masks) * weight
            if mine_ways == 0:
                forced_safe.add(cell)
            elif mine_ways == total_ways:
                forced_mines.add(cell)
            probabilities[cell] = mine_ways / total_ways

    if unconstrained:
        unconstrained_mine_ways = 0
        for component_count, ways in prefix[-1].items():
            remaining = mines_remaining - component_count
            if 1 <= remaining <= unconstrained:
                unconstrained_mine_ways += ways * math.comb(unconstrained - 1, remaining - 1)
        if unconstrained_mine_ways == 0:
            forced_safe.update(unconstrained_cells)
        elif unconstrained_mine_ways == total_ways:
            forced_mines.update(unconstrained_cells)
        unconstrained_probability = unconstrained_mine_ways / total_ways
        for cell in unconstrained_cells:
            probabilities[cell] = unconstrained_probability
    if with_probabilities:
        return forced_safe, forced_mines, probabilities
    return forced_safe, forced_mines


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


def run_game(client: McpClient, difficulty: str, mode: str,
             rng: random.Random, max_steps: int, flag_mines: bool = False) -> str:
    current = client.call("get_game_status")
    current_status = board_status(current)
    first_click = False
    current_mode = current.get("mode")
    expected_mode = GAME_MODE_VALUES.get(mode)
    if current_status in ACTIVE and expected_mode is not None and current_mode != expected_mode:
        raise RuntimeError(
            f"active game mode is {current_mode!r}, requested {mode!r}; "
            "finish or restart the current game before running the tester")
    if current_status in READY or current_status in TERMINAL or current_status == "unknown":
        started = client.call("start_new_game", {"difficulty": difficulty, "mode": mode})
        if not started.get("success"):
            raise RuntimeError(f"start_new_game failed: {started}")
        print("started", difficulty, flush=True)
        first_click = True
    else:
        print("continuing", current_status, flush=True)
    known_mines: set[tuple[int, int]] = set()
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
        reported_mines = snapshot.get("mines_remaining")
        mines_remaining = (
            reported_mines - len(known_mines)
            if isinstance(reported_mines, int) else None)
        safe, mines = deductions(cells, mines_remaining, known_mines)
        new_mines = mines - known_mines
        if not flag_mines:
            known_mines.update(mines)
        unknown = [
            (r, c) for r in range(rows) for c in range(cols)
            if cells[r][c] == -1 and (r, c) not in known_mines]
        if flag_mines and mines:
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
        if not flag_mines and new_mines:
            # Virtual mines change the equations without requiring a right
            # click; recompute against the same visible board immediately.
            continue
        if not unknown:
            time.sleep(0.15)
            continue
        if mode in NO_GUESS_MODES:
            raise RuntimeError(
                f"no forced move at step {step} in {mode}; refusing to guess")
        probabilities = exact_probabilities(
            cells, mines_remaining if mines_remaining is not None else -1,
            known_mines)
        candidates = [cell for cell in unknown if cell in probabilities]
        if candidates:
            target = min(candidates, key=probabilities.__getitem__)
            print("probability guess", target,
                  f"mine-risk={probabilities[target]:.6f}",
                  "step", step, flush=True)
        else:
            target = rng.choice(unknown)
            print("fallback guess", target, "step", step, flush=True)
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
    parser.add_argument("--mode", choices=GAME_MODES, default="standard")
    parser.add_argument("--delay", type=float, default=0.0,
                        help="pause between completed games (default: 0)")
    parser.add_argument("--flag-mines", action="store_true",
                        help="physically right-click proven mines; default is left-click-only")
    parser.add_argument("--max-steps", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()
    client = McpClient(args.host, args.port, 8.0)
    client.connect()
    rng = random.Random(args.seed)
    completed = 0
    try:
        while args.games == 0 or completed < args.games:
            run_game(client, args.difficulty, args.mode, rng, args.max_steps,
                     args.flag_mines)
            completed += 1
            time.sleep(args.delay)
    except KeyboardInterrupt:
        print("stopped by user; games", completed, flush=True)
    print("games completed", completed, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
