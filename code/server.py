# server.py
import logging
from logsetup import setup_logging
setup_logging(logging.INFO) # Configure logging early
logger = logging.getLogger(__name__)

# Standard library imports
from datetime import datetime
from colors import Colors
import time
import threading
import sys
import os
from typing import Any
from queue import Queue, Empty

# Attempt to import AudioInputProcessor; set to None if unavailable
try:
    from audio_in import AudioInputProcessor
except ImportError:
    logger.warning("AudioInputProcessor could not be imported. Voice processing features will be disabled.")
    AudioInputProcessor = None

# Attempt to import SpeechPipelineManager; set to None if unavailable
try:
    from speech_pipeline_manager import SpeechPipelineManager
except ImportError:
    logger.warning("SpeechPipelineManager could not be imported. TTS features will be disabled.")
    SpeechPipelineManager = None

# pyVoIP specific imports
from pyVoIP.VoIP import VoIPPhone, CallState

# --- Global Configuration Variables ---
TTS_START_ENGINE = os.environ.get("TTS_START_ENGINE", "coqui")
LLM_START_PROVIDER = os.environ.get("LLM_START_PROVIDER", "ollama")
LLM_START_MODEL = os.environ.get("LLM_START_MODEL", "dummy-model")
NO_THINK = os.environ.get("NO_THINK", "False").lower() == 'true'
LANGUAGE = "en"

# --- SIP Configuration ---
SIP_SERVER_HOST = os.environ.get('SIP_SERVER_HOST', '127.0.0.1')
SIP_SERVER_PORT = int(os.environ.get('SIP_SERVER_PORT', 5060))
SIP_USERNAME = os.environ.get('SIP_USERNAME', 'pyvoipuser')
SIP_PASSWORD = os.environ.get('SIP_PASSWORD', 'pyvoippass')
SIP_MY_IP = os.environ.get('SIP_MY_IP')
SIP_MY_PORT = int(os.environ.get('SIP_MY_PORT', 5060))

class PyVoIPClient:
    def __init__(self, server, port, username, password, local_ip=None, local_port=5060):
        self.active_call = None
        self.server = server
        self.port = port
        self.username = username
        self.password = password
        self.local_ip = local_ip
        self.local_port = local_port
        self.phone: VoIPPhone = None
        self._stop_event = threading.Event() # For main loop

        self.is_assistant_speaking = False
        self.stop_tts_event = threading.Event() # For interrupting TTS

        self.phone = VoIPPhone(
            server=self.server,
            port=self.port,
            username=self.username,
            password=self.password
        )

        self.phone.on_incoming_call = self._handle_incoming_call
        self.phone.on_call_established = self._handle_call_established
        self.phone.on_call_terminated = self._handle_call_terminated
        self.phone.on_sip_registration_failure = self._handle_sip_registration_failure
        self.phone.on_call_failed = self._handle_call_failed

        logger.info(f"PyVoIPClient core initialized for {username}@{server}:{port}.")

        # Initialize AudioInputProcessor
        self.audio_input_processor = None
        try:
            if AudioInputProcessor:
                self.audio_input_processor = AudioInputProcessor(
                    language=LANGUAGE,
                    pipeline_latency=0.5
                )
                logger.info('AudioInputProcessor initialized.')
                self.audio_input_processor.realtime_callback = self._handle_partial_transcription
                if hasattr(self.audio_input_processor, 'transcriber') and self.audio_input_processor.transcriber:
                    self.audio_input_processor.transcriber.full_transcription_callback = self._handle_final_transcription
                else:
                    logger.warning('AudioInputProcessor.transcriber not found for full_transcription_callback.')
                # Connect AIP's recording_start_callback
                if hasattr(self.audio_input_processor, 'recording_start_callback'):
                     self.audio_input_processor.recording_start_callback = self._handle_user_starts_speaking
                     logger.info("Connected AIP's recording_start_callback.")
                else:
                    logger.warning("AIP or recording_start_callback not available on AIP.")
            else:
                logger.warning('AudioInputProcessor class not available; instance not created.')
        except Exception as e:
            logger.exception('Failed to initialize AudioInputProcessor')
            self.audio_input_processor = None

        # Initialize SpeechPipelineManager
        self.speech_pipeline_manager = None
        self.tts_playback_thread = None
        try:
            if SpeechPipelineManager:
                self.speech_pipeline_manager = SpeechPipelineManager(
                    tts_engine=TTS_START_ENGINE,
                    llm_provider=LLM_START_PROVIDER,
                    llm_model=LLM_START_MODEL,
                    no_think=NO_THINK
                )
                logger.info('SpeechPipelineManager initialized.')
                # Connect SPM's callback for when LLM has response ready for TTS
                if hasattr(self.speech_pipeline_manager, 'on_assistant_response_ready'):
                     self.speech_pipeline_manager.on_assistant_response_ready = self.speak_assistant_response
                     logger.info("Connected SPM's on_assistant_response_ready to speak_assistant_response.")
                else:
                    logger.warning("SPM.on_assistant_response_ready callback not found on SPM object.")
                if hasattr(self.speech_pipeline_manager, 'on_partial_assistant_text'):
                    self.speech_pipeline_manager.on_partial_assistant_text = self._handle_partial_assistant_text
                    logger.info("Connected SPM's on_partial_assistant_text.")
                else:
                    logger.warning("SPM.on_partial_assistant_text callback not found on SPM object.")

            else:
                logger.warning('SpeechPipelineManager class not available; instance not created.')
        except Exception as e:
            logger.exception('Failed to initialize SpeechPipelineManager. TTS output will be disabled.')
            self.speech_pipeline_manager = None

    def _handle_incoming_call(self, call):
        logger.info(f"Incoming call (ID: {call.id}) from {call.remote_uri}, Headers: {call.headers}")
        logger.info(f"Auto-rejecting call ID {call.id} for this basic test.")
        try:
            call.hangup()
        except Exception as e:
            logger.error(f"Error trying to hang up incoming call {call.id}: {e}")

    def _handle_call_established(self, call):
        logger.info(f'Call {call.id} established with {call.remote_uri}')
        self.active_call = call
        self.stop_tts_event.clear() # Clear any previous stop event
        self.is_assistant_speaking = False
        logger.info('Attempting to start audio reading thread.')
        self._start_audio_reading_thread(call)

    def _handle_call_terminated(self, call): # Modified
        logger.info(f"Call terminated (ID: {call.id}) with {call.remote_uri}. Reason: {getattr(call, 'reason', 'Unknown')}")
        if self.is_assistant_speaking:
            logger.info('Call terminated during TTS. Setting stop event for TTS thread.')
            self.stop_tts_event.set()
        if self.speech_pipeline_manager:
            logger.info('Aborting any active SpeechPipelineManager generation due to call termination.')
            self.speech_pipeline_manager.abort_generation(reason='Call terminated')
        if self.active_call and self.active_call.id == call.id:
            logger.info(f'Clearing active call {call.id}.')
            self.active_call = None

    def _handle_sip_registration_failure(self, call, response_code: int, response_text: str):
        logger.error(f"SIP registration failed. Response: {response_code} {response_text}. Call context: {call}")

    def _handle_call_failed(self, call, reason: str = "Unknown", response_code: int = 0):
        logger.error(f"Call failed (ID: {call.id}) to {call.remote_uri}. Reason: {reason}, Code: {response_code}")
        if self.active_call and self.active_call.id == call.id:
            self.active_call = None

    def start(self):
        logger.info("Starting pyVoIP phone...")
        try:
            self.phone.start()
            logger.info("pyVoIP phone started. Waiting for events or calls.")
        except Exception as e:
            logger.exception(f"Exception during pyVoIP phone start: {e}")
            raise

    def stop(self): # Modified
        logger.info("Shutting down PyVoIPClient...")
        self.stop_tts_event.set() # Signal any running TTS/audio threads to stop
        if self.tts_playback_thread and self.tts_playback_thread.is_alive():
            logger.info("Waiting for TTS playback thread to complete...")
            self.tts_playback_thread.join(timeout=1.0)

        if self.active_call: # Try to gracefully end active call if any
            logger.info(f"Hanging up active call {self.active_call.id} during shutdown.")
            try: self.active_call.hangup()
            except Exception as e: logger.error(f"Error hanging up active call: {e}")
            self.active_call = None

        if self.phone:
            logger.info("Stopping pyVoIP phone instance...")
            self.phone.stop()

        if self.speech_pipeline_manager and hasattr(self.speech_pipeline_manager, 'shutdown'):
            logger.info("Shutting down SpeechPipelineManager...")
            self.speech_pipeline_manager.shutdown()

        self._stop_event.set()
        logger.info("PyVoIPClient stop sequence completed.")

    def make_call(self, destination_extension: str):
        # ... (make_call method remains largely the same as previous correct version) ...
        if not self.phone or not self.phone.is_running:
            logger.error("VoIPPhone not started. Cannot make call.")
            return None
        logger.info(f"Attempting to make outbound call to extension: {destination_extension} on server {self.server}")
        try:
            active_call = self.phone.call(extension=destination_extension)
            if active_call:
                logger.info(f"Call initiated to {destination_extension}. Call object: {active_call}")
                return active_call
            else:
                logger.error(f"Failed to initiate call to {destination_extension}. Method returned None.")
                return None
        except Exception as e:
            logger.exception(f"Exception when trying to initiate call to {destination_extension}: {e}")
            return None

    # --- AudioInputProcessor Callbacks ---
    def _handle_partial_transcription(self, text):
        logger.info(f'Partial Transcription: {text}')
        # Potentially pass to SPM for very early processing or barge-in detection refinement

    def _handle_final_transcription(self, text): # Replaced
        logger.info(f'Refactored Final Transcription Received: {text}')
        if self.is_assistant_speaking:
            logger.info('User spoke while assistant was speaking. Aborting current TTS.')
            self.stop_tts_event.set()
            if self.speech_pipeline_manager:
                self.speech_pipeline_manager.abort_generation(reason='User interruption')
            # time.sleep(0.1) # Consider if delay is needed here

        if text and self.speech_pipeline_manager:
            logger.info(f'Passing final user transcription to SpeechPipelineManager: "{text}"')
            # SPM will do LLM processing and then call `on_assistant_response_ready`
            # (which should be connected to self.speak_assistant_response)
            self.speech_pipeline_manager.prepare_generation(text)
        elif not text:
            logger.info('Final transcription was empty, nothing to send to SPM.')
        elif not self.speech_pipeline_manager:
            logger.warning('SpeechPipelineManager not available, cannot process final transcription.')

    # --- pyVoIP Media Handling ---
    def _handle_media_received(self, call, audio_data, sample_rate, channels):
        # ... (remains largely the same as previous correct version) ...
        if not self.audio_input_processor:
            logger.warning('AudioInputProcessor not available, skipping audio frame.')
            return
        metadata = { 'client_sent_ms': int(time.time() * 1000), 'isTTSPlaying': False }
        try:
            self.audio_input_processor.feed_audio(audio_data, metadata)
        except Exception as e:
            logger.exception(f'Error feeding audio to AudioInputProcessor: {e}')

    def _start_audio_reading_thread(self, call_obj):
        # ... (remains largely the same as previous correct version) ...
        if not hasattr(call_obj, 'read') or not callable(call_obj.read):
            logger.error("Call object does not have a callable 'read' method. Cannot read audio.")
            return
        def audio_read_loop(active_call):
            logger.info(f"Starting audio reading loop for call ID: {active_call.id}")
            try:
                while active_call and active_call.state == CallState.ESTABLISHED and not self._stop_event.is_set():
                    audio_frame_bytes = active_call.read(160)
                    if audio_frame_bytes:
                        self._handle_media_received(active_call, audio_frame_bytes, 8000, 1)
                    elif active_call.state != CallState.ESTABLISHED:
                        logger.info(f"Call {active_call.id} no longer established. Stopping audio read loop.")
                        break
                    else: time.sleep(0.01)
            except Exception as e:
                if "Call not established" not in str(e) and "Call closed" not in str(e):
                     logger.exception(f"Exception in audio_read_loop for call {active_call.id}: {e}")
            finally: logger.info(f"Audio reading loop ended for call ID: {active_call.id}")
        audio_thread = threading.Thread(target=audio_read_loop, args=(call_obj,))
        audio_thread.daemon = True; audio_thread.start()
        logger.info(f"Audio reading thread started for call ID: {call_obj.id}")

    # --- TTS Output Methods ---
    def _play_tts_audio(self, call_to_speak_on): # Modified
        logger.info(f'TTS playback thread started for call {call_to_speak_on.id if call_to_speak_on else None}.')
        if not self.speech_pipeline_manager or not hasattr(self.speech_pipeline_manager, 'running_generation') or not self.speech_pipeline_manager.running_generation:
            logger.warning('SPM or running_generation not available for TTS playback in thread.')
            self.is_assistant_speaking = False
            return

        audio_queue = self.speech_pipeline_manager.running_generation.audio_chunks
        try:
            while not self.stop_tts_event.is_set():
                if not (call_to_speak_on and call_to_speak_on.state == CallState.ESTABLISHED):
                    logger.warning(f'Call {call_to_speak_on.id if call_to_speak_on else "N/A"} ended or not established. Stopping TTS playback.')
                    break
                try:
                    chunk = audio_queue.get(block=True, timeout=0.1)
                    if chunk is None:
                        logger.info('TTS audio queue returned None (end of stream signal).')
                        break
                    call_to_speak_on.write_audio(chunk)
                except Empty:
                    if hasattr(self.speech_pipeline_manager.running_generation, 'audio_final_finished') and \
                       self.speech_pipeline_manager.running_generation.audio_final_finished:
                        logger.info('TTS queue empty and SPM generation marked as finished.')
                        break
                    continue
                except Exception as e:
                    logger.exception(f"Error in TTS playback loop for call {call_to_speak_on.id}: {e}")
                    break
            if self.stop_tts_event.is_set():
                logger.info(f"TTS playback for call {call_to_speak_on.id} actively stopped by event.")
        finally:
            self.is_assistant_speaking = False
            if self.stop_tts_event.is_set():
                logger.info("Clearing SPM audio queue due to TTS interruption.")
                while not audio_queue.empty():
                    try: audio_queue.get_nowait()
                    except Empty: break
            logger.info(f'TTS playback thread for call {call_to_speak_on.id if call_to_speak_on else None} finished.')

    def speak_assistant_response(self, text_to_speak): # Modified (called by SPM via callback)
        if not self.speech_pipeline_manager:
            logger.error(f'speak_assistant_response called but SPM is None. Cannot speak: {text_to_speak}')
            return
        if not self.active_call or self.active_call.state != CallState.ESTABLISHED:
            logger.warning(f'No active established call. Cannot speak: {text_to_speak}')
            return

        logger.info(f'SPM provided text to speak: "{text_to_speak[:100]}..." on call {self.active_call.id}')

        if self.tts_playback_thread and self.tts_playback_thread.is_alive():
            logger.info('New TTS request while previous might be finishing. Setting stop event for old thread.')
            self.stop_tts_event.set()
            self.tts_playback_thread.join(timeout=0.5)

        self.is_assistant_speaking = True
        self.stop_tts_event.clear()

        # Text is from SPM, audio for it should be in SPM's queue (from its internal TTS).
        logger.info(f'Starting TTS playback thread for assistant response.')
        self.tts_playback_thread = threading.Thread(target=self._play_tts_audio, args=(self.active_call,))
        self.tts_playback_thread.daemon = True
        self.tts_playback_thread.start()

    # --- New callback methods for SPM/AIP integration ---
    def _handle_partial_assistant_text(self, text):
        logger.info(f'Partial Assistant Text (from SPM): {text}')

    def _handle_user_starts_speaking(self):
        logger.info('User started speaking (VAD triggered).')
        if self.is_assistant_speaking:
            logger.info('User interruption: Assistant was speaking. Signaling TTS to stop.')
            self.stop_tts_event.set()
            if self.speech_pipeline_manager:
                self.speech_pipeline_manager.abort_generation(reason='User interruption')


# --- Main Application Logic (Finalized by Subtask) ---

if __name__ == "__main__":
    # Logger is assumed to be configured at the top of server.py
    # import time # Already imported globally in server.py usually

    log_info = logger.info
    log_exception = logger.exception

    log_info("Finalized Main: Starting SIP Softphone Application...")

    # These SIP configurations are expected to be global in server.py
    # SIP_SERVER_HOST, SIP_SERVER_PORT, SIP_USERNAME, SIP_PASSWORD, SIP_MY_IP, SIP_MY_PORT

    client = None
    try:
        # PyVoIPClient class should be defined above in server.py
        client = PyVoIPClient(
            server=SIP_SERVER_HOST,
            port=SIP_SERVER_PORT,
            username=SIP_USERNAME,
            password=SIP_PASSWORD,
            local_ip=SIP_MY_IP, # PyVoIPClient handles None for this
            local_port=SIP_MY_PORT
        )
        client.start()
        log_info("Finalized Main: PyVoIPClient started. Application is running. Press Ctrl+C to exit.")

        # Keep main thread alive, allowing client threads to run.
        # client.stop() (and thus client._stop_event.set()) will be called by finally.
        while not client._stop_event.is_set():
            time.sleep(0.5) # Check stop event periodically

    except KeyboardInterrupt:
        log_info("Finalized Main: KeyboardInterrupt received. Shutting down application...")
    except NameError as e:
        log_exception(f"Finalized Main: A required name (e.g., PyVoIPClient or SIP config) is not defined: {e}")
    except ImportError as e:
        log_exception(f"Finalized Main: An import error occurred: {e}")
    except Exception as e:
        log_exception(f"Finalized Main: An unexpected error occurred in the main application loop: {e}")
    finally:
        if client and hasattr(client, 'stop'): # Ensure client exists and has stop method
            log_info("Finalized Main: Calling PyVoIPClient.stop() for graceful shutdown.")
            client.stop()
        else:
            log_info("Finalized Main: Client was not initialized or lacks stop method, no explicit stop needed from here.")
        log_info("Finalized Main: Application terminated.")
