#!/usr/bin/env python3
"""voice_assistant.py — 食品知识库语音助手

小型企业工程化实现：键盘/语音输入 → 调用 REST API 问答/溯源 → 语音朗读答案。

- 语音输出：edge-tts（微软免费 TTS，中文可用）
- 语音输入：可选（需装 whisper/faster-whisper + 录音）；未装则键盘文本输入
- 架构：薄客户端，业务全在 REST API 层（api_server.py），可单独部署

用法:
  python voice_assistant.py                      # 交互式（键盘输入，语音朗读）
  python voice_assistant.py "乳制品的数量"        # 单次提问
  python voice_assistant.py --api http://host:8000   # 指定后端

依赖:
  pip install edge-tts requests
  可选: pip install faster-whisper sounddevice numpy  (语音输入)
"""
import os
import sys
import asyncio
import argparse
import tempfile
import urllib.request
import json

# STT 本地模型目录(避免从 HuggingFace 下载被墙; 用 curl 从 hf-mirror 预置)
WHISPER_MODEL_DIR = os.path.join(os.path.expanduser("~"), "whisper-tiny")


def ask_api(question, base_url="http://localhost:8000"):
    """调用食品知识库 REST API 问答。"""
    req = urllib.request.Request(
        f"{base_url}/api/ask",
        data=json.dumps({"question": question}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read().decode())
            return d.get("answer", ""), d.get("mode", "")
    except Exception as e:
        return f"[API 调用失败] {e}", ""


def speak_tts(text, voice="zh-CN-XiaoxiaoNeural"):
    """edge-tts 朗读。返回是否成功。"""
    import edge_tts
    try:
        out = os.path.join(tempfile.gettempdir(), "food_voice.mp3")
        async def _run():
            tts = edge_tts.Communicate(text, voice)
            await tts.save(out)
        asyncio.run(_run())
        if os.path.exists(out):
            # 播放（Windows 用 start / 或 mpg123；无则跳过）
            os.system(f'start "" "{out}"' if os.name == "nt" else f'mpg123 "{out}" >/dev/null 2>&1 &')
            return True
    except Exception as e:
        print(f"[TTS 失败] {e}")
    return False


def record_and_transcribe(timeout=6, lang="zh"):
    """可选语音输入：麦克风录音 → STT。需 faster-whisper/sounddevice，未装则返回空。"""
    try:
        import numpy as np
        import sounddevice as sd
        from faster_whisper import WhisperModel
    except ImportError:
        print("[语音输入] 未装 faster-whisper/sounddevice，改用键盘输入（pip install faster-whisper sounddevice numpy）")
        return None
    try:
        print(f"🎤 请说话（{timeout}秒）…")
        sr = 16000
        audio = sd.rec(int(sr * timeout), samplerate=sr, channels=1, dtype="int16")
        sd.wait()
        model = WhisperModel(WHISPER_MODEL_DIR, device="cpu", compute_type="int8")
        segments, _ = model.transcribe(audio.flatten(), language=lang)
        return "".join(s.text for s in segments).strip() or None
    except Exception as e:
        print(f"[STT 失败] {e}")
        return None


def main():
    ap = argparse.ArgumentParser(description="食品知识库语音助手")
    ap.add_argument("question", nargs="?", help="单次提问（省略则进入交互模式）")
    ap.add_argument("--api", default="http://localhost:8000", help="REST API 地址")
    ap.add_argument("--voice", action="store_true", help="用语音输入(需装STT)")
    args = ap.parse_args()

    print("🍽️ 食品知识库语音助手 (edge-tts 中文朗读)")
    print(f"   后端: {args.api}\n")

    if args.question:
        ans, mode = ask_api(args.question, args.api)
        print(f"[{mode}] {ans}")
        speak_tts(ans)
        return

    while True:
        try:
            q = input("\n问 (输入 exit 退出): ").strip()
        except (KeyboardInterrupt, EOFError):
            break
        if not q:
            continue
        if q.lower() in ("exit", "quit", "退出"):
            break
        if args.voice:
            q = record_and_transcribe() or q
            print(f"  识别: {q}")
        ans, mode = ask_api(q, args.api)
        print(f"[{mode}] {ans}")
        speak_tts(ans)


if __name__ == "__main__":
    main()
