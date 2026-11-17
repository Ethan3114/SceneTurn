from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.core.config import Settings
from app.core.errors import AppError
from app.services.agent_provider import SUPPORTED_PLATFORM_PROTOCOLS, agent_request_body, agent_tools
from app.services.text_provider import MODEL, PRO_MODEL, cost_micros, current_price, model_aliases, peak_price, price_catalog


@pytest.mark.parametrize("protocol", sorted(SUPPORTED_PLATFORM_PROTOCOLS))
@pytest.mark.parametrize("model", [MODEL, PRO_MODEL])
def test_requested_model_is_explicit_and_other_protocol_fields_are_unchanged(protocol, model):
    tools = agent_tools("writer", True, protocol, model)
    messages = [{"role": "system", "content": "检查实际JSON及模型请求参数，不请求模型。"}]
    baseline = agent_request_body(messages, 32768, tools, True, Settings(), protocol, MODEL)
    actual = agent_request_body(messages, 32768, tools, True, Settings(), protocol, model)
    assert actual["model"] == model
    assert actual == {**baseline, "model": model}


def test_text_flash_defaults_and_model_response_aliases_remain_separate():
    assert MODEL == "deepseek-flash" and PRO_MODEL == "deepseek-v4-pro"
    assert price_catalog()["version"] == "deepseek-flash-2026-11-13"
    assert price_catalog()["peak"] == {"cacheHitYuan": 0.04, "cacheMissYuan": 2, "outputYuan": 8}
    assert price_catalog(PRO_MODEL)["peak"] == {"cacheHitYuan": 0.30, "cacheMissYuan": 9, "outputYuan": 27}
    assert price_catalog(PRO_MODEL)["offPeak"] == {"cacheHitYuan": 0.15, "cacheMissYuan": 4.5, "outputYuan": 13.5}
    assert model_aliases(MODEL) == {MODEL, "DeepSeek-V4.1-Flash-1017"}
    assert model_aliases(PRO_MODEL) == {PRO_MODEL, "DeepSeek-V4-Pro-0813"}
    assert not model_aliases(MODEL) & model_aliases(PRO_MODEL)


@pytest.mark.parametrize("month,day,hour,band", [(11, 13, 10, "peak"), (11, 13, 15, "peak"), (11, 13, 13, "offPeak"), (11, 13, 18, "offPeak"), (11, 14, 10, "offPeak"), (10, 1, 10, "offPeak")])
def test_pro_prices_use_china_time_workdays_and_holidays(month, day, hour, band):
    timestamp = datetime(2026, month, day, hour, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    price = current_price(timestamp, model=PRO_MODEL)
    assert price["band"] == band
    assert price["version"] == "deepseek-v4-pro-2026-11-13"
    expected = (300000, 9000000, 27000000) if band == "peak" else (150000, 4500000, 13500000)
    assert tuple(price[key] for key in ("cacheHitMicrosPerMillion", "cacheMissMicrosPerMillion", "outputMicrosPerMillion")) == expected


def test_pro_actual_cost_and_peak_reservation_use_pro_rates():
    peak = peak_price(PRO_MODEL)
    assert cost_micros(1000000, 1000000, 1000000, peak) == 36300000
    off = current_price(datetime(2026, 11, 13, 23, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp(), PRO_MODEL)
    assert cost_micros(1000000, 1000000, 1000000, off) == 18150000
    assert cost_micros(0, 1, 1, peak) == 36
    assert cost_micros(1, 0, 0, peak) == 1
    assert cost_micros(0, 100000, 32768, peak) == 1784736
    assert cost_micros(0, 100000, 32768, peak_price(MODEL)) == 462144


def test_unknown_model_is_rejected_before_supplier_request():
    with pytest.raises(AppError) as error:
        agent_request_body([{"role": "system", "content": "JSON"}], 32768, [], True, Settings(), model="deepseek-unknown")
    assert error.value.code == "TEXT_MODEL_UNSUPPORTED"
    with pytest.raises(AppError):
        peak_price("deepseek-unknown")
