# 元扫雷 MCP 服务

元扫雷第三方插件。使用带 GitHub 插件发现功能的新版元扫雷。

## 安装

1. 在插件管理器中打开「插件」→「插件市场」。
2. 搜索 `metasweeper-mcp`，选择版本并安装，然后重启插件管理器。

也可以选择「从仓库 URL 安装」，输入 <https://github.com/ljzloser/metasweeper-mcp>。此仓库使用 GitHub 主题 `metasweeper-plugin` 和以 `metasweeper-v` 开头的版本标签。插件运行依赖由宿主提供。

服务默认停止。请先在插件界面启动服务，再连接 MCP 客户端。原有端口设置会从元扫雷数据目录读取。升级后如果授权已被清除，可在插件管理器中重新授予控制权限。

[插件使用说明](llm_minesweeper_controller/README.md)

## 开发与测试

在已按主仓说明安装开发依赖的环境中，设置元扫雷源码路径，再从本仓库根目录运行测试：

```powershell
$env:PYTHONPATH = 'D:\work\Python\Solvable-Minesweeper\src'
$env:QT_QPA_PLATFORM = 'offscreen'
& 'D:\work\Python\Solvable-Minesweeper\.venv\Scripts\python.exe' -m pytest
```

Qt 翻译的 `.ts` 和 `.qm` 都随包分发。修改翻译源文件后，用 `pyside6-lrelease` 重新生成对应的 `.qm`。

[GPLv3 许可证](LICENSE)
