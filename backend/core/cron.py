"""5 段 cron 表达式的解析与"下一次触发时刻"。

为什么自己写而不引第三方
========================
调度只需要一个函数: `next_after(expr, now)`。为此引一个依赖(croniter /
APScheduler)会带进来一整套用不上的东西, 而每多一个依赖就多一处
"CI 少装了包 → collection 整片红"的机会(见 PITFALLS 的 V39 六)。

支持的范围
==========
`分 时 日 月 周`, 五段必填, 数值制(不用 JAN / MON 之类的名字 —— 名字要
额外一份映射表, 而"写错就静默不跑"的风险比多敲两个数字大)。

每段支持 `*`、`*/n`、`a`、`a-b`、`a-b/n`, 以及逗号分隔的列表。
星期 `0` 与 `7` 都是周日(与标准 cron 一致)。

⚠️ **星期与日期同时限定时的语义**: 标准 cron 是"**两者满足其一**就触发",
不是"两者都满足"。写成"都满足"的话, `0 3 1 * 1`(每月 1 号**或**每周一
凌晨 3 点)会变成"只有恰好是周一的那个 1 号才跑" —— 一年一次, 而用户以为
配置对了。这个错**不报错**, 只是几乎不跑。

失败必须早
==========
`parse()` 在**创建订阅时**就调用并 400。等到调度器去解析一个坏表达式时,
它只会导致"这个订阅再也不跑了", 而没有任何人报错 —— 那正是本项目最
警惕的那类静默。
"""

from datetime import datetime, timedelta

#: 一周七天; 0 与 7 都是周日
_DOW_LO, _DOW_HI = 0, 7

#: 各段的取值范围: (下界, 上界)
_RANGES = (
    (0, 59),    # 分
    (0, 23),    # 时
    (1, 31),    # 日
    (1, 12),    # 月
    (_DOW_LO, _DOW_HI),   # 星期(7 归一到 0)
)

#: 最多往后找多少天。5 年足以容纳 2 月 29 日; 找不到就报错而不是无限循环。
_MAX_DAYS = 366 * 5


class CronError(ValueError):
    """表达式不合法。用 ValueError 的子类是为了让调用方 `except ValueError`
    一处接住(API 转 400), 同时错误信息仍然能说清是 cron 的问题。"""


def _expand_item(item, lo, hi):
    """展开一段里的一个元素(`*`、`*/3`、`5`、`1-5`、`1-10/2`), 返回整数集合。"""
    item = item.strip()
    if not item:
        raise CronError("空的 cron 字段")
    if item == "*":
        return set(range(lo, hi + 1))

    step = 1
    if "/" in item:
        item, _, tail = item.partition("/")
        if not item:
            raise CronError("`/%s` 前面缺少范围" % tail)
        if not tail.isdigit() or int(tail) < 1:
            raise CronError("步长必须是 >=1 的整数: %r" % tail)
        step = int(tail)

    if item == "*":
        base = list(range(lo, hi + 1))
    elif "-" in item:
        head, _, tail = item.partition("-")
        if not head.isdigit() or not tail.isdigit():
            raise CronError("范围必须是 `起-止` 两个整数: %r" % item)
        start, end = int(head), int(tail)
        if start > end:
            raise CronError("范围的起点大于终点: %r" % item)
        base = list(range(start, end + 1))
    elif item.isdigit():
        base = [int(item)]
    else:
        raise CronError("认不出这一段: %r" % item)

    out = set()
    for i, value in enumerate(base):
        if value < lo or value > hi:
            raise CronError("%d 超出取值范围 %d-%d" % (value, lo, hi))
        if i % step == 0:
            out.add(value)
    if not out:
        raise CronError("这一段展开后是空的: %r" % item)
    return out


def _field(part, lo, hi):
    """展开一个字段(可能含逗号)。"""
    if part.strip() == "*":
        return set(range(lo, hi + 1))
    out = set()
    for piece in part.split(","):
        out |= _expand_item(piece, lo, hi)
    return out


def parse(expr):
    """解析成 `(分, 时, 日, 月, 周)` 五个集合; 不合法抛 CronError。

    ⚠️ 星期段把 7 归一到 0, 并且**保留** 6(周六): 归一要在展开之后做,
    否则 `5-7` 这种跨周日的范围会被切成两段而丢掉一天。
    """
    parts = str(expr or "").split()
    if len(parts) != 5:
        raise CronError(
            "cron 必须是 5 段(`分 时 日 月 周`), 收到 %d 段: %r" % (len(parts), expr)
        )
    fields = [_field(p, lo, hi) for p, (lo, hi) in zip(parts, _RANGES)]
    dows = fields[4]
    if 7 in dows:
        dows.discard(7)
        dows.add(0)
    # 星期在 SQLite/Python 里都是"周一=0 … 周日=6", 与 cron 的"周日=0"差一天。
    # ⚠️ 这里**显式**平移而不是让调用方记: 差一天的错不报错, 只是每周都跑错一天。
    fields[4] = {(d - 1) % 7 for d in dows}
    return tuple(fields)


def describe(expr):
    """校验 + 归一化描述。合法返回 None, 不合法返回**中文错误原因**。

    给 API 用来回 400(用 detail 带具体原因, 而不是一句"cron 不合法" ——
    第 K 条: 代号管"红不红", 文案管"哪里红")。
    """
    try:
        parse(expr)
    except CronError as exc:
        return str(exc)
    return None


def _first_ge(values, target):
    """集合里 >= target 的最小元素; 没有返回 None。"""
    bigger = [v for v in values if v >= target]
    return min(bigger) if bigger else None


def _start_of_next_month(dt):
    """下个月 1 号 00:00。"""
    if dt.month == 12:
        return datetime(dt.year + 1, 1, 1)
    return datetime(dt.year, dt.month + 1, 1)


def _next_day(dt):
    """第二天 00:00。"""
    return (dt + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def _day_matches(dt, doms, dows):
    """这一天是否命中。⚠️ 日与周**同时**被限定时取"满足其一"(标准 cron 语义),
    详见模块 docstring —— 取"同时满足"会让表达式几乎永不触发。"""
    dom_restricted = len(doms) < 31
    dow_restricted = len(dows) < 7
    hit_dom = dt.day in doms
    hit_dow = dt.weekday() in dows
    if dom_restricted and dow_restricted:
        return hit_dom or hit_dow
    if dom_restricted:
        return hit_dom
    if dow_restricted:
        return hit_dow
    return True


def next_after(expr, now=None):
    """表达式在 `now` 之后的第一次触发时刻(分钟精度), 返回 datetime。

    `now` 必须显式给或默认当前时刻; 返回值**总是严格晚于** `now` —— 给当前
    时刻时不会返回"现在", 否则调度器会在同一分钟里把它当成到期、反复触发。

    找不到(比如 `0 0 30 2 *`, 2 月 30 日不存在)时抛 CronError 而不是返回
    None: `None` 落到 `next_run` 列上会变成"永不到期", 而那正是"订阅悄悄
    不跑了"的样子。
    """
    mins, hours, doms, months, dows = parse(expr)
    cur = (now or datetime.now()).replace(second=0, microsecond=0)
    cur += timedelta(minutes=1)

    for _ in range(_MAX_DAYS):
        if cur.month not in months:
            cur = _start_of_next_month(cur)
            continue
        if not _day_matches(cur, doms, dows):
            cur = _next_day(cur)
            continue
        hour = _first_ge(hours, cur.hour)
        if hour is None:
            cur = _next_day(cur)
            continue
        if hour > cur.hour:
            cur = cur.replace(hour=hour, minute=0)
        minute = _first_ge(mins, cur.minute)
        if minute is None:
            # 这个小时剩下的分钟都不行 → 看下一个合法小时(可能跨天, 交给下一轮)
            nxt = _first_ge(hours, cur.hour + 1)
            cur = _next_day(cur) if nxt is None else cur.replace(hour=nxt, minute=0)
            continue
        return cur.replace(hour=hour, minute=minute, second=0, microsecond=0)

    raise CronError("这个表达式在 5 年内都没有下一次触发时间: %r" % expr)


def next_after_str(expr, now=None, fmt="%Y-%m-%d %H:%M:%S"):
    """`next_after` 的字符串版本 —— `watches.next_run` 列存的就是这个格式。"""
    return next_after(expr, now).strftime(fmt)
