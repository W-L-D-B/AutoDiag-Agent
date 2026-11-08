from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import httpx

from .config import Config


class ProviderError(RuntimeError):
    def __init__(self, code: str, message: str, usage: dict | None = None):
        super().__init__(message)
        self.code = code
        self.usage = usage or {}


@dataclass
class ModelTurn:
    message: dict
    usage: dict


class DeepSeekProvider:
    mode = "live"

    def __init__(self, config: Config, client: httpx.AsyncClient | None = None):
        self.config = config
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=config.timeout_s, trust_env=False
        )

    async def _request(self, method: str, path: str, body=None) -> dict:
        if not self.config.api_key:
            raise ProviderError(
                "model_not_configured", "Model API credential is not configured"
            )
        try:
            response = await self.client.request(
                method,
                self.config.base_url.rstrip("/") + path,
                headers={"Authorization": "Bearer " + self.config.api_key},
                json=body,
                timeout=self.config.timeout_s,
            )
        except httpx.TimeoutException as error:
            raise ProviderError("provider_timeout", "Model API timed out") from error
        except httpx.HTTPError as error:
            raise ProviderError(
                "provider_unavailable", "Model API connection failed"
            ) from error
        if response.status_code != 200:
            code = {
                401: "authentication_failed",
                402: "insufficient_balance",
                429: "rate_limited",
            }.get(response.status_code, "provider_error")
            raise ProviderError(code, f"Model API returned HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as error:
            raise ProviderError(
                "invalid_model_response", "Model API returned invalid JSON"
            ) from error
        if not isinstance(data, dict):
            raise ProviderError(
                "invalid_model_response", "Model API returned a non-object response"
            )
        return data

    async def complete(
        self, messages: list[dict], tools: list[dict], force_tools: bool = False
    ) -> ModelTurn:
        body = {
            "model": self.config.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "required" if force_tools else "auto",
            "stream": False,
            "max_tokens": self.config.max_tokens,
        }
        if self.config.provider == "deepseek":
            body["thinking"] = {"type": "disabled"}
            body["response_format"] = {"type": "json_object"}
        data = await self._request("POST", "/chat/completions", body)
        clean_usage = {}
        try:
            usage = data.get("usage") or {}
            clean_usage = {
                key: usage.get(key, 0)
                for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            }
            if any(
                type(value) is not int or not 0 <= value <= 100000000
                for value in clean_usage.values()
            ):
                clean_usage = {}
                raise ValueError("invalid usage")
            choice = data["choices"][0]
            if choice.get("finish_reason") == "length":
                raise ProviderError(
                    "output_limit",
                    "Model reached its output-token limit",
                    usage=clean_usage,
                )
            original = choice["message"]
            content = original.get("content")
            if content is not None and not isinstance(content, str):
                raise ValueError("invalid content")
            calls = original.get("tool_calls") or []
            if not isinstance(calls, list) or len(calls) > 4:
                raise ValueError("invalid tool-call count")
            clean_calls = []
            ids = set()
            for call in calls:
                function = call["function"]
                if (
                    call.get("type") != "function"
                    or not isinstance(call["id"], str)
                    or not 1 <= len(call["id"]) <= 128
                    or call["id"] in ids
                ):
                    raise ValueError("invalid tool-call ID")
                if (
                    not isinstance(function["name"], str)
                    or not isinstance(function["arguments"], str)
                    or len(function["arguments"]) > 8192
                ):
                    raise ValueError("invalid function fields")
                ids.add(call["id"])
                clean_calls.append(
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {
                            "name": function["name"],
                            "arguments": function["arguments"],
                        },
                    }
                )
            message = {"role": "assistant", "content": content}
            if clean_calls:
                message["tool_calls"] = clean_calls
            return ModelTurn(message, clean_usage)
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise ProviderError(
                "invalid_model_response",
                "Model API response violates the expected schema",
                usage=clean_usage,
            ) from error

    async def balance(self) -> dict:
        data = await self._request("GET", "/user/balance")
        if type(data.get("is_available")) is not bool or not isinstance(
            data.get("balance_infos"), list
        ):
            raise ProviderError(
                "invalid_balance_response", "Invalid provider balance response"
            )
        for item in data["balance_infos"]:
            try:
                if not isinstance(item, dict) or not isinstance(
                    item.get("currency"), str
                ):
                    raise TypeError
                amount = item.get("total_balance")
                if not isinstance(amount, str):
                    raise TypeError
                value = Decimal(amount)
                if not value.is_finite() or value < 0:
                    raise ValueError
            except (InvalidOperation, TypeError, ValueError) as error:
                raise ProviderError(
                    "invalid_balance_response", "Invalid provider balance amount"
                ) from error
        return data

    async def models(self) -> list[str]:
        data = await self._request("GET", "/models")
        return [
            item["id"]
            for item in data.get("data", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]

    async def close(self):
        if self._owns_client:
            await self.client.aclose()
