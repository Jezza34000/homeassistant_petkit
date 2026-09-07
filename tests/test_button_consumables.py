"""验证耗材按钮发出的云端请求，不连接真实设备。"""

import asyncio
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
from pypetkitapi import Litter, PypetkitError
from pypetkitapi.client import PetKitClient
import pytest


@pytest.fixture
def reset_action():
    """取得真实按钮映射并隔离集成模块缓存。"""
    # 只跳过集成启动和摄像头注册，按钮、实体和 API client 均使用真实代码。
    package = ModuleType("custom_components.petkit")
    package.__path__ = [str(Path(__file__).parents[1] / "custom_components" / "petkit")]
    prefix = "custom_components.petkit"
    original = {
        name: module
        for name, module in sys.modules.items()
        if name == prefix or name.startswith(prefix + ".")
    }
    sys.modules[prefix] = package
    try:
        button = importlib.import_module(prefix + ".button")
        yield next(
            item.action
            for item in button.BUTTON_MAPPING[Litter]
            if item.translation_key == "reset_n60_odor_eliminator"
        )
    finally:
        for name in tuple(sys.modules):
            if name == prefix or name.startswith(prefix + "."):
                del sys.modules[name]
        sys.modules.update(original)


def test_t6_resets_purification_consumable(reset_action):
    """T6 使用净味小方专用接口和字符串设备编号。"""

    async def run():
        async with aiohttp.ClientSession() as session:
            client = PetKitClient("", "", "CN", "Asia/Shanghai", session=session)
            device = SimpleNamespace(
                id=123, device_nfo=SimpleNamespace(device_type="t6")
            )
            client.petkit_entities[123] = device
            client.get_session_id = AsyncMock(
                return_value={"X-Session": "test-session"}
            )
            client.req.request = AsyncMock(return_value=1)
            await reset_action(client, device)
            client.req.request.assert_awaited_once_with(
                method="POST",
                url="t6/purificationReset",
                params={"deviceId": "123"},
                headers={"X-Session": "test-session"},
            )

    asyncio.run(run())


@pytest.mark.parametrize("device_type", ["t5", "t7"])
def test_other_models_keep_existing_reset_command(reset_action, device_type):
    """其他型号继续使用原有设备动作。"""

    async def run():
        async with aiohttp.ClientSession() as session:
            client = PetKitClient("", "", "CN", "Asia/Shanghai", session=session)
            device = SimpleNamespace(
                id=123, device_nfo=SimpleNamespace(device_type=device_type)
            )
            client.petkit_entities[123] = device
            client.get_session_id = AsyncMock(
                return_value={"X-Session": "test-session"}
            )
            client.req.request = AsyncMock(return_value=1)
            await reset_action(client, device)
            client.req.request.assert_awaited_once_with(
                method="POST",
                url=f"{device_type}/controlDevice",
                data={"id": 123, "kv": '{"start_action": 10}', "type": "start"},
                headers={"X-Session": "test-session"},
            )

    asyncio.run(run())


@pytest.mark.parametrize("failure_stage", ["authentication", "request"])
def test_t6_failure_propagates_without_retry(reset_action, failure_stage):
    """认证和请求错误直接传播，不重复发送重置。"""

    async def run():
        async with aiohttp.ClientSession() as session:
            client = PetKitClient("", "", "CN", "Asia/Shanghai", session=session)
            device = SimpleNamespace(
                id=123, device_nfo=SimpleNamespace(device_type="t6")
            )
            client.petkit_entities[123] = device
            client.get_session_id = AsyncMock(
                return_value={"X-Session": "test-session"}
            )
            client.req.request = AsyncMock(return_value=1)
            error = PypetkitError("test failure")
            if failure_stage == "authentication":
                client.get_session_id.side_effect = error
            else:
                client.req.request.side_effect = error
            with pytest.raises(PypetkitError) as raised:
                await reset_action(client, device)
            assert raised.value is error
            assert client.req.request.await_count == (failure_stage == "request")

    asyncio.run(run())
