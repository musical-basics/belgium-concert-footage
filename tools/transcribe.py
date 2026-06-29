#!/usr/bin/env python3
"""
Transcribe the concert audio bed (Back Camera) into a time-coded transcript.

Runs as 10 independent chunk passes so a single long whisper process can never
hang the whole job: each chunk is cut with ffmpeg, transcribed by a separate
`whisper` CLI invocation, and written to its own JSON. Re-running skips chunks
that are already done (resumable). When all chunks exist they are merged, with
per-chunk time offsets applied, into cache/transcript.json which the editor
server exposes at /api/transcript.

Usage:
    python3 tools/transcribe.py            # default: back.mp4, base.en, 10 chunks
    python3 tools/transcribe.py --merge    # only re-merge existing chunk JSONs
"""
import argparse
import glob
import json
import os
import re
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROXY = os.path.join(ROOT, "proxies", "back.mp4")
WORK = os.path.join(ROOT, "cache", "transcribe")
AUDIO = os.path.join(WORK, "audio.wav")
OUT = os.path.join(ROOT, "cache", "transcript.json")

WHISPER = os.path.expanduser("~/.local/bin/whisper")
N_CHUNKS = 10


def run(cmd, **kw):
    print("  $", " ".join(cmd), flush=True)
    return subprocess.run(cmd, check=True, **kw)


def probe_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return float(out)


def extract_audio(src):
    if os.path.isfile(AUDIO):
        print(f"[audio] reuse {AUDIO}", flush=True)
        return
    print(f"[audio] extracting 16k mono wav from {os.path.basename(src)} …", flush=True)
    run(["ffmpeg", "-v", "error", "-y", "-i", src,
         "-ac", "1", "-ar", "16000", "-vn", AUDIO])


def transcribe_chunk(i, start, length, model, device, language):
    chunk_wav = os.path.join(WORK, f"chunk{i:02d}.wav")
    chunk_json = os.path.join(WORK, f"chunk{i:02d}.json")
    if os.path.isfile(chunk_json):
        print(f"[{i+1}/{N_CHUNKS}] already done -> {os.path.basename(chunk_json)}", flush=True)
        return
    print(f"[{i+1}/{N_CHUNKS}] cut {start:.0f}s +{length:.0f}s", flush=True)
    run(["ffmpeg", "-v", "error", "-y", "-ss", str(start), "-t", str(length),
         "-i", AUDIO, "-ac", "1", "-ar", "16000", chunk_wav])
    print(f"[{i+1}/{N_CHUNKS}] whisper ({model}) …", flush=True)
    cmd = [WHISPER, chunk_wav, "--model", model, "--device", device,
           "--task", "transcribe", "--fp16", "False",
           "--condition_on_previous_text", "False",
           "--output_format", "json", "--output_dir", WORK, "--verbose", "False"]
    if language:
        cmd += ["--language", language]
    run(cmd)
    os.remove(chunk_wav)  # reclaim space; JSON is the durable artifact
    print(f"[{i+1}/{N_CHUNKS}] done", flush=True)


# This is a piano concert: most of the audio is music, where Whisper emits
# hallucinated filler ("You", ".", "Thank you", bracketed sound tags). Keep only
# segments that look like genuine speech, using Whisper's own confidence fields.
FILLERS = {
    "you", "thank you", "thank you.", "thanks for watching",
    "thank you for watching", "thanks for watching.", "bye", "bye.",
    "so", "so.", "okay", "okay.", "ok", "uh", "um", "mm", "mm-hmm",
    "you.", "the", ".", "..", "...",
}


def _is_speech(seg):
    text = (seg.get("text") or "").strip()
    norm = text.lower().strip()
    # strip surrounding quotes Whisper sometimes adds
    norm = norm.strip('"“”')
    if not norm:
        return False
    if seg.get("no_speech_prob", 0) > 0.6:        # model says: not speech
        return False
    if seg.get("avg_logprob", 0) < -1.2:          # low-confidence garbage
        return False
    # nothing but punctuation/whitespace?
    if not re.sub(r"[^\w]", "", norm):
        return False
    # bracketed/parenthesised sound annotation only, e.g. [Pomp and Circumstance]
    if re.fullmatch(r"[\[\(\"].*[\]\)\"]", text.strip()):
        return False
    if norm in FILLERS:
        return False
    return True


def merge(chunk_len, duration, model):
    segments = []
    for path in sorted(glob.glob(os.path.join(WORK, "chunk*.json"))):
        i = int(os.path.basename(path)[5:7])
        offset = i * chunk_len
        with open(path) as f:
            data = json.load(f)
        for seg in data.get("segments", []):
            if not _is_speech(seg):
                continue
            text = (seg.get("text") or "").strip()
            start = round(seg["start"] + offset, 3)
            end = round(seg["end"] + offset, 3)
            if end > duration:
                end = round(duration, 3)
            if end <= start:
                continue
            segments.append({"start": start, "end": end, "text": text})
    segments.sort(key=lambda s: s["start"])
    out = {"model": model, "duration": round(duration, 3),
           "count": len(segments), "segments": segments}
    tmp = OUT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    os.replace(tmp, OUT)
    print(f"[merge] {len(segments)} segments -> {OUT}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=PROXY)
    ap.add_argument("--model", default="base.en")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--language", default="en")
    ap.add_argument("--chunks", type=int, default=N_CHUNKS)
    ap.add_argument("--merge", action="store_true", help="only re-merge existing chunks")
    args = ap.parse_args()

    os.makedirs(WORK, exist_ok=True)
    duration = probe_duration(args.src)
    chunk_len = duration / args.chunks

    if args.merge:
        merge(chunk_len, duration, args.model)
        return

    extract_audio(args.src)
    for i in range(args.chunks):
        start = i * chunk_len
        length = min(chunk_len + 1.0, duration - start)  # +1s overlap guard
        if length <= 0:
            break
        transcribe_chunk(i, start, length, args.model, args.device, args.language)
        merge(chunk_len, duration, args.model)  # refresh after each part -> live progress

    print("[ok] transcription complete", flush=True)


if __name__ == "__main__":
    main()
