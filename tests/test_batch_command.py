from __future__ import annotations

from lib_zmq_plugins.shared.base import CommandResponse
from shared_types.queries import BoardStateResult
from llm_minesweeper_controller.tools import MinesweeperTools
from shared_types.commands import MouseClickCommand


def live_board(*_):
    return BoardStateResult(
        request_id="snapshot", cells=[[10, 10, 10]], rows=1, cols=3,
        mines_remaining=0, game_time=0.0,
        game_status="playing", game_status_code=2)


def test_mcp_batch_sends_one_mouse_click_command_with_all_cells():
    commands = []

    def request(command, timeout):
        commands.append(command)
        return CommandResponse(request_id="batch", success=True)

    tools = MinesweeperTools(lambda _: True, request, live_board)
    result = tools.execute_function("click_cell", {
        "clicks": [{"row": 0, "col": 0}, {"row": 0, "col": 2}],
        "button": "right"})

    assert result["success"] and result["completed"] == 2
    assert len(commands) == 1
    assert isinstance(commands[0], MouseClickCommand)
    assert commands[0].cells == [(0, 0), (0, 2)]
    assert commands[0].button == 2


def test_mcp_batch_rejection_is_unknown_and_never_retried():
    commands = []

    def request(command, timeout):
        commands.append(command)
        return CommandResponse(request_id="batch", success=False, error="Rejected")

    tools = MinesweeperTools(lambda _: True, request, live_board)
    result = tools.execute_function("click_cell", {
        "clicks": [{"row": 0, "col": 0}, {"row": 0, "col": 1}]})

    assert not result["success"] and result["outcome"] == "unknown"
    assert result["completed"] == 0 and result["stopped_index"] == 0
    assert len(result["results"]) == 2
    assert all(entry["outcome"] == "unknown" for entry in result["results"])
    assert len(commands) == 1


def test_mcp_rejects_batch_with_per_cell_buttons_before_sending():
    commands = []
    tools = MinesweeperTools(
        lambda _: True,
        lambda command, timeout: commands.append(command),
        live_board)
    result = tools.execute_function("click_cell", {
        "clicks": [{"row": 0, "col": 0, "button": "right"}]})

    assert not result["success"] and result["completed"] == 0
    assert not commands
