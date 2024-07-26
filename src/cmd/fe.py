import threading
import traceback
import asyncio
import logging
import time
import sys
import os

import uuid

from src.common import interface
from src.common.session import Session
from src.common.utils.audio_utils import save_audio_to_file
from src.common.types import SessionCtx, RECORDS_DIR
from src.common.utils.time import get_current_formatted_time
if os.getenv("INIT_TYPE", 'env') == 'yaml_config':
    from src.cmd.init import YamlConfig as init
else:
    from src.cmd.init import Env as init


class TerminalChatClient:
    def __init__(self, is_save_record=False, is_save_play_chunks=False) -> None:
        self.sid = str(uuid.uuid4())
        self.session = Session(**SessionCtx(self.sid).__dict__)

        audio_out_stream = init.initAudioOutStreamEngine()
        self.player = init.initPlayerEngine()
        self.player.set_out_stream(audio_out_stream)

        audio_in_stream = init.initAudioInStreamEngine()
        self.recorder = init.initRecorderEngine()
        self.recorder.set_in_stream(audio_in_stream)

        self.waker = init.initWakerEngine()
        self.vad = init.initVADEngine()

        self.start_record_event = threading.Event()
        self.play_chunks = []
        self.is_save_play_chunks = is_save_play_chunks
        self.is_save_record = is_save_record

    def run(self, conn: interface.IConnector):
        if self.is_save_play_chunks:
            self.player.set_args(on_play_chunk=self.on_play_chunk,
                                 on_play_end=self.on_play_end)
        self.player.open()
        self.player.start(self.session)

        play_t = threading.Thread(target=self.loop_recv,
                                  args=(conn,))
        record_t = threading.Thread(target=self.loop_record,
                                    args=(conn,))
        play_t.start()
        record_t.start()

        record_t.join()
        play_t.join()
        self.player.close()

    def on_wakeword_detected(self, session: Session, data):
        if "bot_name" in session.ctx.state:
            print(f"{session.ctx.state['bot_name']}~ ",
                  end="", flush=True, file=sys.stderr)

    def on_play_end(self, session: Session):
        sample_info = init.get_stream_info()
        # @todo: need async save i/o
        asyncio.run(save_audio_to_file(
            b''.join(self.play_chunks),
            self.session.get_paly_audio_name(),
            sample_rate=sample_info["rate"],
            channles=sample_info["channels"],
            sample_width=sample_info["sample_width"],
            audio_dir=RECORDS_DIR))
        logging.info(
            f"play end with session.ctx {session.ctx}, saved chunks_len {len(self.play_chunks)}")

    def on_play_chunk(self, session: Session, sub_chunk: bytes):
        logging.info(
            f"play chunk with session.ctx {session.ctx}, sub_chunk_len {len(sub_chunk)}")
        self.play_chunks.append(sub_chunk)

    def loop_record(self, conn: interface.IConnector):
        if self.waker is not None:
            self.waker.set_args(on_wakeword_detected=self.on_wakeword_detected)
        logging.info(
            f"loop_record starting with session ctx: {self.session.ctx}")
        print(
            f"start loop_record with {self.recorder.TAG} ...", flush=True, file=sys.stderr)
        while True:
            try:
                print(f"-- chat round {self.session.chat_round} --",
                      flush=True, file=sys.stdout)
                # self.start_record_event.clear()
                print(f"\n({get_current_formatted_time()}) me >> ",
                      end="", flush=True, file=sys.stderr)

                logging.info(f"start record audio")
                self.session.ctx.waker = self.waker
                self.session.ctx.vad = self.vad
                frames = asyncio.run(self.recorder.record_audio(self.session))
                if len(frames) == 0:
                    logging.info(f"record_audio return empty, continue")
                    continue
                data = b''.join(frames)
                conn.send(("RECORD_FRAMES", data, self.session), 'fe')
                if self.is_save_record:
                    asyncio.run(save_audio_to_file(
                        data, self.session.get_record_audio_name(),
                        audio_dir=RECORDS_DIR))
                self.session.increment_chat_round()

                if self.start_record_event.is_set():
                    logging.info(f"start record audio event is set, clear it!")
                    self.start_record_event.clear()
                logging.info(f"wait start record audio event")
                self.start_record_event.wait()
            except Exception as ex:
                ex_trace = traceback.format_exc()
                logging.warning(
                    f"loop_record Exception {ex} sid:{self.session.ctx.client_id} Trace {ex_trace}")
                time.sleep(1)

    def loop_recv(self, conn: interface.IConnector):
        print(
            f"start loop_recv with {self.player.TAG} ...", flush=True, file=sys.stderr)
        llm_gen_segments = 0
        while True:
            try:
                res = conn.recv('fe')
                if res is None:
                    continue

                msg, recv_data, session = res
                if msg is None or msg.lower() == "stop":
                    break
                if isinstance(recv_data, str):
                    logging.info(
                        f'FE Received: {msg} recv_data: {recv_data}, session: {session}')
                else:
                    logging.info(
                        f'FE Received: {msg} len(recv_data): {len(recv_data)}, session: {session}')

                if msg == "BE_EXCEPTION":
                    print(f"\nBE exception: {recv_data.strip()}",
                          end="", flush=True, file=sys.stderr)
                    self.start_record_event.set()
                    llm_gen_segments = 0
                    continue

                if session.ctx.client_id != self.session.ctx.client_id:
                    logging.warning(
                        f"session.ctx.client_id: {session.ctx.client_id} != self.session.ctx.client_id: {self.session.ctx.client_id}")
                    continue

                if msg == "PLAY_FRAMES":
                    self.session.ctx.state["tts_chunk"] = recv_data
                    self.player.play_audio(self.session)
                elif msg == "PLAY_FRAMES_DONE":
                    self.player.stop(self.session)
                    self.start_record_event.set()
                    llm_gen_segments = 0
                    self.player.start(self.session)
                elif msg == "LLM_GENERATE_TEXT":
                    if llm_gen_segments == 0:
                        bot_name = self.session.ctx.state["bot_name"] if "bot_name" in self.session.ctx.state else "bot"
                        logging.info(f"bot_name: {bot_name}")
                        print(f"\n({get_current_formatted_time()}) {bot_name} >> ",
                              end="", flush=True, file=sys.stderr)
                    print(recv_data.strip(), end="",
                          flush=True, file=sys.stderr)
                    llm_gen_segments += 1
                elif msg == "LLM_GENERATE_DONE":
                    print("\n", end="", flush=True, file=sys.stderr)
                    llm_gen_segments = 0
                elif msg == "ASR_TEXT":
                    print(recv_data.strip(), end="",
                          flush=True, file=sys.stderr)
                elif msg == "ASR_TEXT_DONE":
                    print("\n", end="", flush=True, file=sys.stderr)
                else:
                    logging.warning(f"unsupport msg {msg}")
            except Exception as ex:
                ex_trace = traceback.format_exc()
                logging.warning(f"loop_recv Exception {ex}, trace: {ex_trace}")
                self.start_record_event.set()
                llm_gen_segments = 0

    @staticmethod
    def clear_console():
        os.system('clear' if os.name == 'posix' else 'cls')
