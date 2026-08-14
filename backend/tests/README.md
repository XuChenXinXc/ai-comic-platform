# 测试指南

## 前置条件

### 1. 安装测试依赖

```bash
cd backend
pip install -e ".[dev]"
```

这会安装以下测试依赖：
- `httpx2>=2,<3` - HTTP 客户端（用于 API 测试）
- `pytest>=8.3,<9` - 测试框架
- `pytest-cov>=6,<8` - 覆盖率报告
- `ruff>=0.9,<1` - 代码检查和格式化

### 2. 确保 FFmpeg 可用

Mock Provider 的视频生成测试需要 FFmpeg：

**Windows:**
```powershell
# 检查 FFmpeg 是否在 PATH 中
ffmpeg -version
ffprobe -version

# 如果未安装，下载并添加到 PATH
# https://ffmpeg.org/download.html
```

**Linux/Mac:**
```bash
# Ubuntu/Debian
sudo apt-get install ffmpeg

# macOS
brew install ffmpeg

# 验证安装
ffmpeg -version
ffprobe -version
```

### 3. 工作区权限

测试会在 `backend/.pytest_cache/` 和临时目录创建文件，确保有写权限。

## 运行测试

### 运行所有测试

```bash
cd backend
python -m pytest
```

### 运行单个测试文件

```bash
python -m pytest tests/test_extraction_contracts.py
python -m pytest tests/test_core_database.py
```

### 运行特定测试函数

```bash
python -m pytest tests/test_extraction_contracts.py::test_asset_with_blank_required_text_field_is_rejected
```

### 显示详细输出

```bash
python -m pytest -v
```

### 显示打印输出

```bash
python -m pytest -s
```

### 生成覆盖率报告

```bash
python -m pytest --cov=app --cov-report=term-missing
python -m pytest --cov=app --cov-report=html
```

覆盖率 HTML 报告会生成在 `htmlcov/index.html`。

## 调试测试失败

### 1. 查看完整错误堆栈

```bash
python -m pytest --tb=long
```

### 2. 进入调试器

```bash
python -m pytest --pdb
```

在测试失败时会自动进入 pdb 调试器。

### 3. 只运行失败的测试

```bash
python -m pytest --lf  # last-failed
```

### 4. 检查数据库状态

测试使用内存数据库（`:memory:`），每个测试独立。如果怀疑数据库问题：

```python
# 在测试中添加断点查看数据库内容
def test_something(db_fixture):
    import pdb; pdb.set_trace()
    # 此时可以执行 SQL 查询
```

### 5. 检查临时文件

某些测试会创建临时文件，失败时可能未清理：

```bash
# 查找测试创建的临时目录
ls -la /tmp/pytest-*  # Linux/Mac
dir %TEMP%\pytest-*   # Windows
```

## 常见问题

### 问题 1: 导入错误 `ModuleNotFoundError`

**症状:**
```
ImportError: cannot import name 'xxx' from 'app'
```

**解决:**
```bash
# 确保以可编辑模式安装
cd backend
pip install -e .
```

### 问题 2: FFmpeg 未找到

**症状:**
```
FileNotFoundError: [Errno 2] No such file or directory: 'ffmpeg'
```

**解决:**
- 安装 FFmpeg 并添加到 PATH
- 或在 `.env` 中配置 `FFMPEG_PATH` 和 `FFPROBE_PATH`

### 问题 3: 数据库锁定

**症状:**
```
sqlite3.OperationalError: database is locked
```

**解决:**
- 关闭其他占用数据库的进程（如正在运行的平台实例）
- 测试应该使用内存数据库，检查是否误用了真实数据库文件

### 问题 4: Windows 路径权限

**症状:**
```
PermissionError: [WinError 5] Access is denied
```

**解决:**
- 以管理员身份运行 PowerShell（部分符号链接测试需要）
- 或跳过需要管理员权限的测试：
  ```bash
  pytest -m "not admin_required"
  ```

## 测试组织

### 测试文件命名规则

- `test_core_*.py` - 核心功能测试（数据库、服务层、Provider）
- `test_api_*.py` - API 端点测试
- `test_*_pipeline.py` - 业务流程测试（素材提取、分镜生成）
- `test_*_contracts.py` - 契约测试（数据格式验证）

### Fixture 位置

- `conftest.py` - 全局 fixture（数据库、临时目录、Mock Provider）
- `tests/fixtures/` - 共享测试数据（如果存在）

## CI/CD 集成

### GitHub Actions 示例

```yaml
name: Tests
on: [push, pull_request]
jobs:
  test:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - name: Install FFmpeg
        run: choco install ffmpeg -y
      - name: Install dependencies
        run: |
          cd backend
          pip install -e ".[dev]"
      - name: Run tests
        run: |
          cd backend
          pytest --cov=app --cov-report=xml
      - name: Upload coverage
        uses: codecov/codecov-action@v3
```

### 云效流水线示例

```yaml
version: 1.0
stages:
  - name: test
    image: python:3.11
    script:
      - apt-get update && apt-get install -y ffmpeg
      - cd backend
      - pip install -e ".[dev]"
      - pytest --cov=app --cov-report=term-missing
```

## 编写新测试

### 1. 使用已有 fixture

```python
def test_something(db_session, temp_project_dir):
    # db_session: 数据库会话
    # temp_project_dir: 临时项目目录
    pass
```

### 2. 参数化测试

```python
import pytest

@pytest.mark.parametrize("input,expected", [
    ("hello", "HELLO"),
    ("world", "WORLD"),
])
def test_uppercase(input, expected):
    assert input.upper() == expected
```

### 3. 异常测试

```python
import pytest
from app.services.errors import ServiceError

def test_invalid_input_raises_error():
    with pytest.raises(ServiceError, match="无效"):
        service.do_something("invalid")
```

### 4. Mock 外部依赖

```python
from unittest.mock import patch

@patch('requests.post')
def test_api_call(mock_post):
    mock_post.return_value.json.return_value = {"ok": True}
    result = service.call_external_api()
    assert result["ok"] is True
```

## 性能测试

```bash
# 显示最慢的 10 个测试
pytest --durations=10

# 并行运行测试（需要 pytest-xdist）
pip install pytest-xdist
pytest -n auto
```

## 参考资料

- [pytest 官方文档](https://docs.pytest.org/)
- [pytest-cov 文档](https://pytest-cov.readthedocs.io/)
