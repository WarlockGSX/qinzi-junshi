#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""亲子沟通军师 · 录音转写

把一段音频转成逐字稿，供上层的沟通分析使用。只做转写，不做任何"理解"或心理推断。

两条路，脚本自己选：

  本地（默认）   faster-whisper，全程离线。不需要账号、密钥或付费；录音不上传。
                 缺点：不区分说话人。
  腾讯云         设好密钥后自动启用，支持说话人分离（给每段标一个说话人编号）。
                 缺点：录音会上传到腾讯云，按量计费。

只要有 TENCENTCLOUD_SECRET_ID / TENCENTCLOUD_SECRET_KEY 就自动走云端；
没有就走本地。加 --provider local 可强制本地（绝不上传）。

用法：
    python transcribe.py <音频文件> [--out ./output]
    python transcribe.py <音频文件> --provider local        # 强制离线
    python transcribe.py <音频文件> --model medium          # 听不清多时换大模型
    python transcribe.py <音频文件> --dump-raw              # 存云端原始响应

输出（写到 <out>）：
    <文件名>.md       带时间戳、可读的逐字稿
    <文件名>.jsonl    结构化，每行一段

jsonl 字段：
    start / end          片段起止秒数（整段音频的绝对时间）
    text                 该段文字
    speaker              说话人编号。**只在同一 chunk 内可比**；它只是声学聚类，
                         不是角色（谁是家长、谁是孩子要靠上层向使用者确认）
    chunk                所属切片序号（未切片恒为 0）
    avg_logprob          平均对数概率（本地路径；越接近 0 越有把握）
    no_speech_prob       该段"没有人声"的概率（本地路径）
    compression_ratio    文本重复度（本地路径；偏高提示可能是重复幻觉）
    temperature          解码温度（本地路径）
    rms_dbfs / peak_dbfs 该段音量（由音频算出，不是模型输出）
    chars_per_sec        语速（字/秒，由文字与时长算出）
"""

from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import hmac
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# 国内直连 HuggingFace 常不通，默认走镜像；能直连的话自己设 HF_ENDPOINT 覆盖。
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

SUPPORTED = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".wma", ".mp4", ".amr"}

# "这段没听清"的经验阈值，作用在 avg_logprob 原值上。参考：-0.25 很稳，-1.0 一般，-1.5 以下基本不可信。
# 这是经验值，最好用真实录音校准后再固化。
LOGPROB_FLOOR = -1.0

# ---- 腾讯云录音文件识别 ----
TC_HOST = "asr.tencentcloudapi.com"
TC_SERVICE = "asr"
TC_VERSION = "2019-06-14"
# 必须与官方 SDK 逐字一致：签名只带 content-type 和 host 两个头，且 content-type 不带 charset。
TC_CONTENT_TYPE = "application/json"

TC_HARD_LIMIT = 5 * 1024 * 1024          # Data(base64) 必须 < 5MB
TC_SAFE_LIMIT = int(4.6 * 1024 * 1024)   # 留点余量

LANG_TO_ENGINE = {"zh": "16k_zh", "en": "16k_en", "yue": "16k_yue"}

# 码率阶梯：从高到低试，尽量让整段塞进一次请求（单次请求 = 说话人编号全局一致）。
BITRATE_LADDER = {"opus": [64, 48, 32, 24, 16], "mp3": [128, 96, 64, 48, 32]}
BITRATE_FLOOR = {"opus": 24, "mp3": 32}


def die(msg: str) -> None:
    print(f"\n[错误] {msg}\n", file=sys.stderr)
    sys.exit(1)


def fmt_ts(sec: float) -> str:
    m, s = int(sec // 60), int(sec % 60)
    return f"{m}:{s:02d}"


# ---------------------------------------------------------------- 音频读取
def read_audio(path, sr: int = 16000):
    """解码成 16k 单声道 float32 波形。任何一步失败就返回 (None, None)，绝不让异常冒上去。"""
    try:
        import av
        import numpy as np
    except ImportError:
        return None, None
    try:
        container = av.open(str(path))
        stream = container.streams.audio[0]
        resampler = av.audio.resampler.AudioResampler(format="fltp", layout="mono", rate=sr)
        chunks = []
        for frame in container.decode(stream):
            for rf in resampler.resample(frame):
                chunks.append(rf.to_ndarray().reshape(-1))
        try:
            for rf in resampler.resample(None):  # 冲出重采样器里的尾巴
                chunks.append(rf.to_ndarray().reshape(-1))
        except Exception:
            pass
        container.close()
        if not chunks:
            return None, None
        return np.concatenate(chunks).astype(np.float32), sr
    except Exception:
        return None, None


def dbfs(x, mode: str = "rms"):
    """把一段样本转成 dBFS；空段或全静音返回 None（不编数）。"""
    if x is None or getattr(x, "size", 0) == 0:
        return None
    try:
        import numpy as np
        v = float(np.max(np.abs(x))) if mode == "peak" else float(np.sqrt(np.mean(np.square(x))))
        return round(20 * math.log10(v), 2) if v > 1e-9 else -120.0
    except Exception:
        return None


def cut(audio, sr, start: float, end: float):
    if audio is None or sr is None:
        return None
    i, j = max(0, int(start * sr)), min(audio.size, int(end * sr))
    return audio[i:j] if j > i else None


# ---------------------------------------------------------------- 云端的音频编码
def encode_wav_block(audio, sr: int, codec: str, kbps: int, out_path: Path) -> bool:
    """把 float32 单声道波形压成 mp3 / ogg-opus 写盘。成功 True，失败 False（不抛异常）。"""
    try:
        import av
    except ImportError:
        return False
    try:
        fmt = "mp3" if codec == "mp3" else "ogg"
        container = av.open(str(out_path), mode="w", format=fmt)
        stream = container.add_stream("libmp3lame" if codec == "mp3" else "libopus", rate=sr)
        stream.bit_rate = int(kbps) * 1000
        frame = av.AudioFrame.from_ndarray(audio.reshape(1, -1).astype("float32"),
                                           format="fltp", layout="mono")
        frame.sample_rate = sr
        for pkt in stream.encode(frame):
            container.mux(pkt)
        for pkt in stream.encode(None):
            container.mux(pkt)
        container.close()
        return out_path.exists() and out_path.stat().st_size > 0
    except Exception:
        return False


def b64_size(raw: int) -> int:
    return 4 * ((raw + 2) // 3)


def plan_cloud(audio, sr: int, codec: str, probe: Path, limit: int):
    """实测决定码率与是否切片——不用码率估算（编码器给不准，估过头会把本可一次传完的录音白切碎）。

    返回 (mode, kbps, chunk_seconds, probe_bytes)：mode 为 "single" 或 "chunked"；失败返回 None。
    """
    duration = audio.size / sr
    ladder = [b for b in BITRATE_LADDER[codec] if b >= BITRATE_FLOOR[codec]]
    for br in ladder:
        if not encode_wav_block(audio, sr, codec, br, probe):
            return None
        size = probe.stat().st_size
        if b64_size(size) <= limit:
            return ("single", br, None, size)
    if not encode_wav_block(audio, sr, codec, BITRATE_FLOOR[codec], probe):
        return None
    size = probe.stat().st_size
    bytes_per_sec = size / max(duration, 1e-6)
    max_raw = int((limit - 2048) * 3 / 4)          # base64 后 ≤ limit，反推原始字节上限
    chunk_sec = max(60.0, max_raw / bytes_per_sec * 0.97)
    return ("chunked", BITRATE_FLOOR[codec], chunk_sec, size)


# ---------------------------------------------------------------- 腾讯云 TC3 签名
def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def tc3_authorization(secret_id: str, secret_key: str, host: str, action: str,
                      payload: bytes, ts: int) -> str:
    """按腾讯云官方算法拼 Authorization。参与签名的头只有 content-type 和 host。"""
    date = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d")
    headers = {"content-type": TC_CONTENT_TYPE, "host": host}
    signed = ["content-type", "host"]
    canon_headers = "".join(f"{h}:{headers[h]}\n" for h in signed)
    canonical = "\n".join([
        "POST", "/", "", canon_headers, ";".join(signed),
        hashlib.sha256(payload).hexdigest(),
    ])
    scope = f"{date}/{TC_SERVICE}/tc3_request"
    string_to_sign = "TC3-HMAC-SHA256\n%d\n%s\n%s" % (
        ts, scope, hashlib.sha256(canonical.encode()).hexdigest())
    secret_date = _hmac(("TC3" + secret_key).encode(), date)
    secret_service = _hmac(secret_date, TC_SERVICE)
    secret_signing = _hmac(secret_service, "tc3_request")
    signature = hmac.new(secret_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return (f"TC3-HMAC-SHA256 Credential={secret_id}/{scope}, "
            f"SignedHeaders={';'.join(signed)}, Signature={signature}")


class Tencent:
    def __init__(self, secret_id: str, secret_key: str, region: str):
        self.secret_id, self.secret_key, self.region = secret_id, secret_key, region

    def call(self, action: str, params: dict, timeout: int = 60) -> dict:
        payload = json.dumps(params).encode()   # ensure_ascii 默认 True，body 全 ASCII
        ts = int(time.time())
        req = urllib.request.Request("https://" + TC_HOST + "/", data=payload, method="POST")
        req.add_header("Authorization",
                       tc3_authorization(self.secret_id, self.secret_key, TC_HOST,
                                         action, payload, ts))
        req.add_header("Content-Type", TC_CONTENT_TYPE)
        req.add_header("X-TC-Action", action)
        req.add_header("X-TC-Version", TC_VERSION)
        req.add_header("X-TC-Timestamp", str(ts))
        if self.region:
            req.add_header("X-TC-Region", self.region)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code}：{e.read().decode('utf-8', 'replace')[:400]}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"网络不可达：{e.reason}")
        except Exception as e:
            raise RuntimeError(f"请求失败：{e}")
        out = body.get("Response", {})
        err = out.get("Error")
        if err:
            raise RuntimeError(f"{err.get('Code')}：{err.get('Message')}")
        return out


def pick(d: dict, *names, default=None):
    """大小写不敏感取字段——腾讯云不同版本返回的键名大小写不统一。"""
    low = {str(k).lower(): v for k, v in d.items()}
    for n in names:
        if n.lower() in low:
            return low[n.lower()]
    return default


def tc_run(data_b64: str, raw_len: int, engine: str, cfg: Tencent) -> tuple[dict, dict]:
    resp = cfg.call("CreateRecTask", {
        "EngineModelType": engine, "ChannelNum": 1,
        "SpeakerDiarization": 1,     # 开说话人分离（编号是聚类，不是角色）
        "SpeakerNumber": 0,          # 16k 引擎只能自动
        "ResTextFormat": 2,          # 句子 + 词级时间戳 + 语速 + 标点
        "SourceType": 1, "Data": data_b64, "DataLen": raw_len,
    })
    task_id = pick(resp.get("Data", {}) or {}, "TaskId")
    if not task_id:
        raise RuntimeError("提交后没拿到 TaskId")
    interval, deadline = 3.0, time.time() + 900.0
    while time.time() < deadline:
        r = cfg.call("DescribeTaskStatus", {"TaskId": str(task_id)})
        d = r.get("Data", {}) or {}
        status = pick(d, "Status")
        if status == 2:
            return d, r
        if status == 3:
            raise RuntimeError(f"识别失败：{pick(r, 'ErrorMsg') or pick(d, 'ErrorMsg') or '未知'}")
        time.sleep(interval)
        interval = min(interval * 1.4, 8.0)
    raise RuntimeError("等待识别结果超时（15 分钟）")


def rows_from_cloud(data: dict, offset: float, chunk: int, audio, sr) -> list[dict]:
    detail = pick(data, "ResultDetail") or []
    rows = []
    if isinstance(detail, list) and detail:
        for it in detail:
            text = (pick(it, "FinalSentence") or pick(it, "SliceSentence") or "").strip()
            if not text:
                continue
            st, et = pick(it, "StartTime"), pick(it, "EndTime")
            start = offset + (float(st) / 1000.0 if st is not None else 0.0)
            end = offset + (float(et) / 1000.0 if et is not None else start - offset)
            spk = pick(it, "SpeakerId")
            dur = max(end - start, 1e-6)
            seg = cut(audio, sr, start, end)
            rows.append({
                "start": round(start, 2), "end": round(end, 2), "text": text,
                "speaker": int(spk) if spk is not None else None, "chunk": chunk,
                "avg_logprob": None, "no_speech_prob": None,
                "compression_ratio": None, "temperature": None,
                "rms_dbfs": dbfs(seg, "rms"), "peak_dbfs": dbfs(seg, "peak"),
                "chars_per_sec": round(len(text) / dur, 1),
            })
        return rows
    plain = pick(data, "Result") or ""
    if isinstance(plain, str) and plain.strip():
        rows.append({
            "start": round(offset, 2), "end": round(offset, 2), "text": plain.strip(),
            "speaker": None, "chunk": chunk,
            "avg_logprob": None, "no_speech_prob": None,
            "compression_ratio": None, "temperature": None,
            "rms_dbfs": None, "peak_dbfs": None, "chars_per_sec": None,
        })
    return rows


# ---------------------------------------------------------------- 两条主流程
def run_local(args, audio_path: Path):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        die("未装 faster-whisper，本地转写不可用。先执行：\n    pip install faster-whisper\n"
            "或设好腾讯云密钥改用云端。")
    if args.model_dir:
        src = str(Path(args.model_dir).expanduser().resolve())
        if not Path(src).is_dir():
            die(f"模型目录不存在：{src}")
        print(f"[1/4] 从本地目录加载模型：{src}")
    else:
        src = args.model
        print(f"[1/4] 加载模型 {args.model}（首次运行需下载）...")
    try:
        model = WhisperModel(src, device=args.device, compute_type=args.compute)
    except Exception as e:
        die(f"模型加载失败：{e}\n\n若因镜像下载中断，可手动下载后用 --model-dir 指定本地目录。")

    print("[2/4] 读取音频（用于音量）...")
    audio, sr = read_audio(audio_path)
    print("      未读到音频，音量字段留空（不影响转写）" if audio is None
          else f"      已读取 {audio.size / sr:.1f}s")

    print(f"[3/4] 转写中：{audio_path.name}")
    try:
        segments, info = model.transcribe(str(audio_path), language=args.lang,
                                          vad_filter=True, beam_size=5,
                                          word_timestamps=args.words)
    except Exception as e:
        die(f"转写失败：{e}\n可能原因：音频损坏、编码不支持，或不是有效音频。")

    print(f"      语言：{info.language}（{info.language_probability:.2f}）｜时长：{fmt_ts(info.duration)}")
    rows, low = [], 0
    for i, seg in enumerate(segments, 1):
        text = seg.text.strip()
        if not text:
            continue
        dur = max(seg.end - seg.start, 1e-6)
        seg_audio = cut(audio, sr, seg.start, seg.end)
        rows.append({
            "start": round(seg.start, 2), "end": round(seg.end, 2), "text": text,
            "speaker": None, "chunk": 0,
            "avg_logprob": round(seg.avg_logprob, 4) if seg.avg_logprob is not None else None,
            "no_speech_prob": round(seg.no_speech_prob, 4) if seg.no_speech_prob is not None else None,
            "compression_ratio": round(seg.compression_ratio, 3) if seg.compression_ratio is not None else None,
            "temperature": seg.temperature,
            "rms_dbfs": dbfs(seg_audio, "rms"), "peak_dbfs": dbfs(seg_audio, "peak"),
            "chars_per_sec": round(len(text) / dur, 1),
        })
        if seg.avg_logprob is not None and seg.avg_logprob < LOGPROB_FLOOR:
            low += 1
        if i % 20 == 0:
            print(f"      ...已处理 {i} 段（{fmt_ts(seg.end)}）")

    meta = {"provider": "local", "duration": info.duration, "language": info.language,
            "low_conf": low, "chunked": False, "audio_read": audio is not None}
    return rows, meta


def run_cloud(args, audio_path: Path, out_dir: Path, stem: str, sid: str, skey: str, region: str):
    cfg = Tencent(sid, skey, region)
    engine = LANG_TO_ENGINE.get(args.lang, "16k_zh")
    print(f"[1/4] 云端转写：腾讯云（{region} / {engine}）")
    print("      注意：音频会上传处理；不想上传请用 --provider local。")

    print("[2/4] 读数并编码...")
    audio, sr = read_audio(audio_path)
    if audio is None:
        if b64_size(audio_path.stat().st_size) > TC_HARD_LIMIT:
            die("读不出音频（缺 PyAV），原文件又超过 5MB，无法提交。请先 pip install av。")
        print("      ⚠ 读不出音频，将按原文件提交（采样率非 16k 时识别会变差）。")
        chunks = [(0, 0.0, None, None, None)]
    else:
        duration = audio.size / sr
        probe = out_dir / f".{stem}.probe.{args.codec}"
        plan = plan_cloud(audio, sr, args.codec, probe, TC_SAFE_LIMIT)
        if plan is None:
            die(f"音频编码失败（{args.codec}）。改用 --codec mp3 重试。")
        mode, bitrate, chunk_sec, probe_bytes = plan
        try:
            probe.unlink()
        except Exception:
            pass
        print(f"      时长 {fmt_ts(duration)}；{args.codec} {bitrate}kbps 整段约 {probe_bytes/1048576:.2f}MB")
        if mode == "single":
            chunks = [(0, 0.0, None, args.codec, bitrate)]
            print("      单段提交 —— 说话人编号全局一致")
        else:
            chunks, t, idx = [], 0.0, 0
            while t < duration - 0.5:
                end = min(duration, t + chunk_sec)
                chunks.append((idx, t, end - t, args.codec, bitrate))
                t, idx = end, idx + 1
            print(f"      塞不进 5MB 单次限额，切成 {len(chunks)} 段（约 {chunk_sec/60:.1f} 分钟/段）")
            print("      每段独立做说话人分离 —— 跨段编号不可比")

    print(f"[3/4] 提交识别（{len(chunks)} 次请求）...")
    rows, raw_log, tmp = [], [], []

    def cleanup():
        for f in tmp:  # 中间编码文件里是原始录音，任何退出路径都要删
            try:
                f.unlink()
            except Exception:
                pass

    for idx, offset, dur, codec, br in chunks:
        if codec is None:
            send, raw_len = audio_path, audio_path.stat().st_size
        else:
            send = out_dir / f".{stem}.chunk{idx}.{codec}"
            seg = cut(audio, sr, offset, offset + dur)
            if seg is None or seg.size == 0:
                continue
            if not encode_wav_block(seg, sr, codec, br, send):
                cleanup()
                die(f"第 {idx+1} 段编码失败（{codec}）。改用 --codec mp3 重试。")
            tmp.append(send)
            raw_len = send.stat().st_size
        b64 = base64.b64encode(send.read_bytes()).decode("ascii")
        if len(b64) > TC_HARD_LIMIT:
            cleanup()
            die(f"第 {idx+1} 段 base64 后 {len(b64)/1048576:.2f}MB，超 5MB 限额。")
        print(f"      [{idx+1}/{len(chunks)}] {fmt_ts(offset)} 起，{len(b64)/1048576:.2f}MB ...")
        try:
            data, resp = tc_run(b64, raw_len, engine, cfg)
        except RuntimeError as e:
            cleanup()
            die(f"云端转写失败：{e}")
        if args.dump_raw:
            raw_log.append({"chunk": idx, "response": resp})
        got = rows_from_cloud(data, offset, idx, audio, sr)
        print(f"           ✓ {len(got)} 段")
        rows.extend(got)
    cleanup()

    if args.dump_raw and raw_log:
        (out_dir / f"{stem}.raw.json").write_text(
            json.dumps(raw_log, ensure_ascii=False, indent=2), encoding="utf-8")

    meta = {"provider": "tencent",
            "duration": audio.size / sr if audio is not None else None,
            "language": engine, "low_conf": 0, "chunked": len(chunks) > 1,
            "n_chunks": len(chunks), "audio_read": audio is not None}
    return rows, meta


def write_outputs(out_dir: Path, stem: str, rows: list, meta: dict) -> None:
    with (out_dir / f"{stem}.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    cloud = meta["provider"] == "tencent"
    with (out_dir / f"{stem}.md").open("w", encoding="utf-8") as f:
        f.write(f"# 逐字稿：{stem}\n\n")
        if meta.get("duration"):
            f.write(f"- 音频时长：{fmt_ts(meta['duration'])}\n")
        f.write(f"- 片段数：{len(rows)}\n")
        f.write(f"- 转写路径：{'腾讯云（上传处理）' if cloud else '本地离线'}\n")
        if not cloud:
            f.write(f"- 低置信片段：{meta['low_conf']} 段（已标 [听不清]）\n")
        if meta.get("chunked"):
            f.write(f"- ⚠ 已切成 {meta.get('n_chunks')} 段识别：说话人编号只在同一段内可比\n")
        if not meta.get("audio_read", True):
            f.write("- 未读到音频，本份不含音量数据\n")
        if cloud:
            f.write("- 本份含说话人编号，但编号只是声学聚类，不是角色；云端不返回模型置信度\n")
        else:
            f.write("- **未区分说话人**，需由上层的 AI 推断后由使用者确认\n")
        f.write("\n> 转写只负责把话记下来；音量、语速、把握度只能描述这一段，"
                "不能用于推断心理状态或性格。\n\n---\n\n")
        last = 0
        for r in rows:
            if r.get("chunk", 0) != last:
                last = r.get("chunk", 0)
                f.write(f"\n<!-- 第 {last+1} 段切片开始（段内说话人编号重新计数） -->\n\n")
            tag = " `[听不清]`" if (r.get("avg_logprob") is not None
                                   and r["avg_logprob"] < LOGPROB_FLOOR) else ""
            spk = f" **说话人{r['speaker']}**：" if r.get("speaker") is not None else ""
            f.write(f"**[{fmt_ts(r['start'])}–{fmt_ts(r['end'])}]**{tag}{spk} {r['text']}\n\n")

    print("[4/4] 完成")
    print(f"      逐字稿：{out_dir / f'{stem}.md'}")
    print(f"      结构化：{out_dir / f'{stem}.jsonl'}")
    if not cloud and meta.get("low_conf"):
        pct = meta["low_conf"] / len(rows) * 100
        print(f"\n[注意] {meta['low_conf']} 段置信度偏低（{pct:.1f}%），分析时不能作依据。")
        if pct > 20:
            print("       占比偏高，建议用 --model medium 重跑。")
    if meta.get("chunked"):
        print("\n[注意] 音频被切成多段，说话人编号跨段不可比，分析前必须做一次全局角色确认。")


def main() -> None:
    ap = argparse.ArgumentParser(description="录音转写：本地离线（faster-whisper）或腾讯云")
    ap.add_argument("audio", help="音频文件路径")
    ap.add_argument("--out", default="./output", help="输出目录（默认 ./output）")
    ap.add_argument("--provider", default="auto", choices=["auto", "local", "tencent"],
                    help="auto=有密钥走云端否则本地（默认）；local=强制本地绝不上传；tencent=强制云端")
    ap.add_argument("--model", default="small",
                    choices=["tiny", "base", "small", "medium", "large-v3"],
                    help="本地模型大小，默认 small；听不清多时换 medium")
    ap.add_argument("--model-dir", default=None, help="本地模型目录（镜像下载失败时兜底）")
    ap.add_argument("--lang", default="zh")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--compute", default="int8")
    ap.add_argument("--words", action="store_true", help="本地路径额外输出词级时间戳（体积大）")
    ap.add_argument("--codec", default="opus", choices=["opus", "mp3"])
    ap.add_argument("--tencent-region", default=None)
    ap.add_argument("--dump-raw", action="store_true", help="把云端原始响应存到 .raw.json")
    args = ap.parse_args()

    sid = os.environ.get("TENCENTCLOUD_SECRET_ID", "").strip()
    skey = os.environ.get("TENCENTCLOUD_SECRET_KEY", "").strip()
    region = (args.tencent_region or os.environ.get("TENCENTCLOUD_REGION", "").strip()
              or "ap-guangzhou")
    if args.provider == "tencent" and not (sid and skey):
        die("指定了 --provider tencent 但没找到密钥，请设 TENCENTCLOUD_SECRET_ID / _KEY。")
    provider = ("local" if args.provider == "local"
                else "tencent" if (args.provider == "tencent" or (sid and skey))
                else "local")

    audio_path = Path(args.audio).expanduser().resolve()
    if not audio_path.exists():
        die(f"文件不存在：{audio_path}")
    if audio_path.suffix.lower() not in SUPPORTED:
        die(f"不支持的格式：{audio_path.suffix}\n支持：{', '.join(sorted(SUPPORTED))}")

    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    rows, meta = ((run_cloud(args, audio_path, out_dir, audio_path.stem, sid, skey, region))
                  if provider == "tencent" else run_local(args, audio_path))
    if not rows:
        die("没有识别出任何文字。请检查：是否真有人声、音量是否过小、格式是否受支持。")
    write_outputs(out_dir, audio_path.stem, rows, meta)


if __name__ == "__main__":
    main()
