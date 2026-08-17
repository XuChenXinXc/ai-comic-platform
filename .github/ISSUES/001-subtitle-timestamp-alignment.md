# Issue #1: 字幕时间戳精确对齐

## 功能描述

集成语音识别 API，实现字幕与配音的精确时间戳对齐，替代当前的均分策略。

## 当前实现

- 位置：`backend/app/services/subtitle_service.py:44`
- 策略：整句字幕，从 0 到 duration_sec 均匀分布
- 限制：无法精确对齐到单词或音节级别

## 期望改进

集成第三方语音识别 API，获取精确的时间戳信息：

**候选方案**：
1. Azure Speech Services - 支持中文，准确率高
2. 阿里云语音识别 - 国内服务，低延迟
3. 讯飞开放平台 - 中文优化，性价比高

## 实现建议

1. 添加配置项选择 ASR 提供商
2. 在 `subtitle_service.py` 中添加 ASR 调用逻辑
3. 解析返回的时间戳并生成精确字幕
4. 添加降级策略：ASR 失败时回退到均分策略
5. 添加单元测试和集成测试

## 优先级

- **等级**: Medium
- **版本**: V2
- **依赖**: 无

## 相关文件

- `backend/app/services/subtitle_service.py`
- `backend/app/config.py` (需要添加 ASR 配置)

## 标签

`enhancement`, `v2`, `subtitle`, `asr`
