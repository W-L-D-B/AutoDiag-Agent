import asyncio
import json

import httpx
import pytest

from autodiag_agent.config import Config
from autodiag_agent.provider import DeepSeekProvider, ProviderError


def test_provider_strips_extra_tool_fields_and_sends_bounded_deepseek_request():
    async def run():
        def handler(request):
            body = json.loads(request.content)
            assert body["model"] == "deepseek-flash"
            assert body["thinking"] == {"type": "disabled"}
            assert body["response_format"] == {"type": "json_object"}
            assert body["tool_choice"] == "required"
            assert body["max_tokens"] == 2048
            assert request.headers["authorization"] == "Bearer test-key"
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call1",
                                        "type": "function",
                                        "function": {
                                            "name": "list_ecus",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 4,
                        "total_tokens": 7,
                    },
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = DeepSeekProvider(Config(api_key="test-key"), client)
            turn = await provider.complete(
                [{"role": "user", "content": "JSON"}], [], True
            )
            assert "index" not in turn.message["tool_calls"][0]
            assert turn.usage["total_tokens"] == 7
            await provider.close()
            assert not client.is_closed

    asyncio.run(run())


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "authentication_failed"),
        (402, "insufficient_balance"),
        (429, "rate_limited"),
        (500, "provider_error"),
    ],
)
def test_provider_errors_never_echo_response_or_credential(status, code):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    status, json={"error": "private detail"}
                )
            )
        ) as client:
            provider = DeepSeekProvider(Config(api_key="test-key"), client)
            with pytest.raises(ProviderError) as captured:
                await provider.complete([], [])
            assert captured.value.code == code
            assert "test-key" not in str(captured.value)
            assert "private detail" not in str(captured.value)

    asyncio.run(run())


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"choices": []},
        {"choices": [{"message": {"content": 3}}]},
        {"choices": [{"message": {"content": "{}"}}], "usage": {"total_tokens": True}},
    ],
)
def test_malformed_provider_responses_fail_closed(payload):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json=payload)
            )
        ) as client:
            provider = DeepSeekProvider(Config(api_key="test-key"), client)
            with pytest.raises(ProviderError):
                await provider.complete([], [])

    asyncio.run(run())


def test_configuration_hides_key_and_rejects_untrusted_url_shapes():
    config = Config(api_key="test-key")
    assert "test-key" not in repr(config)
    assert "api_key" not in config.public()
    for url in [
        "http://example.com",
        "https://user:password@example.com",
        "https://example.com?key=value",
    ]:
        with pytest.raises(ValueError):
            Config(base_url=url)


@pytest.mark.parametrize(
    "balances",
    [
        [None],
        [{"currency": "CNY", "total_balance": "NaN"}],
        [{"currency": "CNY", "total_balance": "-1"}],
        [{"currency": "CNY", "total_balance": True}],
        [{"currency": 1, "total_balance": "10"}],
    ],
)
def test_provider_rejects_malformed_balance_items(balances):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200, json={"is_available": True, "balance_infos": balances}
                )
            )
        ) as client:
            provider = DeepSeekProvider(Config(api_key="test-key"), client)
            with pytest.raises(ProviderError) as captured:
                await provider.balance()
            assert captured.value.code == "invalid_balance_response"

    asyncio.run(run())
