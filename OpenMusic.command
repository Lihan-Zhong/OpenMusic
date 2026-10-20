#!/bin/bash
# Double-click in Finder: starts the OpenMusic workbench and opens it in the browser, or opens the one already
# running. To stop it, close this Terminal window or press Ctrl+C in it.
# 在访达里双击：启动 OpenMusic 工作台并在浏览器里打开（已经在运行就直接打开）。关掉这个终端窗口或按 Ctrl+C 停止。
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  echo "没有找到 .venv：先按 README 的「安装」一节装好环境。 / No .venv here: install first (README, Installation)."
  read -r -p "按回车关闭 / Press Enter to close " _
  exit 1
fi
.venv/bin/python experiments/app.py --reuse || read -r -p "出错了，按回车关闭 / Something went wrong; press Enter to close " _
