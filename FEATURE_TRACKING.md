# 功能改进追踪

本文档记录代码中标记的待改进功能。

## 高优先级

### Issue #1: 字幕时间戳精确对齐

**位置:** `backend/app/services/subtitle_service.py:45`

**当前实现:**
```python
# 简单策略：整句字幕，从 0 到 duration_sec
subtitle_data = [
    {
        "text": dialogue_text,
        "start_time": 0.0,
        "end_time": duration_sec,
        "style_preset": "default",
    }
]
```

**问题描述:**

当前字幕生成采用最简单的均分策略：整段对白作为一条字幕，时间跨度为整个音频长度。这在以下场景体验不佳：

1. **多角色对话:** 无法区分不同角色的发言时间
2. **长段独白:** 字幕停留时间过长，观众已读完但字幕仍在
3. **快速对白:** 字幕切换不跟随语音节奏
4. **停顿处理:** 无法在句间停顿时隐藏字幕

**影响范围:**
- 视频观看体验
- 字幕导出质量（SRT/ASS 格式）

**改进方案:**

**方案 A: 集成语音识别 API（推荐）**

调用第三方 ASR 服务获取精确时间戳：

```python
# 伪代码
def generate_subtitle_with_asr(audio_path: str) -> list[dict]:
    # 1. 调用 Whisper API / 阿里云 / 腾讯云语音识别
    asr_result = asr_client.transcribe(audio_path)
    
    # 2. 解析返回的词级时间戳
    subtitles = []
    for segment in asr_result.segments:
        subtitles.append({
            "text": segment.text,
            "start_time": segment.start,
            "end_time": segment.end,
            "confidence": segment.confidence,
        })
    
    # 3. 合并短片段（避免字幕切换过快）
    return merge_short_segments(subtitles, min_duration=1.0)
```

**优点:**
- 精确到词级别
- 支持多语言
- 无需维护分词逻辑

**缺点:**
- 增加外部依赖
- API 调用成本
- 需要处理识别错误

**推荐服务商:**
- OpenAI Whisper API（多语言，高准确率）
- Azure Speech to Text（企业级稳定性）
- 阿里云智能语音（国内速度快）

**方案 B: 基于分词的启发式策略**

不调用外部服务，本地计算时间轴：

```python
def generate_subtitle_heuristic(text: str, duration_sec: float) -> list[dict]:
    # 1. 按标点符号分句
    sentences = re.split(r'[。！？；]', text)
    
    # 2. 计算每句的字符数占比
    total_chars = sum(len(s) for s in sentences)
    
    # 3. 按字符数比例分配时间
    subtitles = []
    current_time = 0.0
    for sentence in sentences:
        if not sentence.strip():
            continue
        ratio = len(sentence) / total_chars
        segment_duration = duration_sec * ratio
        subtitles.append({
            "text": sentence.strip(),
            "start_time": current_time,
            "end_time": current_time + segment_duration,
        })
        current_time += segment_duration
    
    return subtitles
```

**优点:**
- 无外部依赖
- 零成本
- 响应快

**缺点:**
- 不准确（语速不均匀）
- 无法处理停顿
- 中文分词需要额外库（jieba）

**实施建议:**

1. **V1.1 版本（短期）:**
   - 保持当前简单策略
   - 允许用户手动调整字幕时间轴（UI 编辑器）
   - 优先级：P1

2. **V1.2 版本（中期）:**
   - 集成 Whisper API
   - 在 Provider 配置中添加 ASR Provider
   - 优先级：P0

3. **V2.0 版本（长期）:**
   - 支持本地 Whisper 模型（whisper.cpp）
   - 批量优化字幕时间轴
   - 优先级：P2

**依赖配置:**

```python
# backend/app/providers/asr/openai_whisper.py
class OpenAIWhisperProvider:
    def transcribe(self, audio_path: Path) -> list[Segment]:
        response = openai.Audio.transcribe(
            model="whisper-1",
            file=open(audio_path, "rb"),
            response_format="verbose_json",
            timestamp_granularities=["segment"]
        )
        return [
            Segment(
                text=seg["text"],
                start=seg["start"],
                end=seg["end"],
            )
            for seg in response["segments"]
        ]
```

**测试用例:**

```python
def test_subtitle_timing_matches_speech_pauses():
    """验证字幕在语音停顿处切换"""
    audio_path = "tests/fixtures/multi_sentence_audio.mp3"
    subtitles = subtitle_service.generate_with_asr(audio_path)
    
    # 假设音频是："你好。[停顿 0.5s] 欢迎使用。"
    assert len(subtitles) == 2
    assert subtitles[0]["text"] == "你好。"
    assert subtitles[1]["start_time"] > subtitles[0]["end_time"] + 0.3

def test_subtitle_long_monologue_splits_by_sentences():
    """验证长独白按句子拆分"""
    text = "这是第一句。这是第二句！这是第三句？"
    duration = 9.0  # 假设每句 3 秒
    subtitles = subtitle_service.generate_heuristic(text, duration)
    
    assert len(subtitles) == 3
    assert all(2.5 < s["end_time"] - s["start_time"] < 3.5 for s in subtitles)
```

**相关文档:**
- [OpenAI Whisper API](https://platform.openai.com/docs/guides/speech-to-text)
- [字幕格式规范 SRT](https://www.matroska.org/technical/subtitles.html)

**状态:** 📋 计划中  
**标签:** `enhancement`, `audio`, `subtitle`  
**里程碑:** V1.2

---

## 中优先级

暂无

---

## 低优先级

暂无

---

## 已完成

暂无
