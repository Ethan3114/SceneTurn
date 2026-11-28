import json
from secrets import token_urlsafe
from uuid import uuid4

import pytest
from pydantic import SecretStr

from app.services.deployment_queue import DeploymentQueueError, DispatchMarker, epoch_milliseconds
from app.services.deployment_queue_gateway import (
    QUEUE_EVENT_TYPE,
    CloudBaseDeploymentQueue,
    _check_acknowledgement,
    _sign,
    verified_queue_event,
)
from cloud_timer import main_handler


def test_mutations_require_the_correct_token_action_marker_and_time():
    token = token_urlsafe()
    marker = DispatchMarker(epoch_milliseconds(), "text", uuid4(), uuid4())
    issued_at = str(epoch_milliseconds() // 1000)
    event = {
        "Type": QUEUE_EVENT_TYPE,
        "action": "publish",
        "key": marker.key,
        "issuedAt": issued_at,
        "signature": _sign(token, "publish", marker.key, issued_at),
    }
    assert verified_queue_event(event, token) == ("publish", marker)
    for changes in ({"action": "remove"}, {"key": marker.key.upper()}, {"issuedAt": "0" * 10}, {"Type": "Timer"}):
        with pytest.raises(ValueError, match="未授权"):
            verified_queue_event(event | changes, token)
    with pytest.raises(ValueError, match="未授权"):
        verified_queue_event(event, token_urlsafe())
    expired = str(int(issued_at) - 61)
    with pytest.raises(ValueError, match="未授权"):
        verified_queue_event(event | {"issuedAt": expired, "signature": _sign(token, "publish", marker.key, expired)}, token)
    invalid_key = marker.key + ".zip"
    with pytest.raises(ValueError, match="对象路径无效"):
        verified_queue_event(event | {"key": invalid_key, "signature": _sign(token, "publish", invalid_key, issued_at)}, token)


def test_event_handler_rejects_unsigned_mutations_and_timer_events():
    with pytest.raises(ValueError, match="队列写入事件未授权"):
        main_handler({"Type": QUEUE_EVENT_TYPE, "action": "remove"}, None)
    with pytest.raises(ValueError, match="调度事件未授权"):
        main_handler({"Type": "Timer", "TriggerName": "sceneturn-due-jobs"}, None)


def test_queue_result_must_acknowledge_only_the_expected_action_and_key():
    key = DispatchMarker(epoch_milliseconds(), "text", uuid4(), uuid4()).key
    ack = {"action": "remove", "key": key}
    for value in (ack, {"result": ack}, {"result": json.dumps(ack), "requestId": str(uuid4())}):
        _check_acknowledgement(json.dumps(value).encode(), "remove", key)
    for value in (ack | {"action": "publish"}, ack | {"unexpected": True}, {"result": ack, "code": "ACTION_FORBIDDEN"}):
        with pytest.raises(DeploymentQueueError, match="不一致"):
            _check_acknowledgement(json.dumps(value).encode(), "remove", key)
    with pytest.raises(DeploymentQueueError, match="无法读取"):
        _check_acknowledgement(b"{", "remove", key)


def test_cloudbase_queue_client_validates_environment_and_credentials():
    secret = SecretStr(token_urlsafe())
    assert CloudBaseDeploymentQueue("sceneturn-1", secret, secret).env_id == "sceneturn-1"
    with pytest.raises(DeploymentQueueError, match="环境标识无效"):
        CloudBaseDeploymentQueue("anything.com/", secret, secret)
    with pytest.raises(DeploymentQueueError, match="访问凭据无效"):
        CloudBaseDeploymentQueue("sceneturn-1", SecretStr("wrong\r\nkey"), secret)
