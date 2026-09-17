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

## 行为准则

- 尊重所有贡献者
- 建设性讨论
- 专注于对项目最有利的事情
