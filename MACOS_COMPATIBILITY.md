# macOS 兼容性分析与适配方案

**当前状态:** ✅ 支持 macOS（直接 API Key 使用 Keychain）
**原阻塞原因:** Windows DPAPI 依赖（已于 2026-08-15 通过 keyring 解决）
**影响范围:** API Key 加密存储功能  

---

## 兼容性问题分析

### 核心阻塞问题

**问题:** API Key 加密使用 Windows DPAPI（Data Protection API）

**位置:** `backend/app/services/secret_service.py`

```python
def _windows_api() -> tuple[ctypes.WinDLL, ctypes.WinDLL]:
    if os.name != "nt":
        raise SecretServiceError("直接 API Key 仅支持 Windows DPAPI")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    return crypt32, kernel32
```

**影响:**
- ❌ 在 Web UI 的"接口配置"页填写 API Key 会失败
- ❌ 已有包含加密 API Key 的数据库无法在 macOS 上使用
- ✅ 使用环境变量配置 API Key 不受影响（`api_key_env` 方式）

**已于 2026-08-15 修复:** `secret_service.py` 已抽象为平台后端，
macOS 使用 Keychain 保存直接 API Key，Windows 保持 DPAPI。
旧的 Windows DPAPI 密文无法在 macOS 上解密，需在 Web UI 重新填写。

### 其他 Windows 特定代码

#### 1. 路径长度检查（非阻塞）

**位置:** `backend/app/config.py:120-124`

```python
if (
    os.name == "nt"
    and len(str(self.project_storage_dir).encode("utf-16-le")) // 2
    > _MAX_WINDOWS_PROJECTS_DIR_UTF16_UNITS
):
    raise ValueError("APP_PROJECTS_DIR 路径过深，请改用更短的目录")
```

**影响:** macOS 上跳过此检查，无副作用（macOS 路径限制是 1024 字节，比 Windows 更宽松）

#### 2. 符号链接检测（非阻塞）

**位置:** `backend/app/services/file_service.py:328, 346`

```python
if os.name == "nt":
    # Windows 特定的 reparse point 检测
    ...
```

**影响:** macOS 上跳过 Windows 特定检测，使用标准的 `Path.is_symlink()`

#### 3. 平台设置验证（非阻塞）

**位置:** `backend/app/services/setting_service.py:26`

```python
if os.name != "nt":
    return  # 非 Windows 平台跳过某些验证
```

**影响:** 功能降级，但不影响核心使用

---

## macOS 适配方案

### 方案 A: 环境变量方式（立即可用）

**适用场景:** 快速测试、开发环境、单用户使用

**操作步骤:**

1. **安装依赖**

```bash
# 安装 Python 3.11
brew install python@3.11

# 安装 FFmpeg
brew install ffmpeg

# 克隆项目
cd ~/Projects
git clone <repository-url> ai-comic-platform
cd ai-comic-platform

# 安装后端依赖
cd backend
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .

# 安装前端依赖
cd ../frontend
npm install
```

2. **配置环境变量**

```bash
# 创建 .env 文件
cd ~/Projects/ai-comic-platform
cp .env.example .env

# 编辑 .env，添加 API Key
nano .env
```

```env
# 使用环境变量配置 API Key（旧兼容方式）
TEXT_API_KEY=sk-...
IMAGE_API_KEY=sk-...
VIDEO_API_KEY=sk-...

# 其他配置保持默认
APP_HOST=127.0.0.1
APP_PORT=8012
APP_WORKSPACE=./workspace
APP_CORS_ORIGINS=http://127.0.0.1:3000,http://localhost:3000
```

3. **创建 Provider 使用环境变量**

在 Web UI 的"接口配置"页：
- 在 "API Key 环境变量" 字段填写 `TEXT_API_KEY` / `IMAGE_API_KEY` / `VIDEO_API_KEY`
- 也可以直接填写 "API Key" 字段（使用 Keychain 保存）
- 修改环境变量后需重启平台生效

4. **启动平台**

```bash
# 后端
cd ~/Projects/ai-comic-platform/backend
source .venv/bin/activate
uvicorn app.main:app --host 127.0.0.1 --port 8012

# 前端（新终端）
cd ~/Projects/ai-comic-platform/frontend
npm run dev
```

**限制（仅环境变量方式）:**
- ❌ API Key 明文存储在 `.env` 文件中（安全性较低）
- ❌ 修改环境变量后需要重启平台

**优点:**
- ✅ 立即可用，无需修改代码
- ✅ 完整功能可用（Web UI 直接填写 API Key 也已支持）

---

### 方案 B: 跨平台加密适配（✅ 已实现）

**适用场景:** 生产环境、多用户、团队协作

**实现内容:**
- macOS 使用 Keychain
- Linux Secret Service API（未实现，仍走环境变量）
- Windows 保持 DPAPI
- 统一接口，业务调用方零改动

**实现方案:**

#### 1. 抽象加密接口

```python
# backend/app/services/secret_service.py
from __future__ import annotations

import os
import sys
from abc import ABC, abstractmethod


class SecretServiceError(RuntimeError):
    """密钥加解密失败。"""


class SecretBackend(ABC):
    @abstractmethod
    def encrypt(self, plaintext: str) -> str:
        """加密明文，返回 base64 密文。"""
        pass

    @abstractmethod
    def decrypt(self, ciphertext: str) -> str:
        """解密密文，返回明文。"""
        pass


class WindowsDPAPIBackend(SecretBackend):
    """Windows DPAPI 实现"""
    def __init__(self):
        if os.name != "nt":
            raise SecretServiceError("DPAPI 仅支持 Windows")
        import ctypes
        from ctypes import wintypes
        self.crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def encrypt(self, plaintext: str) -> str:
        # ... 现有 DPAPI 加密逻辑
        pass

    def decrypt(self, ciphertext: str) -> str:
        # ... 现有 DPAPI 解密逻辑
        pass


class MacOSKeychainBackend(SecretBackend):
    """macOS Keychain 实现"""
    def __init__(self):
        if sys.platform != "darwin":
            raise SecretServiceError("Keychain 仅支持 macOS")

    def encrypt(self, plaintext: str) -> str:
        """使用 Keychain 存储，返回引用 ID"""
        import keyring
        import uuid
        # 生成唯一 ID
        key_id = f"ai-comic-{uuid.uuid4().hex}"
        # 存储到 Keychain
        keyring.set_password("ai-comic-platform", key_id, plaintext)
        # 返回 ID（加 magic prefix 区分）
        return f"keychain:{key_id}"

    def decrypt(self, ciphertext: str) -> str:
        """从 Keychain 读取"""
        import keyring
        if not ciphertext.startswith("keychain:"):
            raise SecretServiceError("无效的 Keychain 引用")
        key_id = ciphertext.removeprefix("keychain:")
        password = keyring.get_password("ai-comic-platform", key_id)
        if password is None:
            raise SecretServiceError(f"Keychain 中未找到密钥：{key_id}")
        return password


class LinuxSecretServiceBackend(SecretBackend):
    """Linux Secret Service API 实现"""
    def __init__(self):
        if sys.platform not in ("linux", "linux2"):
            raise SecretServiceError("Secret Service 仅支持 Linux")

    def encrypt(self, plaintext: str) -> str:
        import keyring
        import uuid
        key_id = f"ai-comic-{uuid.uuid4().hex}"
        keyring.set_password("ai-comic-platform", key_id, plaintext)
        return f"secretservice:{key_id}"

    def decrypt(self, ciphertext: str) -> str:
        import keyring
        if not ciphertext.startswith("secretservice:"):
            raise SecretServiceError("无效的 Secret Service 引用")
        key_id = ciphertext.removeprefix("secretservice:")
        password = keyring.get_password("ai-comic-platform", key_id)
        if password is None:
            raise SecretServiceError(f"Secret Service 中未找到密钥：{key_id}")
        return password


def _get_backend() -> SecretBackend:
    """根据平台自动选择加密后端"""
    if os.name == "nt":
        return WindowsDPAPIBackend()
    elif sys.platform == "darwin":
        return MacOSKeychainBackend()
    elif sys.platform in ("linux", "linux2"):
        return LinuxSecretServiceBackend()
    else:
        raise SecretServiceError(f"不支持的平台：{sys.platform}")


# 公开接口保持不变
_backend = _get_backend()

def encrypt_secret(value: str) -> str:
    if not value:
        return ""
    return _backend.encrypt(value)

def decrypt_secret(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    return _backend.decrypt(ciphertext)
```

#### 2. 添加依赖

```toml
# backend/pyproject.toml
dependencies = [
    # ... 现有依赖
    "keyring>=24.0,<25; sys_platform == 'darwin' or sys_platform == 'linux'",
]
```

#### 3. 数据迁移注意事项

**已有 Windows 数据库迁移到 macOS:**

```bash
# 1. 在 Windows 上导出明文 API Key
# 运行迁移脚本（需要在 Windows 上执行）
cd backend
python scripts/export_api_keys.py > api_keys.json

# 2. 复制数据库和导出文件到 macOS
scp platform.db api_keys.json mac-user@mac-host:~/ai-comic-platform/workspace/

# 3. 在 macOS 上导入
cd ~/ai-comic-platform/backend
python scripts/import_api_keys.py < ~/ai-comic-platform/workspace/api_keys.json
```

**迁移脚本示例:**

```python
# backend/scripts/export_api_keys.py
import json
from app.db import db
from app.services.secret_service import decrypt_secret
from sqlalchemy import select
from app.models.tables import api_provider

with db.engine.connect() as conn:
    providers = conn.execute(
        select(api_provider.c.id, api_provider.c.api_key_ciphertext)
    ).all()
    
    export = {}
    for provider_id, ciphertext in providers:
        if ciphertext:
            try:
                plaintext = decrypt_secret(ciphertext)
                export[provider_id] = plaintext
            except Exception as e:
                print(f"警告：Provider {provider_id} 解密失败：{e}", file=sys.stderr)
    
    print(json.dumps(export, ensure_ascii=False, indent=2))
```

```python
# backend/scripts/import_api_keys.py
import json
import sys
from app.db import db
from app.services.secret_service import encrypt_secret
from sqlalchemy import update
from app.models.tables import api_provider

data = json.load(sys.stdin)

with db.engine.begin() as conn:
    for provider_id, plaintext in data.items():
        ciphertext = encrypt_secret(plaintext)
        conn.execute(
            update(api_provider)
            .where(api_provider.c.id == provider_id)
            .values(api_key_ciphertext=ciphertext)
        )
        print(f"✅ 已导入 Provider {provider_id}")
```

**优点:**
- ✅ 完整功能，Web UI 可直接配置 API Key
- ✅ 安全性高（使用系统级密钥链）
- ✅ macOS / Windows 统一体验

**进度:**
- ✅ 已重构 `secret_service.py`，macOS 使用 Keychain
- ✅ 已补充单元测试
- ⏳ Windows → macOS 旧密文不自动迁移，需重新填写

---

### 方案 C: 文件加密方式（备选）

**适用场景:** 无法使用系统密钥链时的备选方案

**实现:** 使用 `cryptography` 库的 Fernet 对称加密

```python
from cryptography.fernet import Fernet
import os

# 密钥存储在 ~/.ai-comic-platform/secret.key
# 首次启动时生成，用户负责备份
def get_or_create_key() -> bytes:
    key_file = Path.home() / ".ai-comic-platform" / "secret.key"
    if key_file.exists():
        return key_file.read_bytes()
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    key_file.write_bytes(key)
    os.chmod(key_file, 0o600)  # 仅用户可读
    return key

def encrypt_secret(plaintext: str) -> str:
    key = get_or_create_key()
    f = Fernet(key)
    return f.encrypt(plaintext.encode()).decode()

def decrypt_secret(ciphertext: str) -> str:
    key = get_or_create_key()
    f = Fernet(key)
    return f.decrypt(ciphertext.encode()).decode()
```

**优点:**
- ✅ 跨平台通用
- ✅ 实现简单

**缺点:**
- ⚠️ 密钥文件需要用户手动备份
- ⚠️ 丢失密钥文件后无法恢复 API Key

---

## 推荐方案总结

### 当前使用（已实现）

**直接填写 API Key（推荐）**

1. 在 Web UI 的"接口配置"页创建或编辑 Provider；
2. 在 "API Key" 字段直接填写密钥；
3. 平台使用 macOS Keychain 加密保存，重启后仍可用。

环境变量方式仍然兼容旧配置；修改 `.env` 中的环境变量后需要重启平台。

**Windows → macOS 数据库迁移：** 旧 DPAPI 密文无法在 macOS 解密，
需要在 Web UI 重新填写 API Key。

---

## 其他注意事项

### 1. 启动脚本不兼容

**问题:** 项目提供的启动脚本是 Windows PowerShell（`.ps1`）

**解决:** 创建 Shell 脚本版本

```bash
# start-mac.sh
#!/bin/bash
set -e

# 激活虚拟环境
source backend/.venv/bin/activate

# 检查 FFmpeg
if ! command -v ffmpeg &> /dev/null; then
    echo "❌ FFmpeg 未安装，请运行: brew install ffmpeg"
    exit 1
fi

# 数据库迁移
cd backend
alembic upgrade head

# 启动后端
uvicorn app.main:app --host 127.0.0.1 --port 8012 &
BACKEND_PID=$!

# 启动前端
cd ../frontend
npm run dev &
FRONTEND_PID=$!

echo "✅ 平台启动成功"
echo "   后端: http://127.0.0.1:8012"
echo "   前端: http://127.0.0.1:3000"
echo ""
echo "按 Ctrl+C 停止服务"

# 等待中断信号
trap "kill $BACKEND_PID $FRONTEND_PID" EXIT
wait
```

### 2. 路径差异

- Windows: `D:\LyProject\Other\ai-comic-platform`
- macOS: `~/Projects/ai-comic-platform` 或 `/Users/username/ai-comic-platform`

### 3. 文件权限

macOS 默认不允许执行脚本，需要：

```bash
chmod +x start-mac.sh
./start-mac.sh
```

---

## 总结

| 功能 | 方案 A (环境变量) | 方案 B (Keychain) | 方案 C (文件加密) |
|------|------------------|------------------|------------------|
| 立即可用 | ✅ 是 | ✅ 已实现 | ❌ 需要开发 |
| Web UI 配置 | ❌ 否 | ✅ 是 | ✅ 是 |
| 安全性 | ⚠️ 明文存储 | ✅ 系统级加密 | ⚠️ 用户管理密钥 |
| 跨平台 | ✅ 是 | ✅ 是 | ✅ 是 |
| 迁移成本 | 低 | 中 | 低 |

**建议:**
- **快速测试/开发:** 使用方案 B（已实现，Web UI 直接填写）
- **生产环境/团队协作:** 使用方案 B（已实现，Keychain 加密保存）
- **无系统密钥链访问权限:** 考虑方案 C

---

## 双击启动（.command）

macOS 可直接双击根目录的 `启动平台.command` / `停止平台.command`（已随项目提供）。首次使用前先给脚本加执行权限：

```bash
chmod +x start-mac.sh stop-mac.sh
```

如果双击没有反应：右键 → 打开方式 → 终端（或到「系统设置 → 隐私与安全性」允许）。想要 Windows 那种独立 App 图标，可用 Automator 新建「应用程序」，放入「运行 Shell 脚本」动作，内容指向项目目录下的 `./start-mac.sh`。

---

**更新日期:** 2026-08-15
**维护者:** 方案 B 已实现（macOS Keychain）
