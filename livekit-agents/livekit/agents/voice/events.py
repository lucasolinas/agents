from __future__ import annotations

import time
from enum import Enum, unique
from typing import TYPE_CHECKING, Annotated, Any, Generic, Literal, TypeVar, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing_extensions import Self

from ..llm import (
    LLM,
    ChatMessage,
    FunctionCall,
    FunctionCallOutput,
    LLMError,
    RealtimeModel,
    RealtimeModelError,
)
from ..metrics import AgentMetrics
from ..stt import STT, STTError
from ..tts import TTS, TTSError
from .speech_handle import SpeechHandle

if TYPE_CHECKING:
    from .agent_session import AgentSession


Userdata_T = TypeVar("Userdata_T")


class RunContext(Generic[Userdata_T]):
    # private ctor
    def __init__(
        self,
        *,
        session: AgentSession[Userdata_T],
        speech_handle: SpeechHandle,
        function_call: FunctionCall,
    ) -> None:
        self._session = session
        self._speech_handle = speech_handle
        self._function_call = function_call

        self._initial_step_idx = speech_handle.num_steps - 1

    @property
    def session(self) -> AgentSession[Userdata_T]:
        return self._session

    @property
    def speech_handle(self) -> SpeechHandle:
        return self._speech_handle

    @property
    def function_call(self) -> FunctionCall:
        return self._function_call

    @property
    def userdata(self) -> Userdata_T:
        return self.session.userdata

    def disallow_interruptions(self) -> None:
        """Disable interruptions for this FunctionCall.

        Delegates to the SpeechHandle.allow_interruptions setter,
        which will raise a RuntimeError if the handle is already interrupted.

        Raises:
            RuntimeError: If the SpeechHandle is already interrupted.
        """
        self.speech_handle.allow_interruptions = False

    async def wait_for_playout(self) -> None:
        """Waits for the speech playout corresponding to this function call step.

        Unlike `SpeechHandle.wait_for_playout`, which waits for the full
        assistant turn to complete (including all function tools),
        this method only waits for the assistant's spoken response prior running
        this tool to finish playing."""
        await self.speech_handle._wait_for_generation(step_idx=self._initial_step_idx)


EventTypes = Literal[
    "user_state_changed",
    "agent_state_changed",
    "user_input_transcribed",
    "conversation_item_added",
    "agent_false_interruption",
    "function_tools_executed",
    "metrics_collected",
    "speech_created",
    "error",
    "close",
]

UserState = Literal["speaking", "listening", "away"]
AgentState = Literal["initializing", "idle", "listening", "thinking", "speaking"]


class UserStateChangedEvent(BaseModel):
    type: Literal["user_state_changed"] = "user_state_changed"
    old_state: UserState
    new_state: UserState
    created_at: float = Field(default_factory=time.time)


class AgentStateChangedEvent(BaseModel):
    type: Literal["agent_state_changed"] = "agent_state_changed"
    old_state: AgentState
    new_state: AgentState
    created_at: float = Field(default_factory=time.time)


class UserInputTranscribedEvent(BaseModel):
    type: Literal["user_input_transcribed"] = "user_input_transcribed"
    transcript: str
    is_final: bool
    speaker_id: str | None = None
    created_at: float = Field(default_factory=time.time)


class AgentFalseInterruptionEvent(BaseModel):
    type: Literal["agent_false_interruption"] = "agent_false_interruption"
    message: ChatMessage | None
    """The `assistant` message that got interrupted"""
    extra_instructions: str | None = None
    """Optional instructions originally passed to `AgentSession.generate_reply` via the `instructions` argument.
    Populated only if the user interrupted a speech response generated using `session.generate_reply`.
    Useful for understanding what the agent was attempting to convey before the interruption."""
    created_at: float = Field(default_factory=time.time)


class MetricsCollectedEvent(BaseModel):
    type: Literal["metrics_collected"] = "metrics_collected"
    metrics: AgentMetrics
    created_at: float = Field(default_factory=time.time)


class _TypeDiscriminator(BaseModel):
    type: Literal["unknown"] = "unknown"  # force user to use the type discriminator


class ConversationItemAddedEvent(BaseModel):
    type: Literal["conversation_item_added"] = "conversation_item_added"
    item: ChatMessage | _TypeDiscriminator
    created_at: float = Field(default_factory=time.time)


class FunctionToolsExecutedEvent(BaseModel):
    type: Literal["function_tools_executed"] = "function_tools_executed"
    function_calls: list[FunctionCall]
    function_call_outputs: list[FunctionCallOutput | None]
    created_at: float = Field(default_factory=time.time)

    def zipped(self) -> list[tuple[FunctionCall, FunctionCallOutput | None]]:
        return list(zip(self.function_calls, self.function_call_outputs))

    @model_validator(mode="after")
    def verify_lists_length(self) -> Self:
        if len(self.function_calls) != len(self.function_call_outputs):
            raise ValueError("The number of function_calls and function_call_outputs must match.")

        return self


class SpeechCreatedEvent(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    type: Literal["speech_created"] = "speech_created"
    user_initiated: bool
    """True if the speech was created using public methods like `say` or `generate_reply`"""
    source: Literal["say", "generate_reply", "tool_response"]
    """Source indicating how the speech handle was created"""
    speech_handle: SpeechHandle = Field(..., exclude=True)
    """The speech handle that was created"""
    created_at: float = Field(default_factory=time.time)


class ErrorEvent(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    type: Literal["error"] = "error"
    error: LLMError | STTError | TTSError | RealtimeModelError | Any
    source: LLM | STT | TTS | RealtimeModel | Any
    created_at: float = Field(default_factory=time.time)


@unique
class CloseReason(str, Enum):
    ERROR = "error"
    JOB_SHUTDOWN = "job_shutdown"
    PARTICIPANT_DISCONNECTED = "participant_disconnected"
    USER_INITIATED = "user_initiated"
    TASK_COMPLETED = "task_completed"


class CloseEvent(BaseModel):
    type: Literal["close"] = "close"
    error: LLMError | STTError | TTSError | RealtimeModelError | None = None
    reason: CloseReason
    created_at: float = Field(default_factory=time.time)


AgentEvent = Annotated[
    Union[
        UserInputTranscribedEvent,
        UserStateChangedEvent,
        AgentStateChangedEvent,
        AgentFalseInterruptionEvent,
        MetricsCollectedEvent,
        ConversationItemAddedEvent,
        FunctionToolsExecutedEvent,
        SpeechCreatedEvent,
        ErrorEvent,
        CloseEvent,
    ],
    Field(discriminator="type"),
]
