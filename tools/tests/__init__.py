"""离线自测包 —— **不是** pytest 测试包。

入口在 `tools/selftest.py`（`python tools/selftest.py`），它按固定顺序调用下面
四个模块里的 `test_*` 函数。夹具在 `support.py`。
"""
