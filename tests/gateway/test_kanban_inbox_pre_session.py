import asyncio

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.session import SessionSource


class StubAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True), Platform.TELEGRAM)
        self.sent = []
        self.handler_calls = 0

    async def connect(self, *, is_reconnect: bool = False):
        return True

    async def disconnect(self):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append((chat_id, content, reply_to, metadata))
        return SendResult(success=True, message_id="sent-1")

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


async def _message_handler(event):
    raise AssertionError("normal message handler should not run for captured inbox messages")


def _event():
    return MessageEvent(
        text="capture this",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-1001",
            chat_type="group",
            user_id="42",
            thread_id="7",
        ),
        message_id="99",
    )


def test_pre_session_handler_bypasses_active_session_guard():
    adapter = StubAdapter()
    adapter.set_message_handler(_message_handler)
    adapter.set_pre_session_handler(lambda event: asyncio.sleep(0, result="queued receipt"))
    adapter._active_sessions["agent:main:telegram:group:-1001:thread:7"] = asyncio.Event()

    asyncio.run(adapter.handle_message(_event()))

    assert len(adapter.sent) == 1
    assert adapter.sent[0][1] == "queued receipt"
