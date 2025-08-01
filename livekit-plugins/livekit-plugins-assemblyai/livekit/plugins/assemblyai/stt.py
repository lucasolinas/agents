# Copyright 2023 LiveKit, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import weakref
from dataclasses import dataclass
from urllib.parse import urlencode

import aiohttp

from livekit.agents import (
    DEFAULT_API_CONNECT_OPTIONS,
    APIConnectOptions,
    APIStatusError,
    stt,
    utils,
)
from livekit.agents.types import (
    NOT_GIVEN,
    NotGivenOr,
)
from livekit.agents.utils import AudioBuffer, is_given

from .log import logger


@dataclass
class STTOptions:
    sample_rate: int
    buffer_size_seconds: float
    encoding: str = "pcm_s16le"
    end_of_turn_confidence_threshold: NotGivenOr[float] = NOT_GIVEN
    min_end_of_turn_silence_when_confident: NotGivenOr[int] = NOT_GIVEN
    max_turn_silence: NotGivenOr[int] = NOT_GIVEN
    format_turns: NotGivenOr[bool] = NOT_GIVEN


class STT(stt.STT):
    def __init__(
        self,
        *,
        api_key: NotGivenOr[str] = NOT_GIVEN,
        sample_rate: int = 16000,
        encoding: str = "pcm_s16le",
        end_of_turn_confidence_threshold: NotGivenOr[float] = NOT_GIVEN,
        min_end_of_turn_silence_when_confident: NotGivenOr[int] = NOT_GIVEN,
        max_turn_silence: NotGivenOr[int] = NOT_GIVEN,
        format_turns: NotGivenOr[bool] = NOT_GIVEN,
        http_session: aiohttp.ClientSession | None = None,
        buffer_size_seconds: float = 0.05,
    ):
        super().__init__(
            capabilities=stt.STTCapabilities(streaming=True, interim_results=False),
        )
        assemblyai_api_key = api_key if is_given(api_key) else os.environ.get("ASSEMBLYAI_API_KEY")
        if assemblyai_api_key is None:
            raise ValueError(
                "AssemblyAI API key is required. "
                "Pass one in via the `api_key` parameter, "
                "or set it as the `ASSEMBLYAI_API_KEY` environment variable"
            )
        self._api_key = assemblyai_api_key
        self._opts = STTOptions(
            sample_rate=sample_rate,
            buffer_size_seconds=buffer_size_seconds,
            encoding=encoding,
            end_of_turn_confidence_threshold=end_of_turn_confidence_threshold,
            min_end_of_turn_silence_when_confident=min_end_of_turn_silence_when_confident,
            max_turn_silence=max_turn_silence,
            format_turns=format_turns,
        )
        self._session = http_session
        self._streams = weakref.WeakSet[SpeechStream]()

    @property
    def session(self) -> aiohttp.ClientSession:
        if not self._session:
            self._session = utils.http_context.http_session()
        return self._session

    async def _recognize_impl(
        self,
        buffer: AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions,
    ) -> stt.SpeechEvent:
        raise NotImplementedError("Not implemented")

    def stream(
        self,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> SpeechStream:
        config = dataclasses.replace(self._opts)
        stream = SpeechStream(
            stt=self,
            conn_options=conn_options,
            opts=config,
            api_key=self._api_key,
            http_session=self.session,
        )
        self._streams.add(stream)
        return stream

    def update_options(
        self,
        *,
        buffer_size_seconds: NotGivenOr[float] = NOT_GIVEN,
        end_of_turn_confidence_threshold: NotGivenOr[float] = NOT_GIVEN,
        min_end_of_turn_silence_when_confident: NotGivenOr[int] = NOT_GIVEN,
        max_turn_silence: NotGivenOr[int] = NOT_GIVEN,
    ) -> None:
        if is_given(buffer_size_seconds):
            self._opts.buffer_size_seconds = buffer_size_seconds
        if is_given(end_of_turn_confidence_threshold):
            self._opts.end_of_turn_confidence_threshold = end_of_turn_confidence_threshold
        if is_given(min_end_of_turn_silence_when_confident):
            self._opts.min_end_of_turn_silence_when_confident = (
                min_end_of_turn_silence_when_confident
            )
        if is_given(max_turn_silence):
            self._opts.max_turn_silence = max_turn_silence

        for stream in self._streams:
            stream.update_options(
                buffer_size_seconds=buffer_size_seconds,
                end_of_turn_confidence_threshold=end_of_turn_confidence_threshold,
                min_end_of_turn_silence_when_confident=min_end_of_turn_silence_when_confident,
                max_turn_silence=max_turn_silence,
            )


class SpeechStream(stt.SpeechStream):
    # Used to close websocket
    _CLOSE_MSG: str = json.dumps({"type": "Terminate"})

    def __init__(
        self,
        *,
        stt: STT,
        opts: STTOptions,
        conn_options: APIConnectOptions,
        api_key: str,
        http_session: aiohttp.ClientSession,
    ) -> None:
        super().__init__(stt=stt, conn_options=conn_options, sample_rate=opts.sample_rate)

        self._opts = opts
        self._api_key = api_key
        self._session = http_session
        self._speech_duration: float = 0
        self._reconnect_event = asyncio.Event()

    def update_options(
        self,
        *,
        buffer_size_seconds: NotGivenOr[float] = NOT_GIVEN,
        end_of_turn_confidence_threshold: NotGivenOr[float] = NOT_GIVEN,
        min_end_of_turn_silence_when_confident: NotGivenOr[int] = NOT_GIVEN,
        max_turn_silence: NotGivenOr[int] = NOT_GIVEN,
    ) -> None:
        if is_given(buffer_size_seconds):
            self._opts.buffer_size_seconds = buffer_size_seconds
        if is_given(end_of_turn_confidence_threshold):
            self._opts.end_of_turn_confidence_threshold = end_of_turn_confidence_threshold
        if is_given(min_end_of_turn_silence_when_confident):
            self._opts.min_end_of_turn_silence_when_confident = (
                min_end_of_turn_silence_when_confident
            )
        if is_given(max_turn_silence):
            self._opts.max_turn_silence = max_turn_silence

        self._reconnect_event.set()

    async def _run(self) -> None:
        """
        Run a single websocket connection to AssemblyAI and make sure to reconnect
        when something went wrong.
        """

        closing_ws = False

        async def send_task(ws: aiohttp.ClientWebSocketResponse) -> None:
            nonlocal closing_ws

            samples_per_buffer = self._opts.sample_rate // round(1 / self._opts.buffer_size_seconds)
            audio_bstream = utils.audio.AudioByteStream(
                sample_rate=self._opts.sample_rate,
                num_channels=1,
                samples_per_channel=samples_per_buffer,
            )

            # forward inputs to AssemblyAI
            # if we receive a close message, signal it to AssemblyAI and break.
            # the recv task will then make sure to process the remaining audio and stop
            async for data in self._input_ch:
                if isinstance(data, self._FlushSentinel):
                    frames = audio_bstream.flush()
                else:
                    frames = audio_bstream.write(data.data.tobytes())

                for frame in frames:
                    self._speech_duration += frame.duration
                    await ws.send_bytes(frame.data.tobytes())

            closing_ws = True
            await ws.send_str(SpeechStream._CLOSE_MSG)

        async def recv_task(ws: aiohttp.ClientWebSocketResponse) -> None:
            nonlocal closing_ws
            while True:
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=5)
                except asyncio.TimeoutError:
                    if closing_ws:
                        break
                    continue

                if msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                ):
                    if closing_ws:  # close is expected, see SpeechStream.aclose
                        return

                    raise APIStatusError(
                        "AssemblyAI connection closed unexpectedly",
                    )  # this will trigger a reconnection, see the _run loop

                if msg.type != aiohttp.WSMsgType.TEXT:
                    logger.error("unexpected AssemblyAI message type %s", msg.type)
                    continue

                try:
                    self._process_stream_event(json.loads(msg.data))
                except Exception:
                    logger.exception("failed to process AssemblyAI message")

        ws: aiohttp.ClientWebSocketResponse | None = None

        while True:
            try:
                ws = await self._connect_ws()
                tasks = [
                    asyncio.create_task(send_task(ws)),
                    asyncio.create_task(recv_task(ws)),
                ]
                wait_reconnect_task = asyncio.create_task(self._reconnect_event.wait())

                try:
                    done, _ = await asyncio.wait(
                        (asyncio.gather(*tasks), wait_reconnect_task),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in done:
                        if task != wait_reconnect_task:
                            task.result()

                    if wait_reconnect_task not in done:
                        break

                    self._reconnect_event.clear()
                finally:
                    await utils.aio.gracefully_cancel(*tasks, wait_reconnect_task)
            finally:
                if ws is not None:
                    await ws.close()

    async def _connect_ws(self) -> aiohttp.ClientWebSocketResponse:
        live_config = {
            "sample_rate": self._opts.sample_rate,
            "encoding": self._opts.encoding,
            "format_turns": self._opts.format_turns if is_given(self._opts.format_turns) else None,
            "end_of_turn_confidence_threshold": self._opts.end_of_turn_confidence_threshold
            if is_given(self._opts.end_of_turn_confidence_threshold)
            else None,
            "min_end_of_turn_silence_when_confident": self._opts.min_end_of_turn_silence_when_confident  # noqa: E501
            if is_given(self._opts.min_end_of_turn_silence_when_confident)
            else None,
            "max_turn_silence": self._opts.max_turn_silence
            if is_given(self._opts.max_turn_silence)
            else None,
        }

        headers = {
            "Authorization": self._api_key,
            "Content-Type": "application/json",
            "User-Agent": "AssemblyAI/1.0 (integration=Livekit)",
        }

        ws_url = "wss://streaming.assemblyai.com/v3/ws"
        filtered_config = {k: v for k, v in live_config.items() if v is not None}
        url = f"{ws_url}?{urlencode(filtered_config).lower()}"
        ws = await self._session.ws_connect(url, headers=headers)
        return ws

    def _process_stream_event(self, data: dict) -> None:
        message_type = data.get("type")
        if message_type == "Turn":
            transcript = data.get("transcript")
            words = data.get("words", [])
            end_of_turn = data.get("end_of_turn")

            if transcript and end_of_turn:
                turn_is_formatted = data.get("turn_is_formatted", False)
                if not self._opts.format_turns or (self._opts.format_turns and turn_is_formatted):
                    final_event = stt.SpeechEvent(
                        type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                        # TODO: We can't know the language?
                        alternatives=[stt.SpeechData(language="en-US", text=transcript)],
                    )
                else:
                    # skip emitting final transcript if format_turns is enabled but this
                    # turn isn't formatted
                    return
                self._event_ch.send_nowait(final_event)
                self._event_ch.send_nowait(stt.SpeechEvent(type=stt.SpeechEventType.END_OF_SPEECH))

                if self._speech_duration > 0.0:
                    usage_event = stt.SpeechEvent(
                        type=stt.SpeechEventType.RECOGNITION_USAGE,
                        alternatives=[],
                        recognition_usage=stt.RecognitionUsage(
                            audio_duration=self._speech_duration
                        ),
                    )
                    self._event_ch.send_nowait(usage_event)
                    self._speech_duration = 0

            else:
                non_final_words = [word["text"] for word in words if not word["word_is_final"]]
                interim = " ".join(non_final_words)
                interim_event = stt.SpeechEvent(
                    type=stt.SpeechEventType.INTERIM_TRANSCRIPT,
                    alternatives=[stt.SpeechData(language="en-US", text=f"{transcript} {interim}")],
                )
                self._event_ch.send_nowait(interim_event)
