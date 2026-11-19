from pathlib import Path
import xml.etree.ElementTree as ET

import pytest
from PySide6.QtCore import QCoreApplication, QTranslator

from plugin_manager.translations import _PUBLIC_CONTEXTS


PACKAGE = Path(__file__).resolve().parents[1] / "llm_minesweeper_controller"
CONTEXT = "MinesweeperMcp"


@pytest.mark.parametrize("locale", ("en_US", "de_DE", "ja_JP", "pl_PL"))
def test_catalog_is_private_complete_and_loadable(qapp, locale):
    source = PACKAGE / "translations" / f"{locale}.ts"
    tree = ET.parse(source)
    assert {node.findtext("name") for node in tree.findall(".//context")} == {CONTEXT}
    assert CONTEXT not in _PUBLIC_CONTEXTS
    messages = tree.findall(".//message")
    assert messages
    translator = QTranslator()
    assert translator.load(str(source.with_suffix(".qm")))
    for message in messages:
        translation = message.find("translation")
        assert translation is not None, message.findtext("source")
        assert translation.get("type") not in ("unfinished", "vanished", "obsolete")
        assert "".join(translation.itertext()).strip(), message.findtext("source")
    assert any(translator.translate(CONTEXT, message.findtext("source")) for message in messages)


def test_plugin_sources_use_private_contexts():
    for source in PACKAGE.rglob("*.py"):
        text = source.read_text(encoding="utf-8")
        assert 'translate("Form"' not in text, source
        assert 'translate("SkinManager"' not in text, source


def test_mcp_view_refreshes_after_language_change(qapp, qtbot):
    from llm_minesweeper_controller.widgets import McpControlWidget

    class Translator(QTranslator):
        def isEmpty(self):
            return False

        def translate(self, context, source, disambiguation=None, n=-1):
            values = {"监听端口": "Port", "启动MCP": "Start MCP"}
            return values.get(source) if context == CONTEXT else None

    widget = McpControlWidget()
    qtbot.addWidget(widget)
    widget.show()
    translator = Translator()
    assert QCoreApplication.installTranslator(translator)
    try:
        qtbot.waitUntil(lambda: widget.action_button.text() == "Start MCP")
        assert widget._port_label.text() == "Port"
    finally:
        QCoreApplication.removeTranslator(translator)


def test_plugin_window_starts_hidden(qapp):
    from llm_minesweeper_controller.plugin import LlmMinesweeperControllerPlugin

    assert LlmMinesweeperControllerPlugin.plugin_info().show_window is False
