# server.py
from queue import Queue, Empty
import logging
from logsetup import setup_logging
setup_logging(logging.INFO)
logger = logging.getLogger(__name__)
if __name__ == "__main__":
    logger.info("🖥️👋 Welcome to local real-time voice chat")

from upsample_overlap import UpsampleOverlap
from datetime import datetime
from colors import Colors
import uvicorn
import asyncio
import struct
import json
import time
import threading # Keep threading for SpeechPipelineManager internals and AbortWorker
import sys
import os # Added for environment variable access
import subprocess # For managing Baresip process
import socket # For ctrl_tcp communication (if not using asyncio.open_connection directly for everything)
import re # For parsing Baresip ctrl_tcp responses if needed (though JSON is preferred)
import stat # For S_ISFIFO checks
import base64 # For encoding SIP audio chunks for WebSocket
import threading # For AudioBridge and AbortWorker
from queue import Empty as QueueEmpty # For AudioBridge queue.get timeout

from typing import Any, Dict, Optional, Callable, Awaitable # Added for type hints in docstrings
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import HTMLResponse, Response, FileResponse

USE_SSL = False
# TTS_START_ENGINE = "orpheus"
TTS_START_ENGINE = "kokoro"
# TTS_START_ENGINE = "coqui"
TTS_ORPHEUS_MODEL = "Orpheus_3B-1BaseGGUF/mOrpheus_3B-1Base_Q4_K_M.gguf"
# TTS_ORPHEUS_MODEL = "orpheus-3b-0.1-ft-Q8_0-GGUF/orpheus-3b-0.1-ft-q8_0.gguf"

LLM_START_PROVIDER = "ollama"
LLM_START_MODEL = "gemma3:4b"
# LLM_START_MODEL = "hf.co/bartowski/huihui-ai_Mistral-Small-24B-Instruct-2501-abliterated-GGUF:Q4_K_M"
# LLM_START_PROVIDER = "lmstudio"
# LLM_START_MODEL = "Qwen3-30B-A3B-GGUF/Qwen3-30B-A3B-Q3_K_L.gguf"
NO_THINK = False
DIRECT_STREAM = TTS_START_ENGINE=="kokoro"

if __name__ == "__main__":
    logger.info(f"🖥️⚙️ {Colors.apply('[PARAM]').blue} Starting engine: {Colors.apply(TTS_START_ENGINE).blue}")
    logger.info(f"🖥️⚙️ {Colors.apply('[PARAM]').blue} Direct streaming: {Colors.apply('ON' if DIRECT_STREAM else 'OFF').blue}")

# Baresip Configuration
BARESIP_EXECUTABLE = os.getenv("BARESIP_EXECUTABLE", "baresip")  # Or full path
BARESIP_CONFIG_PATH = os.getenv("BARESIP_CONFIG_PATH", "~/.baresip") # Or a path within the project for bundled config
BARESIP_CTRL_TCP_HOST = os.getenv("BARESIP_CTRL_TCP_HOST", "baresip") # Changed for Docker service discovery
BARESIP_CTRL_TCP_PORT = int(os.getenv("BARESIP_CTRL_TCP_PORT", 4444))
BARESIP_AUDIO_FROM_SIP_FIFO = os.getenv("BARESIP_AUDIO_FROM_SIP_FIFO", "/run/baresip_fifos/audio_from_sip.fifo") # Updated for Docker volume
BARESIP_AUDIO_TO_SIP_FIFO = os.getenv("BARESIP_AUDIO_TO_SIP_FIFO", "/run/baresip_fifos/audio_to_sip.fifo")     # Updated for Docker volume
SIP_AUDIO_SAMPLE_RATE = int(os.getenv("SIP_AUDIO_SAMPLE_RATE", 16000))
SIP_AUDIO_CHANNELS = int(os.getenv("SIP_AUDIO_CHANNELS", 1))
# For Baresip config, L16 is signed 16-bit little-endian PCM
SIP_AUDIO_FORMAT_STR = f"L16/{SIP_AUDIO_SAMPLE_RATE}/{SIP_AUDIO_CHANNELS}"

# Define the maximum allowed size for the incoming audio queue
try:
    MAX_AUDIO_QUEUE_SIZE = int(os.getenv("MAX_AUDIO_QUEUE_SIZE", 50))
    if __name__ == "__main__":
        logger.info(f"🖥️⚙️ {Colors.apply('[PARAM]').blue} Audio queue size limit set to: {Colors.apply(str(MAX_AUDIO_QUEUE_SIZE)).blue}")
except ValueError:
    if __name__ == "__main__":
        logger.warning("🖥️⚠️ Invalid MAX_AUDIO_QUEUE_SIZE env var. Using default: 50")
    MAX_AUDIO_QUEUE_SIZE = 50


if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

#from handlerequests import LanguageProcessor
#from audio_out import AudioOutProcessor
from audio_in import AudioInputProcessor
from speech_pipeline_manager import SpeechPipelineManager
from colors import Colors

LANGUAGE = "en"
# TTS_FINAL_TIMEOUT = 0.5 # unsure if 1.0 is needed for stability
TTS_FINAL_TIMEOUT = 1.0 # unsure if 1.0 is needed for stability


# --------------------------------------------------------------------
# Baresip Manager Class
# --------------------------------------------------------------------
class BaresipManager:
    def __init__(self, app_state: Any, loop: asyncio.AbstractEventLoop):
        self.app_state = app_state
        self.loop = loop
        self.process: Optional[asyncio.subprocess.Process] = None
        self.ctrl_reader: Optional[asyncio.StreamReader] = None
        self.ctrl_writer: Optional[asyncio.StreamWriter] = None
        self.command_queue: asyncio.Queue[Optional[str]] = asyncio.Queue() # To send commands to Baresip from other tasks
        self.active_sip_calls: Dict[str, Dict[str, Any]] = {} # call_id -> call_info
        self.on_incoming_call_callback: Optional[Callable[[str, str], Awaitable[None]]] = None # (call_id, remote_uri)
        self.on_call_established_callback: Optional[Callable[[str], Awaitable[None]]] = None # (call_id)
        self.on_call_closed_callback: Optional[Callable[[str], Awaitable[None]]] = None # (call_id)
        self._stop_event = asyncio.Event()
        self._ctrl_tasks: list[asyncio.Task] = []


    async def start(self):
        """Starts Baresip subprocess and connects to its ctrl_tcp interface."""
        if self._stop_event.is_set():
            self._stop_event.clear() # Allow restarting if previously stopped

        logger.info("🖥️📞 Starting BaresipManager...")
        await self._create_fifos()

        running_in_docker = os.getenv("RUNNING_IN_DOCKER", "false").lower() == "true"

        if not running_in_docker:
            logger.info("🖥️📞 Not running in Docker (or RUNNING_IN_DOCKER not true), will attempt to start Baresip process locally.")
            baresip_command = [
                BARESIP_EXECUTABLE,
                "-f", os.path.expanduser(BARESIP_CONFIG_PATH),
            ]
            try:
                self.process = await asyncio.create_subprocess_exec(
                    *baresip_command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                logger.info(f"🖥️📞 Local Baresip process started (PID: {self.process.pid}).")
                self._ctrl_tasks.append(asyncio.create_task(self._log_stream(self.process.stdout, "Baresip STDOUT")))
                self._ctrl_tasks.append(asyncio.create_task(self._log_stream(self.process.stderr, "Baresip STDERR")))
            except FileNotFoundError:
                logger.error(f"🖥️💥 Baresip executable '{BARESIP_EXECUTABLE}' not found for local execution. Please check path or install Baresip.")
                return
            except Exception as e:
                logger.error(f"🖥️💥 Failed to start local Baresip process: {e}")
                return
        else:
            logger.info("🖥️📞 Running in Docker, assuming Baresip is a separate service. Skipping local Baresip process start.")

        # Wait for Baresip to initialize, especially if it's a separate service
        # This might need adjustment depending on how quickly Baresip starts in its container.
        await asyncio.sleep(os.getenv("BARESIP_CONNECT_DELAY", 5)) # Increased delay for Docker Compose startup

        try:
            logger.info(f"🖥️📞 Attempting to connect to Baresip ctrl_tcp at {BARESIP_CTRL_TCP_HOST}:{BARESIP_CTRL_TCP_PORT}")
            self.ctrl_reader, self.ctrl_writer = await asyncio.open_connection(
                BARESIP_CTRL_TCP_HOST, BARESIP_CTRL_TCP_PORT
            )
            logger.info("🖥️📞 Connected to Baresip ctrl_tcp interface.")
            self._ctrl_tasks.append(asyncio.create_task(self._ctrl_reader_task()))
            self._ctrl_tasks.append(asyncio.create_task(self._ctrl_writer_task()))
        except ConnectionRefusedError:
            logger.error(f"🖥️💥 Baresip ctrl_tcp connection refused at {BARESIP_CTRL_TCP_HOST}:{BARESIP_CTRL_TCP_PORT}. Is Baresip running and ctrl_tcp module loaded and configured?")
        except Exception as e:
            logger.error(f"🖥️💥 Error connecting to Baresip ctrl_tcp: {e}")

    async def _log_stream(self, stream: Optional[asyncio.StreamReader], prefix: str):
        if not stream: return
        try:
            while not stream.at_eof():
                line = await stream.readline()
                if line:
                    logger.info(f"🖥️📞 [{prefix}] {line.decode(errors='ignore').strip()}")
                else:
                    break
        except asyncio.CancelledError:
            logger.info(f"🖥️📞 Log stream task for {prefix} cancelled.")
        except Exception as e:
            logger.error(f"🖥️💥 Error in log stream {prefix}: {e}")


    async def _create_fifos(self):
        logger.info(f"🖥️📞 Ensuring FIFOs exist: {BARESIP_AUDIO_FROM_SIP_FIFO}, {BARESIP_AUDIO_TO_SIP_FIFO}")
        for fifo_path_str in [BARESIP_AUDIO_FROM_SIP_FIFO, BARESIP_AUDIO_TO_SIP_FIFO]:
            fifo_path = os.path.expanduser(fifo_path_str)
            if not os.path.exists(fifo_path):
                try:
                    os.mkfifo(fifo_path)
                    logger.info(f"🖥️📞 Created FIFO: {fifo_path}")
                except OSError as e:
                    logger.error(f"🖥️💥 Failed to create FIFO {fifo_path}: {e}. Check permissions and path.")
            else:
                try:
                    if not stat.S_ISFIFO(os.stat(fifo_path).st_mode):
                        logger.error(f"🖥️💥 {fifo_path} exists but is not a FIFO. Please remove it or use a different path.")
                    else:
                        logger.info(f"🖥️📞 FIFO {fifo_path} already exists.")
                except Exception as e:
                     logger.error(f"🖥️💥 Error checking status of {fifo_path}: {e}")


    async def _ctrl_reader_task(self):
        if not self.ctrl_reader: return
        logger.info("🖥️📞 Baresip ctrl_reader_task started.")
        try:
            while not self._stop_event.is_set() and self.ctrl_reader and not self.ctrl_reader.at_eof():
                line = await self.ctrl_reader.readline()
                if not line:
                    logger.warning("🖥️📞 Baresip ctrl_tcp connection closed by Baresip (EOF).")
                    break
                message = line.decode().strip()
                if message:
                    logger.debug(f"🖥️📞 Baresip Event RAW: {message}")
                    try:
                        event = json.loads(message)
                        await self._handle_baresip_event(event)
                    except json.JSONDecodeError:
                        # Baresip ctrl_tcp can also send non-JSON status messages on command responses
                        logger.debug(f"🖥️📞 Non-JSON message from Baresip ctrl_tcp (possibly command response): {message}")
        except asyncio.CancelledError:
            logger.info("🖥️📞 Baresip ctrl_reader_task cancelled.")
        except ConnectionResetError:
            logger.warning("🖥️📞 Baresip ctrl_tcp connection reset.")
        except Exception as e:
            logger.error(f"🖥️💥 Error in Baresip ctrl_reader_task: {e}", exc_info=True)
        finally:
            logger.info("🖥️📞 Baresip ctrl_reader_task finished.")
            # Ensure other tasks are signalled to stop if the reader fails or connection drops
            await self.stop()


    async def _handle_baresip_event(self, event: Dict[str, Any]):
        # This will be expanded in a later iteration
        event_type = event.get("type")
        call_id = event.get("id")
        param = event.get("param")
        logger.info(f"🖥️📞 Parsed Baresip Event: Type={event_type}, ID={call_id}, Param={param}")

        # Basic state management (will be refined)
        if event_type == "CALL_INCOMING":
            if call_id and param:
                self.active_sip_calls[call_id] = {"state": "incoming", "remote_uri": param, "id": call_id}
                logger.info(f"🖥️📞📞 Incoming SIP call (ID: {call_id}) from {param}")
                if self.on_incoming_call_callback:
                    # Ensure callback is awaited if it's a coroutine
                    res = self.on_incoming_call_callback(call_id, param)
                    if asyncio.iscoroutine(res): await res
        elif event_type == "CALL_ESTABLISHED":
            if call_id and call_id in self.active_sip_calls:
                self.active_sip_calls[call_id]["state"] = "established"
                logger.info(f"🖥️📞📞 SIP call (ID: {call_id}) established with {self.active_sip_calls[call_id]['remote_uri']}")
                if self.on_call_established_callback:
                    res = self.on_call_established_callback(call_id)
                    if asyncio.iscoroutine(res): await res
        elif event_type == "CALL_CLOSED":
            if call_id and call_id in self.active_sip_calls:
                logger.info(f"🖥️📞📞 SIP call (ID: {call_id}) closed. Reason: {param}")
                # Call callback before removing, so it can access call details if needed
                if self.on_call_closed_callback:
                    res = self.on_call_closed_callback(call_id)
                    if asyncio.iscoroutine(res): await res
                del self.active_sip_calls[call_id]
        # TODO: Handle REGISTER_OK, REGISTER_FAIL, etc.

    async def _ctrl_writer_task(self):
        if not self.ctrl_writer: return
        logger.info("🖥️📞 Baresip ctrl_writer_task started.")
        try:
            while not self._stop_event.is_set() or not self.command_queue.empty():
                if self._stop_event.is_set() and self.command_queue.empty():
                    break # Exit if stop is set and queue is drained

                try:
                    command = await asyncio.wait_for(self.command_queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue # Check stop_event again

                if command is None: # Sentinel for stopping
                    self.command_queue.task_done() # Acknowledge sentinel
                    break

                if self.ctrl_writer.is_closing():
                    logger.warning(f"🖥️📞 Baresip ctrl_writer is closing. Cannot send command: {command}")
                    self.command_queue.task_done() # Still mark as done
                    # Optionally re-queue or handle error
                    break

                logger.debug(f"🖥️📞 Sending command to Baresip: {command}")
                self.ctrl_writer.write(f"{command}\n".encode())
                await self.ctrl_writer.drain()
                self.command_queue.task_done()
        except asyncio.CancelledError:
            logger.info("🖥️📞 Baresip ctrl_writer_task cancelled.")
        except ConnectionResetError:
            logger.warning("🖥️📞 Baresip ctrl_tcp connection reset during write.")
        except Exception as e:
            logger.error(f"🖥️💥 Error in Baresip ctrl_writer_task: {e}", exc_info=True)
        finally:
            logger.info("🖥️📞 Baresip ctrl_writer_task finished.")
            # Don't close writer here, stop() method will handle it.

    async def send_command(self, command: str):
        if self._stop_event.is_set() or not self.ctrl_writer or self.ctrl_writer.is_closing():
            logger.warning(f"🖥️📞 BaresipManager not active or writer closed. Cannot send command: {command}")
            return
        await self.command_queue.put(command)

    async def accept_call(self, call_id: Optional[str] = None):
        # Baresip 'accept' or 'a' command. If call_id is provided and Baresip supports
        # accepting a specific call ID, that would be better.
        # Default Baresip 'a' accepts the current/first ringing call.
        command = "accept" # Check Baresip docs for specific call ID acceptance if needed
        logger.info(f"🖥️📞 Instructing Baresip to accept call (Target ID: {call_id if call_id else 'any'}). Using command: {command}")
        await self.send_command(command)

    async def hangup_call(self, call_id: Optional[str] = None):
        command = "hangup" # General hangup
        if call_id and call_id in self.active_sip_calls:
            # Baresip's command is 'h <call_id>' or 'hangup <call_id>'
            # However, some versions might use 'call_hangup <id>' or similar.
            # Let's use a common one, assuming it will hang up the specific call if it's the current one.
            # A more reliable way is to find the specific command for hanging up a call by ID
            # from Baresip's ctrl_tcp documentation if 'hangup <id>' isn't it.
            # For now, we'll use the general hangup and if an ID is known, log it.
            logger.info(f"🖥️📞 Instructing Baresip to hangup call (Known ID: {call_id})")
        elif not call_id and self.active_sip_calls:
            active_call_id = next(iter(self.active_sip_calls), None)
            logger.info(f"🖥️📞 Instructing Baresip to hangup active call (ID: {active_call_id if active_call_id else 'unknown'})")
        else:
            logger.info("🖥️📞 Instructing Baresip to hangup current/all calls.")
        await self.send_command(command)


    async def stop(self):
        if self._stop_event.is_set():
            return
        logger.info("🖥️📞 Stopping BaresipManager...")
        self._stop_event.set()

        # Signal writer task to stop and process remaining queue items
        if self.command_queue:
            await self.command_queue.put(None)

        # Cancel control tasks (reader, writer, loggers)
        for task in self._ctrl_tasks:
            if not task.done():
                task.cancel()
        if self._ctrl_tasks:
            await asyncio.gather(*self._ctrl_tasks, return_exceptions=True)
        self._ctrl_tasks = []

        if self.ctrl_writer and not self.ctrl_writer.is_closing():
            self.ctrl_writer.close()
            try:
                await self.ctrl_writer.wait_closed()
            except Exception as e:
                logger.warning(f"Error closing ctrl_writer: {e}")
        self.ctrl_reader = None # Reader task handles its closure or EOF

        running_in_docker = os.getenv("RUNNING_IN_DOCKER", "false").lower() == "true"
        if not running_in_docker and self.process and self.process.returncode is None:
            logger.info(f"🖥️📞 Terminating local Baresip process (PID: {self.process.pid})...")
            try:
                self.process.terminate() # SIGTERM
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
                logger.info(f"🖥️📞 Local Baresip process (PID: {self.process.pid}) terminated with code {self.process.returncode}.")
            except asyncio.TimeoutError:
                logger.warning(f"🖥️📞 Local Baresip process (PID: {self.process.pid}) did not terminate gracefully, killing (SIGKILL).")
                self.process.kill()
                await self.process.wait()
                logger.info(f"🖥️📞 Local Baresip process (PID: {self.process.pid}) killed with code {self.process.returncode}.")
            except Exception as e: # Catch other potential errors like ProcessLookupError
                logger.error(f"🖥️💥 Error terminating local Baresip process (PID: {self.process.pid if self.process else 'unknown'}): {e}")
        elif running_in_docker:
            logger.info("🖥️📞 Running in Docker, Baresip process is managed externally (by Docker Compose).")

        # FIFO cleanup is better handled by the system or on next start if needed,
        # as other processes might still be using them if Baresip didn't close them.
        # Forcing removal here can be problematic. Let _create_fifos handle existing ones.
        logger.info("🖥️📞 BaresipManager stopped.")

# --------------------------------------------------------------------
# Audio Bridge Class
# --------------------------------------------------------------------
class AudioBridge:
    def __init__(self, loop: asyncio.AbstractEventLoop,
                 from_sip_fifo_path_str: str, to_sip_fifo_path_str: str,
                 audio_to_ws_queue: asyncio.Queue, # Python reads from FIFO, puts here for WS
                 audio_from_ws_queue: asyncio.Queue, # Python gets from here (from WS), writes to FIFO
                 chunk_size: int = 640 # Default: 20ms of 16kHz 16-bit mono audio (320 samples * 2 bytes/sample)
                 ):
        self.loop = loop
        self.from_sip_fifo_path = os.path.expanduser(from_sip_fifo_path_str)
        self.to_sip_fifo_path = os.path.expanduser(to_sip_fifo_path_str)
        self.audio_to_ws_queue = audio_to_ws_queue
        self.audio_from_ws_queue = audio_from_ws_queue
        self._stop_event = threading.Event()
        self.reader_thread: Optional[threading.Thread] = None
        self.writer_thread: Optional[threading.Thread] = None
        # CHUNK_SIZE: e.g. 16kHz, 16-bit mono, 20ms chunks -> 16000 * 2 * 0.020 = 640 bytes
        # This should align with what Baresip is configured to send/receive if possible,
        # or be a reasonable size for processing.
        self.CHUNK_SIZE = chunk_size
        self._reader_fifo_fd: Optional[int] = None
        self._writer_fifo_fd: Optional[int] = None


    def _read_from_sip_fifo_thread(self):
        logger.info(f"🎧🔊 Starting FIFO reader thread for {self.from_sip_fifo_path}")
        fifo = None
        try:
            # Open in non-blocking mode to allow stop_event checking if FIFO isn't ready,
            # but os.open then requires more careful read handling.
            # For simplicity, let's use blocking open first. If Baresip isn't writing, this will block.
            # This fd is primarily to allow interrupting the read via os.close() in stop()
            self._reader_fifo_fd = os.open(self.from_sip_fifo_path, os.O_RDONLY)
            # Now open it as a Python file object for easier reading
            fifo = open(self._reader_fifo_fd, "rb")
            logger.info(f"🎧🔊 FIFO {self.from_sip_fifo_path} opened for reading by Python.")
            while not self._stop_event.is_set():
                chunk = fifo.read(self.CHUNK_SIZE) # This read can block
                if not chunk: # EOF
                    if self._stop_event.is_set(): # Expected if stopping
                        logger.info(f"🎧🔊 FIFO {self.from_sip_fifo_path} read EOF during stop.")
                    else: # Unexpected EOF
                        logger.warning(f"🎧🔊 FIFO {self.from_sip_fifo_path} read EOF. Baresip might have closed.")
                    break
                asyncio.run_coroutine_threadsafe(self.audio_to_ws_queue.put(chunk), self.loop)
        except FileNotFoundError:
            if not self._stop_event.is_set():
                logger.error(f"🎧💥 FIFO {self.from_sip_fifo_path} not found for reading.")
        except OSError as e: # Covers cases like trying to open FIFO not opened for writing yet if O_NONBLOCK was used, or other OS errors
             if not self._stop_event.is_set():
                logger.error(f"🎧💥 OSError in FIFO reader thread ({self.from_sip_fifo_path}): {e.strerror} (errno {e.errno})")
        except Exception as e:
            if not self._stop_event.is_set():
                logger.exception(f"🎧💥 Unhandled error in FIFO reader thread ({self.from_sip_fifo_path})")
        finally:
            if fifo:
                try:
                    fifo.close() # This also closes the fd if it's the only reference
                except Exception as e:
                    logger.warning(f"🎧🔊 Error closing reader FIFO file object: {e}")
            elif self._reader_fifo_fd is not None: # If only fd was opened
                 try:
                    os.close(self._reader_fifo_fd)
                 except Exception as e:
                    logger.warning(f"🎧🔊 Error closing reader FIFO fd: {e}")
            self._reader_fifo_fd = None
            logger.info(f"🎧🔊 FIFO reader thread for {self.from_sip_fifo_path} stopped.")

    def _write_to_sip_fifo_thread(self):
        logger.info(f"🎧🎤 Starting FIFO writer thread for {self.to_sip_fifo_path}")
        fifo = None
        try:
            self._writer_fifo_fd = os.open(self.to_sip_fifo_path, os.O_WRONLY)
            fifo = open(self._writer_fifo_fd, "wb")
            logger.info(f"🎧🎤 FIFO {self.to_sip_fifo_path} opened for writing by Python.")
            while not self._stop_event.is_set():
                try:
                    # Timeout allows checking _stop_event periodically
                    chunk = self.audio_from_ws_queue.get(timeout=0.1)
                    if chunk is None: # Sentinel for stopping
                        self.audio_from_ws_queue.task_done()
                        break

                    fifo.write(chunk)
                    fifo.flush() # Ensure it's written immediately
                    self.audio_from_ws_queue.task_done()
                except QueueEmpty:
                    continue
                except BrokenPipeError:
                    if not self._stop_event.is_set():
                        logger.warning(f"🎧🎤 Broken pipe writing to FIFO {self.to_sip_fifo_path}. Baresip might have closed its reading end.")
                    break # Exit thread if pipe is broken
                except OSError as e: # Other OS errors related to FIFO
                    if not self._stop_event.is_set():
                        logger.error(f"🎧🎤 OSError in FIFO writer thread ({self.to_sip_fifo_path}): {e.strerror} (errno {e.errno})")
                    break

        except FileNotFoundError:
            if not self._stop_event.is_set():
                logger.error(f"🎧💥 FIFO {self.to_sip_fifo_path} not found for writing.")
        except OSError as e: # Covers cases like trying to open FIFO not opened for reading yet if O_NONBLOCK was used
             if not self._stop_event.is_set():
                logger.error(f"🎧💥 OSError opening FIFO writer ({self.to_sip_fifo_path}): {e.strerror} (errno {e.errno})")
        except Exception as e:
            if not self._stop_event.is_set():
                 logger.exception(f"🎧💥 Unhandled error in FIFO writer thread ({self.to_sip_fifo_path})")
        finally:
            if fifo:
                try:
                    fifo.close()
                except Exception as e:
                    logger.warning(f"🎧🎤 Error closing writer FIFO file object: {e}")
            elif self._writer_fifo_fd is not None:
                 try:
                    os.close(self._writer_fifo_fd)
                 except Exception as e:
                    logger.warning(f"🎧🎤 Error closing writer FIFO fd: {e}")
            self._writer_fifo_fd = None
            logger.info(f"🎧🎤 FIFO writer thread for {self.to_sip_fifo_path} stopped.")

    def start(self):
        if self.reader_thread and self.reader_thread.is_alive() or \
           self.writer_thread and self.writer_thread.is_alive():
            logger.warning("🎧🌉 AudioBridge threads already running. Not starting again.")
            return

        self._stop_event.clear()
        self.reader_thread = threading.Thread(target=self._read_from_sip_fifo_thread, name="AudioBridge-Reader", daemon=True)
        self.writer_thread = threading.Thread(target=self._write_to_sip_fifo_thread, name="AudioBridge-Writer", daemon=True)
        self.reader_thread.start()
        self.writer_thread.start()
        logger.info("🎧🌉 AudioBridge started with FIFO reader/writer threads.")

    def stop(self):
        logger.info("🎧🌉 Stopping AudioBridge...")
        self._stop_event.set()

        # Unblock writer thread by putting sentinel
        if self.writer_thread and self.writer_thread.is_alive():
            try:
                # No need for run_coroutine_threadsafe, this is called from main thread or asyncio task usually
                self.audio_from_ws_queue.put_nowait(None)
            except asyncio.QueueFull: # Should not happen with sentinel if queue has space
                logger.warning("🎧🌉 AudioBridge: Writer queue full, couldn't place sentinel immediately.")
            except Exception as e:
                 logger.warning(f"🎧🌉 AudioBridge: Error putting sentinel to writer queue: {e}")


        # Attempt to interrupt blocking os.read() in reader_thread
        # This is a bit hacky and platform-dependent.
        # Closing the file descriptor from another thread can cause read() to return an error.
        if self._reader_fifo_fd is not None:
            try:
                logger.info(f"🎧🌉 Attempting to close reader FIFO fd {self._reader_fifo_fd} to unblock thread.")
                os.close(self._reader_fifo_fd) # This should make fifo.read() in thread raise OSError or return EOF
            except OSError as e:
                logger.warning(f"🎧🌉 Error closing reader FIFO fd during stop: {e}")
            # self._reader_fifo_fd = None # fd is closed now

        if self.reader_thread and self.reader_thread.is_alive():
            self.reader_thread.join(timeout=2.0)
            if self.reader_thread.is_alive():
                logger.warning(f"🎧🔊 FIFO reader thread for {self.from_sip_fifo_path} did not stop cleanly via join.")

        if self.writer_thread and self.writer_thread.is_alive():
            self.writer_thread.join(timeout=2.0)
            if self.writer_thread.is_alive():
                 logger.warning(f"🎧🎤 FIFO writer thread for {self.to_sip_fifo_path} did not stop cleanly via join.")
        logger.info("🎧🌉 AudioBridge stopped.")

# --------------------------------------------------------------------
# Custom no-cache StaticFiles
# --------------------------------------------------------------------
class NoCacheStaticFiles(StaticFiles):
    """
    Serves static files without allowing client-side caching.

    Overrides the default Starlette StaticFiles to add 'Cache-Control' headers
    that prevent browsers from caching static assets. Useful for development.
    """
    async def get_response(self, path: str, scope: Dict[str, Any]) -> Response:
        """
        Gets the response for a requested path, adding no-cache headers.

        Args:
            path: The path to the static file requested.
            scope: The ASGI scope dictionary for the request.

        Returns:
            A Starlette Response object with cache-control headers modified.
        """
        response: Response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        # These might not be strictly necessary with no-store, but belt and suspenders
        if "etag" in response.headers:
             response.headers.__delitem__("etag")
        if "last-modified" in response.headers:
             response.headers.__delitem__("last-modified")
        return response

# --------------------------------------------------------------------
# Lifespan management
# --------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Manages the application's lifespan, initializing and shutting down resources.

    Initializes global components like SpeechPipelineManager, Upsampler, and
    AudioInputProcessor and stores them in `app.state`. Handles cleanup on shutdown.

    Args:
        app: The FastAPI application instance.
    """
    logger.info("🖥️▶️ Server starting up")
    # Initialize global components, not connection-specific state
    app.state.SpeechPipelineManager = SpeechPipelineManager(
        tts_engine=TTS_START_ENGINE,
        llm_provider=LLM_START_PROVIDER,
        llm_model=LLM_START_MODEL,
        no_think=NO_THINK,
        orpheus_model=TTS_ORPHEUS_MODEL,
    )

    app.state.Upsampler = UpsampleOverlap()
    app.state.AudioInputProcessor = AudioInputProcessor(
        LANGUAGE,
        is_orpheus=TTS_START_ENGINE=="orpheus",
        pipeline_latency=app.state.SpeechPipelineManager.full_output_pipeline_latency / 1000, # seconds
    )
    app.state.Aborting = False # Keep this? Its usage isn't clear in the provided snippet. Minimizing changes.

    yield

    logger.info("🖥️⏹️ Server shutting down")
    app.state.AudioInputProcessor.shutdown()

    # Stop BaresipManager and AudioBridge
    if hasattr(app.state, 'AudioBridgeInstance') and app.state.AudioBridgeInstance:
        logger.info("🖥️⏹️ Stopping AudioBridge...")
        app.state.AudioBridgeInstance.stop()
    if hasattr(app.state, 'BaresipManagerInstance') and app.state.BaresipManagerInstance:
        logger.info("🖥️⏹️ Stopping BaresipManager...")
        await app.state.BaresipManagerInstance.stop()

# --------------------------------------------------------------------
# FastAPI app instance and global state for SIP
# --------------------------------------------------------------------
class SIPWebSocketAppState:
    def __init__(self):
        self.active_websocket_for_sip: Optional[WebSocket] = None
        self.active_websocket_message_queue: Optional[asyncio.Queue] = None

# Lifespan manager
@asynccontextmanager
async def lifespan(app_fastapi: FastAPI): # Renamed app to app_fastapi to avoid conflict
    logger.info("🖥️▶️ Server starting up")
    loop = asyncio.get_running_loop()

    # Initialize global app state for SIP WebSocket management
    app_fastapi.state.sip_ws_app_state = SIPWebSocketAppState()

    # Initialize existing components
    app_fastapi.state.SpeechPipelineManager = SpeechPipelineManager(
        tts_engine=TTS_START_ENGINE,
        llm_provider=LLM_START_PROVIDER,
        llm_model=LLM_START_MODEL,
        no_think=NO_THINK,
        orpheus_model=TTS_ORPHEUS_MODEL,
    )
    app_fastapi.state.Upsampler = UpsampleOverlap()
    app_fastapi.state.AudioInputProcessor = AudioInputProcessor(
        LANGUAGE,
        is_orpheus=TTS_START_ENGINE=="orpheus",
        pipeline_latency=app_fastapi.state.SpeechPipelineManager.full_output_pipeline_latency / 1000, # seconds
    )
    app_fastapi.state.Aborting = False

    # Initialize BaresipManager
    app_fastapi.state.BaresipManagerInstance = BaresipManager(app_fastapi.state, loop)
    asyncio.create_task(app_fastapi.state.BaresipManagerInstance.start())

    # Initialize AudioBridge related queues
    app_fastapi.state.sip_audio_to_ws_queue = asyncio.Queue()  # Audio from Baresip (SIP) to WebSocket
    app_fastapi.state.sip_audio_from_ws_queue = asyncio.Queue() # Audio from WebSocket to Baresip (SIP)

    # Calculate chunk size for AudioBridge based on SIP audio settings (e.g., for 20ms packets)
    # Samples per chunk = sample_rate * packet_duration_ms / 1000
    # Bytes per chunk = samples_per_chunk * bytes_per_sample (L16 is 2 bytes)
    sip_packet_duration_ms = 20
    samples_per_chunk = int(SIP_AUDIO_SAMPLE_RATE * (sip_packet_duration_ms / 1000.0))
    bytes_per_sample = 2 # For L16 (16-bit PCM)
    audio_bridge_chunk_size = samples_per_chunk * bytes_per_sample

    app_fastapi.state.AudioBridgeInstance = AudioBridge(
        loop,
        BARESIP_AUDIO_FROM_SIP_FIFO,
        BARESIP_AUDIO_TO_SIP_FIFO,
        app_fastapi.state.sip_audio_to_ws_queue,
        app_fastapi.state.sip_audio_from_ws_queue,
        chunk_size=audio_bridge_chunk_size
    )
    # AudioBridge will be started/stopped by BaresipManager based on call state typically,
    # or started here if it should always run. For now, let BaresipManager callbacks handle it.
    # app_fastapi.state.AudioBridgeInstance.start()

    yield

    logger.info("🖥️⏹️ Server shutting down")
    # Stop BaresipManager and AudioBridge first
    if hasattr(app_fastapi.state, 'AudioBridgeInstance') and app_fastapi.state.AudioBridgeInstance:
        logger.info("🖥️⏹️ Stopping AudioBridge...")
        app_fastapi.state.AudioBridgeInstance.stop() # This is synchronous
    if hasattr(app_fastapi.state, 'BaresipManagerInstance') and app_fastapi.state.BaresipManagerInstance:
        logger.info("🖥️⏹️ Stopping BaresipManager...")
        await app_fastapi.state.BaresipManagerInstance.stop() # This is asynchronous

    if hasattr(app_fastapi.state, 'AudioInputProcessor') and app_fastapi.state.AudioInputProcessor:
        app_fastapi.state.AudioInputProcessor.shutdown()
    # Other cleanup if necessary

app = FastAPI(lifespan=lifespan)

# Enable CORS if needed
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files with no cache
app.mount("/static", NoCacheStaticFiles(directory="static"), name="static")

@app.get("/favicon.ico")
async def favicon():
    """
    Serves the favicon.ico file.

    Returns:
        A FileResponse containing the favicon.
    """
    return FileResponse("static/favicon.ico")

@app.get("/")
async def get_index() -> HTMLResponse:
    """
    Serves the main index.html page.

    Reads the content of static/index.html and returns it as an HTML response.

    Returns:
        An HTMLResponse containing the content of index.html.
    """
    with open("static/index.html", "r", encoding="utf-8") as f:
        html_content = f.read()
    return HTMLResponse(content=html_content)

# --------------------------------------------------------------------
# Audio Utilities (including Transcoding Placeholders)
# --------------------------------------------------------------------
def resample_audio_placeholder(pcm_data: bytes, input_rate: int, output_rate: int, num_channels: int = 1, sample_width: int = 2) -> bytes:
    """
    Placeholder for audio resampling.
    In a real implementation, use a library like librosa, scipy.signal.resample,
    soundfile, or ffmpeg.
    This placeholder will just return the data if rates match, otherwise log a warning
    and return the original data. A proper implementation is crucial for audio quality.
    """
    if input_rate == output_rate:
        return pcm_data

    logger.warning(
        f"Audio resampling placeholder: Attempting to convert from {input_rate}Hz to {output_rate}Hz. "
        f"NO ACTUAL RESAMPLING IS PERFORMED by this placeholder. Audio will likely be speed up/slowed down or distorted. "
        f"Implement proper resampling using a library like librosa, scipy.signal, or by calling ffmpeg."
    )

    # Crude example: if downsampling 48k -> 16k, factor = 3. Take 1 sample, skip 2.
    # This is NOT a quality resampling method.
    if input_rate > output_rate and input_rate % output_rate == 0:
        factor = input_rate // output_rate
        output_pcm = bytearray()
        frame_size = num_channels * sample_width
        num_frames = len(pcm_data) // frame_size
        for i in range(num_frames):
            if i % factor == 0:
                output_pcm.extend(pcm_data[i*frame_size : (i+1)*frame_size])
        logger.info(f"Crude downsample applied: original bytes {len(pcm_data)}, new bytes {len(output_pcm)}")
        return bytes(output_pcm)

    # Crude example: if upsampling 16k -> 48k, factor = 3. Repeat each sample 3 times.
    # This is also NOT a quality resampling method.
    elif output_rate > input_rate and output_rate % input_rate == 0:
        factor = output_rate // input_rate
        output_pcm = bytearray()
        frame_size = num_channels * sample_width
        num_frames = len(pcm_data) // frame_size
        for i in range(num_frames):
            frame = pcm_data[i*frame_size : (i+1)*frame_size]
            for _ in range(factor):
                output_pcm.extend(frame)
        logger.info(f"Crude upsample applied: original bytes {len(pcm_data)}, new bytes {len(output_pcm)}")
        return bytes(output_pcm)

    # Fallback for non-integer factors or if no crude method applies
    return pcm_data


# Placeholder for target WebSocket audio format (client-side often uses higher rates)
WS_CLIENT_SAMPLE_RATE = int(os.getenv("WS_CLIENT_SAMPLE_RATE", 48000)) # e.g., 48kHz often used by browsers
WS_CLIENT_CHANNELS = 1
WS_CLIENT_SAMPLE_WIDTH = 2 # Bytes, e.g., 2 for 16-bit PCM


# --------------------------------------------------------------------
# Utility functions
# --------------------------------------------------------------------
def parse_json_message(text: str) -> dict:
    """
    Safely parses a JSON string into a dictionary.

    Logs a warning if the JSON is invalid and returns an empty dictionary.

    Args:
        text: The JSON string to parse.

    Returns:
        A dictionary representing the parsed JSON, or an empty dictionary on error.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.warning("🖥️⚠️ Ignoring client message with invalid JSON")
        return {}

def format_timestamp_ns(timestamp_ns: int) -> str:
    """
    Formats a nanosecond timestamp into a human-readable HH:MM:SS.fff string.

    Args:
        timestamp_ns: The timestamp in nanoseconds since the epoch.

    Returns:
        A string formatted as hours:minutes:seconds.milliseconds.
    """
    # Split into whole seconds and the nanosecond remainder
    seconds = timestamp_ns // 1_000_000_000
    remainder_ns = timestamp_ns % 1_000_000_000

    # Convert seconds part into a datetime object (local time)
    dt = datetime.fromtimestamp(seconds)

    # Format the main time as HH:MM:SS
    time_str = dt.strftime("%H:%M:%S")

    # For instance, if you want milliseconds, divide the remainder by 1e6 and format as 3-digit
    milliseconds = remainder_ns // 1_000_000
    formatted_timestamp = f"{time_str}.{milliseconds:03d}"

    return formatted_timestamp

# --------------------------------------------------------------------
# WebSocket data processing
# --------------------------------------------------------------------

async def process_incoming_data(ws: WebSocket, app: FastAPI, incoming_chunks: asyncio.Queue, callbacks: 'TranscriptionCallbacks') -> None:
    """
    Receives messages via WebSocket, processes audio and text messages.

    Handles binary audio chunks, extracting metadata (timestamp, flags) and
    putting the audio PCM data with metadata into the `incoming_chunks` queue.
    Applies back-pressure if the queue is full.
    Parses text messages (assumed JSON) and triggers actions based on message type
    (e.g., updates client TTS state via `callbacks`, clears history, sets speed).

    Args:
        ws: The WebSocket connection instance.
        app: The FastAPI application instance (for accessing global state if needed).
        incoming_chunks: An asyncio queue to put processed audio metadata dictionaries into.
        callbacks: The TranscriptionCallbacks instance for this connection to manage state.
    """
    try:
        while True:
            msg = await ws.receive()
            if "bytes" in msg and msg["bytes"]:
                raw = msg["bytes"]

                # Ensure we have at least an 8‑byte header: 4 bytes timestamp_ms + 4 bytes flags
                if len(raw) < 8:
                    logger.warning("🖥️⚠️ Received packet too short for 8‑byte header.")
                    continue

                # Unpack big‑endian uint32 timestamp (ms) and uint32 flags
                timestamp_ms, flags = struct.unpack("!II", raw[:8])
                client_sent_ns = timestamp_ms * 1_000_000

                # Build metadata using fixed fields
                metadata = {
                    "client_sent_ms":           timestamp_ms,
                    "client_sent":              client_sent_ns,
                    "client_sent_formatted":    format_timestamp_ns(client_sent_ns),
                    "isTTSPlaying":             bool(flags & 1),
                }

                # Record server receive time
                server_ns = time.time_ns()
                metadata["server_received"] = server_ns
                metadata["server_received_formatted"] = format_timestamp_ns(server_ns)

                # The rest of the payload is raw PCM bytes
                metadata["pcm"] = raw[8:]

                # Check queue size before putting data
                current_qsize = incoming_chunks.qsize()
                if current_qsize < MAX_AUDIO_QUEUE_SIZE:
                    # Now put only the metadata dict (containing PCM audio) into the processing queue.
                    await incoming_chunks.put(metadata)
                else:
                    # Queue is full, drop the chunk and log a warning
                    logger.warning(
                        f"🖥️⚠️ Audio queue full ({current_qsize}/{MAX_AUDIO_QUEUE_SIZE}); dropping chunk. Possible lag."
                    )

            elif "text" in msg and msg["text"]:
                # Text-based message: parse JSON
                data = parse_json_message(msg["text"])
                msg_type = data.get("type")
                logger.info(Colors.apply(f"🖥️📥 ←←Client: {data}").orange)


                if msg_type == "tts_start":
                    logger.info("🖥️ℹ️ Received tts_start from client.")
                    # Update connection-specific state via callbacks
                    callbacks.tts_client_playing = True
                elif msg_type == "tts_stop":
                    logger.info("🖥️ℹ️ Received tts_stop from client.")
                    # Update connection-specific state via callbacks
                    callbacks.tts_client_playing = False
                # Add to the handleJSONMessage function in server.py
                elif msg_type == "clear_history":
                    logger.info("🖥️ℹ️ Received clear_history from client.")
                    app.state.SpeechPipelineManager.reset()
                elif msg_type == "set_speed":
                    speed_value = data.get("speed", 0)
                    speed_factor = speed_value / 100.0  # Convert 0-100 to 0.0-1.0
                    turn_detection = app.state.AudioInputProcessor.transcriber.turn_detection
                    if turn_detection:
                        turn_detection.update_settings(speed_factor)
                        logger.info(f"🖥️⚙️ Updated turn detection settings to factor: {speed_factor:.2f}")


    except asyncio.CancelledError:
        pass # Task cancellation is expected on disconnect
    except WebSocketDisconnect as e:
        logger.warning(f"🖥️⚠️ {Colors.apply('WARNING').red} disconnect in process_incoming_data: {repr(e)}")
    except RuntimeError as e:  # Often raised on closed transports
        logger.error(f"🖥️💥 {Colors.apply('RUNTIME_ERROR').red} in process_incoming_data: {repr(e)}")
    except Exception as e:
        logger.exception(f"🖥️💥 {Colors.apply('EXCEPTION').red} in process_incoming_data: {repr(e)}")

async def send_text_messages(ws: WebSocket, message_queue: asyncio.Queue) -> None:
    """
    Continuously sends text messages from a queue to the client via WebSocket.

    Waits for messages on the `message_queue`, formats them as JSON, and sends
    them to the connected WebSocket client. Logs non-TTS messages.

    Args:
        ws: The WebSocket connection instance.
        message_queue: An asyncio queue yielding dictionaries to be sent as JSON.
    """
    try:
        while True:
            await asyncio.sleep(0.001) # Yield control
            data = await message_queue.get()
            msg_type = data.get("type")
            if msg_type != "tts_chunk":
                logger.info(Colors.apply(f"🖥️📤 →→Client: {data}").orange)
            await ws.send_json(data)
    except asyncio.CancelledError:
        pass # Task cancellation is expected on disconnect
    except WebSocketDisconnect as e:
        logger.warning(f"🖥️⚠️ {Colors.apply('WARNING').red} disconnect in send_text_messages: {repr(e)}")
    except RuntimeError as e:  # Often raised on closed transports
        logger.error(f"🖥️💥 {Colors.apply('RUNTIME_ERROR').red} in send_text_messages: {repr(e)}")
    except Exception as e:
        logger.exception(f"🖥️💥 {Colors.apply('EXCEPTION').red} in send_text_messages: {repr(e)}")

async def _reset_interrupt_flag_async(app: FastAPI, callbacks: 'TranscriptionCallbacks'):
    """
    Resets the microphone interruption flag after a delay (async version).

    Waits for 1 second, then checks if the AudioInputProcessor is still marked
    as interrupted. If so, resets the flag on both the processor and the
    connection-specific callbacks instance.

    Args:
        app: The FastAPI application instance (to access AudioInputProcessor).
        callbacks: The TranscriptionCallbacks instance for the connection.
    """
    await asyncio.sleep(1)
    # Check the AudioInputProcessor's own interrupted state
    if app.state.AudioInputProcessor.interrupted:
        logger.info(f"{Colors.apply('🖥️🎙️ ▶️ Microphone continued (async reset)').cyan}")
        app.state.AudioInputProcessor.interrupted = False
        # Reset connection-specific interruption time via callbacks
        callbacks.interruption_time = 0
        logger.info(Colors.apply("🖥️🎙️ interruption flag reset after TTS chunk (async)").cyan)

async def send_tts_chunks(app: FastAPI, message_queue: asyncio.Queue, callbacks: 'TranscriptionCallbacks') -> None:
    """
    Continuously sends TTS audio chunks from the SpeechPipelineManager to the client.

    Monitors the state of the current speech generation (if any) and the client
    connection (via `callbacks`). Retrieves audio chunks from the active generation's
    queue, upsamples/encodes them, and puts them onto the outgoing `message_queue`
    for the client. Handles the end-of-generation logic and state resets.

    Args:
        app: The FastAPI application instance (to access global components).
        message_queue: An asyncio queue to put outgoing TTS chunk messages onto.
        callbacks: The TranscriptionCallbacks instance managing this connection's state.
    """
    try:
        logger.info("🖥️🔊 Starting TTS chunk sender")
        last_quick_answer_chunk = 0
        last_chunk_sent = 0
        prev_status = None

        while True:
            await asyncio.sleep(0.001) # Yield control

            # Use connection-specific interruption_time via callbacks
            if app.state.AudioInputProcessor.interrupted and callbacks.interruption_time and time.time() - callbacks.interruption_time > 2.0:
                app.state.AudioInputProcessor.interrupted = False
                callbacks.interruption_time = 0 # Reset via callbacks
                logger.info(Colors.apply("🖥️🎙️ interruption flag reset after 2 seconds").cyan)

            is_tts_finished = app.state.SpeechPipelineManager.is_valid_gen() and app.state.SpeechPipelineManager.running_generation.audio_quick_finished

            def log_status():
                nonlocal prev_status
                last_quick_answer_chunk_decayed = (
                    last_quick_answer_chunk
                    and time.time() - last_quick_answer_chunk > TTS_FINAL_TIMEOUT
                    and time.time() - last_chunk_sent > TTS_FINAL_TIMEOUT
                )

                curr_status = (
                    # Access connection-specific state via callbacks
                    int(callbacks.tts_to_client),
                    int(callbacks.tts_client_playing),
                    int(callbacks.tts_chunk_sent),
                    1, # Placeholder?
                    int(callbacks.is_hot), # from callbacks
                    int(callbacks.synthesis_started), # from callbacks
                    int(app.state.SpeechPipelineManager.running_generation is not None), # Global manager state
                    int(app.state.SpeechPipelineManager.is_valid_gen()), # Global manager state
                    int(is_tts_finished), # Calculated local variable
                    int(app.state.AudioInputProcessor.interrupted) # Input processor state
                )

                if curr_status != prev_status:
                    status = Colors.apply("🖥️🚦 State ").red
                    logger.info(
                        f"{status} ToClient {curr_status[0]}, "
                        f"ttsClientON {curr_status[1]}, " # Renamed slightly for clarity
                        f"ChunkSent {curr_status[2]}, "
                        f"hot {curr_status[4]}, synth {curr_status[5]}"
                        f" gen {curr_status[6]}"
                        f" valid {curr_status[7]}"
                        f" tts_q_fin {curr_status[8]}"
                        f" mic_inter {curr_status[9]}"
                    )
                    prev_status = curr_status

            # Use connection-specific state via callbacks
            if not callbacks.tts_to_client:
                await asyncio.sleep(0.001)
                log_status()
                continue

            if not app.state.SpeechPipelineManager.running_generation:
                await asyncio.sleep(0.001)
                log_status()
                continue

            if app.state.SpeechPipelineManager.running_generation.abortion_started:
                await asyncio.sleep(0.001)
                log_status()
                continue

            if not app.state.SpeechPipelineManager.running_generation.audio_quick_finished:
                app.state.SpeechPipelineManager.running_generation.tts_quick_allowed_event.set()

            if not app.state.SpeechPipelineManager.running_generation.quick_answer_first_chunk_ready:
                await asyncio.sleep(0.001)
                log_status()
                continue

            chunk = None
            try:
                chunk = app.state.SpeechPipelineManager.running_generation.audio_chunks.get_nowait()
                if chunk:
                    last_quick_answer_chunk = time.time()
            except Empty:
                final_expected = app.state.SpeechPipelineManager.running_generation.quick_answer_provided
                audio_final_finished = app.state.SpeechPipelineManager.running_generation.audio_final_finished

                if not final_expected or audio_final_finished:
                    logger.info("🖥️🏁 Sending of TTS chunks and 'user request/assistant answer' cycle finished.")
                    callbacks.send_final_assistant_answer() # Callbacks method

                    assistant_answer = app.state.SpeechPipelineManager.running_generation.quick_answer + app.state.SpeechPipelineManager.running_generation.final_answer                    
                    app.state.SpeechPipelineManager.running_generation = None

                    callbacks.tts_chunk_sent = False # Reset via callbacks
                    callbacks.reset_state() # Reset connection state via callbacks

                await asyncio.sleep(0.001)
                log_status()
                continue

            base64_chunk = app.state.Upsampler.get_base64_chunk(chunk)
            message_queue.put_nowait({
                "type": "tts_chunk",
                "content": base64_chunk
            })
            last_chunk_sent = time.time()

            # Use connection-specific state via callbacks
            if not callbacks.tts_chunk_sent:
                # Use the async helper function instead of a thread
                asyncio.create_task(_reset_interrupt_flag_async(app, callbacks))

            callbacks.tts_chunk_sent = True # Set via callbacks

    except asyncio.CancelledError:
        pass # Task cancellation is expected on disconnect
    except WebSocketDisconnect as e:
        logger.warning(f"🖥️⚠️ {Colors.apply('WARNING').red} disconnect in send_tts_chunks: {repr(e)}")
    except RuntimeError as e:
        logger.error(f"🖥️💥 {Colors.apply('RUNTIME_ERROR').red} in send_tts_chunks: {repr(e)}")
    except Exception as e:
        logger.exception(f"🖥️💥 {Colors.apply('EXCEPTION').red} in send_tts_chunks: {repr(e)}")


# --------------------------------------------------------------------
# Callback class to handle transcription events
# --------------------------------------------------------------------
class TranscriptionCallbacks:
    """
    Manages state and callbacks for a single WebSocket connection's transcription lifecycle.

    This class holds connection-specific state flags (like TTS status, user interruption)
    and implements callback methods triggered by the `AudioInputProcessor` and
    `SpeechPipelineManager`. It sends messages back to the client via the provided
    `message_queue` and manages interaction logic like interruptions and final answer delivery.
    It also includes a threaded worker to handle abort checks based on partial transcription.
    """
    def __init__(self, app: FastAPI, message_queue: asyncio.Queue):
        """
        Initializes the TranscriptionCallbacks instance for a WebSocket connection.

        Args:
            app: The FastAPI application instance (to access global components).
            message_queue: An asyncio queue for sending messages back to the client.
        """
        self.app = app
        self.message_queue = message_queue
        self.final_transcription = ""
        self.abort_text = ""
        self.last_abort_text = ""

        # Initialize connection-specific state flags here
        self.tts_to_client: bool = False
        self.user_interrupted: bool = False
        self.tts_chunk_sent: bool = False
        self.tts_client_playing: bool = False
        self.interruption_time: float = 0.0

        # These were already effectively instance variables or reset logic existed
        self.silence_active: bool = True
        self.is_hot: bool = False
        self.user_finished_turn: bool = False
        self.synthesis_started: bool = False
        self.assistant_answer: str = ""
        self.final_assistant_answer: str = ""
        self.is_processing_potential: bool = False
        self.is_processing_final: bool = False
        self.last_inferred_transcription: str = ""
        self.final_assistant_answer_sent: bool = False
        self.partial_transcription: str = "" # Added for clarity

        self.reset_state() # Call reset to ensure consistency

        self.abort_request_event = threading.Event()
        self.abort_worker_thread = threading.Thread(target=self._abort_worker, name="AbortWorker", daemon=True)
        self.abort_worker_thread.start()


    def reset_state(self):
        """Resets connection-specific state flags and variables to their initial values."""
        # Reset all connection-specific state flags
        self.tts_to_client = False
        self.user_interrupted = False
        self.tts_chunk_sent = False
        # Don't reset tts_client_playing here, it reflects client state reports
        self.interruption_time = 0.0

        # Reset other state variables
        self.silence_active = True
        self.is_hot = False
        self.user_finished_turn = False
        self.synthesis_started = False
        self.assistant_answer = ""
        self.final_assistant_answer = ""
        self.is_processing_potential = False
        self.is_processing_final = False
        self.last_inferred_transcription = ""
        self.final_assistant_answer_sent = False
        self.partial_transcription = ""

        # Keep the abort call related to the audio processor/pipeline manager
        self.app.state.AudioInputProcessor.abort_generation()


    def _abort_worker(self):
        """Background thread worker to check for abort conditions based on partial text."""
        while True:
            was_set = self.abort_request_event.wait(timeout=0.1) # Check every 100ms
            if was_set:
                self.abort_request_event.clear()
                # Only trigger abort check if the text actually changed
                if self.last_abort_text != self.abort_text:
                    self.last_abort_text = self.abort_text
                    logger.debug(f"🖥️🧠 Abort check triggered by partial: '{self.abort_text}'")
                    self.app.state.SpeechPipelineManager.check_abort(self.abort_text, False, "on_partial")

    def on_partial(self, txt: str):
        """
        Callback invoked when a partial transcription result is available.

        Updates internal state, sends the partial result to the client,
        and signals the abort worker thread to check for potential interruptions.

        Args:
            txt: The partial transcription text.
        """
        self.final_assistant_answer_sent = False # New user speech invalidates previous final answer sending state
        self.final_transcription = "" # Clear final transcription as this is partial
        self.partial_transcription = txt
        self.message_queue.put_nowait({"type": "partial_user_request", "content": txt})
        self.abort_text = txt # Update text used for abort check
        self.abort_request_event.set() # Signal the abort worker

    def safe_abort_running_syntheses(self, reason: str):
        """Placeholder for safely aborting syntheses (currently does nothing)."""
        # TODO: Implement actual abort logic if needed, potentially interacting with SpeechPipelineManager
        pass

    def on_tts_allowed_to_synthesize(self):
        """Callback invoked when the system determines TTS synthesis can proceed."""
        # Access global manager state
        if self.app.state.SpeechPipelineManager.running_generation and not self.app.state.SpeechPipelineManager.running_generation.abortion_started:
            logger.info(f"{Colors.apply('🖥️🔊 TTS ALLOWED').blue}")
            self.app.state.SpeechPipelineManager.running_generation.tts_quick_allowed_event.set()

    def on_potential_sentence(self, txt: str):
        """
        Callback invoked when a potentially complete sentence is detected by the STT.

        Triggers the preparation of a speech generation based on this potential sentence.

        Args:
            txt: The potential sentence text.
        """
        logger.debug(f"🖥️🧠 Potential sentence: '{txt}'")
        # Access global manager state
        self.app.state.SpeechPipelineManager.prepare_generation(txt)

    def on_potential_final(self, txt: str):
        """
        Callback invoked when a potential *final* transcription is detected (hot state).

        Logs the potential final transcription.

        Args:
            txt: The potential final transcription text.
        """
        logger.info(f"{Colors.apply('🖥️🧠 HOT: ').magenta}{txt}")

    def on_potential_abort(self):
        """Callback invoked if the STT detects a potential need to abort based on user speech."""
        # Placeholder: Currently logs nothing, could trigger abort logic.
        pass

    def on_before_final(self, audio: bytes, txt: str):
        """
        Callback invoked just before the final STT result for a user turn is confirmed.

        Sets flags indicating user finished, allows TTS if pending, interrupts microphone input,
        releases TTS stream to client, sends final user request and any pending partial
        assistant answer to the client, and adds user request to history.

        Args:
            audio: The raw audio bytes corresponding to the final transcription. (Currently unused)
            txt: The transcription text (might be slightly refined in on_final).
        """
        logger.info(Colors.apply('🖥️🏁 =================== USER TURN END ===================').light_gray)
        self.user_finished_turn = True
        self.user_interrupted = False # Reset connection-specific flag (user finished, not interrupted)
        # Access global manager state
        if self.app.state.SpeechPipelineManager.is_valid_gen():
            logger.info(f"{Colors.apply('🖥️🔊 TTS ALLOWED (before final)').blue}")
            self.app.state.SpeechPipelineManager.running_generation.tts_quick_allowed_event.set()

        # first block further incoming audio (Audio processor's state)
        if not self.app.state.AudioInputProcessor.interrupted:
            logger.info(f"{Colors.apply('🖥️🎙️ ⏸️ Microphone interrupted (end of turn)').cyan}")
            self.app.state.AudioInputProcessor.interrupted = True
            self.interruption_time = time.time() # Set connection-specific flag

        logger.info(f"{Colors.apply('🖥️🔊 TTS STREAM RELEASED').blue}")
        self.tts_to_client = True # Set connection-specific flag

        # Send final user request (using the reliable final_transcription OR current partial if final isn't set yet)
        user_request_content = self.final_transcription if self.final_transcription else self.partial_transcription
        self.message_queue.put_nowait({
            "type": "final_user_request",
            "content": user_request_content
        })

        # Access global manager state
        if self.app.state.SpeechPipelineManager.is_valid_gen():
            # Send partial assistant answer (if available) to the client
            # Use connection-specific user_interrupted flag
            if self.app.state.SpeechPipelineManager.running_generation.quick_answer and not self.user_interrupted:
                self.assistant_answer = self.app.state.SpeechPipelineManager.running_generation.quick_answer
                self.message_queue.put_nowait({
                    "type": "partial_assistant_answer",
                    "content": self.assistant_answer
                })

        logger.info(f"🖥️🧠 Adding user request to history: '{user_request_content}'")
        # Access global manager state
        self.app.state.SpeechPipelineManager.history.append({"role": "user", "content": user_request_content})

    def on_final(self, txt: str):
        """
        Callback invoked when the final transcription result for a user turn is available.

        Logs the final transcription and stores it.

        Args:
            txt: The final transcription text.
        """
        logger.info(f"\n{Colors.apply('🖥️✅ FINAL USER REQUEST (STT Callback): ').green}{txt}")
        if not self.final_transcription: # Store it if not already set by on_before_final logic
             self.final_transcription = txt

    def abort_generations(self, reason: str):
        """
        Triggers the abortion of any ongoing speech generation process.

        Logs the reason and calls the SpeechPipelineManager's abort method.

        Args:
            reason: A string describing why the abortion is triggered.
        """
        logger.info(f"{Colors.apply('🖥️🛑 Aborting generation:').blue} {reason}")
        # Access global manager state
        self.app.state.SpeechPipelineManager.abort_generation(reason=f"server.py abort_generations: {reason}")

    def on_silence_active(self, silence_active: bool):
        """
        Callback invoked when the silence detection state changes.

        Updates the internal silence_active flag.

        Args:
            silence_active: True if silence is currently detected, False otherwise.
        """
        # logger.debug(f"🖥️🎙️ Silence active: {silence_active}") # Optional: Can be noisy
        self.silence_active = silence_active

    def on_partial_assistant_text(self, txt: str):
        """
        Callback invoked when a partial text result from the assistant (LLM) is available.

        Updates the internal assistant answer state and sends the partial answer to the client,
        unless the user has interrupted.

        Args:
            txt: The partial assistant text.
        """
        logger.info(f"{Colors.apply('🖥️💬 PARTIAL ASSISTANT ANSWER: ').green}{txt}")
        # Use connection-specific user_interrupted flag
        if not self.user_interrupted:
            self.assistant_answer = txt
            # Use connection-specific tts_to_client flag
            if self.tts_to_client:
                self.message_queue.put_nowait({
                    "type": "partial_assistant_answer",
                    "content": txt
                })

    def on_recording_start(self):
        """
        Callback invoked when the audio input processor starts recording user speech.

        If client-side TTS is playing, it triggers an interruption: stops server-side
        TTS streaming, sends stop/interruption messages to the client, aborts ongoing
        generation, sends any final assistant answer generated so far, and resets relevant state.
        """
        logger.info(f"{Colors.ORANGE}🖥️🎙️ Recording started.{Colors.RESET} TTS Client Playing: {self.tts_client_playing}")
        # Use connection-specific tts_client_playing flag
        if self.tts_client_playing:
            self.tts_to_client = False # Stop server sending TTS
            self.user_interrupted = True # Mark connection as user interrupted
            logger.info(f"{Colors.apply('🖥️❗ INTERRUPTING TTS due to recording start').blue}")

            # Send final assistant answer *if* one was generated and not sent
            logger.info(Colors.apply("🖥️✅ Sending final assistant answer (forced on interruption)").pink)
            self.send_final_assistant_answer(forced=True)

            # Minimal reset for interruption:
            self.tts_chunk_sent = False # Reset chunk sending flag
            # self.assistant_answer = "" # Optional: Clear partial answer if needed

            logger.info("🖥️🛑 Sending stop_tts to client.")
            self.message_queue.put_nowait({
                "type": "stop_tts", # Client handles this to mute/ignore
                "content": ""
            })

            logger.info(f"{Colors.apply('🖥️🛑 RECORDING START ABORTING GENERATION').red}")
            self.abort_generations("on_recording_start, user interrupts, TTS Playing")

            logger.info("🖥️❗ Sending tts_interruption to client.")
            self.message_queue.put_nowait({ # Tell client to stop playback and clear buffer
                "type": "tts_interruption",
                "content": ""
            })

            # Reset state *after* performing actions based on the old state
            # Be careful what exactly needs reset vs persists (like tts_client_playing)
            # self.reset_state() # Might clear too much, like user_interrupted prematurely

    def send_final_assistant_answer(self, forced=False):
        """
        Sends the final (or best available) assistant answer to the client.

        Constructs the full answer from quick and final parts if available.
        If `forced` and no full answer exists, uses the last partial answer.
        Cleans the text and sends it as 'final_assistant_answer' if not already sent.

        Args:
            forced: If True, attempts to send the last partial answer if no complete
                    final answer is available. Defaults to False.
        """
        final_answer = ""
        # Access global manager state
        if self.app.state.SpeechPipelineManager.is_valid_gen():
            final_answer = self.app.state.SpeechPipelineManager.running_generation.quick_answer + self.app.state.SpeechPipelineManager.running_generation.final_answer

        if not final_answer: # Check if constructed answer is empty
            # If forced, try using the last known partial answer from this connection
            if forced and self.assistant_answer:
                 final_answer = self.assistant_answer
                 logger.warning(f"🖥️⚠️ Using partial answer as final (forced): '{final_answer}'")
            else:
                logger.warning(f"🖥️⚠️ Final assistant answer was empty, not sending.")
                return# Nothing to send

        logger.debug(f"🖥️✅ Attempting to send final answer: '{final_answer}' (Sent previously: {self.final_assistant_answer_sent})")

        if not self.final_assistant_answer_sent and final_answer:
            import re
            # Clean up the final answer text
            cleaned_answer = re.sub(r'[\r\n]+', ' ', final_answer)
            cleaned_answer = re.sub(r'\s+', ' ', cleaned_answer).strip()
            cleaned_answer = cleaned_answer.replace('\\n', ' ')
            cleaned_answer = re.sub(r'\s+', ' ', cleaned_answer).strip()

            if cleaned_answer: # Ensure it's not empty after cleaning
                logger.info(f"\n{Colors.apply('🖥️✅ FINAL ASSISTANT ANSWER (Sending): ').green}{cleaned_answer}")
                self.message_queue.put_nowait({
                    "type": "final_assistant_answer",
                    "content": cleaned_answer
                })
                app.state.SpeechPipelineManager.history.append({"role": "assistant", "content": cleaned_answer})
                self.final_assistant_answer_sent = True
                self.final_assistant_answer = cleaned_answer # Store the sent answer
            else:
                logger.warning(f"🖥️⚠️ {Colors.YELLOW}Final assistant answer was empty after cleaning.{Colors.RESET}")
                self.final_assistant_answer_sent = False # Don't mark as sent
                self.final_assistant_answer = "" # Clear the stored answer
        elif forced and not final_answer: # Should not happen due to earlier check, but safety
             logger.warning(f"🖥️⚠️ {Colors.YELLOW}Forced send of final assistant answer, but it was empty.{Colors.RESET}")
             self.final_assistant_answer = "" # Clear the stored answer


# --------------------------------------------------------------------
# Main WebSocket endpoint
# --------------------------------------------------------------------
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    app_state = app.state # Shortcut to app.state for convenience
    """
    Handles the main WebSocket connection for real-time voice chat.

    Accepts a connection, sets up connection-specific state via `TranscriptionCallbacks`,
    initializes audio/message queues, and creates asyncio tasks for handling
    incoming data, audio processing, outgoing text messages, and outgoing TTS chunks.
    Manages the lifecycle of these tasks and cleans up on disconnect.

    Args:
        ws: The WebSocket connection instance provided by FastAPI.
    """
    await ws.accept()
    logger.info("🖥️✅ Client connected via WebSocket.")

    message_queue = asyncio.Queue()
    audio_chunks = asyncio.Queue()

    # Set up callback manager - THIS NOW HOLDS THE CONNECTION-SPECIFIC STATE
    callbacks = TranscriptionCallbacks(app, message_queue)

    # Assign callbacks to the AudioInputProcessor (global component)
    # These methods within callbacks will now operate on its *instance* state
    app.state.AudioInputProcessor.realtime_callback = callbacks.on_partial
    app.state.AudioInputProcessor.transcriber.potential_sentence_end = callbacks.on_potential_sentence
    app.state.AudioInputProcessor.transcriber.on_tts_allowed_to_synthesize = callbacks.on_tts_allowed_to_synthesize
    app.state.AudioInputProcessor.transcriber.potential_full_transcription_callback = callbacks.on_potential_final
    app.state.AudioInputProcessor.transcriber.potential_full_transcription_abort_callback = callbacks.on_potential_abort
    app.state.AudioInputProcessor.transcriber.full_transcription_callback = callbacks.on_final
    app.state.AudioInputProcessor.transcriber.before_final_sentence = callbacks.on_before_final
    app.state.AudioInputProcessor.recording_start_callback = callbacks.on_recording_start
    app.state.AudioInputProcessor.silence_active_callback = callbacks.on_silence_active

    # Assign callback to the SpeechPipelineManager (global component)
    app_state.SpeechPipelineManager.on_partial_assistant_text = callbacks.on_partial_assistant_text

    # --- SIP Integration: Manage active WebSocket for SIP ---
    # For now, the latest connecting WebSocket client will handle SIP interactions.
    # This is a simplification; a robust app might have dedicated SIP handling clients or UIs.
    app_state.sip_ws_app_state.active_websocket_for_sip = ws
    app_state.sip_ws_app_state.active_websocket_message_queue = client_message_queue
    logger.info(f"🖥️📞 WebSocket {ws.client.host}:{ws.client.port} is now the active handler for SIP events.")

    bm = app_state.BaresipManagerInstance if hasattr(app_state, 'BaresipManagerInstance') else None
    ab = app_state.AudioBridgeInstance if hasattr(app_state, 'AudioBridgeInstance') else None

    if bm:
        async def handle_incoming_sip_call(call_id: str, remote_uri: str):
            logger.info(f"🖥️📞 Relaying incoming SIP call {call_id} from {remote_uri} to active WebSocket client.")
            if app_state.sip_ws_app_state.active_websocket_message_queue:
                await app_state.sip_ws_app_state.active_websocket_message_queue.put({
                    "type": "sip_event",
                    "event": "incoming_call",
                    "call_id": call_id,
                    "remote_uri": remote_uri
                })
        bm.on_incoming_call_callback = handle_incoming_sip_call

        async def handle_sip_call_established(call_id: str):
            logger.info(f"🖥️📞 Relaying SIP call established {call_id} to active WebSocket client.")
            if app_state.sip_ws_app_state.active_websocket_message_queue:
                await app_state.sip_ws_app_state.active_websocket_message_queue.put({
                    "type": "sip_event",
                    "event": "call_established",
                    "call_id": call_id
                })
            if ab: # Start AudioBridge if a call is established
                if not (ab.reader_thread and ab.reader_thread.is_alive()): # Check if already running
                    logger.info(f"🎧🌉 AudioBridge starting for SIP call {call_id}.")
                    ab.start()
                else:
                    logger.info(f"🎧🌉 AudioBridge already running, not restarting for call {call_id}.")

        bm.on_call_established_callback = handle_sip_call_established

        async def handle_sip_call_closed(call_id: str):
            logger.info(f"🖥️📞 Relaying SIP call closed {call_id} to active WebSocket client.")
            if app_state.sip_ws_app_state.active_websocket_message_queue:
                await app_state.sip_ws_app_state.active_websocket_message_queue.put({
                    "type": "sip_event",
                    "event": "call_closed",
                    "call_id": call_id
                })
            # Stop AudioBridge if this was the only call (simplified logic)
            if ab and bm and not bm.active_sip_calls: # Check if BaresipManager has any other active calls
                 if ab.reader_thread and ab.reader_thread.is_alive(): # Check if running
                    logger.info(f"🎧🌉 AudioBridge stopping as SIP call {call_id} closed and no other calls active.")
                    ab.stop()
            elif ab and bm and bm.active_sip_calls:
                logger.info(f"🎧🌉 AudioBridge not stopping as other SIP calls ({len(bm.active_sip_calls)}) are still active.")

        bm.on_call_closed_callback = handle_sip_call_closed

    # --- Task to send SIP audio (from Baresip via AudioBridge) to this WebSocket client ---
    async def send_sip_audio_to_websocket_task():
        if not (hasattr(app_state, 'sip_audio_to_ws_queue') and app_state.sip_audio_to_ws_queue):
            logger.warning("send_sip_audio_to_websocket_task: sip_audio_to_ws_queue not found.")
            return

        sip_q_to_ws = app_state.sip_audio_to_ws_queue
        active_sip_calls_exist = lambda: bm and bm.active_sip_calls

        try:
            while True:
                if not active_sip_calls_exist() or not (ab and ab.reader_thread and ab.reader_thread.is_alive()):
                    await asyncio.sleep(0.1) # Sleep if no active SIP calls or AudioBridge not running
                    continue

                try:
                    pcm_chunk = await asyncio.wait_for(sip_q_to_ws.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue # Check conditions again

                if pcm_chunk is None: # Sentinel not expected here, but good practice
                    break

                # Transcode from SIP audio format (L16/16kHz) to WebSocket client format
                transcoded_for_ws = resample_audio_placeholder(
                    pcm_chunk,
                    input_rate=SIP_AUDIO_SAMPLE_RATE,
                    output_rate=WS_CLIENT_SAMPLE_RATE, # Client might prefer a different rate
                    num_channels=SIP_AUDIO_CHANNELS,   # Baresip FIFOs are mono
                    sample_width=2                     # L16 is 16-bit (2 bytes)
                )

                # Assuming client expects Base64 encoded PCM for audio chunks
                base64_chunk = base64.b64encode(transcoded_for_ws).decode('utf-8')
                # Use the specific client_message_queue for this WebSocket connection
                await client_message_queue.put({
                    "type": "sip_audio_chunk",
                    "content": base64_chunk
                })
                sip_q_to_ws.task_done()
        except asyncio.CancelledError:
            logger.info("🖥️🔊 SIP audio to WebSocket task cancelled.")
        except Exception as e:
            logger.error(f"🖥️💥 Error in SIP audio to WebSocket task: {e}", exc_info=True)
        finally:
            logger.info("🖥️🔊 SIP audio to WebSocket task finished.")

    # --- Wrapped process_incoming_data to handle SIP commands and route audio ---
    async def process_incoming_ws_data_wrapper(
            websocket: WebSocket,
            # app_fastapi: FastAPI, # app_state is used directly
            audio_q_for_stt: asyncio.Queue,
            cb_stt: TranscriptionCallbacks):

        sip_audio_q_to_baresip = app_state.sip_audio_from_ws_queue if hasattr(app_state, 'sip_audio_from_ws_queue') else None

        try:
            while True:
                msg = await websocket.receive()
                # Check if this WebSocket is currently handling an active SIP call
                # This relies on bm.active_sip_calls reflecting the state of calls Baresip is handling.
                is_sip_call_active_for_this_ws = bm and bm.active_sip_calls and \
                                                 (app_state.sip_ws_app_state.active_websocket_for_sip == websocket)


                if "bytes" in msg and msg["bytes"]:
                    raw_audio_from_ws = msg["bytes"]
                    # Assuming standard 8-byte header for all incoming audio for now
                    if len(raw_audio_from_ws) < 8:
                        logger.warning("🖥️⚠️ Received packet too short for 8‑byte header.")
                        continue

                    client_timestamp_ms, flags = struct.unpack("!II", raw_audio_from_ws[:8])
                    pcm_payload = raw_audio_from_ws[8:]

                    if is_sip_call_active_for_this_ws and sip_audio_q_to_baresip and \
                       (ab and ab.writer_thread and ab.writer_thread.is_alive()):

                        # Transcode from WebSocket client format to SIP format (L16/16kHz)
                        transcoded_for_sip = resample_audio_placeholder(
                            pcm_payload,
                            input_rate=WS_CLIENT_SAMPLE_RATE, # Assuming client sends at this rate
                            output_rate=SIP_AUDIO_SAMPLE_RATE,
                            num_channels=WS_CLIENT_CHANNELS, # Assuming client sends mono
                            sample_width=WS_CLIENT_SAMPLE_WIDTH # Assuming 16-bit
                        )
                        await sip_audio_q_to_baresip.put(transcoded_for_sip)
                    else:
                        # Not in an active SIP call for this WS, or SIP components not ready,
                        # so route to existing STT pipeline.
                        # Reconstruct metadata for STT pipeline.
                        metadata_for_stt = {
                            "client_sent_ms": client_timestamp_ms,
                            "client_sent": client_timestamp_ms * 1_000_000,
                            "isTTSPlaying": bool(flags & 1), # Example flag
                            "pcm": pcm_payload,
                            "server_received": time.time_ns(),
                            # Add other fields expected by AudioInputProcessor.process_chunk_queue
                        }
                        metadata_for_stt["client_sent_formatted"] = format_timestamp_ns(metadata_for_stt["client_sent"])
                        metadata_for_stt["server_received_formatted"] = format_timestamp_ns(metadata_for_stt["server_received"])

                        current_qsize = audio_q_for_stt.qsize()
                        if current_qsize < MAX_AUDIO_QUEUE_SIZE:
                            await audio_q_for_stt.put(metadata_for_stt)
                        else:
                            logger.warning(
                                f"🖥️⚠️ STT Audio queue full ({current_qsize}/{MAX_AUDIO_QUEUE_SIZE}); dropping chunk."
                            )

                elif "text" in msg and msg["text"]:
                    data = parse_json_message(msg["text"])
                    msg_type = data.get("type")
                    logger.info(Colors.apply(f"🖥️📥 ←←Client (WS Text): {data}").orange)

                    if bm: # Ensure BaresipManager is available
                        if msg_type == "answer_sip_call":
                            call_id_to_answer = data.get("call_id") # Client should provide this from 'sip_event'
                            logger.info(f"🖥️📞 WS Client requests to answer SIP call: {call_id_to_answer if call_id_to_answer else 'any'}")
                            await bm.accept_call(call_id_to_answer)
                        elif msg_type == "hangup_sip_call":
                            call_id_to_hangup = data.get("call_id")
                            logger.info(f"🖥️📞 WS Client requests to hangup SIP call: {call_id_to_hangup if call_id_to_hangup else 'current'}")
                            await bm.hangup_call(call_id_to_hangup)
                        # --- Pass to existing STT/TTS related text message handler ---
                        elif msg_type == "tts_start": cb_stt.tts_client_playing = True
                        elif msg_type == "tts_stop": cb_stt.tts_client_playing = False
                        elif msg_type == "clear_history": app_state.SpeechPipelineManager.reset()
                        elif msg_type == "set_speed":
                            speed_value = data.get("speed", 0)
                            speed_factor = speed_value / 100.0
                            turn_detection = app_state.AudioInputProcessor.transcriber.turn_detection
                            if turn_detection:
                                turn_detection.update_settings(speed_factor)
                                logger.info(f"🖥️⚙️ Updated turn detection settings to factor: {speed_factor:.2f}")
                        else:
                            logger.warning(f"Unhandled text message type from client: {msg_type}")
                    else:
                         logger.warning(f"BaresipManager not available, ignoring SIP command: {msg_type}")


        except asyncio.CancelledError:
            logger.info(f"process_incoming_ws_data_wrapper for {websocket.client} cancelled.")
        except WebSocketDisconnect as e:
            logger.warning(f"🖥️⚠️ WS disconnect for {websocket.client} in process_incoming_ws_data_wrapper: {repr(e)}")
        except Exception as e:
            logger.exception(f"🖥️💥 EXCEPTION in process_incoming_ws_data_wrapper for {websocket.client}: {repr(e)}")


    # Create tasks for handling different responsibilities
    # Pass the 'callbacks' instance to tasks that need connection-specific state
    all_tasks = [
        # Wrapped incoming data processor
        asyncio.create_task(process_incoming_ws_data_wrapper(ws, audio_chunks, callbacks)),
        # Existing tasks for STT/TTS pipeline
        asyncio.create_task(app_state.AudioInputProcessor.process_chunk_queue(audio_chunks)),
        asyncio.create_task(send_text_messages(ws, client_message_queue)), # client_message_queue now gets SIP events too
        asyncio.create_task(send_tts_chunks(app_state, client_message_queue, callbacks)),
        # New task for sending SIP audio (from Baresip) to this client
        asyncio.create_task(send_sip_audio_to_websocket_task()),
    ]

    try:
        # Wait for any task to complete (e.g., client disconnect)
        done, pending = await asyncio.wait(all_tasks, return_when=asyncio.FIRST_COMPLETED)
        for task_to_cancel in pending: # Corrected variable name
            if not task_to_cancel.done():
                task_to_cancel.cancel()
        # Await cancelled tasks to let them clean up if needed
        if pending: # Only gather if there are pending tasks
            await asyncio.gather(*pending, return_exceptions=True)
    except Exception as e:
        logger.error(f"🖥️💥 {Colors.apply('ERROR').red} in WebSocket session main task loop: {repr(e)}")
    finally:
        logger.info(f"🖥️🧹 Cleaning up WebSocket tasks for client {ws.client}...")
        for task_to_clean in all_tasks: # Corrected variable name
            if not task_to_clean.done():
                task_to_clean.cancel()
        # Ensure all tasks are awaited after cancellation
        await asyncio.gather(*all_tasks, return_exceptions=True)

        # --- SIP Integration: Cleanup for this WebSocket client ---
        if app_state.sip_ws_app_state.active_websocket_for_sip == ws:
            logger.info(f"🖥️📞 WebSocket client {ws.client} (was SIP handler) disconnected. Clearing SIP handler.")
            app_state.sip_ws_app_state.active_websocket_for_sip = None
            app_state.sip_ws_app_state.active_websocket_message_queue = None
            # Clear BaresipManager callbacks if they were set for this specific client's context
            # This is important if callbacks capture 'ws' or 'client_message_queue'
            if bm:
                # A more robust way would be to use functools.partial or classes for callbacks
                # to avoid direct comparison of function objects if they are dynamically created.
                # For now, assuming they are the ones set above.
                if bm.on_incoming_call_callback == handle_incoming_sip_call: bm.on_incoming_call_callback = None
                if bm.on_call_established_callback == handle_sip_call_established: bm.on_call_established_callback = None
                if bm.on_call_closed_callback == handle_sip_call_closed: bm.on_call_closed_callback = None

        logger.info(f"🖥️❌ WebSocket session ended for client {ws.client}.")

# --------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------
if __name__ == "__main__":

    # Run the server without SSL
    if not USE_SSL:
        logger.info("🖥️▶️ Starting server without SSL.")
        uvicorn.run("server:app", host="0.0.0.0", port=8000, log_config=None)

    else:
        logger.info("🖥️🔒 Attempting to start server with SSL.")
        # Check if cert files exist
        cert_file = "127.0.0.1+1.pem"
        key_file = "127.0.0.1+1-key.pem"
        if not os.path.exists(cert_file) or not os.path.exists(key_file):
             logger.error(f"🖥️💥 SSL cert file ({cert_file}) or key file ({key_file}) not found.")
             logger.error("🖥️💥 Please generate them using mkcert:")
             logger.error("🖥️💥   choco install mkcert") # Assuming Windows based on earlier check, adjust if needed
             logger.error("🖥️💥   mkcert -install")
             logger.error("🖥️💥   mkcert 127.0.0.1 YOUR_LOCAL_IP") # Remind user to replace with actual IP if needed
             logger.error("🖥️💥 Exiting.")
             sys.exit(1)

        # Run the server with SSL
        logger.info(f"🖥️▶️ Starting server with SSL (cert: {cert_file}, key: {key_file}).")
        uvicorn.run(
            "server:app",
            host="0.0.0.0",
            port=8000,
            log_config=None,
            ssl_certfile=cert_file,
            ssl_keyfile=key_file,
        )
