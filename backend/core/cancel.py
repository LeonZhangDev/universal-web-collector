"""取消信号。

单独建这个模块是为了打破循环依赖: downloaders 需要在自己的异常处理里
识别"用户点了停止", 但它不能 import core.task_manager(后者反过来依赖
downloaders)。于是把异常类型放在谁都能依赖的最底层。

为什么非要让下载层知道: 如果不特殊对待, TaskCancelled 会被
`except Exception` 当成一次普通失败 —— 结果是退避重睡若干秒后重试一个
注定被叫停的请求, 用户点了停止却要干等半分钟。
"""


class TaskCancelled(Exception):
    """任务被用户叫停。

    载体是当前任务的 threading.Event, 检查点分散在:
      * 资源边界(_download_one 开头)
      * 每个数据块(progress_cb, 避免 500MB 的视频要下完才退出)
      * 状态迁移点(_transition)
    """
