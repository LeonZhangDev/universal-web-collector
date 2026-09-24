"""闸的结论: **结构化**, 不是"一串给人看的文案"。

为什么单独一个模块
==================
`Gate` / `Problem` 是**所有门禁脚本共用的判据形状**: `scripts/add_site.py`(加站四闸)
与 `scripts/gateguard.py`(仓库级"声称 vs 实际")读的是同一份定义。

与 `core/jsonstore.py` / `core/layout.py` 同一条道理: **只留一个定义**, 其他组件都
来问它。判据形状一旦有两份, 就会出现"这边认 `checked`、那边不认" —— 而这种不一致
不会报错, 只会让某一道闸悄悄变成永远绿。

两条规矩, 各治一种"绿/红是假的"的病
====================================

① 每道闸必须报 `checked`(实际核了几项)。`checked == 0` **不许算绿**。
   治的是**假绿**里最常见的一种: 闸跑通了、印了一行 ok, 但它一项都没核 ——
   因为它要核的东西压根不存在(没有样本 / 拼不出 URL / 站点没注册)。
   "没验到" 与 "验过了没问题" 是两件事, 混在一行 ok 里就是撒谎。

② 问题带 `kind`(代号), 测试断言 `kind`, 不断言中文文案。
   治的是**假红**: 本项目真实踩过一条 —— 断言写 `"chrome" not in ip`, 而那句
   文案里提到 Chrome 是在说"**换指纹没用**"(IP 封禁只能换出口), 于是一条**正确**
   的文案把测试判红了。文案是给人看的: 会改措辞、会有反讽、会被翻译; 判据必须是代号。
   (同族: `core/errors.py` 用 `error_kind` 归类, 而不是 match 中文标签。)
"""

from __future__ import annotations

__all__ = ["Gate", "Problem", "NOTHING_CHECKED", "problem"]

#: 闸跑了, 但一项都没核 -> 一律算红
NOTHING_CHECKED = "nothing-checked"


class Problem:
    """一条问题: `kind` 给机器判, `message` 给人看。"""

    __slots__ = ("kind", "message")

    def __init__(self, kind, message):
        self.kind = kind
        self.message = message

    def __str__(self):
        return self.message

    def __repr__(self):
        return "Problem(%r, %r)" % (self.kind, self.message)


class Gate:
    """一道闸的结论。

    `checked` 是**必填**且**必须是实际数目** —— 它是这整套门禁里唯一能防"空转绿"
    的字段。写新闸时忘了填, 这闸就是永远绿的, 而没人看得出来。
    """

    __slots__ = ("title", "checked", "problems", "rows")

    def __init__(self, title, checked=0, problems=(), rows=()):
        self.title = title
        self.checked = int(checked)
        self.problems = list(problems)
        self.rows = list(rows)

    @property
    def empty(self):
        """空转: 一项都没核 —— 这不是"通过"。"""
        return self.checked == 0

    def effective_problems(self):
        """对外的问题清单: **空转也算一条问题**。

        放在这里(而不是散在各个 `section()` 分支里)的理由: 让"空转不许算绿"成为
        `Gate` 自己的性质, 那么 `ok` / `verify()` / 测试三处读的是同一个判据 ——
        不会出现"脚本记得拦、测试忘了拦"这种半拉子护栏。
        """
        if self.empty and not self.problems:
            return [problem(NOTHING_CHECKED,
                            "这道闸**一项都没核到**(要核的对象不存在)。"
                            "「没验到」与「验过了没问题」是两件事 —— 前者不许算绿。")]
        return list(self.problems)

    @property
    def ok(self):
        return not self.effective_problems()

    def kinds(self):
        return {p.kind for p in self.effective_problems()}

    def verdict(self):
        """一行结论 —— 所有门禁脚本印的是**同一句话的形状**。

        为什么把它放在 `Gate` 上而不是各自 `print`: 这条行是"每道闸都必须报核了
        几项"的唯一可见载体。让两个脚本各写一遍, 迟早有一个忘掉 `checked`
        (2026-09-24 之前 `gateguard` 就是这么漏的), 于是**这道闸又变成永远绿**。
        """
        problems = self.effective_problems()
        if not problems:
            return "核了 %d 项: ok" % self.checked
        kinds = ", ".join(sorted({p.kind for p in problems}))
        return "核了 %d 项: %d 个问题 [%s]" % (self.checked, len(problems), kinds)


def problem(kind, message):
    """造一条问题。`kind` 给机器判, `message` 给人看。"""
    return Problem(kind, message)
