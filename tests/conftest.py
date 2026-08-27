"""pytest 公共装置。

插件目录本身就是一个 Python 包（有 __init__.py），运行时 AstrBot 是以
astrbot_plugin_persona_prism.prism.xxx 这样的全名导入的。这里把插件的父目录
加进 sys.path，让单测用同样的路径导入，避免出现「测试能过、上线报 ImportError」。

这个文件刻意只做 sys.path 处理、不导入任何插件模块 —— pytest 会先加载
conftest 再加载测试模块，所以各测试文件顶部可以正常写 import。
"""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
PLUGIN_PARENT = str(PLUGIN_DIR.parent)

if PLUGIN_PARENT not in sys.path:
    sys.path.insert(0, PLUGIN_PARENT)
