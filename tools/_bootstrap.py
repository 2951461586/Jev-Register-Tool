"""把仓库根加进 `sys.path`，让 `tools/` 任意深度的脚本都能 `import src.*`。

为什么需要它：脚本被移进 `tools/probes/` 之后，`Path(__file__).parent.parent`
算出来是 `tools/` 而不是仓库根，`import src` 就炸了。
与其在每个脚本里手算层级（加一层目录就要改一遍），不如统一 import 这个模块 ——
它按**标记文件**定位仓库根，跟脚本自身深度无关。

用法（放在其他 import 之前）：

    from _bootstrap import ROOT      # noqa: F401  （有副作用：改 sys.path）

    from src import config
"""

from __future__ import annotations

import sys
from pathlib import Path

#: 仓库根的标记文件（同时存在才认定是根，避免误判到子目录）
_MARKERS = ("src", ".gitignore")


def find_root(start: Path | None = None) -> Path:
    """从 start（默认本文件所在目录）向上找，返回第一个含全部标记的目录。"""
    here = (start or Path(__file__).resolve().parent).resolve()
    for cand in (here, *here.parents):
        if all((cand / m).exists() for m in _MARKERS):
            return cand
    raise RuntimeError(
        f"向上找不到仓库根（需要同时含 {_MARKERS}），起点 {here}"
    )


ROOT = find_root()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
