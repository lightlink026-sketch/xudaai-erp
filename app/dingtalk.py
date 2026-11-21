"""DingTalk Stream mode adapter. Uses official open-dingtalk Python SDK."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger("xudaai.dingtalk")


async def run_stream(client_id: str, client_secret: str, answer_fn: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]], search_fn: Callable[[str], dict[str, Any]]) -> None:
    try:
        import dingtalk_stream
    except ImportError as e:
        raise RuntimeError("Install dingtalk-stream to enable the DingTalk adapter") from e

    class ProductLibraryHandler(dingtalk_stream.ChatbotHandler):
        async def process(self, callback: dingtalk_stream.CallbackMessage):
            try:
                message = dingtalk_stream.ChatbotMessage.from_dict(callback.data)
                question = str(getattr(getattr(message, "text", None), "content", "") or "").strip()
                if not question:
                    reply = "请发送产品问题或关键词。我会检索已同步的产品资料。"
                else:
                    try:
                        answer = await answer_fn({"question": question})
                        reply = answer["answer"]
                        if answer.get("sources"):
                            reply += "\n\n资料来源：" + "；".join(dict.fromkeys(x["path"] for x in answer["sources"][:5]))
                    except Exception:
                        result = search_fn(question)
                        reply = "\n\n".join(f"{x['path']}：{x['snippet'][:350]}" for x in result.get("results", [])[:5]) or "资料库中没有找到相关内容。"
                await asyncio.to_thread(self.reply_text, reply[:4000], message)
                return dingtalk_stream.AckMessage.STATUS_OK, "OK"
            except Exception:
                logger.exception("DingTalk message handling failed")
                return dingtalk_stream.AckMessage.STATUS_SYSTEM_EXCEPTION, "error"

    client = dingtalk_stream.DingTalkStreamClient(dingtalk_stream.Credential(client_id, client_secret))
    client.register_callback_handler(dingtalk_stream.chatbot.ChatbotMessage.TOPIC, ProductLibraryHandler())
    logger.info("Starting DingTalk Stream bot")
    await client.start()
