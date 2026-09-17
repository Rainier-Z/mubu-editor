# Contributing

欢迎贡献！

## 如何贡献

请在本地工作区提出修改，先说明问题和预期行为，再附上可复现步骤与验证结果。远程同步、提交和发布由项目维护者另行处理。

## 开发环境

```bash
# 进入本地项目目录（你克隆到的路径）
cd mubu-editor

# 安装依赖：Linux / macOS
pip install -r requirements.txt -r requirements-dev.txt

# 安装依赖：Windows（开发锁文件多一个 colorama）
pip install -r requirements.txt -r requirements-dev-windows.txt

# 运行全部测试
PYTHONPATH=scripts python -m pytest -v

# 脚本运行入口
python3 scripts/mubu_api.py --help
```

## 修改依赖后必须重新生成锁文件

`requirements*.txt` 是 `pip-tools` 生成的**哈希锁定**文件，CI 会校验它们可安装。
改完 `requirements*.in` 后必须重新生成对应的锁文件，否则 CI 会失败：

```bash
# Linux / macOS 锁文件
pip install "pip-tools>=7,<8"
pip-compile --generate-hashes -o requirements.txt requirements.in
pip-compile --generate-hashes -o requirements-dev.txt requirements-dev.in

# Windows 锁文件（多出 colorama，必须在 Windows 上生成）
pip-compile --generate-hashes -o requirements-dev-windows.txt requirements-dev.in
```

> 带 `; python_version < "3.11"` 之类的条件依赖请显式写进 `.in`：它会被保留在锁文件里。

## 行为准则

- 尊重所有贡献者
- 建设性讨论
- 专注于对项目最有利的事情
