import re
import logging
from abc import abstractmethod
from typing import AsyncGenerator

from apipeline.processors.frame_processor import FrameDirection
from apipeline.pipeline.pipeline import FrameDirection
from apipeline.frames.control_frames import EndFrame

from src.processors.ai_processor import AIProcessor
from src.types.frames.control_frames import LLMFullResponseEndFrame, TTSStartedFrame, TTSStoppedFrame, TTSVoiceUpdateFrame
from src.types.frames.data_frames import Frame, TTSSpeakFrame, TextFrame, VisionImageRawFrame
from src.types.frames.sys_frames import StartInterruptionFrame


ENDOFSENTENCE_PATTERN_STR = r"""
    (?<![A-Z])       # Negative lookbehind: not preceded by an uppercase letter (e.g., "U.S.A.")
    (?<!\d)          # Negative lookbehind: not preceded by a digit (e.g., "1. Let's start")
    (?<!\d\s[ap])    # Negative lookbehind: not preceded by time (e.g., "3:00 a.m.")
    (?<!Mr|Ms|Dr)    # Negative lookbehind: not preceded by Mr, Ms, Dr (combined bc. length is the same)
    (?<!Mrs)         # Negative lookbehind: not preceded by "Mrs"
    (?<!Prof)        # Negative lookbehind: not preceded by "Prof"
    [\.\?\!:]        # Match a period, question mark, exclamation point, or colon
    $                # End of string
"""
ENDOFSENTENCE_PATTERN = re.compile(ENDOFSENTENCE_PATTERN_STR, re.VERBOSE)


def match_endofsentence(text: str) -> bool:
    return ENDOFSENTENCE_PATTERN.search(text.rstrip()) is not None


class TTSProcessor(AIProcessor):
    def __init__(
            self,
            *,
            aggregate_sentences: bool = True,
            # if True, subclass is responsible for pushing TextFrames and LLMFullResponseEndFrames
            push_text_frames: bool = True,
            **kwargs):
        super().__init__(**kwargs)
        self._aggregate_sentences: bool = aggregate_sentences
        self._push_text_frames: bool = push_text_frames
        self._current_sentence: str = ""

    @abstractmethod
    async def set_voice(self, voice: str):
        pass

    # Converts the text to audio.
    @abstractmethod
    async def run_tts(self, text: str) -> AsyncGenerator[Frame, None]:
        pass

    async def say(self, text: str):
        await self.process_frame(TextFrame(text=text), FrameDirection.DOWNSTREAM)

    async def _handle_interruption(self, frame: StartInterruptionFrame, direction: FrameDirection):
        self._current_sentence = ""
        await self.push_frame(frame, direction)

    async def _process_text_frame(self, frame: TextFrame):
        text: str | None = None
        if not self._aggregate_sentences:
            text = frame.text
        else:
            self._current_sentence += frame.text
            if match_endofsentence(self._current_sentence):
                text = self._current_sentence
                self._current_sentence = ""

        if text:
            await self._push_tts_frames(text)

    async def _push_tts_frames(self, text: str, text_passthrough: bool = True):
        text = text.strip()
        if not text:
            return

        await self.push_frame(TTSStartedFrame())
        await self.start_processing_metrics()
        await self.process_generator(self.run_tts(text))
        await self.stop_processing_metrics()
        await self.push_frame(TTSStoppedFrame())
        if self._push_text_frames:
            # We send the original text after the audio. This way, if we are
            # interrupted, the text is not added to the assistant context.
            await self.push_frame(TextFrame(text))

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, TextFrame):
            await self._process_text_frame(frame)
        elif isinstance(frame, StartInterruptionFrame):
            await self._handle_interruption(frame, direction)
        elif isinstance(frame, LLMFullResponseEndFrame) or isinstance(frame, EndFrame):
            sentence = self._current_sentence
            self._current_sentence = ""
            await self._push_tts_frames(sentence)
            if isinstance(frame, LLMFullResponseEndFrame):
                if self._push_text_frames:
                    await self.push_frame(frame, direction)
            else:
                await self.push_frame(frame, direction)
        elif isinstance(frame, TTSSpeakFrame):
            await self._push_tts_frames(frame.text, False)
        elif isinstance(frame, TTSVoiceUpdateFrame):
            await self.set_voice(frame.voice)
        else:
            await self.push_frame(frame, direction)
