"""Drop every request field a student's client sends that is not listed here.

LiteLLM forwards unknown fields to Bedrock and accepts settings of its own in
the request body. Some cost more than LiteLLM records: Bedrock's ultrafast
service tier is 6x standard but is billed to the group at the standard price,
whether it arrives as service_tier or inside extra_body. Loaded by the callbacks
line in config.yaml. If a framework needs a harmless field, add it here.
"""

from litellm.integrations.custom_logger import CustomLogger

ALLOWED = {
    "model", "messages", "max_tokens", "max_completion_tokens", "temperature", "top_p", "stop", "stream",
    "stream_options", "tools", "tool_choice", "parallel_tool_calls", "response_format", "seed", "n",
    "presence_penalty", "frequency_penalty", "logit_bias", "logprobs", "top_logprobs", "user", "reasoning_effort",
    "prompt", "suffix", "echo", "best_of",                  # completions
    "input", "encoding_format", "dimensions",               # embeddings
}


class RequestGuard(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        sent = data.get("proxy_server_request", {}).get("body") or {}
        for field in set(sent) - ALLOWED:
            data.pop(field, None)
        return data


guard = RequestGuard()
