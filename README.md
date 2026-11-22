# pairwise-lobby-ready

## 领域

游戏、图形与多媒体

## 主题

房间准备

## 说明

服务端状态、房主迁移与座位保留。

初始状态下 `gameplay/lobby_ready.py` 中所有函数抛出 `NotImplementedError`，测试全部失败。
修复实现后运行：

```bash
python -m pytest -q
```

期望全部通过。
