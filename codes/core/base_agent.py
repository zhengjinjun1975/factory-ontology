#!/usr/bin/env python3
"""base_agent.py — 原子智能体统一接口。

每个原子智能体 = 一个单一职责、无状态的 Python 模块，实现 BaseAgent 接口。
编排器通过统一的 run(input)->output 契约调度它们，互不感知、可独立替换。

设计原则：
- 单一职责：每个智能体只做一件事
- 无状态：不保存会话，输入输出都是纯数据
- 统一契约：run(task)->result，错误走 result.error
- 可组合：编排器按需串/并联
"""

from abc import ABC, abstractmethod


class AgentResult:
    """原子智能体的统一返回。"""
    def __init__(self, ok=True, data=None, error=None, agent="", elapsed_s=0.0):
        self.ok = ok
        self.data = data
        self.error = error
        self.agent = agent
        self.elapsed_s = elapsed_s

    def to_dict(self):
        return {
            "ok": self.ok, "agent": self.agent,
            "data": self.data, "error": self.error,
            "elapsed_s": round(self.elapsed_s, 3),
        }

    def __repr__(self):
        return f"<AgentResult {self.agent} ok={self.ok} err={self.error}>"


class BaseAgent(ABC):
    """原子智能体基类。所有智能体继承它并实现 run()。"""
    name = "base"

    def __init__(self):
        import time
        self._time = time

    @abstractmethod
    def run(self, task: dict) -> AgentResult:
        """执行任务。task 是 dict，返回 AgentResult。
        task 结构由各智能体定义，编排器只透传。"""
        raise NotImplementedError

    # 便捷方法
    def _ok(self, data=None, agent=None):
        return AgentResult(ok=True, data=data, agent=agent or self.name)

    def _err(self, error, agent=None):
        return AgentResult(ok=False, error=str(error), agent=agent or self.name)

    def _timed(self, fn, *a, **k):
        t0 = self._time.time()
        try:
            r = fn(*a, **k)
            r.elapsed_s = self._time.time() - t0
            return r
        except Exception as e:
            r = self._err(e)
            r.elapsed_s = self._time.time() - t0
            return r
