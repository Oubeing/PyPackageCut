# -*- coding: utf-8 -*-
"""PyPackageCut 备选入口（带图标）。

逻辑完全复用 main.py / cutcore.py，仅额外设置窗口图标，保持两入口行为一致。
"""

import sys
from pathlib import Path
from PyQt5.QtGui import QIcon

import main


def main_with_icon():
    app = main.QApplication(sys.argv)
    window = main.MasterGui()
    icon = Path(__file__).parent.joinpath("file.ico")
    if icon.exists():
        window.setWindowIcon(QIcon(str(icon)))
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main_with_icon()
