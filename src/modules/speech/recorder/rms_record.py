import logging
import struct
import time


from src.common.audio_stream import RingBuffer
from src.common.session import Session
from src.common.types import SILENCE_THRESHOLD, SILENT_CHUNKS, RATE
from .base import PyAudioRecorder


class RMSRecorder(PyAudioRecorder):
    TAG = "rms_recorder"

    def __init__(self, **args) -> None:
        super().__init__(**args)
        if self.args.rate != RATE:
            raise Exception(
                f"Sampling rate of the audio just support 16000Hz at now")

    def compute_rms(self, data):
        # Assuming data is in 16-bit samples
        format = "<{}h".format(len(data) // 2)
        ints = struct.unpack(format, data)

        # Calculate RMS
        sum_squares = sum(i ** 2 for i in ints)
        rms = (sum_squares / len(ints)) ** 0.5
        return rms

    async def record_audio(self, session: Session) -> list[bytes]:
        silent_chunks = 0
        audio_started = False
        frames = []
        silence_timeout = 0
        if "silence_timeout_s" in session.ctx.state:
            logging.info(
                f"rms recording with silence_timeout {session.ctx.state['silence_timeout_s']} s")
            silence_timeout = int(session.ctx.state['silence_timeout_s'])

        self.audio.start_stream()
        logging.debug("start rms recording")
        start_time = time.time()
        if self.args.is_stream_callback is False:
            self.set_args(num_frames=self.args.frames_per_buffer)
        while True:
            data = self.get_record_buf()
            if len(data) == 0:
                time.sleep(self.args.no_stream_sleep_time_s)
                continue
            rms = self.compute_rms(data)
            if audio_started:
                frames.append(data)
                if rms < SILENCE_THRESHOLD:
                    silent_chunks += 1
                    if silent_chunks > self.args.silent_chunks:
                        break
                else:
                    silent_chunks = 0
            elif rms >= SILENCE_THRESHOLD:
                frames.append(data)
                audio_started = True
            else:
                if silence_timeout > 0 \
                        and time.time() - start_time > silence_timeout:
                    logging.warning(f"rms recording silence timeout")
                    break

        self.audio.stop_stream()
        logging.debug("end rms recording")

        return frames


class WakeWordsRMSRecorder(RMSRecorder):
    TAG = "wakeword_rms_recorder"

    def __init__(self, **args) -> None:
        super().__init__(**args)

    async def record_audio(self, session: Session) -> list[bytes]:
        if session.ctx.waker is None:
            logging.warning(
                f"WakeWordsRMSRecorder no waker instance in session ctx, use RMSRecorder")
            return await super().record_audio(session)

        sample_rate, frame_length = session.ctx.waker.get_sample_info()
        self.sample_rate, self.frame_length = sample_rate, frame_length

        # ring buffer
        pre_recording_buffer_duration = 3.0
        maxlen = int((sample_rate // frame_length) *
                     pre_recording_buffer_duration)
        self.audio_buffer = RingBuffer(maxlen)

        self.audio.start_stream()
        logging.info(
            f"start wake words detector rms recording; audio sample_rate: {self.sample_rate},frame_length:{self.frame_length}, audio buffer maxlen: {maxlen}")

        if self.args.is_stream_callback is False:
            self.set_args(num_frames=self.frame_length)

        while True:
            data = self.get_record_buf()
            if len(data) == 0:
                time.sleep(self.args.no_stream_sleep_time_s)
                continue
            session.ctx.read_audio_frames = data
            session.ctx.waker.set_audio_data(self.audio_buffer.get_buf())
            res = await session.ctx.waker.detect(session)
            if res is True:
                break
            self.audio_buffer.extend(data)

        self.audio.stop_stream()
        logging.debug("end wake words detector rms recording")

        if self.args.silence_timeout_s > 0:
            session.ctx.state["silence_timeout_s"] = self.args.silence_timeout_s
        return await super().record_audio(session)
