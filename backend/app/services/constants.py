"""服务层常量定义

集中管理服务层使用的常量，避免硬编码魔法数字。

⚠️ 本文件多数常量目前**没有任何代码引用**（2026-08 审查确认：45 个常量中仅
``FFMPEG_DEFAULT_TIMEOUT`` / ``FFMPEG_SHORT_TIMEOUT`` /
``FFMPEG_EXECUTOR_MAX_WORKERS`` / ``MAX_IMAGE_SIZE`` / ``MAX_VIDEO_SIZE`` /
``MAX_BGM_SIZE`` / ``MAX_DOWNLOAD_SIZE`` / ``PROVIDER_REFERENCE_*`` 在用）。
未引用的值是预留占位，**不代表对应功能已实现**——尤其「速率限制」与「缓存」
两节：平台当前既没有限流中间件也没有缓存层，不要因为看到常量就以为有。
接线时请把常量真正引用起来，或直接删除对应条目。
"""

# ==================== Job 相关 ====================

# Job 清理
JOB_CLEANUP_DEFAULT_DAYS = 7  # 默认保留最近 7 天的任务
JOB_CLEANUP_KEEP_FAILED_DEFAULT = True  # 默认保留失败的任务

# Job 重试
JOB_MAX_RETRY_COUNT = 3  # 最大重试次数
JOB_RETRY_DELAY_SECONDS = 5  # 重试延迟（秒）

# ==================== FFmpeg 相关 ====================

# FFmpeg 超时
FFMPEG_DEFAULT_TIMEOUT = 300  # 默认超时 5 分钟
FFMPEG_LONG_TIMEOUT = 600  # 长视频处理超时 10 分钟
FFMPEG_SHORT_TIMEOUT = 30  # 短操作超时 30 秒

# FFmpeg 线程池
FFMPEG_EXECUTOR_MAX_WORKERS = 4  # FFmpeg 线程池最大工作线程数

# 视频参数
VIDEO_DEFAULT_FPS = 24  # 默认帧率
VIDEO_DEFAULT_CRF = 23  # 默认 CRF 质量（0-51，越小越好）
VIDEO_MAX_BITRATE = "5M"  # 最大码率

# ==================== 文件服务 ====================

# 文件大小限制（字节）
MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10 MB
MAX_VIDEO_SIZE = 100 * 1024 * 1024  # 100 MB
MAX_AUDIO_SIZE = 20 * 1024 * 1024  # 20 MB（单条分镜配音）
MAX_BGM_SIZE = 50 * 1024 * 1024  # 50 MB（整首背景音乐，比单条配音宽）
MIN_FILE_SIZE = 1  # 最小文件大小（避免空文件）

# 文件下载
DOWNLOAD_DEFAULT_TIMEOUT = 120  # 下载超时 120 秒
DOWNLOAD_CHUNK_SIZE = 8192  # 下载块大小（8KB）
# Provider 返回的下载地址不可信，单个文件写入上限，避免故障或恶意响应写满磁盘
MAX_DOWNLOAD_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB

# ==================== Provider 相关 ====================

# Provider 超时
PROVIDER_DEFAULT_TIMEOUT = 120  # 默认超时 120 秒
PROVIDER_LONG_TIMEOUT = 300  # 长任务超时 300 秒

# Provider 重试
PROVIDER_MAX_RETRIES = 3  # 最大重试次数
PROVIDER_RETRY_DELAY = 2  # 重试延迟（秒）

# Provider 轮询
PROVIDER_POLL_INTERVAL = 5  # 轮询间隔（秒）
PROVIDER_POLL_MAX_ATTEMPTS = 120  # 最大轮询次数（总时长 = 间隔 × 次数）

# ==================== 文本生成 ====================

# 文本长度限制
MAX_PROMPT_LENGTH = 10000  # 最大提示词长度
MAX_SYSTEM_PROMPT_LENGTH = 5000  # 最大系统提示词长度
MAX_OUTPUT_LENGTH = 50000  # 最大输出长度

# ==================== 图片生成 ====================

# 图片尺寸
IMAGE_DEFAULT_WIDTH = 1024
IMAGE_DEFAULT_HEIGHT = 1024
IMAGE_MIN_SIZE = 256  # 最小尺寸
IMAGE_MAX_SIZE = 2048  # 最大尺寸

# Provider 输入参考图压缩（仅提交前使用，本地原图不压缩）
PROVIDER_REFERENCE_MAX_SIDE = 2048  # 最长边像素
PROVIDER_REFERENCE_JPEG_QUALITY = 92  # JPEG 初始质量（提高以降低参考图重压缩损失）
PROVIDER_REFERENCE_MAX_BYTES = 1024 * 1024  # 单张最大字节数（1 MB）

# ==================== 音频生成 ====================

# 音频参数
AUDIO_DEFAULT_SAMPLE_RATE = 44100  # 默认采样率
AUDIO_DEFAULT_BITRATE = "128k"  # 默认码率

# TTS
TTS_MAX_TEXT_LENGTH = 5000  # TTS 单次最大文本长度

# ==================== 数据库 ====================

# 分页
DEFAULT_PAGE_SIZE = 50  # 默认分页大小
MAX_PAGE_SIZE = 1000  # 最大分页大小

# 批量操作
BATCH_INSERT_SIZE = 100  # 批量插入大小
BATCH_UPDATE_SIZE = 100  # 批量更新大小

# ==================== 缓存 ====================

# 缓存过期时间（秒）
CACHE_SHORT_TTL = 60  # 短期缓存 1 分钟
CACHE_MEDIUM_TTL = 300  # 中期缓存 5 分钟
CACHE_LONG_TTL = 3600  # 长期缓存 1 小时

# ==================== 安全 ====================

# 加密
ENCRYPTION_KEY_LENGTH = 32  # AES-256 密钥长度
SALT_LENGTH = 16  # 盐值长度

# 速率限制
RATE_LIMIT_PER_MINUTE = 60  # 每分钟请求限制
RATE_LIMIT_PER_HOUR = 1000  # 每小时请求限制
