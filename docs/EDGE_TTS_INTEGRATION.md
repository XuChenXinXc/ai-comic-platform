# Edge TTS 集成说明

## ✅ 已完成的功能

### 1. 后端支持

#### Edge TTS Provider
- **文件**: `backend/app/providers/tts/edge_tts.py`
- **功能**:
  - 异步 TTS 生成
  - 支持所有 Edge TTS 中文声音
  - 预定义 12 个常用声音
  - 无需 API Key，完全免费

#### API 端点
- **GET** `/audio/edge-tts/voices` - 获取可用声音列表
- **POST** `/audio/shots/{shot_id}/audio` - 生成配音（支持 Edge TTS）

#### 音频生成逻辑
- **文件**: `backend/app/services/audio_service.py`
- **功能**:
  - 自动检测 `voice_id` 是否以 `edge-tts:` 开头
  - Edge TTS: 直接生成，无需 Provider
  - 其他: 使用配置的 TTS Provider

### 2. 前端界面

#### 配音工作台
- **文件**: `frontend/src/pages/AudioWorkbenchPage.tsx`
- **功能**:
  - TTS 模式切换（Edge TTS / 自定义 Provider）
  - 12 个预定义中文声音选择
  - 角色音色自动分配
  - 批量生成和单个生成
  - 实时状态显示
  - 免费标签显示

---

## 🎯 使用方法

### 安装依赖

```bash
# 安装 Edge TTS
pip install edge-tts
```

### 使用步骤

1. **打开配音工作台**
   - 项目 → 章节 → 配音工作台

2. **选择 TTS 模式**
   - 默认选择 **"Edge TTS（免费，推荐）"**
   - 如需使用 OpenAI 等付费服务，切换到 **"自定义 Provider"**

3. **分配角色音色**
   - 系统自动为每个角色分配默认声音
   - 可手动修改每个角色的声音
   - 12 个中文声音可选

4. **生成配音**
   - **批量生成**: 点击 "批量生成全部配音"
   - **单个生成**: 点击每条对话右侧的 "生成配音" 按钮

---

## 🎤 可用声音列表

| 声音 ID | 显示名称 | 性别 | 描述 |
|---------|----------|------|------|
| `zh-CN-XiaoxiaoNeural` | 晓晓 - 温柔女声 | 女 | 温柔、亲切，适合旁白、温柔女性角色 |
| `zh-CN-XiaoyiNeural` | 晓伊 - 活泼女声 | 女 | 活泼、可爱，适合少女、活力角色 |
| `zh-CN-YunjianNeural` | 云健 - 沉稳男声 | 男 | 沉稳、成熟，适合成熟男性、权威角色 |
| `zh-CN-YunxiNeural` | 云希 - 阳光男声 | 男 | 阳光、开朗，适合年轻男性、活力角色 |
| `zh-CN-YunxiaNeural` | 云霞 - 知性男声 | 男 | 知性、专业，适合解说、正式场合 |
| `zh-CN-YunyangNeural` | 云扬 - 专业男声 | 男 | 专业、稳重，适合新闻播报、正式场合 |
| `zh-CN-liaoning-XiaobeiNeural` | 晓北 - 东北女声 | 女 | 东北口音，活泼、幽默 |
| `zh-CN-shaanxi-XiaoniNeural` | 晓妮 - 陕西女声 | 女 | 陕西口音，朴实、亲切 |
| `zh-HK-HiuMaanNeural` | 曉曼 - 粤语女声 | 女 | 粤语，温柔、优雅 |
| `zh-HK-WanLungNeural` | 雲龍 - 粤语男声 | 男 | 粤语，沉稳、成熟 |
| `zh-TW-HsiaoChenNeural` | 曉臻 - 台湾女声 | 女 | 台湾口音，温柔、亲切 |
| `zh-TW-YunJheNeural` | 雲哲 - 台湾男声 | 男 | 台湾口音，稳重、专业 |

---

## 🔧 技术细节

### Voice ID 格式

- **Edge TTS**: `edge-tts:{voice_name}`
  - 示例: `edge-tts:zh-CN-XiaoxiaoNeural`
- **Provider TTS**: 直接使用 voice ID
  - 示例: `alloy`, `echo`, `nova`

### 后端检测逻辑

```python
# 检查是否使用 Edge TTS
use_edge_tts = voice_id.startswith("edge-tts:")

if use_edge_tts:
    # 使用 Edge TTS（免费）
    edge_voice = voice_id.replace("edge-tts:", "")
    audio_data = await self._generate_with_edge_tts(text, edge_voice)
else:
    # 使用 Provider TTS（需要配置）
    audio_data = await tts_provider.synthesize(text, voice_id)
```

### 前端模式切换

```typescript
// TTS 模式
const [ttsMode, setTtsMode] = useState<"edge" | "provider">("edge");

// 根据模式选择声音列表
{ttsMode === "edge" ? (
  // Edge TTS 声音
  edgeTTSVoices.map(voice => (
    <option value={`edge-tts:${voice.name}`}>
      {voice.display_name}
    </option>
  ))
) : (
  // OpenAI 声音
  OPENAI_VOICES.map(voice => (
    <option value={voice.id}>{voice.name}</option>
  ))
)}
```

---

## ✅ 优势

### Edge TTS
- ✅ **完全免费** - 无需 API Key
- ✅ **音质优秀** - 接近真人
- ✅ **声音丰富** - 12+ 中文声音
- ✅ **开箱即用** - 无需配置
- ✅ **支持方言** - 东北话、陕西话、粤语、台湾腔

### 对比其他方案

| 方案 | 费用 | 音质 | 配置难度 |
|------|------|------|----------|
| Edge TTS | 免费 | ⭐⭐⭐⭐⭐ | 无需配置 |
| OpenAI TTS | $15/百万字符 | ⭐⭐⭐⭐⭐ | 需要 API Key |
| Coqui TTS | 免费 | ⭐⭐⭐ | 复杂 |

---

## 📝 注意事项

1. **网络要求**
   - Edge TTS 需要网络连接
   - 访问微软服务器
   - 国内可直接使用

2. **性能**
   - 异步生成，不阻塞
   - 批量生成时按顺序处理
   - 临时文件自动清理

3. **限制**
   - 依赖微软服务稳定性
   - 单次文本不建议超过 5000 字符
   - 无官方 API 限制说明

---

## 🐛 故障排除

### 1. "Edge TTS 未安装"

**解决方案**:
```bash
pip install edge-tts
```

### 2. 声音列表为空

**解决方案**:
- 检查网络连接
- 确认可以访问微软服务
- 刷新页面重新加载

### 3. 生成失败

**可能原因**:
- 网络不稳定
- 文本过长
- 服务暂时不可用

**解决方案**:
- 检查网络
- 缩短文本
- 稍后重试
- 切换到 Provider 模式使用其他 TTS

---

## 🔄 未来扩展

### 计划功能

1. **更多声音**
   - 支持英文、日文等其他语言
   - 自定义语速、音调

2. **离线模式**
   - 集成本地 TTS 引擎
   - 完全离线运行

3. **声音克隆**
   - 上传音频样本
   - 生成自定义声音

---

## 📚 相关资源

- [Edge TTS GitHub](https://github.com/rany2/edge-tts)
- [微软 TTS 文档](https://learn.microsoft.com/en-us/azure/cognitive-services/speech-service/language-support)
- [AI 漫剧平台文档](../docs/)

---

**享受免费高质量的配音体验！** 🎉
