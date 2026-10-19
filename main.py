import asyncio
import io
import logging
import os
import re
import tempfile
import wave
from collections import defaultdict, deque
from datetime import datetime

import numpy as np
import soundfile as sf
import uvicorn
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect
from faster_whisper import WhisperModel
from google import genai
from google.genai import types
from markdown import markdown
from piper import PiperVoice
from pydantic import BaseModel, Field
from pytz import timezone

from robot_commands import (
    MAX_DURATION_MS,
    RobotCommand,
    RobotCommandError,
    RobotCommandQueue,
    command_from_dict,
    confirmation_for,
    parse_robot_command,
)

logging.basicConfig(level=logging.INFO)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

STT_MODEL_SIZE = "base"
TTS_MODEL = "tts_models/en_US-libritts_r-medium.onnx"
LLM_MODEL = "gemini-2.5-flash-lite"
WEBSOCKET_HOST = "0.0.0.0"
WEBSOCKET_PORT = int(os.getenv("PORT", "7860"))
CHUNK_SIZE = 4096
SAMPLE_RATE = 16000
MAX_HISTORY = 10
TIMEZONE = "Asia/Kolkata"
PROMPT = "You are a helpful voice assistant. Keep answers concise."

print("Loading models...")
tts_voice = PiperVoice.load(f"{os.getcwd()}/{TTS_MODEL}")
stt_model = WhisperModel(STT_MODEL_SIZE, device="cpu", compute_type="int8")
llm_client = genai.Client()
print("Models loaded successfully!")

app = FastAPI(title="ESP32 Voice Assistant")
clients: set[WebSocket] = set()
audio_buffers: dict[int, io.BytesIO] = {}
conversation_histories = defaultdict(lambda: deque(maxlen=MAX_HISTORY))
robot_commands = RobotCommandQueue()


class RobotCommandRequest(BaseModel):
    action: str
    speed: int = Field(default=160, ge=0, le=255)
    duration_ms: int = Field(..., ge=0, le=MAX_DURATION_MS)


def sanitize_text(input_text: str) -> str:
    html = markdown(input_text)
    text = BeautifulSoup(html, features="html.parser").get_text()
    emoji = re.compile(
        "["
        "\U0001F600-\U0001F64F\U0001F300-\U0001F6FF\U0001F1E0-\U0001F1FF"
        "\U00010000-\U0010ffff\u2600-\u2B55\u200d\ufe0f"
        "]+",
        re.UNICODE,
    )
    return re.sub(emoji, "", text).strip()


def convert_to_text(file_path: str) -> tuple[str, str, float]:
    segments, info = stt_model.transcribe(file_path, beam_size=5)
    return " ".join(segment.text for segment in segments), info.language, info.language_probability


def generate_response(query: str, client_id: int) -> str:
    history = conversation_histories[client_id]
    contents = []
    for user_msg, assistant_msg in history:
        contents.extend(
            [
                types.Content(role="user", parts=[types.Part(text=user_msg)]),
                types.Content(role="model", parts=[types.Part(text=assistant_msg)]),
            ]
        )
    contents.append(types.Content(role="user", parts=[types.Part(text=query)]))
    try:
        response = llm_client.models.generate_content(
            model=LLM_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(system_instruction=PROMPT),
        )
        response_text = sanitize_text(response.text)
    except Exception:
        logging.exception("Gemini response generation failed")
        response_text = "I'm sorry, but I can't answer right now."
    history.append((query, response_text))
    return response_text


def convert_to_speech(text: str, output_file: str) -> None:
    with wave.open(output_file, "wb") as wav_file:
        tts_voice.synthesize_wav(text, wav_file)


async def stream_audio(websocket: WebSocket, file_path: str) -> None:
    message, samplerate = sf.read(file_path, dtype="float32")
    if samplerate != SAMPLE_RATE:
        x_old = np.linspace(0, len(message), len(message))
        x_new = np.linspace(0, len(message), int(len(message) * SAMPLE_RATE / samplerate))
        message = np.interp(x_new, x_old, message)
    if len(message.shape) > 1:
        message = np.mean(message, axis=1)
    message = (np.clip(message, -1, 1) * 127 + 128).astype(np.uint8)
    for index in range(0, len(message), CHUNK_SIZE):
        await websocket.send_bytes(message[index:index + CHUNK_SIZE].tobytes())
        await asyncio.sleep(CHUNK_SIZE / SAMPLE_RATE)
    await websocket.send_text("Done playing")


async def process_and_stream_audio(websocket: WebSocket, input_filename: str, client_id: int) -> None:
    temp_output_path = None
    try:
        await websocket.send_text("Converting to speech to text")
        speech_text, _, _ = convert_to_text(input_filename)
        if not speech_text.strip():
            await websocket.send_text("No speech detected")
            return

        robot_command = parse_robot_command(speech_text)
        if robot_command is not None:
            robot_commands.put(robot_command)
            bot_speech_text = confirmation_for(robot_command)
        elif "what is the time" in speech_text.lower() or "what's the time" in speech_text.lower():
            bot_speech_text = f"It's {datetime.now(timezone(TIMEZONE)).strftime('%I:%M %p')} right now."
        elif "what is the date" in speech_text.lower() or "what's the date" in speech_text.lower():
            bot_speech_text = f"It's {datetime.now(timezone(TIMEZONE)).strftime('%B %d, %Y')} today."
        elif "clear conversation" in speech_text.lower() or "clear history" in speech_text.lower():
            conversation_histories[client_id].clear()
            bot_speech_text = "Conversation history cleared."
        else:
            await websocket.send_text("Generating LLM response")
            bot_speech_text = generate_response(speech_text, client_id)

        await websocket.send_text("Converting to speech")
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_output:
            temp_output_path = temp_output.name
        convert_to_speech(bot_speech_text, temp_output_path)
        await websocket.send_text("Streaming audio")
        await stream_audio(websocket, temp_output_path)
    finally:
        if temp_output_path and os.path.exists(temp_output_path):
            os.unlink(temp_output_path)
        if os.path.exists(input_filename):
            os.unlink(input_filename)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/simulate-robot-command", status_code=201)
async def simulate_robot_command(request: RobotCommandRequest) -> dict[str, str | int]:
    try:
        command = command_from_dict(request.model_dump())
    except RobotCommandError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    robot_commands.put(command)
    return command.as_dict()


@app.get("/robot-command", response_model=None)
async def get_robot_command() -> Response | dict[str, str | int]:
    command = robot_commands.get()
    if command is None:
        return Response(status_code=204)
    return command.as_dict()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    client_id = id(websocket)
    clients.add(websocket)
    audio_buffers[client_id] = io.BytesIO()
    try:
        while True:
            message = await websocket.receive()
            if message.get("bytes") is not None:
                audio_buffers[client_id].write(message["bytes"])
                continue
            text = message.get("text")
            if text == "ping":
                await websocket.send_text("pong")
            elif text == "pause":
                await websocket.send_text("Recording paused")
            elif text == "clear_history":
                conversation_histories[client_id].clear()
                await websocket.send_text("Conversation history cleared")
            elif text == "stop":
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_input:
                    input_path = temp_input.name
                with wave.open(input_path, "wb") as wav_file:
                    wav_file.setnchannels(1)
                    wav_file.setsampwidth(2)
                    wav_file.setframerate(SAMPLE_RATE)
                    wav_file.writeframes(audio_buffers[client_id].getvalue())
                audio_buffers[client_id] = io.BytesIO()
                await websocket.send_text("Processing your audio")
                await process_and_stream_audio(websocket, input_path, client_id)
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(websocket)
        audio_buffers.pop(client_id, None)
        conversation_histories.pop(client_id, None)


if __name__ == "__main__":
    uvicorn.run(app, host=WEBSOCKET_HOST, port=WEBSOCKET_PORT)
