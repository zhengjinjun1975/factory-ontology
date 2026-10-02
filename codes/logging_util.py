# -*- coding: utf-8 -*-
"""容错留痕统一出口（代码审查 P1-2）。

背景：仓库里有大量 ``except Exception: pass/return 空值`` 的容错点。容错语义本身没问题，
但完全不留痕会让"配置损坏"表现为"数据不存在"：例如 kbs.json 读失败静默成空注册表，
随后所有 kb 查询都答复"未注册"，现场只能靠猜。本模块只做一件事：把降级事实写进与
主服务同名的 logger，**不改变任何控制流**。
"""

import logging

logger = logging.getLogger("food-api")


def note_swallow(where, exc):
    """记录一处被容错的异常：where = 所属函数，exc = 捕获到的异常实例。"""
    logger.warning("[容错] %s: %s: %s", where, type(exc).__name__, exc)
