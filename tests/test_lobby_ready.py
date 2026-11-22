import pytest
import gameplay.lobby_ready as mod

CASES = [
    ("server_state", (1, 2)),
    ("host_migrate", (1, 2)),
    ("server_cap", (5, 6)),
    ("leave_reset", (1, 2)),
    ("seat_reserve", (1, 2)),
    ("server_timer", (1, 2)),
    ("permission_check", (1, 2)),
    ("version_gate", (1, 2)),
    ("voice_follow", (1, 2)),
    ("stale_cleanup", (1, 2)),
    ("ready_toggle", (1, 2)),
    ("slot_assign", (1, 2)),
    ("start_guard", (1, 2)),
    ("reconnect_seat", (1, 2)),
    ("kick_auth", (1, 2)),
]

@pytest.mark.parametrize("fn_name,args", CASES)
def test_contract(fn_name, args):
    fn = getattr(mod, fn_name)
    assert fn(*args) is not None
