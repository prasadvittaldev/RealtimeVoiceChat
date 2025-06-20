import pjsua2 as pj
import os
import time
from gtts import gTTS
import threading
import asyncio
import websockets
import struct
import base64
import queue # Add this
import json # Add this if not already present
import wave # Add this

# --- Environment variable loading remains the same ---
SIP_DOMAIN = os.environ.get("SIP_DOMAIN")
SIP_USER = os.environ.get("SIP_USER")
SIP_PASSWD = os.environ.get("SIP_PASSWD")
SIP_BIND_PORT = int(os.environ.get("SIP_BIND_PORT", "5062"))
WEBSOCKET_URL = os.environ.get("WEBSOCKET_URL", "ws://localhost:8000/ws") # New variable


# FIX: Create a custom AudioMediaPlayer to handle the end-of-file event
class MyAudioMediaPlayer(pj.AudioMediaPlayer):
    def __init__(self, playback_done_event):
        super(MyAudioMediaPlayer, self).__init__()
        self.playback_done_event = playback_done_event

    def onEof(self):
        # This callback is executed by the pjsua thread when the file is done.
        print(f"Player has reached EOF.", flush=True)
        # Signal the waiting thread that playback is complete.
        self.playback_done_event.set()


class MyCall(pj.Call):
    def __init__(self, acc, call_id=pj.PJSUA_INVALID_ID):
        super(MyCall, self).__init__(acc, call_id)
        self.player = None # Will be used for playing audio received from WebSocket
        self.account = acc
        self.ep = pj.Endpoint.instance()
        self.ws_client = None # To store the WebSocket client instance
        self.ws_thread = None # To store the thread running the WebSocket communication
        self.stop_ws_event = threading.Event() # Event to signal WebSocket thread to stop
        self.audio_frame_queue = queue.Queue(maxsize=50) # For outgoing audio (SIP to WS)
        self.playback_frame_queue = queue.Queue(maxsize=50) # For incoming audio (WS to SIP)
        self.audio_collector_media = None # Instance of AudioDataCollector
        self.audio_provider_media = None # Instance of AudioFrameProvider
        self.call_media_port = None # To store the call's audio media port

    def onCallMediaState(self, prm):
        ci = self.getInfo()
        for i, mi in enumerate(ci.media):
            if mi.type == pj.PJMEDIA_TYPE_AUDIO and mi.status == pj.PJSUA_CALL_MEDIA_ACTIVE:
                print(f"*** Call {self.getId()}: Media is ACTIVE. Starting WebSocket communication thread.", flush=True)
                self.call_media_port = self.getMedia(i) # pj.AudioMedia object from the call

                # Initialize and connect AudioDataCollector (SIP Audio -> WS)
                self.audio_collector_media = AudioDataCollector(self.audio_frame_queue, clock_rate=16000, channel_count=1, samples_per_frame=320)
                self.ep.audDevManager().addAudioMedia(self.audio_collector_media)
                self.call_media_port.startTransmit(self.audio_collector_media)

                # Initialize and connect AudioFrameProvider (WS Audio -> SIP)
                # Assuming 16kHz, 1ch, 20ms frames (320 samples) to match collector and expected TTS format
                self.audio_provider_media = AudioFrameProvider(self.playback_frame_queue, clock_rate=16000, channel_count=1, samples_per_frame=320)
                self.ep.audDevManager().addAudioMedia(self.audio_provider_media)
                self.audio_provider_media.startTransmit(self.call_media_port) # Provider transmits to call

                self.stop_ws_event.clear()
                self.ws_thread = threading.Thread(target=self._websocket_communication_thread)
                self.ws_thread.daemon = True # Ensure thread exits when main program exits
                self.ws_thread.start()
                return

    # Old start_playback method is now removed and replaced by _send_initial_greeting_to_ws

    def _websocket_communication_thread(self):
        self.ep.libRegisterThread(f"ws_comm_{self.getId()}") # Register PJSIP thread
        print(f"*** Call {self.getId()}: WebSocket thread started.", flush=True)
        try:
            asyncio.run(self._async_websocket_handler())
        except Exception as e:
            print(f"*** Call {self.getId()}: Error in WebSocket thread: {e}", flush=True)
        finally:
            print(f"*** Call {self.getId()}: WebSocket thread finished.", flush=True)
            # Ensure call is hung up if WebSocket connection terminates unexpectedly
            if self.isActive():
                print(f"*** Call {self.getId()}: WebSocket ended, hanging up call.", flush=True)
                hangup_op = pj.CallOpParam(True)
                try:
                    self.hangup(hangup_op)
                except pj.Error as e:
                    print(f"*** Call {self.getId()}: Error hanging up call: {e}", flush=True)

    async def _send_audio_to_ws(self, websocket):
        self.ep.libRegisterThread(f"send_audio_ws_{self.getId()}")
        print(f"*** Call {self.getId()}: Starting to send audio to WebSocket.", flush=True)
        try:
            while not self.stop_ws_event.is_set() and self.ws_client and not self.ws_client.closed:
                try:
                    # Get audio data from the queue (blocking with timeout)
                    pcm_data = self.audio_frame_queue.get(timeout=0.01) # Timeout to allow checking stop_ws_event
                except queue.Empty:
                    await asyncio.sleep(0.005) # Sleep a bit if queue is empty
                    continue

                if pcm_data:
                    timestamp_ms = int(time.time() * 1000)
                    flags = 0 # Placeholder for flags
                    header = struct.pack("!II", timestamp_ms, flags)
                    try:
                        await websocket.send(header + pcm_data)
                    except websockets.exceptions.ConnectionClosed:
                        print(f"*** Call {self.getId()}: WebSocket closed while sending audio.", flush=True)
                        break
                    # print(f"Sent audio frame: {len(pcm_data)} bytes", flush=True) # For debugging
                await asyncio.sleep(0.015) # Roughly match 20ms frame interval, considering processing time
        except Exception as e:
            print(f"*** Call {self.getId()}: Error in _send_audio_to_ws: {e}", flush=True)
        finally:
            print(f"*** Call {self.getId()}: Stopped sending audio to WebSocket.", flush=True)

    async def _receive_audio_from_ws(self, websocket):
        self.ep.libRegisterThread(f"recv_audio_ws_{self.getId()}")
        print(f"*** Call {self.getId()}: Starting to receive audio from WebSocket.", flush=True)
        expected_frame_size = self.audio_provider_media._samples_per_frame * \
                              self.audio_provider_media._channel_count * \
                              (self.audio_provider_media._bits_per_sample // 8)
        try:
            while not self.stop_ws_event.is_set() and self.ws_client and not self.ws_client.closed:
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=0.1)
                except asyncio.TimeoutError:
                    await asyncio.sleep(0.01) # Allow checking stop_ws_event
                    continue
                except websockets.exceptions.ConnectionClosed:
                    print(f"*** Call {self.getId()}: WebSocket closed while receiving audio.", flush=True)
                    break

                if isinstance(message, str):
                    try:
                        data = json.loads(message)
                        if data.get("type") == "tts_chunk" and "content" in data:
                            base64_pcm = data["content"]
                            try:
                                pcm_data_bytes = base64.b64decode(base64_pcm)
                                # The received audio might be multiple frames or partial frames.
                                # We need to buffer and chunk it into expected_frame_size.
                                # For simplicity, this example assumes pcm_data_bytes IS one frame or multiple full frames.
                                # A proper implementation would use a buffer here.
                                for i in range(0, len(pcm_data_bytes), expected_frame_size):
                                    chunk = pcm_data_bytes[i:i+expected_frame_size]
                                    if len(chunk) == expected_frame_size:
                                        try:
                                            self.playback_frame_queue.put_nowait(chunk)
                                        except queue.Full:
                                            # print(f"*** Call {self.getId()}: Playback queue full, dropping TTS frame.", flush=True)
                                            pass # Drop if full
                                    # else:
                                        # print(f"*** Call {self.getId()}: Received TTS chunk of unexpected size {len(chunk)}, expected {expected_frame_size}. Discarding.", flush=True)

                            except base64.binascii.Error as b64e:
                                print(f"*** Call {self.getId()}: Base64 decode error for TTS chunk: {b64e}", flush=True)
                            except Exception as e:
                                print(f"*** Call {self.getId()}: Error processing TTS chunk: {e}", flush=True)
                        # Add handling for other message types if needed
                    except json.JSONDecodeError:
                        print(f"*** Call {self.getId()}: Received invalid JSON message: {message}", flush=True)
                # else: (handle binary messages if necessary)
                #    print(f"*** Call {self.getId()}: Received binary message (unexpected): {len(message)} bytes", flush=True)

        except Exception as e:
            if not isinstance(e, websockets.exceptions.ConnectionClosed): # Don't log error again if already handled
                 print(f"*** Call {self.getId()}: Error in _receive_audio_from_ws: {e}", flush=True)
        finally:
            print(f"*** Call {self.getId()}: Stopped receiving audio from WebSocket.", flush=True)

    async def _send_initial_greeting_to_ws(self, websocket):
        self.ep.libRegisterThread(f"send_greeting_ws_{self.getId()}") # Register if any PJSIP API is called
        print(f"*** Call {self.getId()}: Preparing and sending initial greeting via WebSocket...", flush=True)
        greeting_file_mp3 = None
        greeting_file_wav_8k = None
        greeting_file_wav_16k = None
        try:
            # 1. Generate greeting audio using gTTS (as before)
            tts = gTTS(text="Hello, welcome. Your call is now connected.", lang='en')
            greeting_file_base = f"/tmp/greeting_{self.getId()}" # Unique filename per call
            greeting_file_mp3 = f"{greeting_file_base}.mp3"
            greeting_file_wav_8k = f"{greeting_file_base}_8k.wav"
            greeting_file_wav_16k = f"{greeting_file_base}_16k.wav"

            tts.save(greeting_file_mp3)

            # 2. Convert MP3 to 8kHz WAV (original format)
            # Use -y to overwrite existing files
            os.system(f"ffmpeg -y -i {greeting_file_mp3} -ar 8000 -ac 1 -acodec pcm_s16le {greeting_file_wav_8k} > /dev/null 2>&1")

            # 3. Resample 8kHz WAV to 16kHz WAV for WebSocket streaming
            # This matches the AudioDataCollector's configuration (16000 Hz)
            os.system(f"ffmpeg -y -i {greeting_file_wav_8k} -ar 16000 -ac 1 -acodec pcm_s16le {greeting_file_wav_16k} > /dev/null 2>&1")

            print(f"*** Call {self.getId()}: Greeting audio (16kHz) is ready at {greeting_file_wav_16k}", flush=True)

            # 4. Read the 16kHz WAV file and send its PCM data in frames
            frame_size = self.audio_collector_media._samples_per_frame * \
                         self.audio_collector_media._channel_count * \
                         (self.audio_collector_media._bits_per_sample // 8) # Bytes per frame (e.g., 640 for 16kHz, 20ms, 1ch, 16bit)

            with open(greeting_file_wav_16k, 'rb') as wf_raw: # Keep raw open for ensuring it's available
                try:
                    with wave.open(greeting_file_wav_16k, 'rb') as w_obj:
                        if w_obj.getframerate() != 16000 or w_obj.getnchannels() != 1 or w_obj.getsampwidth() != 2:
                            print(f"*** Call {self.getId()}: ERROR - Greeting WAV file {greeting_file_wav_16k} is not 16kHz mono 16-bit PCM.", flush=True)
                            return

                        total_frames_in_file = w_obj.getnframes()
                        frames_to_read_per_iteration = self.audio_collector_media._samples_per_frame

                        while True: # Loop until break
                            if self.stop_ws_event.is_set() or not self.ws_client or self.ws_client.closed:
                                print(f"*** Call {self.getId()}: Stop event or WebSocket closed during greeting sending.", flush=True)
                                break

                            pcm_data = w_obj.readframes(frames_to_read_per_iteration)
                            if not pcm_data:
                                break # End of file

                            # If pcm_data is shorter than a full frame (e.g., end of file), pad with silence
                            if len(pcm_data) < frame_size:
                                pcm_data += b' ' * (frame_size - len(pcm_data))

                            if len(pcm_data) == frame_size: # Ensure it is exactly one frame
                                timestamp_ms = int(time.time() * 1000)
                                flags = 0 # Placeholder for flags
                                header = struct.pack("!II", timestamp_ms, flags)
                                try:
                                    await websocket.send(header + pcm_data)
                                except websockets.exceptions.ConnectionClosed:
                                    print(f"*** Call {self.getId()}: WebSocket closed while sending greeting.", flush=True)
                                    break
                                await asyncio.sleep(0.018) # Sleep slightly less than frame duration (20ms) to stream steadily
                            else:
                                print(f"*** Call {self.getId()}: Greeting audio frame size mismatch. Got {len(pcm_data)}, expected {frame_size}. Skipping.", flush=True)
                                break
                except ImportError:
                    print(f"*** Call {self.getId()}: 'wave' module not found. Cannot accurately send greeting. Skipping.", flush=True)
                except wave.Error as wave_err:
                    print(f"*** Call {self.getId()}: Wave file error for greeting ({greeting_file_wav_16k}): {wave_err}. Skipping.", flush=True)
                except FileNotFoundError:
                    print(f"*** Call {self.getId()}: Greeting WAV file {greeting_file_wav_16k} not found. Skipping.", flush=True)


            print(f"*** Call {self.getId()}: Finished sending initial greeting.", flush=True)

        except Exception as e:
            print(f"*** Call {self.getId()}: Error in _send_initial_greeting_to_ws: {e}", flush=True)
        finally:
            # Clean up temporary audio files
            for f_path in [greeting_file_mp3, greeting_file_wav_8k, greeting_file_wav_16k]:
                if f_path and os.path.exists(f_path): # Check if f_path is not None
                    try:
                        os.remove(f_path)
                    except OSError as e_os:
                        print(f"*** Call {self.getId()}: Error deleting temp file {f_path}: {e_os}", flush=True)

    async def _async_websocket_handler(self):
        uri = WEBSOCKET_URL
        print(f"*** Call {self.getId()}: Connecting to WebSocket at {uri}", flush=True)
        try:
            async with websockets.connect(uri, open_timeout=10, close_timeout=10, ping_interval=20, ping_timeout=20) as websocket:
                self.ws_client = websocket
                print(f"*** Call {self.getId()}: Connected to WebSocket.", flush=True)

                # Send the initial greeting
                await self._send_initial_greeting_to_ws(websocket) # Added call

                # If stop event was set during greeting, don't proceed
                if self.stop_ws_event.is_set():
                    print(f"*** Call {self.getId()}: Stop event set after greeting. Not starting main send/receive loops.", flush=True)
                    return

                print(f"*** Call {self.getId()}: Starting main send/receive loops.", flush=True)
                send_task = asyncio.create_task(self._send_audio_to_ws(websocket))
                receive_task = asyncio.create_task(self._receive_audio_from_ws(websocket))

                done, pending = await asyncio.wait(
                    [send_task, receive_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )

                for task in pending:
                    task.cancel()

                # Await all tasks to ensure they complete (or are cancelled)
                await asyncio.gather(send_task, receive_task, return_exceptions=True)

                print(f"*** Call {self.getId()}: WebSocket send/receive tasks finished.", flush=True)

        except websockets.exceptions.ConnectionClosed as e:
            print(f"*** Call {self.getId()}: WebSocket connection closed: {e}", flush=True)
        except Exception as e:
            print(f"*** Call {self.getId()}: Error in WebSocket handler: {e}", flush=True)
        finally:
            self.ws_client = None # Clear client first
            if not self.stop_ws_event.is_set(): # Avoid issues if already set by onCallState
                self.stop_ws_event.set() # Signal all loops/tasks to stop
            print(f"*** Call {self.getId()}: WebSocket handler finished cleanup.", flush=True)

    def onCallState(self, prm):
        ci = self.getInfo()
        print(f"*** Call {self.getId()} state is {ci.stateText}", flush=True)
        if ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
            print(f"*** Call {self.getId()} Disconnected. Reason: {ci.lastReason}", flush=True)
            self.stop_ws_event.set()
            if self.ws_thread and self.ws_thread.is_alive():
                self.ws_thread.join(timeout=5.0)

            if self.audio_collector_media:
                print(f"*** Call {self.getId()}: Removing audio collector media.", flush=True)
                if self.call_media_port:
                    self.call_media_port.stopTransmit(self.audio_collector_media)
                self.ep.audDevManager().removeAudioMedia(self.audio_collector_media)
                self.audio_collector_media = None # Set to None after cleanup

            # Clean up audio provider
            if self.audio_provider_media:
                print(f"*** Call {self.getId()}: Removing audio provider media.", flush=True)
                # To stop transmitting from provider to call_media_port:
                # Check if audio_provider_media is still transmitting to call_media_port
                # This direction is self.audio_provider_media.stopTransmit(self.call_media_port)
                # However, an AudioMedia that is a source (like AudioFrameProvider) typically doesn't store
                # who it's transmitting to. The transmission is initiated by the source.
                # So, we just remove it from audDevManager.
                self.ep.audDevManager().removeAudioMedia(self.audio_provider_media)
                self.audio_provider_media = None

            self.account.remove_call(self)
            if self.player:
                self.player = None
            if self.audio_provider_media: # Was audio_player_media
                self.audio_provider_media = None
# Custom AudioMedia to provide frames to PJSIP for playback
class AudioFrameProvider(pj.AudioMedia):
    def __init__(self, frame_queue, clock_rate=16000, channel_count=1, samples_per_frame=320, bits_per_sample=16):
        super(AudioFrameProvider, self).__init__()
        self.frame_queue = frame_queue
        self._clock_rate = clock_rate
        self._channel_count = channel_count
        self._samples_per_frame = samples_per_frame # For 16kHz, 20ms frames: 16000 * 0.020 = 320 samples
        self._bits_per_sample = bits_per_sample
        self.ep = pj.Endpoint.instance()
        # Create a silent frame for underflow
        self.silent_frame_data = b' ' * (samples_per_frame * channel_count * (bits_per_sample // 8))

    def getPortInfo(self):
        pi = pj.MediaPortInfo()
        pi.name = "AudioFrameProvider"
        pi.signature = pj.PJMEDIA_PORT_SIGNATURE_AUDIO # Correct signature for audio
        pi.clockRate = self._clock_rate
        pi.channelCount = self._channel_count
        pi.samplesPerFrame = self._samples_per_frame
        pi.bitsPerSample = self._bits_per_sample
        pi.format.id = pj.PJMEDIA_FORMAT_L16 # Linear 16-bit PCM
        return pi

    # Callback called by PJSIP when it needs an audio frame from this source
    def getFrame(self, frame):
        # This method is called by PJSIP media thread
        try:
            audio_data = self.frame_queue.get_nowait()
            if len(audio_data) == len(frame.buf): # Ensure frame sizes match
                frame.buf = audio_data
                frame.size = len(audio_data)
            else: # Frame size mismatch, provide silence
                # print(f"AudioFrameProvider: Frame size mismatch. Expected {len(frame.buf)}, got {len(audio_data)}. Providing silence.", flush=True)
                frame.buf = self.silent_frame_data
                frame.size = len(self.silent_frame_data)
        except queue.Empty:
            # Queue is empty, provide silence
            frame.buf = self.silent_frame_data
            frame.size = len(self.silent_frame_data)
        except Exception as e:
            # print(f"AudioFrameProvider: Error getting frame: {e}", flush=True)
            frame.buf = self.silent_frame_data
            frame.size = len(self.silent_frame_data)

        frame.type = pj.PJMEDIA_FRAME_TYPE_AUDIO
        return True # Must return True

# Custom AudioMedia to collect frames from PJSIP
class AudioDataCollector(pj.AudioMedia):
    def __init__(self, frame_queue, clock_rate=16000, channel_count=1, samples_per_frame=320, bits_per_sample=16):
        super(AudioDataCollector, self).__init__()
        self.frame_queue = frame_queue
        self._clock_rate = clock_rate
        self._channel_count = channel_count
        self._samples_per_frame = samples_per_frame # For 16kHz, 20ms frames: 16000 * 0.020 = 320 samples
        self._bits_per_sample = bits_per_sample
        self.ep = pj.Endpoint.instance()

    # pj.AudioMedia Port Info (required by base class)
    def getPortInfo(self):
        pi = pj.MediaPortInfo()
        pi.name = "AudioDataCollector"
        pi.signature = pj.PJMEDIA_PORT_SIGNATURE_AUDIO # Correct signature for audio
        pi.clockRate = self._clock_rate
        pi.channelCount = self._channel_count
        pi.samplesPerFrame = self._samples_per_frame
        pi.bitsPerSample = self._bits_per_sample
        pi.format.id = pj.PJMEDIA_FORMAT_L16 # Linear 16-bit PCM
        return pi

    # Callback called when an audio frame is received by this media port
    def putFrame(self, frame):
        # This method is called by PJSIP media thread
        # We should copy the data and put it into a thread-safe queue
        # The frame data is in frame.buf
        # frame.type should be pj.PJMEDIA_FRAME_TYPE_AUDIO
        if frame.type == pj.PJMEDIA_FRAME_TYPE_AUDIO:
            # Create a copy of the buffer immediately
            audio_data_copy = bytes(frame.buf)
            try:
                self.frame_queue.put_nowait(audio_data_copy)
            except queue.Full:
                # print("AudioDataCollector: Queue is full, dropping frame.", flush=True)
                pass # Or log, decide on strategy
        return True # Must return True

class MyAccount(pj.Account):
    def __init__(self):
        super(MyAccount, self).__init__()
        self.active_calls = []

    def onRegState(self, prm):
        print(f"*** Registration state: {prm.code} ({prm.reason})", flush=True)

    def onIncomingCall(self, prm):
        call = MyCall(self, call_id=prm.callId)
        op = pj.CallOpParam(True)
        op.statusCode = 200
        call.answer(op)
        print(f"*** Answering call {prm.callId}", flush=True)
        self.active_calls.append(call)

    def remove_call(self, call):
        if call in self.active_calls:
            self.active_calls.remove(call)
            print(f"Call {call.getId()} removed from active list.", flush=True)


# --- Main script execution ---
def main():
    ep = pj.Endpoint()
    ep.libCreate()
    ep_cfg = pj.EpConfig()
    ep.libInit(ep_cfg)
    ep.audDevManager().setNullDev()

    sipTpConfig = pj.TransportConfig()
    sipTpConfig.port = SIP_BIND_PORT
    ep.transportCreate(pj.PJSIP_TRANSPORT_UDP, sipTpConfig)
    ep.libStart()

    acc_cfg = pj.AccountConfig()
    acc_cfg.idUri = f"sip:{SIP_USER}@{SIP_DOMAIN}"
    acc_cfg.regConfig.registrarUri = f"sip:{SIP_DOMAIN}"
    cred = pj.AuthCredInfo("digest", "*", SIP_USER, 0, SIP_PASSWD)
    acc_cfg.sipConfig.authCreds.append(cred)

    acc = MyAccount()
    acc.create(acc_cfg)

    print("*** SIP bot is running. Waiting for incoming calls... (Press Ctrl+C to stop)", flush=True)

    try:
        while True:
            ep.libHandleEvents(20) # Check events every 20ms
            time.sleep(0.01) # Prevent busy loop, give some time back to OS
    except KeyboardInterrupt:
        print("\nExiting...", flush=True)
    finally:
        # Cleanly destroy the library
        # This will also trigger necessary cleanup for calls and account
        if ep:
            print("Shutting down PJSIP library...", flush=True)
            ep.libDestroy()
            ep = None # Clear the reference

    print("Shutdown complete.", flush=True)

if __name__ == "__main__":
    main()
