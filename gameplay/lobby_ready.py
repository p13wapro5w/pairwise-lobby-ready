"""服务端权威的房间准备状态机。

房间的全部关键状态都存放在服务端：准备状态、座位、房主、倒计时和语音频道。
客户端只提交操作意图，不能提交权威值（准备标记、人数上限、开始时间等）。

约定：
- 房间状态是一个普通 ``dict``，由 ``_as_room`` 归一化，便于序列化与测试。
- 修改房间的函数会原地更新房间并返回快照（``dict``）或布尔/整数结果。
- 所有入口都做了防御式处理：传入非房间对象时按空房间处理，绝不返回 ``None``。
"""

from __future__ import annotations

import functools
import threading
import time
from typing import Any

# ---------------------------------------------------------------------------
# 服务端策略常量（客户端配置不可覆盖这些值）
# ---------------------------------------------------------------------------

#: 房间人数硬上限。客户端即使把本地配置改成 6 或更大，服务端也只认这个值。
SERVER_MAX_CAPACITY = 5
DEFAULT_CAPACITY = 5

#: 服务端倒计时总时长；客户端只负责根据服务端给出的剩余时间渲染。
COUNTDOWN_MS = 5_000

#: 掉线后座位保留时长；期间重连可回到原座位，超时后座位回收。
SEAT_RESERVE_MS = 30_000

#: 无心跳超时。异常退出（没有正常断开事件）也会在一段时间后被清理。
STALE_TIMEOUT_MS = 45_000

#: 房间协议版本。主版本必须一致，次版本客户端不得低于服务端要求。
PROTOCOL_VERSION = "1.4.0"

#: 只有房主可以执行的管理操作。
HOST_ACTIONS = frozenset(
    {
        "kick",
        "start",
        "close",
        "transfer_host",
        "set_capacity",
        "lock",
        "remove_member",
    }
)

#: 普通成员可以对自己执行的操作。
MEMBER_ACTIONS = frozenset({"ready", "unready", "leave", "join_voice"})

_LOCK = threading.RLock()


def _serialized(fn):
    """串行化对房间状态的读写，避免同一房间出现并发写覆盖。"""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _LOCK:
            return fn(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    """服务端单调时钟（毫秒）。倒计时只以此为准。"""

    return time.monotonic_ns() // 1_000_000


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _voice_channel(room_id: Any) -> str:
    """语音频道由房间 ID 派生，换房即换频道。"""

    label = "unassigned" if room_id is None else str(room_id)
    return f"room:{label}:voice"


def _new_countdown() -> dict:
    return {
        "active": False,
        "started_at": None,
        "ends_at": None,
        "generation": 0,
    }


def _new_room(room_id: Any = None) -> dict:
    return {
        "id": room_id,
        "host": None,
        "members": {},
        "order": [],
        "capacity": DEFAULT_CAPACITY,
        "required_players": DEFAULT_CAPACITY,
        "countdown": _new_countdown(),
        "voice_channel": _voice_channel(room_id),
        "protocol_version": PROTOCOL_VERSION,
    }


def _as_room(value: Any) -> dict:
    """把任意输入归一化成服务端房间状态。

    - ``dict`` 视为已有房间并补齐缺失字段；
    - 其它值（例如测试里的房间 ID）视为新房间，而不是抛异常。
    - 人数上限强制夹在服务端允许的范围内，忽略客户端的越权配置。
    """

    room = value if isinstance(value, dict) else _new_room(value)

    room.setdefault("id", None)
    if not isinstance(room.get("members"), dict):
        room["members"] = {}
    if not isinstance(room.get("order"), list):
        room["order"] = []

    capacity = _safe_int(room.get("capacity"), DEFAULT_CAPACITY)
    room["capacity"] = max(1, min(capacity, SERVER_MAX_CAPACITY))

    required = _safe_int(room.get("required_players"), room["capacity"])
    room["required_players"] = max(1, min(required, room["capacity"]))

    if not isinstance(room.get("countdown"), dict):
        room["countdown"] = _new_countdown()
    else:
        countdown = room["countdown"]
        countdown.setdefault("active", False)
        countdown.setdefault("started_at", None)
        countdown.setdefault("ends_at", None)
        countdown.setdefault("generation", 0)
        if countdown["active"] and countdown["ends_at"] is None:
            countdown["active"] = False

    room.setdefault("host", None)
    room.setdefault("protocol_version", PROTOCOL_VERSION)
    room.setdefault("voice_channel", _voice_channel(room["id"]))

    for player, member in list(room["members"].items()):
        if not isinstance(member, dict):
            member = {}
            room["members"][player] = member
        member.setdefault("seat", None)
        member.setdefault("ready", False)
        member.setdefault("connected", True)
        member.setdefault("joined_at", None)
        member.setdefault("last_seen", member["joined_at"])
        member.setdefault("disconnected_at", None)
        member.setdefault("reserved_until", None)
        member.setdefault("version", None)
        member.setdefault("voice_channel", room["voice_channel"])

    for player in room["members"]:
        if player not in room["order"]:
            room["order"].append(player)
    room["order"][:] = [p for p in room["order"] if p in room["members"]]

    return room


def _ordered_players(room: dict) -> list:
    """按加入顺序返回玩家，保证房主迁移结果稳定。"""

    order = [p for p in room["order"] if p in room["members"]]
    for player in room["members"]:
        if player not in order:
            order.append(player)
    if order != room["order"]:
        room["order"][:] = order
    return list(room["order"])


def _next_host(room: dict) -> Any:
    """优先选择仍在线的最早加入者；都掉线时保留最早加入者作为房主。"""

    players = _ordered_players(room)
    for player in players:
        if room["members"][player].get("connected", True):
            return player
    return players[0] if players else None


def _remove_member(room: dict, player: Any):
    """移除成员；若移除的是房主则立即迁移。返回被移除的成员记录。"""

    if player not in room["members"]:
        return None
    member = room["members"].pop(player)
    if player in room["order"]:
        room["order"].remove(player)
    if room.get("host") == player:
        room["host"] = _next_host(room)
    return member


def _free_seat(room: dict):
    taken = {
        member.get("seat")
        for member in room["members"].values()
        if member.get("seat") is not None
    }
    for seat in range(int(room["capacity"])):
        if seat not in taken:
            return seat
    return None


def _cancel_countdown(room: dict, reason: str, now: int) -> bool:
    """取消正在进行的倒计时。返回是否真的发生了取消。"""

    countdown = room["countdown"]
    if not countdown.get("active"):
        return False
    countdown["active"] = False
    countdown["started_at"] = None
    countdown["ends_at"] = None
    countdown["generation"] = _safe_int(countdown.get("generation"), 0) + 1
    room["last_countdown_reset"] = {"reason": reason, "at": now}
    return True


def _countdown_view(room: dict, now: int) -> dict:
    countdown = room["countdown"]
    generation = _safe_int(countdown.get("generation"), 0)
    if not countdown.get("active"):
        return {
            "active": False,
            "started_at": None,
            "ends_at": None,
            "remaining_ms": 0,
            "complete": False,
            "generation": generation,
        }

    ends_at = _safe_int(countdown.get("ends_at"), now)
    remaining = max(0, ends_at - now)
    return {
        "active": True,
        "started_at": countdown.get("started_at"),
        "ends_at": ends_at,
        "remaining_ms": remaining,
        "complete": remaining == 0,
        "generation": generation,
    }


def _member_view(player: Any, member: dict, room_channel: str) -> dict:
    return {
        "player": player,
        "seat": member.get("seat"),
        "ready": bool(member.get("ready")),
        "connected": bool(member.get("connected", True)),
        "reserved_until": member.get("reserved_until"),
        "version": member.get("version"),
        "voice_channel": member.get("voice_channel", room_channel),
    }


def _snapshot(room: dict, now_ms: Any = None) -> dict:
    now = _now_ms() if now_ms is None else _safe_int(now_ms, 0)
    players = _ordered_players(room)
    members = [
        _member_view(player, room["members"][player], room["voice_channel"])
        for player in players
    ]
    seats = sorted(
        (
            {
                "seat": member["seat"],
                "player": player,
                "ready": member["ready"],
                "connected": member["connected"],
            }
            for player, member in (
                (player, room["members"][player]) for player in players
            )
            if member.get("seat") is not None
        ),
        key=lambda entry: entry["seat"],
    )
    ready_count = sum(1 for member in members if member["ready"])

    return {
        "room_id": room["id"],
        "host": room["host"],
        "capacity": room["capacity"],
        "required_players": room["required_players"],
        "member_count": len(members),
        "ready_count": ready_count,
        "all_ready": bool(members) and ready_count == len(members),
        "members": members,
        "seats": seats,
        "countdown": _countdown_view(room, now),
        "voice_channel": room["voice_channel"],
        "protocol_version": room["protocol_version"],
        "last_countdown_reset": room.get("last_countdown_reset"),
        "last_host_migration": room.get("last_host_migration"),
        "server_time_ms": now,
    }


def _parse_version(value: Any):
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in {"v", "V"}:
            text = text[1:]
        parts = text.split(".") if text else []
    elif isinstance(value, (tuple, list)):
        parts = list(value)
    elif isinstance(value, int) and not isinstance(value, bool):
        parts = [value]
    else:
        return None

    if not parts:
        return None

    numbers = []
    for part in parts[:3]:
        try:
            numbers.append(int(part))
        except (TypeError, ValueError):
            return None
    while len(numbers) < 3:
        numbers.append(0)
    return tuple(numbers)


def _version_compatible(room: dict, client_version: Any) -> bool:
    required = _parse_version(room.get("protocol_version"))
    client = _parse_version(client_version)
    if required is None or client is None:
        return False
    # 主版本必须一致（跨平台协议不兼容），次版本不得低于服务端要求。
    if client[0] != required[0]:
        return False
    return client[1:] >= required[1:]


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------


@_serialized
def server_state(room=None, now_ms=None, *_args) -> dict:
    """返回服务端权威快照；客户端的准备状态不会覆盖这里的值。"""

    room = _as_room(room)
    return _snapshot(room, now_ms)


@_serialized
def host_migrate(room=None, now_ms=None, *_args) -> dict:
    """房主掉线/离开后，把房主迁移给最早加入且仍在线的成员。"""

    room = _as_room(room)
    now = _now_ms() if now_ms is None else _safe_int(now_ms, 0)
    previous = room.get("host")
    current = previous
    current_ok = (
        current in room["members"]
        and room["members"][current].get("connected", True)
    )
    if not current_ok:
        current = _next_host(room)
    room["host"] = current
    if current != previous:
        room["last_host_migration"] = {
            "from": previous,
            "to": current,
            "at": now,
        }
    snapshot = _snapshot(room, now)
    snapshot["previous_host"] = previous
    return snapshot


@_serialized
def server_cap(room=None, requested=None, *_args) -> dict:
    """人数上限只认服务端配置，客户端传来的上限仅用于对照。"""

    room = _as_room(room)
    capacity = int(room["capacity"])
    requested_cap = (
        None if requested is None else _safe_int(requested, capacity)
    )
    member_count = len(room["members"])
    return {
        "capacity": capacity,
        "server_max": SERVER_MAX_CAPACITY,
        "requested": requested_cap,
        "honored": requested_cap is None or requested_cap == capacity,
        "member_count": member_count,
        "open_seats": max(0, capacity - member_count),
        "accepts_join": member_count < capacity,
    }


@_serialized
def leave_reset(room=None, player=None, now_ms=None, *_args) -> dict:
    """成员离开：移除成员，并取消/重置进行中的服务端倒计时。"""

    room = _as_room(room)
    now = _now_ms() if now_ms is None else _safe_int(now_ms, 0)
    removed = _remove_member(room, player)
    countdown_reset = False
    if removed is not None:
        countdown_reset = _cancel_countdown(room, "member_left", now)
    snapshot = _snapshot(room, now)
    snapshot["removed"] = player if removed is not None else None
    snapshot["countdown_reset"] = countdown_reset
    return snapshot


@_serialized
def seat_reserve(room=None, player=None, now_ms=None, *_args) -> dict:
    """掉线时保留原座位，给重连留出宽限期。"""

    room = _as_room(room)
    now = _now_ms() if now_ms is None else _safe_int(now_ms, 0)
    member = room["members"].get(player)
    reserved = member is not None
    if reserved:
        member["connected"] = False
        member["disconnected_at"] = now
        member["reserved_until"] = now + SEAT_RESERVE_MS
        # seat 不变：这是重连回原座位的依据。
    snapshot = _snapshot(room, now)
    snapshot["reserved"] = reserved
    snapshot["seat"] = member.get("seat") if reserved else None
    return snapshot


@_serialized
def server_timer(room=None, now_ms=None, *_args) -> dict:
    """根据服务端时钟计算倒计时，客户端只按返回值渲染。"""

    room = _as_room(room)
    now = _now_ms() if now_ms is None else _safe_int(now_ms, 0)
    view = _countdown_view(room, now)
    view["server_time_ms"] = now
    return view


@_serialized
def permission_check(room=None, actor=None, action="kick", *_args) -> bool:
    """校验操作权限：管理操作仅房主可做，成员操作要求是房间成员。"""

    room = _as_room(room)
    if actor is None:
        return False
    host = room.get("host")
    if action in MEMBER_ACTIONS:
        return actor == host or actor in room["members"]
    if action in HOST_ACTIONS:
        return host is not None and actor == host
    return False


@_serialized
def version_gate(room=None, client_version=None, *_args) -> bool:
    """协议版本门槛；未知版本一律拒绝，避免跨版本进同一局直接崩溃。"""

    room = _as_room(room)
    return _version_compatible(room, client_version)


@_serialized
def voice_follow(room=None, player=None, *_args) -> str:
    """语音频道跟随房间；换房后所有成员一起切到新频道。"""

    room = _as_room(room)
    channel = _voice_channel(room["id"])
    room["voice_channel"] = channel
    for member in room["members"].values():
        member["voice_channel"] = channel
    return channel


@_serialized
def stale_cleanup(room=None, now_ms=None, *_args) -> dict:
    """清理超时占位：保留期已过的掉线成员、长期无心跳的成员。"""

    room = _as_room(room)
    now = _now_ms() if now_ms is None else _safe_int(now_ms, 0)
    cleaned = []

    for player in list(_ordered_players(room)):
        member = room["members"].get(player)
        if member is None:
            continue

        if member.get("connected", True):
            last_seen = member.get("last_seen")
            if (
                last_seen is not None
                and now - _safe_int(last_seen, now) > STALE_TIMEOUT_MS
            ):
                _remove_member(room, player)
                cleaned.append(player)
            continue

        deadline = member.get("reserved_until")
        if deadline is None:
            disconnected_at = member.get("disconnected_at")
            deadline = (
                None
                if disconnected_at is None
                else _safe_int(disconnected_at, now) + SEAT_RESERVE_MS
            )
        if deadline is not None and now >= _safe_int(deadline, now):
            _remove_member(room, player)
            cleaned.append(player)

    countdown_reset = (
        _cancel_countdown(room, "stale_member_removed", now) if cleaned else False
    )
    snapshot = _snapshot(room, now)
    snapshot["cleaned_players"] = cleaned
    snapshot["countdown_reset"] = countdown_reset
    return snapshot


@_serialized
def ready_toggle(room=None, player=None, ready=True, *_args) -> dict:
    """准备状态只写服务端；状态变化会打断进行中的倒计时。"""

    room = _as_room(room)
    now = _now_ms()
    member = room["members"].get(player)
    changed = False
    if member is not None:
        desired = bool(ready)
        changed = bool(member.get("ready")) != desired
        member["ready"] = desired
        member["last_seen"] = now
        if changed:
            _cancel_countdown(room, "ready_changed", now)
    snapshot = _snapshot(room, now)
    snapshot["changed"] = changed
    return snapshot


@_serialized
def slot_assign(room=None, player=None, version=None, now_ms=None, *_args) -> int:
    """分配座位（加入房间）。返回座位号，满员或版本不符时返回 -1。"""

    room = _as_room(room)
    now = _now_ms() if now_ms is None else _safe_int(now_ms, 0)
    if player is None:
        return -1

    existing = room["members"].get(player)
    if existing is not None:
        existing["last_seen"] = now
        seat = existing.get("seat")
        return -1 if seat is None else int(seat)

    if version is not None and not _version_compatible(room, version):
        return -1
    if len(room["members"]) >= int(room["capacity"]):
        return -1

    seat = _free_seat(room)
    if seat is None:
        return -1

    room["members"][player] = {
        "seat": seat,
        "ready": False,
        "connected": True,
        "joined_at": now,
        "last_seen": now,
        "disconnected_at": None,
        "reserved_until": None,
        "version": version,
        "voice_channel": room["voice_channel"],
    }
    room["order"].append(player)
    if room["host"] is None:
        room["host"] = player
    return seat


@_serialized
def start_guard(room=None, now_ms=None, *_args) -> dict:
    """开局校验并由服务端启动倒计时。

    返回 ``{"ok", "reason", "launch", "countdown"}``：
    - 条件不足：``ok=False``，``reason`` 说明原因；
    - 条件满足且刚启动倒计时：``ok=True``，``launch=False``；
    - 倒计时结束：``ok=True``，``launch=True``，由服务端放行开局。
    """

    room = _as_room(room)
    now = _now_ms() if now_ms is None else _safe_int(now_ms, 0)
    members = room["members"]

    if room["countdown"].get("active"):
        view = _countdown_view(room, now)
        return {
            "ok": view["complete"],
            "reason": None if view["complete"] else "countdown_in_progress",
            "launch": view["complete"],
            "countdown": view,
        }

    required = int(room["required_players"])
    reason = None
    if room["host"] is None or room["host"] not in members:
        reason = "no_host"
    elif len(members) < required:
        reason = "not_enough_players"
    elif len(members) > int(room["capacity"]):
        reason = "over_capacity"
    elif any(member.get("seat") is None for member in members.values()):
        reason = "unseated_player"
    elif any(not member.get("connected", True) for member in members.values()):
        reason = "player_offline"
    elif any(not member.get("ready", False) for member in members.values()):
        reason = "player_not_ready"
    elif any(
        member.get("version") is not None
        and not _version_compatible(room, member["version"])
        for member in members.values()
    ):
        reason = "version_mismatch"

    if reason is not None:
        return {
            "ok": False,
            "reason": reason,
            "launch": False,
            "countdown": _countdown_view(room, now),
        }

    countdown = room["countdown"]
    countdown["active"] = True
    countdown["started_at"] = now
    countdown["ends_at"] = now + COUNTDOWN_MS
    countdown["generation"] = _safe_int(countdown.get("generation"), 0) + 1
    return {
        "ok": True,
        "reason": "countdown_started",
        "launch": False,
        "countdown": _countdown_view(room, now),
    }


@_serialized
def reconnect_seat(room=None, player=None, now_ms=None, *_args) -> dict:
    """重连回原座位；保留期已过则回收座位并拒绝恢复。"""

    room = _as_room(room)
    now = _now_ms() if now_ms is None else _safe_int(now_ms, 0)
    member = room["members"].get(player)
    if member is None:
        return {
            "recovered": False,
            "reason": "unknown_player",
            "seat": None,
            "state": _snapshot(room, now),
        }

    if member.get("connected", True):
        member["last_seen"] = now
        return {
            "recovered": True,
            "reason": "already_connected",
            "seat": member.get("seat"),
            "state": _snapshot(room, now),
        }

    reserved_until = member.get("reserved_until")
    if reserved_until is None:
        disconnected_at = _safe_int(member.get("disconnected_at"), now)
        reserved_until = disconnected_at + SEAT_RESERVE_MS
        member["reserved_until"] = reserved_until

    if now > _safe_int(reserved_until, now):
        _remove_member(room, player)
        _cancel_countdown(room, "reservation_expired", now)
        return {
            "recovered": False,
            "reason": "reservation_expired",
            "seat": None,
            "state": _snapshot(room, now),
        }

    member["connected"] = True
    member["last_seen"] = now
    member["disconnected_at"] = None
    member["reserved_until"] = None
    return {
        "recovered": True,
        "reason": "seat_restored",
        "seat": member.get("seat"),
        "state": _snapshot(room, now),
    }


@_serialized
def kick_auth(room=None, actor=None, target=None, *_args) -> bool:
    """只有房主能踢人，且房主自身不能被踢（需要先转移房主）。"""

    room = _as_room(room)
    now = _now_ms()
    if not permission_check(room, actor, "kick"):
        return False
    if target is None or target not in room["members"]:
        return False
    if target == room["host"]:
        return False

    _remove_member(room, target)
    _cancel_countdown(room, "member_kicked", now)
    return True
